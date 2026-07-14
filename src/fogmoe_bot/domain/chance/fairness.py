"""@brief 承诺揭示与无偏取样原语 / Commit-reveal and unbiased sampling primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import secrets
from typing import Final
from uuid import UUID


COMMITMENT_DOMAIN: Final[bytes] = b"fogmoe/chance/commitment/v1\x00"
"""@brief 服务器种子承诺的域分离前缀 / Domain-separation prefix for server-seed commitments."""

ROLL_DOMAIN: Final[bytes] = b"fogmoe/chance/roll/v1\x00"
"""@brief 每轮 HMAC 随机数的域分离前缀 / Domain-separation prefix for per-round HMAC randomness."""

MAX_UNIFORM_BOUND: Final[int] = 1 << 256
"""@brief 单个 SHA-256 摘要可无偏映射的最大上界 / Largest bound mappable without bias from one SHA-256 digest."""

MAX_NONCE: Final[int] = (1 << 63) - 1
"""@brief 协议允许的最大业务随机数 nonce / Largest business nonce admitted by the protocol."""

MAX_REJECTION_ATTEMPTS: Final[int] = 1_000_000
"""@brief 拒绝取样的防御性最大重试次数 / Defensive maximum number of rejection-sampling attempts."""


@dataclass(frozen=True, slots=True, repr=False)
class ServerSeed:
    """@brief 未揭示的服务器随机种子 / Unrevealed server randomness seed.

    ``repr`` 永远隐藏原始字节，避免日志意外泄露。结算后可通过 ``reveal_hex`` 显式
    发布。
    ``repr`` always hides raw bytes to avoid accidental logging. They may be explicitly
    published after settlement through ``reveal_hex``.

    @param value 至少 128 bit 的不可变随机字节 / Immutable random bytes with at least 128 bits.
    """

    value: bytes = field(repr=False)
    """@brief 原始服务器种子；结算前必须保密 / Raw server seed; must remain secret before settlement."""

    def __post_init__(self) -> None:
        """@brief 校验服务器种子强度 / Validate server-seed strength.

        @return None / None.
        @raise TypeError 种子不是不可变 bytes 时抛出 / Raised when the seed is not immutable bytes.
        @raise ValueError 熵长度少于 128 bit 时抛出 / Raised when entropy is below 128 bits.
        """

        if not isinstance(self.value, bytes):
            raise TypeError("Server seed must be immutable bytes")
        if len(self.value) < 16:
            raise ValueError("Server seed must contain at least 128 bits")

    @classmethod
    def random(cls, *, length: int = 32) -> ServerSeed:
        """@brief 从系统熵生成服务器种子 / Generate a server seed from operating-system entropy.

        @param length 随机字节长度，至少 16 / Random byte length, at least 16.
        @return 新服务器种子 / New server seed.
        @raise ValueError 长度不足时抛出 / Raised when the requested length is too short.
        """

        if isinstance(length, bool) or not isinstance(length, int) or length < 16:
            raise ValueError("Server-seed length must be at least 16 bytes")
        return cls(secrets.token_bytes(length))

    def reveal_hex(self) -> str:
        """@brief 显式编码已结算种子 / Explicitly encode a settled seed for publication.

        @return 小写十六进制服务器种子 / Lowercase hexadecimal server seed.
        """

        return self.value.hex()

    def __repr__(self) -> str:
        """@brief 防止调试输出泄露服务器种子 / Prevent debug output from leaking the server seed.

        @return 已脱敏的表示 / Redacted representation.
        """

        return "ServerSeed(<redacted>)"


@dataclass(frozen=True, slots=True)
class ServerSeedCommitment:
    """@brief 结算前公开的服务器种子承诺 / Public pre-settlement commitment to a server seed.

    @param hex_digest SHA-256 承诺的小写十六进制编码 / Lowercase hexadecimal SHA-256 commitment.
    """

    hex_digest: str
    """@brief SHA-256 承诺摘要 / SHA-256 commitment digest."""

    def __post_init__(self) -> None:
        """@brief 校验承诺编码 / Validate commitment encoding.

        @return None / None.
        @raise ValueError 摘要不是规范 SHA-256 十六进制时抛出 /
            Raised when the digest is not canonical SHA-256 hexadecimal.
        """

        if not isinstance(self.hex_digest, str):
            raise TypeError("Server-seed commitment must be text")
        try:
            decoded = bytes.fromhex(self.hex_digest)
        except ValueError as error:
            raise ValueError("Server-seed commitment must be hexadecimal") from error
        if len(decoded) != hashlib.sha256().digest_size:
            raise ValueError("Server-seed commitment must be a SHA-256 digest")
        if decoded.hex() != self.hex_digest:
            raise ValueError("Server-seed commitment must use lowercase canonical hex")


@dataclass(frozen=True, slots=True)
class ClientSeed:
    """@brief 玩家在揭示前给出的公开种子 / Public player seed supplied before reveal.

    @param value 用于 HMAC transcript 的 UTF-8 文本 / UTF-8 text used in the HMAC transcript.
    """

    value: str
    """@brief 玩家公开种子文本 / Player public seed text."""

    def __post_init__(self) -> None:
        """@brief 校验客户端种子 / Validate the client seed.

        @return None / None.
        @raise TypeError 种子不是文本时抛出 / Raised when the seed is not text.
        @raise ValueError 种子为空、过长或包含 NUL 时抛出 /
            Raised when the seed is empty, oversized, or contains NUL.
        """

        if not isinstance(self.value, str):
            raise TypeError("Client seed must be text")
        encoded = self.value.encode("utf-8")
        if not self.value or len(encoded) > 512 or "\x00" in self.value:
            raise ValueError("Client seed must contain 1-512 non-NUL UTF-8 bytes")


@dataclass(frozen=True, slots=True)
class FairnessSample:
    """@brief 拒绝取样后得到的无偏整数票 / Unbiased integer ticket obtained by rejection sampling.

    @param ticket ``[0, upper_bound)`` 的均匀整数 / Uniform integer in ``[0, upper_bound)``.
    @param attempt 接受摘要所用的从零开始尝试序号 / Zero-based attempt that produced the accepted digest.
    @param digest_hex 接受的 HMAC-SHA-256 摘要 / Accepted HMAC-SHA-256 digest.
    """

    ticket: int
    """@brief 无偏整数票 / Unbiased integer ticket."""

    attempt: int
    """@brief 接受时的重试序号 / Retry index at acceptance."""

    digest_hex: str
    """@brief 接受摘要的十六进制编码 / Hexadecimal encoding of the accepted digest."""

    def __post_init__(self) -> None:
        """@brief 校验取样快照 / Validate sampling snapshot.

        @return None / None.
        @raise ValueError 票、重试序号或摘要非法时抛出 /
            Raised when the ticket, retry index, or digest is invalid.
        """

        if (
            isinstance(self.ticket, bool)
            or not isinstance(self.ticket, int)
            or self.ticket < 0
        ):
            raise ValueError("Fairness ticket must be non-negative")
        if (
            isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or self.attempt < 0
        ):
            raise ValueError("Fairness attempt must be non-negative")
        if not isinstance(self.digest_hex, str):
            raise TypeError("Fairness digest must be text")
        try:
            decoded = bytes.fromhex(self.digest_hex)
        except ValueError as error:
            raise ValueError("Fairness digest must be hexadecimal") from error
        if len(decoded) != hashlib.sha256().digest_size:
            raise ValueError("Fairness digest must be a SHA-256 digest")
        if decoded.hex() != self.digest_hex:
            raise ValueError("Fairness digest must use lowercase canonical hex")


@dataclass(frozen=True, slots=True)
class FairnessProof:
    """@brief 一轮结算可独立复验的公平性证明 / Independently verifiable fairness proof for one settlement.

    @param round_id 随机活动轮次标识 / Chance-round identity.
    @param commitment 开奖前发布的承诺 / Commitment published before the draw.
    @param revealed_server_seed 开奖后披露的服务器种子 / Server seed disclosed after the draw.
    @param client_seed 玩家开奖前给出的种子 / Player seed supplied before the draw.
    @param nonce 轮次内业务 nonce / Business nonce within the round.
    @param upper_bound 无偏整数票的上界 / Upper bound of the unbiased ticket.
    @param sample 已接受的拒绝取样快照 / Accepted rejection-sampling snapshot.
    """

    round_id: UUID
    """@brief 随机活动轮次标识 / Chance-round identity."""

    commitment: ServerSeedCommitment
    """@brief 服务器种子承诺 / Server-seed commitment."""

    revealed_server_seed: ServerSeed
    """@brief 已揭示服务器种子 / Revealed server seed."""

    client_seed: ClientSeed
    """@brief 玩家客户端种子 / Player client seed."""

    nonce: int
    """@brief 业务 nonce / Business nonce."""

    upper_bound: int
    """@brief 整数票上界 / Integer-ticket upper bound."""

    sample: FairnessSample
    """@brief 无偏取样结果 / Unbiased sampling result."""

    def __post_init__(self) -> None:
        """@brief 校验证明自身的一致性 / Validate proof self-consistency.

        @return None / None.
        @raise ValueError nonce、上界或票与上界不一致时抛出 /
            Raised when nonce, bound, or ticket/bound consistency is invalid.
        """

        if not isinstance(self.round_id, UUID):
            raise TypeError("Fairness proof requires a UUID round identifier")
        if not isinstance(self.commitment, ServerSeedCommitment):
            raise TypeError("Fairness proof requires ServerSeedCommitment")
        if not isinstance(self.revealed_server_seed, ServerSeed):
            raise TypeError("Fairness proof requires ServerSeed")
        if not isinstance(self.client_seed, ClientSeed):
            raise TypeError("Fairness proof requires ClientSeed")
        if not isinstance(self.sample, FairnessSample):
            raise TypeError("Fairness proof requires FairnessSample")
        _validate_nonce(self.nonce)
        _validate_upper_bound(self.upper_bound)
        if self.sample.ticket >= self.upper_bound:
            raise ValueError("Fairness ticket must fall below its upper bound")

    def verifies(self) -> bool:
        """@brief 复算承诺、HMAC 与拒绝取样 / Recompute commitment, HMAC, and rejection sampling.

        @return 所有公开字段与协议一致时为 True / True when all public fields match the protocol.
        """

        return verify_fairness_proof(self)


def commit_server_seed(server_seed: ServerSeed) -> ServerSeedCommitment:
    """@brief 对服务器种子建立不可逆承诺 / Create a one-way commitment to a server seed.

    @param server_seed 尚未揭示的服务器种子 / Server seed that has not yet been revealed.
    @return 可在开奖前发布的承诺 / Commitment safe to publish before settlement.
    @raise TypeError 参数不是服务器种子时抛出 / Raised when the argument is not a server seed.
    """

    if not isinstance(server_seed, ServerSeed):
        raise TypeError("Server-seed commitment requires ServerSeed")
    digest = hashlib.sha256(COMMITMENT_DOMAIN + server_seed.value).hexdigest()
    return ServerSeedCommitment(digest)


def sample_uniform_ticket(
    *,
    server_seed: ServerSeed,
    round_id: UUID,
    client_seed: ClientSeed,
    nonce: int,
    upper_bound: int,
) -> FairnessSample:
    """@brief 以 HMAC 和拒绝取样生成无偏整数票 / Generate an unbiased integer ticket with HMAC and rejection sampling.

    令 ``M = 2^256``，``n = upper_bound``，接受阈值为
    ``L = M - (M mod n)``。仅当原始摘要整数 ``x < L`` 时取 ``x mod n``，
    因此每个余数恰有相同数量的原像，不会产生模偏差（modulo bias）。
    Let ``M = 2^256``, ``n = upper_bound``, and ``L = M - (M mod n)``. The
    sampler returns ``x mod n`` only when raw digest integer ``x < L``. Every
    residue therefore has the same number of preimages, eliminating modulo bias.

    @param server_seed 未揭示或已揭示的服务器种子 / Unrevealed or revealed server seed.
    @param round_id 轮次 UUID / Round UUID.
    @param client_seed 玩家客户端种子 / Player client seed.
    @param nonce 轮次内业务 nonce / Business nonce within the round.
    @param upper_bound 输出整数的开区间上界 / Exclusive upper bound of the output integer.
    @return 无偏票及其可审计摘要 / Unbiased ticket and its auditable digest.
    @raise RuntimeError 防御性重试上限被耗尽时抛出 / Raised if the defensive retry limit is exhausted.
    """

    if not isinstance(server_seed, ServerSeed):
        raise TypeError("Uniform sampling requires ServerSeed")
    if not isinstance(client_seed, ClientSeed):
        raise TypeError("Uniform sampling requires ClientSeed")
    if not isinstance(round_id, UUID):
        raise TypeError("Uniform sampling requires a UUID round identifier")
    _validate_nonce(nonce)
    _validate_upper_bound(upper_bound)

    acceptance_limit = MAX_UNIFORM_BOUND - (MAX_UNIFORM_BOUND % upper_bound)
    for attempt in range(MAX_REJECTION_ATTEMPTS):
        digest = hmac.new(
            server_seed.value,
            _roll_message(round_id, client_seed, nonce, attempt),
            hashlib.sha256,
        ).digest()
        candidate = int.from_bytes(digest, byteorder="big", signed=False)
        if candidate < acceptance_limit:
            return FairnessSample(
                ticket=candidate % upper_bound,
                attempt=attempt,
                digest_hex=digest.hex(),
            )
    raise RuntimeError("Rejection sampling exhausted its defensive retry limit")


def reveal_fairness_proof(
    *,
    round_id: UUID,
    commitment: ServerSeedCommitment,
    server_seed: ServerSeed,
    client_seed: ClientSeed,
    nonce: int,
    upper_bound: int,
) -> FairnessProof:
    """@brief 揭示种子并建立完整公平性证明 / Reveal a seed and build the complete fairness proof.

    @param round_id 轮次 UUID / Round UUID.
    @param commitment 开奖前承诺 / Pre-draw commitment.
    @param server_seed 当前揭示的服务器种子 / Server seed being revealed now.
    @param client_seed 玩家客户端种子 / Player client seed.
    @param nonce 轮次内业务 nonce / Business nonce within the round.
    @param upper_bound 输出整数的开区间上界 / Exclusive upper bound of the output integer.
    @return 可公开复验的公平性证明 / Publicly verifiable fairness proof.
    @raise ValueError 种子不匹配开奖前承诺时抛出 /
        Raised when the seed does not match the pre-draw commitment.
    """

    if not isinstance(commitment, ServerSeedCommitment):
        raise TypeError("Fairness reveal requires ServerSeedCommitment")
    if not isinstance(round_id, UUID):
        raise TypeError("Fairness reveal requires a UUID round identifier")
    expected_commitment = commit_server_seed(server_seed)
    if not hmac.compare_digest(commitment.hex_digest, expected_commitment.hex_digest):
        raise ValueError("Revealed server seed does not match the published commitment")
    return FairnessProof(
        round_id=round_id,
        commitment=commitment,
        revealed_server_seed=server_seed,
        client_seed=client_seed,
        nonce=nonce,
        upper_bound=upper_bound,
        sample=sample_uniform_ticket(
            server_seed=server_seed,
            round_id=round_id,
            client_seed=client_seed,
            nonce=nonce,
            upper_bound=upper_bound,
        ),
    )


def verify_fairness_proof(proof: object) -> bool:
    """@brief 独立验证一个已揭示的公平性证明 / Independently verify a revealed fairness proof.

    @param proof 待验证的公开证明 / Public proof to verify.
    @return 承诺、取样票与接受摘要均匹配时为 True / True when commitment, ticket, and digest all match.
    """

    if not isinstance(proof, FairnessProof):
        return False
    try:
        expected_commitment = commit_server_seed(proof.revealed_server_seed)
        expected_sample = sample_uniform_ticket(
            server_seed=proof.revealed_server_seed,
            round_id=proof.round_id,
            client_seed=proof.client_seed,
            nonce=proof.nonce,
            upper_bound=proof.upper_bound,
        )
    except (TypeError, ValueError, RuntimeError):
        return False
    return (
        hmac.compare_digest(proof.commitment.hex_digest, expected_commitment.hex_digest)
        and proof.sample.ticket == expected_sample.ticket
        and proof.sample.attempt == expected_sample.attempt
        and hmac.compare_digest(proof.sample.digest_hex, expected_sample.digest_hex)
    )


def _roll_message(
    round_id: UUID,
    client_seed: ClientSeed,
    nonce: int,
    attempt: int,
) -> bytes:
    """@brief 构造无歧义 HMAC transcript / Construct an unambiguous HMAC transcript.

    @param round_id 轮次 UUID / Round UUID.
    @param client_seed 玩家客户端种子 / Player client seed.
    @param nonce 轮次内业务 nonce / Business nonce within the round.
    @param attempt 拒绝取样尝试序号 / Rejection-sampling attempt index.
    @return 长度前缀明确的二进制 transcript / Length-prefixed binary transcript.
    """

    encoded_seed = client_seed.value.encode("utf-8")
    return b"".join(
        (
            ROLL_DOMAIN,
            round_id.bytes,
            nonce.to_bytes(8, byteorder="big", signed=False),
            attempt.to_bytes(8, byteorder="big", signed=False),
            len(encoded_seed).to_bytes(2, byteorder="big", signed=False),
            encoded_seed,
        )
    )


def _validate_nonce(nonce: int) -> None:
    """@brief 校验协议业务 nonce / Validate a protocol business nonce.

    @param nonce 待校验 nonce / Nonce to validate.
    @return None / None.
    @raise ValueError nonce 非法时抛出 / Raised when the nonce is invalid.
    """

    if isinstance(nonce, bool) or not isinstance(nonce, int):
        raise ValueError("Fairness nonce must be an integer")
    if nonce < 0 or nonce > MAX_NONCE:
        raise ValueError("Fairness nonce falls outside the protocol range")


def _validate_upper_bound(upper_bound: int) -> None:
    """@brief 校验拒绝取样上界 / Validate a rejection-sampling upper bound.

    @param upper_bound 待校验的开区间上界 / Exclusive upper bound to validate.
    @return None / None.
    @raise ValueError 上界非法时抛出 / Raised when the bound is invalid.
    """

    if isinstance(upper_bound, bool) or not isinstance(upper_bound, int):
        raise ValueError("Uniform upper bound must be an integer")
    if upper_bound <= 0 or upper_bound > MAX_UNIFORM_BOUND:
        raise ValueError("Uniform upper bound falls outside the protocol range")
