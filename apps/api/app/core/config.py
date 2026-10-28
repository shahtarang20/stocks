"""Application configuration via pydantic-settings.

All settings are read from environment variables with sane defaults so the app
boots without any env configured (local/test).
"""

from __future__ import annotations

import decimal
import json
import re
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Insecure defaults that are acceptable for local/dev/test but MUST be overridden
# in any non-dev deployment. The validator below refuses to boot otherwise.
_DEV_AUTH_SECRET = "dev-auth-secret-change-me"
_DEV_SESSION_SECRET = "dev-session-secret-change-me"
_DEV_SIGNER_KEY = "0x4f3edf983ac636a65a842ce7c78d9aa706d3b113bce9c46f30d7d21715b23b1d"
# Proof-of-Privacy (ADR-0005) development keys. Public knowledge, so a non-dev deployment that
# still uses them is refused at boot (see the validator).
_DEV_PRIVACY_VERIFIER_KEY = "7c1f0e9a3b5d2c4e6f8a0b1c2d3e4f5061728394a5b6c7d8e9f0a1b2c3d4e5f6"
_DEV_PRIVACY_LOG_SIGNING_KEY = "5a0c3e7b9d1f2a4c6e8b0d2f4a6c8e0b1d3f5a7c9e1b3d5f7a9c1e3b5d7f9a1c"
_DEV_PRIVACY_GUARDIAN_PRIVATE_KEYS = (
    '{"computecoin":"28c9a4d0e1f2a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8091a2b4c",'
    '"partner":"409c8d7e6f5a4b3c2d1e0f1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d65"}'
)
_DEV_ENVS = {"development", "dev", "test", "testing", "local"}
# Local-chain contract addresses shipped as defaults. On a real network these point at nothing (or at a
# stranger's contract), so a non-dev deployment with settlement on must set its own.
_DEV_CONTRACT_DEFAULTS = {
    "SETTLEMENT_CONTRACT_ADDRESS": "0x5FbDB2315678afecb367f032d93F642f64180aa3",
    "COMPUTE_TOKEN_ADDRESS": "0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512",
    "PLATFORM_TREASURY_ADDRESS": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
}
_LOCAL_CHAIN_ID = 31337
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")


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
    OBJECT_STORAGE_ACCESS_KEY: str = ""
    OBJECT_STORAGE_SECRET_KEY: str = ""
    OBJECT_STORAGE_REGION: str = "us-east-1"

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
    # Wallet linking / token balance view. The chain the app tells users to connect to,
    # and an optional block-explorer base URL for links (e.g. https://testnet.bscscan.com).
    WALLET_CHAIN_ID: int = 0  # 0 -> use SETTLEMENT_CHAIN_ID
    WALLET_CHAIN_NAME: str = ""
    WALLET_EXPLORER_BASE: str = ""
    WALLET_LINK_MESSAGE_TTL_SECONDS: int = 600

    # Token deposits (stage 1 of ADR-0004): COMPUTE sent to the platform address from a wallet the
    # user has linked is credited to their ledger balance. OFF unless every setting below is valid.
    TOKEN_DEPOSIT_ENABLED: bool = False
    TOKEN_DEPOSIT_ADDRESS: str = ""  # the platform address that receives deposits
    TOKEN_CREDITS_PER_TOKEN: str = ""  # credits per 1 COMPUTE, a decimal string; the owner decides this
    TOKEN_DEPOSIT_CONFIRMATIONS: int = 12  # blocks to wait, so a reorganised block is never credited
    TOKEN_DEPOSIT_MIN_TOKENS: str = "1"
    TOKEN_DEPOSIT_MAX_TOKENS: str = "100000"
    TOKEN_DEPOSIT_FROM_BLOCK: int = 0  # first block to scan: set to the token's deployment block
    # Dev/test platform signer private key (matches account #3 on standard dev nodes).
    # In a non-dev ENVIRONMENT this default is rejected at startup when settlement
    # is enabled (the private half is public).
    PLATFORM_SIGNER_PRIVATE_KEY: str = _DEV_SIGNER_KEY
    PLATFORM_TREASURY_ADDRESS: str = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"

    # Phase B — verification, reputation/slashing, spend controls, webhooks
    # Fraction of eligible jobs replaced by an injected known-answer challenge.
    CHALLENGE_SAMPLE_RATE: float = 0.02
    # Fraction of completed jobs re-run on a second machine for comparison.
    REDUNDANCY_SAMPLE_RATE: float = 0.02
    # When True, provider earnings for a sampled job stay in the holding account
    # until its verification result is known.
    VERIFICATION_HOLD_PAYOUT: bool = False
    # Half-life window for reputation time-decay; old failures stop counting.
    REPUTATION_DECAY_DAYS: int = 90
    # Floor applied after any penalty so a score can never go negative.
    REPUTATION_MIN_SCORE: float = 0.0
    # Upper bound on a single slash, as a fraction of the job's provider payout.
    SLASH_MAX_FRACTION: float = 1.0
    WEBHOOK_TIMEOUT_SECONDS: float = 5.0
    WEBHOOK_MAX_ATTEMPTS: int = 5
    # SSRF guard: customer-supplied webhook URLs resolving to private/link-local
    # ranges are refused unless this is explicitly enabled (local dev only).
    WEBHOOK_ALLOW_PRIVATE_IPS: bool = False
    # Default budget-alert thresholds, as a percent CSV (see spend_alert_thresholds).
    SPEND_ALERT_THRESHOLDS: str = "50,80,100"
    # When True, a missing/broken Redis STRICTLY DEGRADES rate limiting rather
    # than trusting the best-effort in-process counters: outside dev/test the
    # effective budget drops to ceil(limit/10) (floor 1) per window and the
    # limiter reports `degraded`. Deliberately not fail-closed — a Redis outage
    # must not take the whole API down. See app/core/rate_limit.py.
    RATE_LIMIT_REQUIRE_REDIS: bool = False
    WS_JOB_PUSH_ENABLED: bool = True

    # Phase C — batches, datasets, SSO, Stripe Connect payouts
    BATCH_MAX_ITEMS: int = 1000
    # Disclosed bulk discount percent (Decimal string — never float money).
    BATCH_DISCOUNT_PCT: str = "10"
    DATASET_MAX_BYTES: int = 5 * 1024 * 1024 * 1024  # 5 GiB
    SSO_ENABLED: bool = False
    STRIPE_CONNECT_ENABLED: bool = False
    STRIPE_CONNECT_CLIENT_ID: str = ""

    # Phase D — chain reconciliation
    CHAIN_RPC_URL: str = ""

    # --- Proof-of-Privacy (ADR-0005) ---
    # Off by default outside development: privacy tiers are then refused, never silently served.
    # When on in a non-dev environment, the keys below must be real (enforced at boot).
    PRIVACY_ENABLED: bool = False
    # Ed25519 seed (hex) that signs attestation verdicts. Its public key is published at
    # GET /v1/privacy/verifier-key so receipts can be checked offline.
    PRIVACY_VERIFIER_SIGNING_KEY: str = _DEV_PRIVACY_VERIFIER_KEY
    # JSON {guardian_name: x25519_private_hex} for guardians whose key lives in THIS process.
    # In a non-dev deployment fewer than PRIVACY_GUARDIAN_THRESHOLD may be held here, so the
    # platform alone can never rebuild a customer's Data Key.
    PRIVACY_GUARDIAN_PRIVATE_KEYS: str = _DEV_PRIVACY_GUARDIAN_PRIVATE_KEYS
    # JSON {guardian_name: x25519_public_hex} for EVERY guardian, including external ones.
    # Empty = derived from PRIVACY_GUARDIAN_PRIVATE_KEYS.
    PRIVACY_GUARDIAN_PUBLIC_KEYS: str = ""
    PRIVACY_GUARDIAN_THRESHOLD: int = 2
    # JSON {guardian_name: url} for external guardian services (WP4).
    PRIVACY_EXTERNAL_GUARDIANS: str = "{}"
    # Ed25519 seed (hex) that signs transparency-log checkpoints. External guardians pin its
    # public key, so they verify the log themselves instead of trusting a root we hand them.
    PRIVACY_LOG_SIGNING_KEY: str = _DEV_PRIVACY_LOG_SIGNING_KEY
    # The Sealed tiers need a real TPM quote checked against the TPM vendor's certificate chain,
    # and Confidential needs TDX/SEV-SNP plus NVIDIA reports. Neither verifier exists yet, so
    # verdicts can only be issued on the software attestation path, which is for development
    PRIVACY_SOFTWARE_ATTESTATION: bool = False
    # Genuine hardware attestation verifiers (TPM 2.0 / TDX / SEV-SNP / NVIDIA CC - WP-F).
    # Cannot be enabled without trusted vendor root certificates configured.
    PRIVACY_HARDWARE_ATTESTATION: bool = False
    PRIVACY_HARDWARE_ROOT_CERTS: str = ""

    # Transparency log on-chain anchoring (WP-C / WP9)
    PRIVACY_ANCHOR_ENABLED: bool = False
    PRIVACY_ANCHOR_RPC_URL: str = ""
    PRIVACY_ANCHOR_CHAIN_ID: int = _LOCAL_CHAIN_ID
    PRIVACY_ANCHOR_VAULT_ADDRESS: str = ""
    PRIVACY_ANCHOR_PRIVATE_KEY: str = ""
    PRIVACY_ANCHOR_INTERVAL_SECONDS: int = 600

    # Transparency log witnesses (WP-E / WP21)
    # JSON {name: {"url": str, "public_key": str}} for independent log witnesses.
    PRIVACY_WITNESSES: str = "{}"
    PRIVACY_WITNESS_THRESHOLD: int = 0
    # How often the worker has the latest checkpoint cosigned (receipts carry the result).
    PRIVACY_WITNESS_INTERVAL_SECONDS: int = 300

    CHAIN_RECONCILE_ENABLED: bool = False
    CHAIN_RECONCILE_INTERVAL_SECONDS: int = 900
    #: Max block span per ``eth_getLogs`` call. Hosted RPC providers cap this
    #: (commonly 10k, some 2k) and a request over the cap is either rejected or,
    #: worse, silently truncated — which would read as "no escrows, all clear".
    #: The scan is chunked to this width so the full range is always covered.
    CHAIN_LOG_CHUNK_BLOCKS: int = 10_000
    #: First block to scan for settlement events. Leave at 0 for a local chain;
    #: set to the contract's deployment block in production so the scan does not
    #: walk the entire history of the chain on every sweep.
    CHAIN_LOG_FROM_BLOCK: int = 0

    @model_validator(mode="after")
    def _reject_insecure_defaults_in_prod(self) -> "Settings":
        """Refuse to boot a non-dev deployment that still uses shipped dev secrets.

        A publicly-known AUTH_SECRET lets anyone forge admin JWTs and artifact
        grants; the dev signer key lets anyone forge settlement vouchers."""
        if self.PRIVACY_HARDWARE_ATTESTATION:
            from app.services.hardware_attestation import parse_root_certs

            try:
                # JSON {flavor: [PEM, ...]}; every entry must be a valid, self-signed CA certificate.
                parse_root_certs(self.PRIVACY_HARDWARE_ROOT_CERTS)
            except ValueError as exc:
                raise ValueError(
                    f"PRIVACY_HARDWARE_ATTESTATION cannot be enabled without valid PRIVACY_HARDWARE_ROOT_CERTS: {exc}"
                ) from exc
        if self.ENVIRONMENT.lower() in _DEV_ENVS:
            return self

        insecure = []
        if self.AUTH_SECRET == _DEV_AUTH_SECRET:
            insecure.append("AUTH_SECRET")
        if self.SESSION_SECRET == _DEV_SESSION_SECRET:
            insecure.append("SESSION_SECRET")
        if self.PRIVACY_ENABLED:
            if self.PRIVACY_VERIFIER_SIGNING_KEY == _DEV_PRIVACY_VERIFIER_KEY:
                insecure.append("PRIVACY_VERIFIER_SIGNING_KEY")
            if self.PRIVACY_LOG_SIGNING_KEY == _DEV_PRIVACY_LOG_SIGNING_KEY:
                insecure.append("PRIVACY_LOG_SIGNING_KEY")
            if self.PRIVACY_GUARDIAN_PRIVATE_KEYS == _DEV_PRIVACY_GUARDIAN_PRIVATE_KEYS:
                insecure.append("PRIVACY_GUARDIAN_PRIVATE_KEYS")
            if self.PRIVACY_SOFTWARE_ATTESTATION:
                insecure.append("PRIVACY_SOFTWARE_ATTESTATION")
            try:
                local_guardians = len(json.loads(self.PRIVACY_GUARDIAN_PRIVATE_KEYS or "{}"))
            except ValueError:
                local_guardians = -1
            if local_guardians < 0 or local_guardians >= self.PRIVACY_GUARDIAN_THRESHOLD:
                insecure.append("PRIVACY_GUARDIAN_PRIVATE_KEYS (must hold fewer keys than the threshold)")
        if self.PRIVACY_ANCHOR_ENABLED:
            # The anchor client speaks JSON-RPC over HTTP(S) only.
            if not self.PRIVACY_ANCHOR_RPC_URL.startswith(("http://", "https://")):
                insecure.append("PRIVACY_ANCHOR_RPC_URL")
            if not self.PRIVACY_ANCHOR_VAULT_ADDRESS or not _ADDRESS_RE.fullmatch(self.PRIVACY_ANCHOR_VAULT_ADDRESS):
                insecure.append("PRIVACY_ANCHOR_VAULT_ADDRESS")
            clean_anchor_key = self.PRIVACY_ANCHOR_PRIVATE_KEY.removeprefix("0x").lower()
            if (
                not re.fullmatch(r"[0-9a-f]{64}", clean_anchor_key)
                or clean_anchor_key == _DEV_SIGNER_KEY.removeprefix("0x").lower()
                # Keys stay where they belong (R8): the anchor key only commits roots; it must not
                # also be the key that signs settlement vouchers.
                or clean_anchor_key == self.PLATFORM_SIGNER_PRIVATE_KEY.removeprefix("0x").lower()
            ):
                insecure.append("PRIVACY_ANCHOR_PRIVATE_KEY")
            if self.PRIVACY_ANCHOR_CHAIN_ID <= 0 or self.PRIVACY_ANCHOR_CHAIN_ID == _LOCAL_CHAIN_ID:
                insecure.append("PRIVACY_ANCHOR_CHAIN_ID")
        if self.PRIVACY_ENABLED and self.PRIVACY_WITNESS_THRESHOLD > 0:
            try:
                w_dict = json.loads(self.PRIVACY_WITNESSES or "{}")
                distinct_keys = {
                    str(w.get("public_key", "")).removeprefix("0x").lower() for w in w_dict.values() if isinstance(w, dict)
                } if isinstance(w_dict, dict) else set()
                # The threshold counts independent witnesses: one key under two names is one witness.
                if not isinstance(w_dict, dict) or len(distinct_keys) < self.PRIVACY_WITNESS_THRESHOLD:
                    insecure.append("PRIVACY_WITNESSES")
                else:
                    for w_name, w_info in w_dict.items():
                        if not isinstance(w_info, dict):
                            insecure.append(f"PRIVACY_WITNESSES({w_name})")
                            break
                        url = str(w_info.get("url", ""))
                        pk = str(w_info.get("public_key", "")).removeprefix("0x")
                        if not (url.startswith("http://") or url.startswith("https://")):
                            insecure.append(f"PRIVACY_WITNESSES({w_name}.url)")
                            break
                        if len(pk) != 64 or not re.fullmatch(r"[0-9a-fA-F]{64}", pk):
                            insecure.append(f"PRIVACY_WITNESSES({w_name}.public_key)")
                            break
            except Exception:
                insecure.append("PRIVACY_WITNESSES")
        if self.SETTLEMENT_ENABLED and self.PLATFORM_SIGNER_PRIVATE_KEY == _DEV_SIGNER_KEY:
            insecure.append("PLATFORM_SIGNER_PRIVATE_KEY")
        if self.SETTLEMENT_ENABLED:
            for name, dev_value in _DEV_CONTRACT_DEFAULTS.items():
                value = getattr(self, name)
                if value.lower() == dev_value.lower() or not _ADDRESS_RE.fullmatch(value):
                    insecure.append(name)
            if self.SETTLEMENT_CHAIN_ID == _LOCAL_CHAIN_ID:
                insecure.append("SETTLEMENT_CHAIN_ID")
        if insecure:
            raise ValueError(
                f"Refusing to start in ENVIRONMENT={self.ENVIRONMENT!r} with insecure "
                f"default value(s) for: {', '.join(insecure)}. "
                "Set strong values via environment variables."
            )
        return self

    @property
    def token_deposits_enabled(self) -> bool:
        import decimal

        if not self.TOKEN_DEPOSIT_ENABLED or not self.CHAIN_RPC_URL.strip():
            return False
        try:
            rate = decimal.Decimal(self.TOKEN_CREDITS_PER_TOKEN)
            lo, hi = decimal.Decimal(self.TOKEN_DEPOSIT_MIN_TOKENS), decimal.Decimal(self.TOKEN_DEPOSIT_MAX_TOKENS)
        except (decimal.InvalidOperation, ValueError):
            return False
        a = self.TOKEN_DEPOSIT_ADDRESS.strip()
        valid_address = len(a) == 42 and a.startswith("0x")
        return rate > 0 and 0 < lo <= hi and valid_address and self.TOKEN_DEPOSIT_CONFIRMATIONS >= 0

    @property
    def privacy_active(self) -> bool:
        """Proof-of-Privacy tiers are served only when enabled, or in a dev/test environment."""
        return self.PRIVACY_ENABLED or self.ENVIRONMENT.lower() in _DEV_ENVS

    @property
    def wallet_chain_id(self) -> int:
        return self.WALLET_CHAIN_ID or self.SETTLEMENT_CHAIN_ID

    @property
    def artifact_content_types(self) -> set[str]:
        return {c.strip() for c in self.ARTIFACT_CONTENT_TYPES.split(",") if c.strip()}

    @property
    def spend_alert_thresholds(self) -> list[int]:
        """Parsed, de-duplicated, ascending alert thresholds (percent)."""
        values: set[int] = set()
        for raw in self.SPEND_ALERT_THRESHOLDS.split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                pct = int(raw)
            except ValueError:
                continue
            if 0 < pct <= 1000:
                values.add(pct)
        return sorted(values)

    @property
    def batch_discount_pct(self) -> decimal.Decimal:
        """Bulk discount as a Decimal percent. Money math is never float."""
        try:
            pct = decimal.Decimal(self.BATCH_DISCOUNT_PCT)
        except decimal.InvalidOperation:
            return decimal.Decimal("0")
        return min(max(pct, decimal.Decimal("0")), decimal.Decimal("100"))

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
