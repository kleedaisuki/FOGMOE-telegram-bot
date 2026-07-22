"""@brief PostgreSQL 随机活动适配器的无数据库映射测试 / Database-free mapping tests for the PostgreSQL chance adapter."""

from __future__ import annotations

import json
from typing import cast
from uuid import UUID

import pytest

from fogmoe_bot.application.chance.models import CommitChanceRound
from fogmoe_bot.application.chance.service import ChanceService, ServerSeedSource
from fogmoe_bot.application.chance.workflow_models import (
    BindAndSettleChanceRound,
    ChanceRoundStatus,
    ChanceRoundView,
    ChanceWorkflowCode,
    ChanceWorkflowResult,
    CommitDurableChanceRound,
)
from fogmoe_bot.domain.chance.examples import sicbo_like_ruleset
from fogmoe_bot.domain.chance.fairness import ClientSeed, ServerSeed
from fogmoe_bot.domain.chance.money import FreeTokenStake
from fogmoe_bot.domain.chance.scope import GroupRoundScope
from fogmoe_bot.infrastructure.database.chance import (
    _activity_pot_can_cover_payout,
    _bind_request_fingerprint,
    _commit_request_fingerprint,
    _result_from_mapping,
    _result_mapping,
)

_ROUND_ID = UUID("00000000-0000-0000-0000-00000000cafe")
"""@brief 映射测试使用的稳定轮次 UUID / Stable round UUID used by mapping tests."""


class _FixedSeeds:
    """@brief 为可重现映射测试提供固定服务器 seed / Provide a fixed server seed for reproducible mapping tests."""

    def next_server_seed(self) -> ServerSeed:
        """@brief 返回固定且足够长的服务器 seed / Return a fixed sufficiently long server seed.

        @return 固定服务器 seed / Fixed server seed.
        """

        return ServerSeed(b"postgres-chance-mapping-seed-0001")


def _private_round() -> tuple[ChanceService, CommitChanceRound]:
    """@brief 构造固定的机会活动服务和开轮命令 / Build a fixed chance service and commit command.

    @return 服务与有效开轮命令 / Service and valid commit command.
    """

    command = CommitChanceRound(
        round_id=_ROUND_ID,
        scope=GroupRoundScope(-100_42, topic_id=9),
        player_id=42,
        ruleset=sicbo_like_ruleset(),
        rule_code="big",
        stake=FreeTokenStake(100),
        nonce=17,
    )
    return ChanceService(cast(ServerSeedSource, _FixedSeeds())), command


def test_receipt_mapping_replays_complete_committed_and_settled_views() -> None:
    """@brief 回执 JSON 可完整恢复 committed/settled 视图且不提前泄露 seed /
    Receipt JSON fully restores committed/settled views without premature seed disclosure.
    """

    service, command = _private_round()
    private = service.commit(command)
    committed_result = ChanceWorkflowResult(
        ChanceWorkflowCode.SUCCESS,
        ChanceRoundView(private.committed_round, ChanceRoundStatus.COMMITTED),
    )
    committed_payload = _result_mapping(committed_result)
    encoded_committed = json.dumps(committed_payload, sort_keys=True)

    assert private.server_seed.reveal_hex() not in encoded_committed
    replayed_committed = _result_from_mapping(committed_payload, replayed=True)
    assert replayed_committed.replayed
    assert replayed_committed.view == committed_result.view

    settlement = private.bind_client_seed(ClientSeed("klee-seed")).settlement()
    settled_result = ChanceWorkflowResult(
        ChanceWorkflowCode.SUCCESS,
        ChanceRoundView(
            private.committed_round,
            ChanceRoundStatus.SETTLED,
            settlement,
        ),
    )
    settled_payload = _result_mapping(settled_result)
    replayed_settled = _result_from_mapping(settled_payload, replayed=True)

    assert replayed_settled.replayed
    assert replayed_settled.view == settled_result.view
    assert replayed_settled.view is not None
    assert replayed_settled.view.settlement is not None
    assert replayed_settled.view.settlement.proof.verifies()


def test_receipt_mapping_rejects_a_ruleset_payload_with_wrong_fingerprint() -> None:
    """@brief 回执恢复拒绝被替换规则集负载 / Receipt restoration rejects a substituted ruleset payload."""

    service, command = _private_round()
    private = service.commit(command)
    payload = _result_mapping(
        ChanceWorkflowResult(
            ChanceWorkflowCode.SUCCESS,
            ChanceRoundView(private.committed_round, ChanceRoundStatus.COMMITTED),
        )
    )
    tampered = json.loads(json.dumps(payload))
    raw_view = cast(dict[str, object], tampered["view"])
    raw_round = cast(dict[str, object], raw_view["round"])
    raw_round["ruleset_fingerprint"] = "0" * 64

    with pytest.raises(ValueError, match="fingerprint"):
        _result_from_mapping(tampered, replayed=True)


def test_request_fingerprints_bind_full_scope_and_client_seed() -> None:
    """@brief 幂等指纹绑定完整 group topic、客户端 seed 与开轮语义 /
    Idempotency fingerprints bind complete group topic, client seed, and commit semantics.
    """

    _, command = _private_round()
    commit = CommitDurableChanceRound(
        actor_id=42,
        round=command,
        idempotency_key="telegram:chance:commit:99",
    )
    first = BindAndSettleChanceRound(
        round_id=_ROUND_ID,
        actor_id=42,
        scope=GroupRoundScope(-100_42, topic_id=9),
        client_seed=ClientSeed("alpha"),
        idempotency_key="telegram:chance:bind:99",
    )
    changed_topic = BindAndSettleChanceRound(
        round_id=_ROUND_ID,
        actor_id=42,
        scope=GroupRoundScope(-100_42, topic_id=10),
        client_seed=ClientSeed("alpha"),
        idempotency_key="telegram:chance:bind:99",
    )
    changed_seed = BindAndSettleChanceRound(
        round_id=_ROUND_ID,
        actor_id=42,
        scope=GroupRoundScope(-100_42, topic_id=9),
        client_seed=ClientSeed("beta"),
        idempotency_key="telegram:chance:bind:99",
    )

    assert len(_commit_request_fingerprint(commit)) == 64
    assert _bind_request_fingerprint(first) != _bind_request_fingerprint(changed_topic)
    assert _bind_request_fingerprint(first) != _bind_request_fingerprint(changed_seed)


def test_activity_pot_must_cover_payout_without_implicit_issuance() -> None:
    """@brief 现有奖池加本轮押注不足时不得隐式发行 / An underfunded pot never receives implicit issuance."""

    assert _activity_pot_can_cover_payout(25, 10, 0)
    assert _activity_pot_can_cover_payout(25, 10, 35)
    assert not _activity_pot_can_cover_payout(25, 10, 36)
    with pytest.raises(ValueError, match="non-negative"):
        _activity_pot_can_cover_payout(-1, 1, 1)
    with pytest.raises(ValueError, match="positive"):
        _activity_pot_can_cover_payout(1, 0, 1)
    with pytest.raises(ValueError, match="non-negative"):
        _activity_pot_can_cover_payout(1, 1, -1)
