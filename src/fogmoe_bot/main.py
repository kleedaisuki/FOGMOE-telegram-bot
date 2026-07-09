from fogmoe_bot.infrastructure.logging.bot_logging import configure_logging
from fogmoe_bot.infrastructure.database.migration_service import run_startup_migrations
from fogmoe_bot.presentation.telegram.bot_app import run


def main() -> None:
    configure_logging()
    run_startup_migrations()
    run()


if __name__ == '__main__':
    main()
