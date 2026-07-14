"""@brief 确定性、可审计的每日探索 / Deterministic, auditable daily exploration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Final
from uuid import UUID

from fogmoe_bot.domain.personal_rpg._validation import normalize_day, normalize_instant
from fogmoe_bot.domain.personal_rpg.catalog import MaterialBundle, MaterialKind
from fogmoe_bot.domain.world.scope import PersonalScope


EXPLORATION_RULESET_VERSION: Final[str] = "personal-rpg-exploration-v1"
"""@brief 每日探索规则集版本 / Version of the daily-exploration ruleset."""


class ExplorationRoute(StrEnum):
    """@brief 每日探索可选路线 / Routes available for daily exploration."""

    WOODLAND = "woodland"
    """@brief 林地路线，产出纤维和药草 / Woodland route yielding fiber and herbs."""

    QUARRY = "quarry"
    """@brief 采石场路线，产出石料和矿石 / Quarry route yielding stone and ore."""

    SHORE = "shore"
    """@brief 海岸路线，产出贝壳和海藻 / Shore route yielding shells and algae."""


@dataclass(frozen=True, slots=True)
class ExplorationReward:
    """@brief 一次每日探索的确定性奖励 / Deterministic reward for one daily exploration.

    当前规则只给予经验和材料，不定义任何金币奖励；因此没有隐式货币来源。
    The current rules grant only experience and materials and define no token reward, so no
    implicit monetary source exists.

    @param experience 严格正的探索经验 / Strictly positive exploration experience.
    @param materials 探索采集到的非空材料束 / Non-empty material bundle gathered through exploration.
    """

    experience: int
    """@brief 严格正的探索经验 / Strictly positive exploration experience."""

    materials: MaterialBundle
    """@brief 采集到的非空材料束 / Non-empty gathered material bundle."""

    def __post_init__(self) -> None:
        """@brief 验证探索奖励不变量 / Validate exploration-reward invariants.

        @return None / None.
        @raise TypeError 经验或材料类型非法时抛出 / Raised when experience or material type is invalid.
        @raise ValueError 经验不为正时抛出 / Raised when experience is not positive.
        """

        if isinstance(self.experience, bool) or not isinstance(self.experience, int):
            raise TypeError("Exploration experience reward must be an integer")
        if self.experience <= 0:
            raise ValueError("Exploration experience reward must be positive")
        if not isinstance(self.materials, MaterialBundle):
            raise TypeError("Exploration materials must be MaterialBundle")


WOODLAND_REWARD: Final[ExplorationReward] = ExplorationReward(
    experience=12,
    materials=MaterialBundle({MaterialKind.FIBER: 2, MaterialKind.HERB: 1}),
)
"""@brief 林地路线固定奖励 / Fixed reward for the woodland route."""

QUARRY_REWARD: Final[ExplorationReward] = ExplorationReward(
    experience=12,
    materials=MaterialBundle({MaterialKind.STONE: 2, MaterialKind.ORE: 1}),
)
"""@brief 采石场路线固定奖励 / Fixed reward for the quarry route."""

SHORE_REWARD: Final[ExplorationReward] = ExplorationReward(
    experience=12,
    materials=MaterialBundle({MaterialKind.SHELL: 2, MaterialKind.ALGAE: 1}),
)
"""@brief 海岸路线固定奖励 / Fixed reward for the shore route."""


def reward_for_route(route: ExplorationRoute) -> ExplorationReward:
    """@brief 获取一条路线的固定奖励 / Get the fixed reward for one route.

    @param route 每日探索路线 / Daily exploration route.
    @return 该路线的固定经验和材料奖励 / Fixed experience and material reward for the route.
    @raise TypeError 路线类型非法时抛出 / Raised when the route type is invalid.
    """

    if not isinstance(route, ExplorationRoute):
        raise TypeError("Exploration reward lookup must use ExplorationRoute")
    return {
        ExplorationRoute.WOODLAND: WOODLAND_REWARD,
        ExplorationRoute.QUARRY: QUARRY_REWARD,
        ExplorationRoute.SHORE: SHORE_REWARD,
    }[route]


def _canonical_reward_payload(reward: ExplorationReward) -> str:
    """@brief 构造奖励的稳定审计载荷 / Build stable audit payload for one reward.

    @param reward 待序列化探索奖励 / Exploration reward to serialize.
    @return 不含传输格式歧义的稳定文本 / Stable text without transport-format ambiguity.
    """

    material_payload = ",".join(
        f"{kind.value}:{quantity}"
        for kind, quantity in reward.materials.quantities.items()
    )
    return f"xp:{reward.experience}|materials:{material_payload}"


def exploration_audit_digest(
    *,
    scope: PersonalScope,
    day: date,
    route: ExplorationRoute,
    exploration_id: UUID,
    reward: ExplorationReward,
) -> str:
    """@brief 计算可独立复验的探索审计摘要 / Compute an independently verifiable exploration audit digest.

    摘要只使用公开、确定性字段，不使用服务器秘密或随机数；任何人可用同一规则集重新计算。
    The digest uses only public deterministic fields, no server secret or randomness; anyone can
    recompute it under the same ruleset.

    @param scope 个人探索范围 / Personal exploration scope.
    @param day UTC 业务日 / UTC business day.
    @param route 已选路线 / Selected route.
    @param exploration_id 稳定探索标识 / Stable exploration identity.
    @param reward 固定奖励快照 / Fixed reward snapshot.
    @return 小写 SHA-256 十六进制摘要 / Lowercase SHA-256 hexadecimal digest.
    @raise TypeError 范围、日期、路线、标识或奖励类型非法时抛出 /
        Raised when scope, day, route, identity, or reward type is invalid.
    """

    if not isinstance(scope, PersonalScope):
        raise TypeError("Exploration audit must use PersonalScope")
    normalized_day = normalize_day(day, field="Exploration day")
    if not isinstance(route, ExplorationRoute):
        raise TypeError("Exploration audit route must be ExplorationRoute")
    if not isinstance(exploration_id, UUID):
        raise TypeError("Exploration audit ID must be a UUID")
    if not isinstance(reward, ExplorationReward):
        raise TypeError("Exploration audit reward must be ExplorationReward")
    payload = "|".join(
        (
            EXPLORATION_RULESET_VERSION,
            str(scope.user_id),
            normalized_day.isoformat(),
            route.value,
            str(exploration_id),
            _canonical_reward_payload(reward),
        )
    )
    return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DailyExploration:
    """@brief 一次私聊范围内可审计的每日探索 / One auditable daily exploration in a private personal scope.

    ``reward`` 必须等于固定路线规则的结果，``audit_digest`` 必须由公共字段重算得到。
    因此持久化层即使被错误调用，也不能把任意奖励伪装成一次正常探索。
    ``reward`` must equal the fixed route-rule result and ``audit_digest`` must be recomputed from
    public fields. Thus, even an erroneous persistence call cannot present an arbitrary reward as
    a normal exploration.

    @param exploration_id 稳定探索标识 / Stable exploration identity.
    @param scope 发起探索的个人范围 / Personal scope initiating exploration.
    @param day UTC 业务日 / UTC business day.
    @param route 已选探索路线 / Selected exploration route.
    @param explored_at 执行探索的 UTC 时刻 / UTC instant at which exploration occurred.
    @param reward 固定经验与材料奖励 / Fixed experience and material reward.
    @param audit_digest 可复验的 SHA-256 摘要 / Verifiable SHA-256 digest.
    """

    exploration_id: UUID
    """@brief 稳定探索标识 / Stable exploration identity."""

    scope: PersonalScope
    """@brief 个人探索范围 / Personal exploration scope."""

    day: date
    """@brief UTC 业务日 / UTC business day."""

    route: ExplorationRoute
    """@brief 已选探索路线 / Selected exploration route."""

    explored_at: datetime
    """@brief 执行探索的时刻 / Exploration execution instant."""

    reward: ExplorationReward
    """@brief 固定经验与材料奖励 / Fixed experience and material reward."""

    audit_digest: str
    """@brief 可复验 SHA-256 摘要 / Verifiable SHA-256 digest."""

    def __post_init__(self) -> None:
        """@brief 验证探索快照、固定奖励和审计摘要 / Validate exploration snapshot, fixed reward, and audit digest.

        @return None / None.
        @raise TypeError 字段类型非法时抛出 / Raised when a field type is invalid.
        @raise ValueError 业务日、奖励或摘要不一致时抛出 / Raised when business day, reward, or digest is inconsistent.
        """

        if not isinstance(self.exploration_id, UUID):
            raise TypeError("Daily exploration ID must be a UUID")
        if not isinstance(self.scope, PersonalScope):
            raise TypeError("Daily exploration must use PersonalScope")
        normalized_day = normalize_day(self.day, field="Daily exploration day")
        if not isinstance(self.route, ExplorationRoute):
            raise TypeError("Daily exploration route must be ExplorationRoute")
        normalized_explored_at = normalize_instant(
            self.explored_at,
            field="Daily exploration time",
        )
        if normalized_explored_at.date() != normalized_day:
            raise ValueError("Daily exploration time must fall on its UTC business day")
        if not isinstance(self.reward, ExplorationReward):
            raise TypeError("Daily exploration reward must be ExplorationReward")
        if self.reward != reward_for_route(self.route):
            raise ValueError(
                "Daily exploration reward must match its fixed route reward"
            )
        if not isinstance(self.audit_digest, str):
            raise TypeError("Daily exploration audit digest must be a string")
        expected_digest = exploration_audit_digest(
            scope=self.scope,
            day=normalized_day,
            route=self.route,
            exploration_id=self.exploration_id,
            reward=self.reward,
        )
        if self.audit_digest != expected_digest:
            raise ValueError(
                "Daily exploration audit digest does not match the snapshot"
            )
        object.__setattr__(self, "day", normalized_day)
        object.__setattr__(self, "explored_at", normalized_explored_at)

    def verify(self) -> bool:
        """@brief 独立验证探索审计摘要 / Independently verify the exploration audit digest.

        @return 摘要与所有公开字段匹配时为 True / True when digest matches all public fields.
        """

        return self.audit_digest == exploration_audit_digest(
            scope=self.scope,
            day=self.day,
            route=self.route,
            exploration_id=self.exploration_id,
            reward=self.reward,
        )


def create_daily_exploration(
    *,
    exploration_id: UUID,
    scope: PersonalScope,
    day: date,
    route: ExplorationRoute,
    explored_at: datetime,
) -> DailyExploration:
    """@brief 按固定规则创建一次每日探索 / Create one daily exploration under fixed rules.

    @param exploration_id 调用方生成的稳定探索标识 / Stable exploration identity generated by caller.
    @param scope 发起探索的个人范围 / Personal scope initiating exploration.
    @param day UTC 业务日 / UTC business day.
    @param route 已选探索路线 / Selected exploration route.
    @param explored_at 执行探索的时刻 / Exploration execution instant.
    @return 完整且可审计的每日探索快照 / Complete auditable daily-exploration snapshot.
    @raise TypeError 范围、日期、路线或标识类型非法时抛出 /
        Raised when scope, day, route, or identity type is invalid.
    @raise ValueError 探索时刻不在业务日时抛出 / Raised when exploration time is outside the business day.
    """

    if not isinstance(scope, PersonalScope):
        raise TypeError("Daily exploration creation must use PersonalScope")
    normalized_day = normalize_day(day, field="Daily exploration day")
    if not isinstance(route, ExplorationRoute):
        raise TypeError("Daily exploration creation route must be ExplorationRoute")
    if not isinstance(exploration_id, UUID):
        raise TypeError("Daily exploration creation ID must be a UUID")
    normalized_explored_at = normalize_instant(
        explored_at,
        field="Daily exploration time",
    )
    if normalized_explored_at.date() != normalized_day:
        raise ValueError("Daily exploration time must fall on its UTC business day")
    reward = reward_for_route(route)
    audit_digest = exploration_audit_digest(
        scope=scope,
        day=normalized_day,
        route=route,
        exploration_id=exploration_id,
        reward=reward,
    )
    return DailyExploration(
        exploration_id=exploration_id,
        scope=scope,
        day=normalized_day,
        route=route,
        explored_at=normalized_explored_at,
        reward=reward,
        audit_digest=audit_digest,
    )
