"""Application configuration via pydantic-settings.

All settings are read from environment variables with sane defaults so the app
boots without any env configured (local/test).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Insecure defaults that are acceptable for local/dev/test but MUST be overridden
# in any non-dev deployment. The validator below refuses to boot otherwise.
_DEV_AUTH_SECRET = "dev-auth-secret-change-me"
_DEV_SESSION_SECRET = "dev-session-secret-change-me"
_DEV_SIGNER_KEY = "0x4f3edf983ac636a65a842ce7c78d9aa706d3b113bce9c46f30d7d21715b23b1d"
_DEV_ENVS = {"development", "dev", "test", "testing", "local"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core infra
    DATABASE_URL: str = "sqlite+aiosqlite:///./computecoin.db"
    REDIS_URL: str = "redis://localhost:6379/0"

    # Secrets (overridden in prod via env; a non-dev ENVIRONMENT with these
    # defaults is rejected at startup — see the validator below).
    AUTH_SECRET: str = _DEV_AUTH_SECRET
    SESSION_SECRET: str = _DEV_SESSION_SECRET

    # Token lifetimes
    ACCESS_TOKEN_TTL_SECONDS: int = 900  # 15 minutes
    REFRESH_TOKEN_TTL_SECONDS: int = 60 * 60 * 24 * 14  # 14 days

    # Token lifetimes for email/password flows
    EMAIL_VERIFICATION_TTL_SECONDS: int = 60 * 60 * 24  # 24 hours
    PASSWORD_RESET_TTL_SECONDS: int = 60 * 60  # 1 hour

    # Environment / logging / monitoring
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    # Optional bearer token protecting GET /metrics. Unset = open (dev);
    # set it in prod and scrape over a private network.
    METRICS_TOKEN: str = ""
    STRUCTURED_LOGGING: bool | None = None
    FRONTEND_URL: str = "http://localhost:3000"
    SENTRY_DSN: str = ""
    SENTRY_TRACES_SAMPLE_RATE: float = 0.1
    SENTRY_PROFILES_SAMPLE_RATE: float = 0.1

    # Email provider configuration ("console" | "resend" | "smtp")
    EMAIL_PROVIDER: str = "console"
    RESEND_API_KEY: str = ""
    EMAIL_FROM: str = "ComputeCoin <noreply@computecoin.ai>"
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_TLS: bool = True

    # Auth cookies & CSRF
    COOKIE_SECURE: bool | None = None
    COOKIE_SAMESITE: str = "lax"

    # JWT
    JWT_ALGORITHM: str = "HS256"
    JWT_ISSUER: str = "computecoin"

    # Rate limiting (requests per window)
    LOGIN_RATE_LIMIT: int = 5
    LOGIN_RATE_WINDOW_SECONDS: int = 60
    LOGIN_IP_RATE_LIMIT: int = 50
    LOGIN_IP_RATE_WINDOW_SECONDS: int = 60
    REGISTER_RATE_LIMIT: int = 50
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
    # Background recovery sweeper interval (seconds); 0 disables the sweeper.
    SWEEP_INTERVAL_SECONDS: int = 30
    JOB_MAX_RUNTIME_DEFAULT: int = 300
    JOB_POLL_INTERVAL_SECONDS: int = 5
    JOB_RESERVATION_TTL_SECONDS: int = 3600  # how long a job reservation holds capacity
    # Transparent scheduler weights (price is "lower is better").
    SCHEDULER_WEIGHT_PRICE: float = 0.5
    SCHEDULER_WEIGHT_RELIABILITY: float = 0.3
    SCHEDULER_WEIGHT_AVAILABILITY: float = 0.2
    # Artifact storage limits + object store configuration.
    ARTIFACT_MAX_BYTES: int = 25 * 1024 * 1024  # 25 MiB
    # Global request-body cap (must exceed ARTIFACT_MAX_BYTES). Requests whose
    # declared Content-Length exceeds this are rejected before buffering.
    MAX_REQUEST_BYTES: int = 32 * 1024 * 1024  # 32 MiB
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
    PAYMENT_PROVIDER: str = "simulated"  # "simulated" | "stripe"
    STRIPE_SECRET_KEY: str = ""
    STRIPE_PUBLISHABLE_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_SUCCESS_URL_DEFAULT: str = "http://localhost:3000/billing?status=success"
    STRIPE_CANCEL_URL_DEFAULT: str = "http://localhost:3000/billing?status=cancelled"

    # Phase 6 — inference endpoints
    INFERENCE_RATE_LIMIT: int = 60
    INFERENCE_RATE_WINDOW_SECONDS: int = 60
    INFERENCE_MAX_TOKENS_DEFAULT: int = 4096
    INFERENCE_EMBEDDING_DIM: int = 256
    INFERENCE_MAX_INPUT_ITEMS: int = 100  # max batch items for embeddings

    # Phase 9 — Telemetry & Time-series at scale
    TIMESCALE_ENABLED: bool = False
    TIMESCALE_URL: str = ""
    TELEMETRY_BATCH_MAX_SIZE: int = 500
    TELEMETRY_RETENTION_DAYS: int = 30
    ALERT_TEMP_THRESHOLD_C: float = 85.0
    ALERT_MEM_UTIL_THRESHOLD_PCT: float = 95.0

    # Phase 10 — On-chain settlement & smart contracts
    SETTLEMENT_ENABLED: bool = True
    SETTLEMENT_CHAIN_ID: int = 31337
    SETTLEMENT_CONTRACT_ADDRESS: str = "0x5FbDB2315678afecb367f032d93F642f64180aa3"
    COMPUTE_TOKEN_ADDRESS: str = "0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512"
    # Dev/test platform signer private key (matches account #3 on standard dev nodes).
    # In a non-dev ENVIRONMENT this default is rejected at startup when settlement
    # is enabled (the private half is public).
    PLATFORM_SIGNER_PRIVATE_KEY: str = _DEV_SIGNER_KEY
    PLATFORM_TREASURY_ADDRESS: str = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"

    @model_validator(mode="after")
    def _reject_insecure_defaults_in_prod(self) -> "Settings":
        """Refuse to boot a non-dev deployment that still uses shipped dev secrets.

        A publicly-known AUTH_SECRET lets anyone forge admin JWTs and artifact
        grants; the dev signer key lets anyone forge settlement vouchers."""
        if self.ENVIRONMENT.lower() in _DEV_ENVS:
            return self
        insecure = []
        if self.AUTH_SECRET == _DEV_AUTH_SECRET:
            insecure.append("AUTH_SECRET")
        if self.SESSION_SECRET == _DEV_SESSION_SECRET:
            insecure.append("SESSION_SECRET")
        if self.SETTLEMENT_ENABLED and self.PLATFORM_SIGNER_PRIVATE_KEY == _DEV_SIGNER_KEY:
            insecure.append("PLATFORM_SIGNER_PRIVATE_KEY")
        if insecure:
            raise ValueError(
                f"Refusing to start in ENVIRONMENT={self.ENVIRONMENT!r} with insecure "
                f"default value(s) for: {', '.join(insecure)}. "
                "Set strong values via environment variables."
            )
        return self

    @property
    def artifact_content_types(self) -> set[str]:
        return {c.strip() for c in self.ARTIFACT_CONTENT_TYPES.split(",") if c.strip()}

    @property
    def is_test(self) -> bool:
        return self.ENVIRONMENT.lower() in {"test", "testing"}

    @property
    def register_rate_limit(self) -> int:
        if self.ENVIRONMENT.lower() in _DEV_ENVS:
            return max(self.REGISTER_RATE_LIMIT, 500)
        return self.REGISTER_RATE_LIMIT

    @property
    def login_ip_rate_limit(self) -> int:
        if self.ENVIRONMENT.lower() in _DEV_ENVS:
            return max(self.LOGIN_IP_RATE_LIMIT, 500)
        return self.LOGIN_IP_RATE_LIMIT

    @property
    def expose_dev_tokens(self) -> bool:
        """Whether email/reset tokens are returned in API responses (dev/test only)."""
        return self.ENVIRONMENT.lower() in {"development", "dev", "test", "testing", "local"}

    @property
    def cookie_secure(self) -> bool:
        if self.COOKIE_SECURE is not None:
            return self.COOKIE_SECURE
        return self.ENVIRONMENT.lower() in {"production", "prod"}

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() in {"production", "prod"}

    @property
    def use_structured_logging(self) -> bool:
        if self.STRUCTURED_LOGGING is not None:
            return self.STRUCTURED_LOGGING
        return self.ENVIRONMENT.lower() in {"production", "prod", "staging"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
