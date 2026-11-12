from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_name: str = "TAMP"
    environment: str = "development"
    database_url: str = "postgresql+asyncpg://tamp:tamp@localhost:5432/tamp"
    secret_key: str = "development-only-change-before-deployment"
    cookie_secure: bool = False
    session_hours: int = 24
    session_idle_minutes: int = 60

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
    frontend_url: str = "http://localhost:3000"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@tamp.local"
    smtp_starttls: bool = True
    geocoding_provider: str = "photon"
    photon_url: str = "https://photon.komoot.io"
    tomtom_url: str = "https://api.tomtom.com"
    tomtom_api_key: str = ""
    routing_provider: str = "valhalla"
    valhalla_url: str = "https://valhalla1.openstreetmap.de"
    match_offer_minutes: int = 30
    tracking_retention_days: int = 30
    geofence_radius_m: int = 250
    geofence_exit_radius_m: int = 350
    upload_path: str = "storage/uploads"
    max_upload_bytes: int = 10 * 1024 * 1024
    max_account_upload_bytes: int = 100 * 1024 * 1024
    quote_base_zar: float = 500
    quote_per_km_zar: float = 12
    quote_per_tonne_km_zar: float = 0.35
    platform_commission_rate: float = 0.10
    invoice_due_days: int = 7
    rate_limit: int = 240
    auth_rate_limit: int = 15
    auth_account_attempts: int = 5
    auth_account_window_minutes: int = 15
    admin_mfa_required: bool = False
    clamav_host: str = ""
    clamav_port: int = 3310
    require_malware_scan: bool = False
    webhook_allowed_hosts: list[str] = []
    log_path: str = "logs"
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 5

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
            if self.geocoding_provider == "tomtom" and not self.tomtom_api_key:
                raise ValueError("Production TomTom geocoding requires TOMTOM_API_KEY")
            if self.session_hours > 24 or not 5 <= self.session_idle_minutes <= 120:
                raise ValueError("Production sessions require a 5-120 minute idle timeout and at most 24 hours")
            if not self.admin_mfa_required:
                raise ValueError("Production requires ADMIN_MFA_REQUIRED=true")
            if not self.require_malware_scan or not self.clamav_host:
                raise ValueError(
                    "Production requires REQUIRE_MALWARE_SCAN=true and CLAMAV_HOST"
                )
            if (
                self.default_platform_admin_password
                and len(self.default_platform_admin_password) < 16
            ):
                raise ValueError(
                    "Production DEFAULT_PLATFORM_ADMIN_PASSWORD must be at least 16 characters"
                )
        return self


@lru_cache
def get_settings():
    return Settings()


settings = get_settings()
