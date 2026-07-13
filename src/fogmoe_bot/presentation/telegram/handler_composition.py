"""@brief Telegram handler capability 组合根 / Telegram handler capability composition root."""

from telegram import Bot

from fogmoe_bot.config import BotSettings, EconomySettings, IdentitySettings
from fogmoe_bot.application.accounts.operations import (
    ACCOUNT_SERVICE_DATA_KEY,
    AccountService,
)
from fogmoe_bot.application.crypto.workflow import (
    CRYPTO_SERVICE_DATA_KEY,
    CryptoService,
)
from fogmoe_bot.application.economy.service import EconomyService
from fogmoe_bot.application.economy.staking import StakingService
from fogmoe_bot.application.games.gamble.service import (
    GAMBLE_SERVICE_DATA_KEY,
    GambleService,
)
from fogmoe_bot.application.games.omikuji.service import (
    OMIKUJI_SERVICE_DATA_KEY,
    OmikujiService,
)
from fogmoe_bot.application.games.rpg.character_service import (
    RPG_CHARACTER_SERVICE_DATA_KEY,
    RpgCharacterService,
)
from fogmoe_bot.application.games.ports.rpg.equipment import (
    RPG_EQUIPMENT_OPERATIONS_DATA_KEY,
)
from fogmoe_bot.application.games.rpg.inventory_service import (
    RPG_INVENTORY_SERVICE_DATA_KEY,
    RpgInventoryService,
)
from fogmoe_bot.application.games.rps_service import RPS_SERVICE_DATA_KEY, RpsService
from fogmoe_bot.application.games.runtime import GAMES_RUNTIME_DATA_KEY, GamesRuntime
from fogmoe_bot.application.games.sicbo.service import (
    SICBO_SERVICE_DATA_KEY,
    SicBoService,
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
from fogmoe_bot.infrastructure.blocking import AsyncBlockingBulkhead
from fogmoe_bot.infrastructure.crypto.binance_price import BinanceBtcPriceSource
from fogmoe_bot.infrastructure.database.account_operations import (
    PostgresAccountOperations,
)
from fogmoe_bot.infrastructure.database.crypto_operations.accounts import (
    PostgresCryptoAccountReader,
)
from fogmoe_bot.infrastructure.database.crypto_operations.chart import (
    PostgresChartOperations,
)
from fogmoe_bot.infrastructure.database.crypto_operations.prediction import (
    PostgresPredictionOperations,
)
from fogmoe_bot.infrastructure.database.crypto_operations.swap import (
    PostgresSwapOperations,
)
from fogmoe_bot.infrastructure.database.economy.accounts import (
    PostgresAccountLookup,
)
from fogmoe_bot.infrastructure.database.economy.community import (
    PostgresCommunityOperations,
)
from fogmoe_bot.infrastructure.database.economy.redemption import (
    PostgresRedemptionOperations,
)
from fogmoe_bot.infrastructure.database.economy.referral import (
    PostgresReferralOperations,
)
from fogmoe_bot.infrastructure.database.economy.rewards import (
    PostgresRewardOperations,
)
from fogmoe_bot.infrastructure.database.economy.shop import (
    PostgresShopOperations,
)
from fogmoe_bot.infrastructure.database.economy.topup import (
    PostgresTopUpOperations,
)
from fogmoe_bot.infrastructure.database.economy.web_password import (
    PostgresWebPasswordOperations,
)
from fogmoe_bot.infrastructure.database.economy_staking import (
    PostgresStakeTransactions,
)
from fogmoe_bot.infrastructure.database.game_operations.gamble import (
    PostgresGambleOperations,
)
from fogmoe_bot.infrastructure.database.game_operations.omikuji import (
    PostgresOmikujiOperations,
)
from fogmoe_bot.infrastructure.database.game_operations.rpg.character import (
    PostgresRpgCharacterOperations,
)
from fogmoe_bot.infrastructure.database.game_operations.rpg.equipment import (
    PostgresRpgEquipmentOperations,
)
from fogmoe_bot.infrastructure.database.game_operations.rpg.inventory import (
    PostgresRpgInventoryOperations,
)
from fogmoe_bot.infrastructure.database.game_operations.sicbo import (
    PostgresSicBoOperations,
)
from fogmoe_bot.infrastructure.database.media.account import (
    PostgresMediaAccountProfiles,
)
from fogmoe_bot.infrastructure.database.media.music import (
    PostgresMusicSessionRepository,
)
from fogmoe_bot.infrastructure.database.media.picture import (
    PostgresPictureRepository,
)
from fogmoe_bot.infrastructure.database.repositories.verification_repository import (
    PostgresVerificationRepository,
)
from fogmoe_bot.infrastructure.database.rps_ledger import PostgresRpsLedger
from fogmoe_bot.infrastructure.http.media.binary import AiohttpBinaryFetcher
from fogmoe_bot.infrastructure.http.media.music import JkyMusicSource
from fogmoe_bot.infrastructure.http.media.picture import BooruPictureSource
from fogmoe_bot.resources import BotResources

from . import (
    economy_handlers,
    rps_handlers,
    stake_handlers,
    verification_handlers,
)
from .game_handlers.gamble import TelegramGambleSettlementRenderer
from .handler_catalog import TelegramApplication
from .media_handlers import picture as media_picture
from .moderation_composition import (
    MODERATION_CAPABILITY_DATA_KEY,
    TelegramModerationCapability,
    create_moderation_ingress_capability,
)
from .runtime_settings import TELEGRAM_SETTINGS_DATA_KEY, TelegramRuntimeSettings

HANDLER_BLOCKING_BULKHEADS_DATA_KEY = "fogmoe.handler_blocking_bulkheads"


def create_games_capabilities(identity: IdentitySettings) -> dict[str, object]:
    """@brief 装配各个窄 Games capability / Assemble the narrow Games capabilities.

    @param identity 管理员身份设置 / Administrator identity settings.
    @return 显式 capability 键到对应服务或 worker 的映射 /
        Explicit capability keys mapped to their services or worker.
    """

    administrator_id = identity.administrator.user_id
    gamble = PostgresGambleOperations(admin_user_id=administrator_id)
    sicbo = PostgresSicBoOperations(admin_user_id=administrator_id)
    return {
        GAMBLE_SERVICE_DATA_KEY: GambleService(gamble),
        SICBO_SERVICE_DATA_KEY: SicBoService(sicbo),
        OMIKUJI_SERVICE_DATA_KEY: OmikujiService(
            PostgresOmikujiOperations(admin_user_id=administrator_id)
        ),
        RPG_CHARACTER_SERVICE_DATA_KEY: RpgCharacterService(
            PostgresRpgCharacterOperations(admin_user_id=administrator_id)
        ),
        RPG_EQUIPMENT_OPERATIONS_DATA_KEY: PostgresRpgEquipmentOperations(),
        RPG_INVENTORY_SERVICE_DATA_KEY: RpgInventoryService(
            PostgresRpgInventoryOperations()
        ),
        GAMES_RUNTIME_DATA_KEY: GamesRuntime(
            gamble,
            sicbo,
            TelegramGambleSettlementRenderer(),
        ),
    }


def create_rps_service(
    bot: Bot,
    *,
    identity: IdentitySettings,
) -> RpsService:
    """@brief 装配猜拳服务 / Assemble the RPS service.

    @param bot Telegram 投递客户端 / Telegram delivery client.
    @param identity 管理员身份设置 / Administrator identity settings.
    @return 注入 PostgreSQL 与 Telegram 端口的服务 / Service with PostgreSQL and Telegram ports.
    """

    return RpsService(
        ledger=PostgresRpsLedger(identity.administrator.user_id),
        lifecycle_sink=rps_handlers.TelegramRpsLifecycleSink(bot),
    )


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
    identity: IdentitySettings,
) -> PictureService:
    """@brief 装配图片用例 / Assemble picture use cases.

    @param accounts 媒体账户资料端口 / Media-account profile port.
    @param telemetry 进程唯一遥测 recorder / Sole process telemetry recorder.
    @param identity 管理员身份设置 / Administrator identity settings.
    @return 图片服务 / Picture service.
    """

    return PictureService(
        accounts=accounts,
        repository=PostgresPictureRepository(identity.administrator.user_id),
        source=BooruPictureSource(telemetry=telemetry),
        binary_fetcher=AiohttpBinaryFetcher(telemetry=telemetry),
        runtime=PictureRuntime(),
        preview_outbound=media_picture.TelegramPicturePreviewOutboundFactory(),
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
        topups=PostgresTopUpOperations(admin_user_id=identity.administrator.user_id),
        rewards=PostgresRewardOperations(),
        community=PostgresCommunityOperations(
            admin_user_id=identity.administrator.user_id
        ),
        redemption=PostgresRedemptionOperations(
            admin_user_id=identity.administrator.user_id
        ),
        referrals=PostgresReferralOperations(),
        web_passwords=PostgresWebPasswordOperations(),
        shop=PostgresShopOperations(admin_user_id=identity.administrator.user_id),
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

    return AccountService(
        PostgresAccountOperations(),
        initial_coins=economy.new_user_bonus_coins,
        admin_user_id=identity.administrator.user_id,
    )


def create_crypto_service(
    *,
    price_bulkhead: AsyncBlockingBulkhead,
    identity: IdentitySettings,
) -> CryptoService:
    """@brief 装配加密货币服务 / Assemble the crypto service.

    @param identity 管理员身份设置 / Administrator identity settings.
    @return 注入原子操作与有界 Binance adapter 的服务 /
        Service with atomic operations and a bounded Binance adapter.
    """

    return CryptoService(
        accounts=PostgresCryptoAccountReader(),
        charts=PostgresChartOperations(),
        predictions=PostgresPredictionOperations(
            admin_user_id=identity.administrator.user_id
        ),
        swaps=PostgresSwapOperations(admin_user_id=identity.administrator.user_id),
        prices=BinanceBtcPriceSource(bulkhead=price_bulkhead),
    )


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
        (RPS_SERVICE_DATA_KEY, "RPS service"),
        (stake_handlers.STAKING_SERVICE_DATA_KEY, "staking service"),
        (economy_handlers.ECONOMY_SERVICE_DATA_KEY, "economy service"),
        (PICTURE_SERVICE_DATA_KEY, "picture service"),
        (MUSIC_SERVICE_DATA_KEY, "music service"),
        (MODERATION_CAPABILITY_DATA_KEY, "moderation capability"),
        (CRYPTO_SERVICE_DATA_KEY, "Crypto service"),
        (GAMBLE_SERVICE_DATA_KEY, "Gamble service"),
        (SICBO_SERVICE_DATA_KEY, "Sic Bo service"),
        (OMIKUJI_SERVICE_DATA_KEY, "Omikuji service"),
        (RPG_CHARACTER_SERVICE_DATA_KEY, "RPG character service"),
        (RPG_EQUIPMENT_OPERATIONS_DATA_KEY, "RPG equipment operations"),
        (RPG_INVENTORY_SERVICE_DATA_KEY, "RPG inventory service"),
        (GAMES_RUNTIME_DATA_KEY, "Games runtime"),
        (ACCOUNT_SERVICE_DATA_KEY, "Account service"),
        (TELEGRAM_SETTINGS_DATA_KEY, "Telegram runtime settings"),
        (HANDLER_BLOCKING_BULKHEADS_DATA_KEY, "handler blocking bulkheads"),
    )
    for key, label in configured_keys:
        if key in application.bot_data:
            raise RuntimeError(f"{label} was configured more than once")

    verification_service, verification_worker = create_verification_runtime(
        application.bot
    )
    identity = settings.identity
    games_capabilities = create_games_capabilities(identity)
    media_accounts = PostgresMediaAccountProfiles()
    price_bulkhead = AsyncBlockingBulkhead(
        capacity=4,
        queue_timeout=2.0,
        call_timeout=15.0,
        task_name="binance-btc-price",
    )
    application.bot_data.update(
        {
            VERIFICATION_SERVICE_DATA_KEY: verification_service,
            VERIFICATION_WORKER_DATA_KEY: verification_worker,
            RPS_SERVICE_DATA_KEY: create_rps_service(
                application.bot,
                identity=identity,
            ),
            stake_handlers.STAKING_SERVICE_DATA_KEY: StakingService(
                PostgresStakeTransactions(),
                admin_user_id=identity.administrator.user_id,
            ),
            economy_handlers.ECONOMY_SERVICE_DATA_KEY: create_economy_service(identity),
            ACCOUNT_SERVICE_DATA_KEY: create_account_service(
                identity, settings.economy
            ),
            PICTURE_SERVICE_DATA_KEY: create_picture_service(
                media_accounts,
                telemetry=telemetry,
                identity=identity,
            ),
            MUSIC_SERVICE_DATA_KEY: create_music_service(
                media_accounts, telemetry=telemetry
            ),
            CRYPTO_SERVICE_DATA_KEY: create_crypto_service(
                price_bulkhead=price_bulkhead,
                identity=identity,
            ),
            HANDLER_BLOCKING_BULKHEADS_DATA_KEY: (price_bulkhead,),
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
    "HANDLER_BLOCKING_BULKHEADS_DATA_KEY",
    "create_account_service",
    "create_crypto_service",
    "create_economy_service",
    "create_games_capabilities",
    "create_music_service",
    "create_picture_service",
    "create_moderation_capability",
    "create_rps_service",
    "create_verification_runtime",
]
