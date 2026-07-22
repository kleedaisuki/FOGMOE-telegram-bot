"""@brief Telegram handler capability 组合根 / Telegram handler capability composition root."""

from telegram import Bot

from fogmoe_bot.application.accounts.operations import (
    ACCOUNT_SERVICE_DATA_KEY,
    AccountService,
)
from fogmoe_bot.application.banking.service import BANK_SERVICE_DATA_KEY, BankService
from fogmoe_bot.application.billing.service import (
    BILLING_SERVICE_DATA_KEY,
    BillingService,
)
from fogmoe_bot.application.chance.service import ChanceService
from fogmoe_bot.application.chance.workflow import (
    CHANCE_WORKFLOW_DATA_KEY,
    ChanceWorkflow,
)
from fogmoe_bot.application.crypto.chart_service import (
    CHART_SERVICE_DATA_KEY,
    ChartService,
)
from fogmoe_bot.application.economy.service import EconomyService
from fogmoe_bot.application.games.omikuji.service import (
    OMIKUJI_SERVICE_DATA_KEY,
    OmikujiService,
)
from fogmoe_bot.application.media.account import MediaAccountProfiles
from fogmoe_bot.application.media.music_runtime import MusicRuntime
from fogmoe_bot.application.media.music_service import (
    MUSIC_SERVICE_DATA_KEY,
    MusicService,
)
from fogmoe_bot.application.media.picture_runtime import PictureRuntime
from fogmoe_bot.application.media.picture_service import (
    PICTURE_SERVICE_DATA_KEY,
    PictureService,
)
from fogmoe_bot.application.moderation.verification_service import (
    VERIFICATION_SERVICE_DATA_KEY,
    VerificationService,
)
from fogmoe_bot.application.moderation.verification_worker import (
    VERIFICATION_WORKER_DATA_KEY,
    VerificationTimeoutWorker,
)
from fogmoe_bot.application.observability.telemetry import Telemetry
from fogmoe_bot.application.personal_rpg.service import (
    PERSONAL_RPG_SERVICE_DATA_KEY,
    PersonalRpgService,
)
from fogmoe_bot.application.town.service import TOWN_SERVICE_DATA_KEY, TownService
from fogmoe_bot.config import BotSettings, EconomySettings, IdentitySettings
from fogmoe_bot.domain.accounts.plan import AccountPlanPolicy
from fogmoe_bot.infrastructure.billing.payment_events import (
    DenyUnconfiguredPaymentEventVerifier,
)
from fogmoe_bot.infrastructure.database.account_operations import (
    PostgresAccountOperations,
)
from fogmoe_bot.infrastructure.database.account_plan import PostgresAccountPlanResolver
from fogmoe_bot.infrastructure.database.banking import PostgresBankOperations
from fogmoe_bot.infrastructure.database.billing import PostgresBillingOperations
from fogmoe_bot.infrastructure.database.chance import PostgresChanceRoundOperations
from fogmoe_bot.infrastructure.database.crypto_operations.chart import (
    PostgresChartOperations,
)
from fogmoe_bot.infrastructure.database.economy.accounts import (
    PostgresAccountLookup,
)
from fogmoe_bot.infrastructure.database.economy.community import (
    PostgresCommunityOperations,
)
from fogmoe_bot.infrastructure.database.economy.referral import (
    PostgresReferralOperations,
)
from fogmoe_bot.infrastructure.database.economy.rewards import (
    PostgresRewardOperations,
)
from fogmoe_bot.infrastructure.database.economy.web_password import (
    PostgresWebPasswordOperations,
)
from fogmoe_bot.infrastructure.database.game_operations.omikuji import (
    PostgresOmikujiOperations,
)
from fogmoe_bot.infrastructure.database.media.account import (
    PostgresMediaAccountProfiles,
)
from fogmoe_bot.infrastructure.database.media.music import (
    PostgresMusicSessionRepository,
)
from fogmoe_bot.infrastructure.database.personal_rpg import (
    PostgresPersonalRpgOperations,
)
from fogmoe_bot.infrastructure.database.moderation.verification import (
    PostgresVerificationRepository,
)
from fogmoe_bot.infrastructure.database.town import PostgresTownOperations
from fogmoe_bot.infrastructure.http.media.music import JkyMusicSource
from fogmoe_bot.infrastructure.http.media.picture import BooruPictureSource
from fogmoe_bot.resources import BotResources

