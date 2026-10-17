"""Application configuration via pydantic-settings.

All settings are read from environment variables with sane defaults so the app
boots without any env configured (local/test).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core infra
    DATABASE_URL: str = "sqlite+aiosqlite:///./computecoin.db"
    REDIS_URL: str = "redis://localhost:6379/0"

    # Secrets (overridden in prod via env)
    AUTH_SECRET: str = "dev-auth-secret-change-me"
    SESSION_SECRET: str = "dev-session-secret-change-me"

    # Token lifetimes
    ACCESS_TOKEN_TTL_SECONDS: int = 900  # 15 minutes
    REFRESH_TOKEN_TTL_SECONDS: int = 60 * 60 * 24 * 14  # 14 days

    # Token lifetimes for email/password flows
    EMAIL_VERIFICATION_TTL_SECONDS: int = 60 * 60 * 24  # 24 hours
    PASSWORD_RESET_TTL_SECONDS: int = 60 * 60  # 1 hour

    # Environment / logging
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    FRONTEND_URL: str = "http://localhost:3000"

    # JWT
    JWT_ALGORITHM: str = "HS256"
    JWT_ISSUER: str = "computecoin"

    # Rate limiting (requests per window)
    LOGIN_RATE_LIMIT: int = 5
    LOGIN_RATE_WINDOW_SECONDS: int = 60
    REGISTER_RATE_LIMIT: int = 5
    REGISTER_RATE_WINDOW_SECONDS: int = 60
    PASSWORD_RESET_RATE_LIMIT: int = 5
    PASSWORD_RESET_RATE_WINDOW_SECONDS: int = 60
    ENROLLMENT_TOKEN_RATE_LIMIT: int = 10
    ENROLLMENT_TOKEN_RATE_WINDOW_SECONDS: int = 60

    # Phase 2 — provider / machine agent protocol
    ENROLLMENT_TOKEN_TTL_SECONDS: int = 60 * 60  # 1 hour
    HEARTBEAT_INTERVAL_SECONDS: int = 30
    ONLINE_WINDOW_SECONDS: int = 90
    MACHINE_HMAC_SKEW_SECONDS: int = 300

    # Phase 3 — marketplace / quotes / reservations
    QUOTE_TTL_SECONDS: int = 120
    RESERVATION_TTL_SECONDS: int = 900
    DEFAULT_PLATFORM_FEE_RATE: str = "0.10"
    MARKETPLACE_PAGE_SIZE_DEFAULT: int = 20
    MARKETPLACE_PAGE_SIZE_MAX: int = 100

    # Phase 4 — job execution / FSM / scheduling / artifacts
    LEASE_TTL_SECONDS: int = 60
    JOB_MAX_RUNTIME_DEFAULT: int = 300
    JOB_POLL_INTERVAL_SECONDS: int = 5
    JOB_RESERVATION_TTL_SECONDS: int = 3600  # how long a job reservation holds capacity
    # Transparent scheduler weights (price is "lower is better").
    SCHEDULER_WEIGHT_PRICE: float = 0.5
    SCHEDULER_WEIGHT_RELIABILITY: float = 0.3
    SCHEDULER_WEIGHT_AVAILABILITY: float = 0.2
    # Artifact storage limits + object store configuration.
    ARTIFACT_MAX_BYTES: int = 25 * 1024 * 1024  # 25 MiB
    ARTIFACT_CONTENT_TYPES: str = "application/json,text/plain,application/octet-stream"
    ARTIFACT_GRANT_TTL_SECONDS: int = 900
    OBJECT_STORAGE_LOCAL_DIR: str = "./.artifact_store"
    OBJECT_STORAGE_ENDPOINT: str = ""  # set -> use S3Store
    OBJECT_STORAGE_BUCKET: str = "computecoin-artifacts"

    # Phase 5 — billing / double-entry ledger / invoices / payouts / disputes
    BILLING_SIGNUP_GRANT_MINOR: int = 100000  # 1000.00 simulated promo credits
    # Disclosed billing policy: a completed job is metered for at least this many
    # seconds, so sub-second (e.g. simulated) jobs still incur a minimum charge
    # rather than rounding to zero. Set to 0 to bill pure actual runtime.
    BILLING_MIN_BILLABLE_SECONDS: int = 60
    BILLING_CURRENCY: str = "USDC_CREDITS"
    BILLING_CURRENCY_EXPONENT: int = 2
    PAYMENT_PROVIDER_CONFIGURATION: str = "simulated"

    # Phase 6 — inference endpoints
    INFERENCE_RATE_LIMIT: int = 60
    INFERENCE_RATE_WINDOW_SECONDS: int = 60
    INFERENCE_MAX_TOKENS_DEFAULT: int = 4096
    INFERENCE_EMBEDDING_DIM: int = 256
    INFERENCE_MAX_INPUT_ITEMS: int = 100  # max batch items for embeddings

    @property
    def artifact_content_types(self) -> set[str]:
        return {c.strip() for c in self.ARTIFACT_CONTENT_TYPES.split(",") if c.strip()}

    @property
    def is_test(self) -> bool:
        return self.ENVIRONMENT.lower() in {"test", "testing"}

    @property
    def expose_dev_tokens(self) -> bool:
        """Whether email/reset tokens are returned in API responses (dev/test only)."""
        return self.ENVIRONMENT.lower() in {"development", "dev", "test", "testing", "local"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
