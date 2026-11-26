from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(BASE_DIR / ".env"), env_file_encoding="utf-8")

    anthropic_api_key: str = ""
    # Sonnet at low effort: ~2-3 cents a photo. Haiku 5.5 is ~20x cheaper but
    # misjudges portions more often; compare both on real photos before switching.
    recognize_model: str = "claude-sonnet-5-5"
    recognize_effort: str = "low"  # low | medium | high

    secret_key: str = "change-me"
    # Sessions last this long; friends add the app to the home screen and stay signed in.
    session_days: int = 90

    # Spending caps in USD. A user's own cap (users.daily_limit_usd) overrides the default.
    default_daily_limit_usd: float = 0.30
    monthly_limit_usd: float = 15.0

    # Timezone of new users until their browser reports its own.
    timezone: str = "Europe/Berlin"

    db_path: str = "data/food.db"

    @property
    def db_url(self) -> str:
        db_file = (BASE_DIR / self.db_path).resolve()
        db_file.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{db_file}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