from . import (
    economy_handlers,
    verification_handlers,
)
from .handler_catalog import TelegramApplication
from .moderation_composition import (
    MODERATION_CAPABILITY_DATA_KEY,
    TelegramModerationCapability,
    create_moderation_ingress_capability,
)
from .runtime_settings import TELEGRAM_SETTINGS_DATA_KEY, TelegramRuntimeSettings
from .town_handlers import TelegramTownAuthorization


def create_games_capabilities() -> dict[str, object]:
    """@brief 装配各个窄 Games capability / Assemble the narrow Games capabilities.

    @return 显式 capability 键到对应服务或 worker 的映射 /
        Explicit capability keys mapped to their services or worker.
    """

    return {
        OMIKUJI_SERVICE_DATA_KEY: OmikujiService(PostgresOmikujiOperations()),
    }


def create_verification_runtime(
    bot: Bot,
) -> tuple[VerificationService, VerificationTimeoutWorker]:
    """@brief 装配验证服务与 worker / Assemble verification service and worker.

    @param bot Telegram 投递客户端 / Telegram delivery client.
    @return 共享仓储的服务与 worker / Service and worker sharing one repository.
    """

    repository = PostgresVerificationRepository()
    service = VerificationService(
        repository=repository,
        delivery=verification_handlers.TelegramVerificationDelivery(bot),
    )
    return service, VerificationTimeoutWorker(repository=repository, service=service)


def create_picture_service(
    accounts: MediaAccountProfiles,
    *,
    telemetry: Telemetry,
) -> PictureService:
    """@brief 装配图片用例 / Assemble picture use cases.

    @param accounts 媒体账户资料端口 / Media-account profile port.
    @param telemetry 进程唯一遥测 recorder / Sole process telemetry recorder.
    @return 图片服务 / Picture service.
    """

    return PictureService(
        accounts=accounts,
        source=BooruPictureSource(telemetry=telemetry),
        runtime=PictureRuntime(),
    )


def create_music_service(
    accounts: MediaAccountProfiles, *, telemetry: Telemetry
) -> MusicService:
    """装配音乐用例 / Assemble music use cases."""

    return MusicService(
        accounts=accounts,
        sessions=PostgresMusicSessionRepository(),
        source=JkyMusicSource(telemetry=telemetry),
        runtime=MusicRuntime(),
    )


def create_economy_service(identity: IdentitySettings) -> EconomyService:
    """@brief 装配经济服务 / Assemble the economy service.

    @param identity 管理员身份设置 / Administrator identity settings.
    @return 注入 PostgreSQL 原子操作的服务 / Service with PostgreSQL atomic operations.
    """

    return EconomyService(
        accounts=PostgresAccountLookup(),
        rewards=PostgresRewardOperations(),
        community=PostgresCommunityOperations(),
        referrals=PostgresReferralOperations(),
        web_passwords=PostgresWebPasswordOperations(),
    )


def create_bank_service(identity: IdentitySettings) -> BankService:
    """@brief 装配银行账本服务 / Assemble the banking-ledger service.

    @param identity 管理员身份设置 / Administrator identity settings.
    @return 注入 PostgreSQL 原子银行操作的服务 / Service with PostgreSQL atomic bank operations.
    """

    return BankService(
        PostgresBankOperations(),
        administrator_id=identity.administrator.user_id,
    )


