from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_name: str = "TAMP Backend"
    environment: str = "development"
    database_url: str = "postgresql+psycopg://tamp:tamp@localhost:5432/tamp"
    secret_key: str = "development-only-change-before-deployment"
    cookie_secure: bool = False
    session_hours: int = 24

    # Initial platform account. Bootstrap runs only when BOOTSTRAP_SECRET is set.
    bootstrap_secret: str | None = None
    default_platform_admin_username: str = "tamp-admin"
    default_platform_admin_email: str = "tamp-admin@tamp.local"
    default_platform_admin_password: str | None = None
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "backend", "testserver"]
    trusted_proxy_ips: list[str] = []
    dev_email_codes: bool = False
    mailjet_api_key: str = ""
    mailjet_secret_key: str = ""
    mailjet_from_email: str = ""
    mailjet_from_name: str = "TAMP"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@tamp.local"
    smtp_starttls: bool = True
    google_maps_api_key: str = ""
    upload_path: str = "storage/uploads"
    max_upload_bytes: int = 10 * 1024 * 1024
    max_account_upload_bytes: int = 100 * 1024 * 1024
    quote_base_zar: float = 500
    quote_per_km_zar: float = 12
    quote_per_tonne_km_zar: float = 0.35
    rate_limit: int = 240
    auth_rate_limit: int = 15
    webhook_allowed_hosts: list[str] = []

    @model_validator(mode="after")
    def production(self):
        if self.environment == "production":
            if (
                self.secret_key.startswith(("development", "replace-with", "change-me"))
                or len(self.secret_key) < 32
            ):
                raise ValueError(
                    "Production requires a random SECRET_KEY of at least 32 characters"
                )
            if "replace-with" in self.database_url or ":tamp@" in self.database_url:
                raise ValueError("Production requires non-default database credentials")
            mailjet_configured = all(
                (self.mailjet_api_key, self.mailjet_secret_key, self.mailjet_from_email)
            )
            if (
                not self.cookie_secure
                or self.dev_email_codes
                or not (mailjet_configured or self.smtp_host)
            ):
                raise ValueError(
                    "Production requires secure cookies, email delivery, and disabled dev email codes"
                )
            if "*" in self.allowed_hosts or "*" in self.cors_origins:
                raise ValueError("Production requires explicit host and origin allowlists")
        return self


@lru_cache
def get_settings():
    return Settings()


settings = get_settings()
