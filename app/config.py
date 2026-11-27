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
    # Weekly review: one call per user per day at most, a few cents each.
    review_model: str = "claude-opus-5-5"
    review_effort: str = "medium"
    # Coach chat: a fresh snapshot of the diary goes with every message.
    coach_model: str = "claude-opus-5-5"
    coach_effort: str = "low"
    coach_daily_messages: int = 25  # per user, on top of the $ limits

    secret_key: str = "change-me"
    # Sessions last this long; friends add the app to the home screen and stay signed in.
    session_days: int = 90

    # Spending caps in USD. A user's own cap (users.daily_limit_usd) overrides the default.
    default_daily_limit_usd: float = 0.30
    monthly_limit_usd: float = 15.0

    # Web Push keys: generate with `python -m scripts.vapid`. Empty = no push.
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = ""  # a mailto: or https: contact the push services may use
    # The reminder loop; tests switch it off.
    scheduler_enabled: bool = True

    # Timezone of new users until their browser reports its own.
    timezone: str = "Europe/Berlin"

    # Android APK (a Trusted Web Activity around this site): the package name and the SHA-256
    # fingerprint(s) of its signing certificate, comma-separated. Chrome checks them at
    # /.well-known/assetlinks.json and hides the address bar only when they match.
    android_package: str = ""
    android_cert_sha256: str = ""

    db_path: str = "data/food.db"

    # Meal photo thumbnails (~20 KB each) are kept this many days; 0 = never stored.
    photo_keep_days: int = 30

    @property
    def data_dir(self) -> Path:
        """Where the database lives; photos sit next to it."""
        return (BASE_DIR / self.db_path).resolve().parent

    @property
    def db_url(self) -> str:
        db_file = (BASE_DIR / self.db_path).resolve()
        db_file.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{db_file}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