def create_billing_service(identity: IdentitySettings) -> BillingService:
    """@brief 装配独立的 Billing 与权益服务 / Assemble the independent Billing and entitlement service.

    @param identity 管理员身份设置 / Administrator identity settings.
    @return 以安全拒绝验证器启动的 Billing 服务 / Billing service started with a safe deny-by-default verifier.
    @note 生产部署只有显式接入支付渠道签名验证器后，才应替换默认拒绝适配器。/
        A production deployment must replace the deny-by-default adapter only after explicitly
        integrating a payment-provider signature verifier.
    """

    return BillingService(
        operations=PostgresBillingOperations(),
        payment_events=DenyUnconfiguredPaymentEventVerifier(),
        administrator_id=identity.administrator.user_id,
    )


def create_chance_workflow() -> ChanceWorkflow:
    """@brief 装配 Free-only 的可验证随机活动工作流 / Assemble the Free-only verifiable-chance workflow.

    @return 连接纯数学服务与原子 PostgreSQL 端口的工作流 /
        Workflow connecting the pure mathematical service to an atomic PostgreSQL port.
    """

    return ChanceWorkflow(
        operations=PostgresChanceRoundOperations(),
        chance=ChanceService(),
    )


def create_personal_rpg_service() -> PersonalRpgService:
    """@brief 装配私聊个人冒险服务 / Assemble the private personal-adventure service.

    @return 以个人 RPG PostgreSQL 端口支撑的服务 / Service backed by the personal-RPG PostgreSQL port.
    """

    return PersonalRpgService(operations=PostgresPersonalRpgOperations())


def create_town_service(bot: Bot) -> TownService:
    """@brief 装配群组小镇服务与 Telegram 授权 / Assemble the group-town service and Telegram authorization.

    @param bot Telegram Bot 客户端 / Telegram Bot client.
    @return 使用小镇事务端口和成员资格提前拒绝层的服务 /
        Service using the town transaction port and membership early-rejection layer.
    """

    return TownService(
        operations=PostgresTownOperations(),
        authorization=TelegramTownAuthorization(bot),
    )


def create_account_service(
    identity: IdentitySettings,
    economy: EconomySettings,
) -> AccountService:
    """@brief 装配账户服务 / Assemble the account service.

    @param identity 管理员身份设置 / Administrator identity settings.
    @param economy 经济系统设置 / Economy-system settings.
    @return 注入 PostgreSQL receipts 的服务 / Service backed by PostgreSQL receipts.
    """

    plans = PostgresAccountPlanResolver(
        AccountPlanPolicy(administrator_id=identity.administrator.user_id)
    )
    # @brief 由实时付费余额、订阅与显式管理员身份推导方案 /
    # Derive plans from live paid balance, subscriptions, and explicit administrator identity.
    return AccountService(
        PostgresAccountOperations(plans),
        initial_coins=economy.new_user_bonus_coins,
    )


def create_chart_service() -> ChartService:
    """@brief 装配仅群组图表能力 / Assemble the group-chart-only capability.

    @return 不含报价、预测或资产交易逻辑的图表服务 /
        Chart service without pricing, prediction, or asset-trading logic.
    """

    return ChartService(PostgresChartOperations())


def create_moderation_capability(
    bot: Bot,
    *,
    resources: BotResources,
) -> TelegramModerationCapability:
    """@brief 装配治理 capability / Assemble the moderation capability.

    @param bot Telegram 投递客户端 / Telegram delivery client.
    @param resources 组合根加载的只读资源 / Read-only resources loaded by the composition root.
    @return runtime-owned 治理 capability / Runtime-owned moderation capability.
    """

    return create_moderation_ingress_capability(
        bot,
        wordlist_path=resources.moderation_wordlist_path,
    )


