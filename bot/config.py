"""Application configuration loaded from environment variables."""

from dataclasses import dataclass
import os

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str
    redis_url: str
    review_chat_id: int | None
    publish_channel_id: int | None
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        if not bot_token or bot_token == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
            raise RuntimeError(
                "BOT_TOKEN não foi definido. Adicione-o ao seu arquivo .env local ou às variáveis do Railway."
            )

        # DATABASE_URL / REDIS_URL are optional: when absent or empty the bot
        # still starts and simply runs without persistence / caching. This keeps
        # local testing easy; on Railway the linked services fill these in.
        database_url = os.getenv("DATABASE_URL", "").strip()
        redis_url = os.getenv("REDIS_URL", "").strip()
        review_chat_id = cls._optional_int("REVIEW_CHAT_ID")
        publish_channel_id = cls._optional_int("PUBLISH_CHANNEL_ID")

        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
        return cls(
            bot_token=bot_token,
            database_url=database_url,
            redis_url=redis_url,
            review_chat_id=review_chat_id,
            publish_channel_id=publish_channel_id,
            log_level=log_level,
        )

    @staticmethod
    def _optional_int(name: str) -> int | None:
        raw_value = os.getenv(name, "").strip()
        if not raw_value:
            return None
        return int(raw_value)
