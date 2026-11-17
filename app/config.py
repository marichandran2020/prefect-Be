from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    APP_NAME: str = "PERFECT ENGINERING Attendance API"
    MONGO_URI: str = "mongodb://localhost:27017"
    MONGO_DB: str = "perfect_enginering"

    JWT_SECRET: str = "change-me-in-production-use-a-long-random-string"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 12

    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"
    TIMEZONE: str = "Asia/Kolkata"

    # Shift A: 08:00 - 17:00 (9 hrs). Hours beyond this count as overtime.
    SHIFT_START: str = "08:00"
    SHIFT_END: str = "17:00"
    LATE_GRACE_MINUTES: int = 15

    # OTP / reset link are returned in the API response while no SMS / email
    # gateway is configured. Set to false once a gateway is plugged in.
    DEMO_MODE: bool = True
    OTP_EXPIRE_MINUTES: int = 5

    # Default super admin, created on first startup if missing
    DEFAULT_ADMIN_NAME: str = "ARAVIND (அரவிந்த்)"
    DEFAULT_ADMIN_EMAIL: str = "aravind@perfectenginering.com"
    DEFAULT_ADMIN_PASSWORD: str = "Aravind@2026"

    UPLOAD_DIR: Path = BASE_DIR / "uploads"
    MAX_PHOTO_BYTES: int = 3 * 1024 * 1024

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

SITES = ["Jamnagar - Reliance PV Solar Gate", "Dholeara - Tata"]
TRADES = [
    "Site Supervisor", "Lead Engineer", "Electrician", "Welder", "Technician",
    "Solar Technician", "Site Technician", "Fitter", "Rigger", "Safety Officer", "Helper",
]