def assemble_handler_capabilities(
    application: TelegramApplication,
    *,
    telemetry: Telemetry,
    settings: BotSettings,
    resources: BotResources,
) -> None:
    """@brief 装配 handler 所需服务 / Assemble services required by handlers.

    @param application PTB Application / PTB Application.
    @param telemetry 进程唯一遥测 recorder / Sole process telemetry recorder.
    @param settings 已验证的 Bot 设置 / Validated Bot settings.
    @param resources 组合根加载的只读资源 / Read-only resources loaded by the composition root.
    @return None / None.
    @raise RuntimeError capability 被重复装配时抛出 / Raised when a capability is assembled twice.
    """

    configured_keys = (
        (VERIFICATION_SERVICE_DATA_KEY, "verification runtime"),
        (economy_handlers.ECONOMY_SERVICE_DATA_KEY, "economy service"),
        (BANK_SERVICE_DATA_KEY, "bank service"),
        (BILLING_SERVICE_DATA_KEY, "Billing service"),
        (CHANCE_WORKFLOW_DATA_KEY, "verifiable chance workflow"),
        (PERSONAL_RPG_SERVICE_DATA_KEY, "personal RPG service"),
        (TOWN_SERVICE_DATA_KEY, "town service"),
        (PICTURE_SERVICE_DATA_KEY, "picture service"),
        (MUSIC_SERVICE_DATA_KEY, "music service"),
        (MODERATION_CAPABILITY_DATA_KEY, "moderation capability"),
        (CHART_SERVICE_DATA_KEY, "chart service"),
        (OMIKUJI_SERVICE_DATA_KEY, "Omikuji service"),
        (ACCOUNT_SERVICE_DATA_KEY, "Account service"),
        (TELEGRAM_SETTINGS_DATA_KEY, "Telegram runtime settings"),
    )
    for key, label in configured_keys:
        if key in application.bot_data:
            raise RuntimeError(f"{label} was configured more than once")

    verification_service, verification_worker = create_verification_runtime(
        application.bot
    )
    identity = settings.identity
    games_capabilities = create_games_capabilities()
    media_accounts = PostgresMediaAccountProfiles()
    application.bot_data.update(
        {
            VERIFICATION_SERVICE_DATA_KEY: verification_service,
            VERIFICATION_WORKER_DATA_KEY: verification_worker,
            economy_handlers.ECONOMY_SERVICE_DATA_KEY: create_economy_service(identity),
            BANK_SERVICE_DATA_KEY: create_bank_service(identity),
            BILLING_SERVICE_DATA_KEY: create_billing_service(identity),
            CHANCE_WORKFLOW_DATA_KEY: create_chance_workflow(),
            PERSONAL_RPG_SERVICE_DATA_KEY: create_personal_rpg_service(),
            TOWN_SERVICE_DATA_KEY: create_town_service(application.bot),
            ACCOUNT_SERVICE_DATA_KEY: create_account_service(
                identity, settings.economy
            ),
            PICTURE_SERVICE_DATA_KEY: create_picture_service(
                media_accounts,
                telemetry=telemetry,
            ),
            MUSIC_SERVICE_DATA_KEY: create_music_service(
                media_accounts, telemetry=telemetry
            ),
            CHART_SERVICE_DATA_KEY: create_chart_service(),
            **games_capabilities,
            MODERATION_CAPABILITY_DATA_KEY: create_moderation_capability(
                application.bot,
                resources=resources,
            ),
            TELEGRAM_SETTINGS_DATA_KEY: TelegramRuntimeSettings(
                administrator_id=identity.administrator.user_id,
                administrator_contact_name=identity.administrator.contact_name,
                new_user_bonus=settings.economy.new_user_bonus_coins,
            ),
        }
    )


__all__ = [
    "assemble_handler_capabilities",
    "create_account_service",
    "create_bank_service",
    "create_billing_service",
    "create_chance_workflow",
    "create_chart_service",
    "create_economy_service",
    "create_games_capabilities",
    "create_music_service",
    "create_picture_service",
    "create_moderation_capability",
    "create_personal_rpg_service",
    "create_town_service",
    "create_verification_runtime",
]
