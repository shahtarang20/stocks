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
    # Optional regex of EXTRA browser origins allowed with credentials (e.g. your own Vercel preview domain such as
    # r"^https://computecoin-[a-z0-9-]+-myteam\.vercel\.app$"). Empty by default: a blanket "*.vercel.app" pattern
    # would let any page anyone deploys on Vercel make credentialed requests as a logged-in user.
    CORS_ALLOW_ORIGIN_REGEX: str = ""
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
    # Real (hardware) inference: how long a request waits for its job, and how often it polls.
    INFERENCE_TIMEOUT_SECONDS: int = 120
    INFERENCE_POLL_INTERVAL_SECONDS: float = 0.5
    # Streaming (B2): characters per simulated chunk, and the cap on relayed text per stream.
    INFERENCE_STREAM_CHUNK_CHARS: int = 24
    INFERENCE_STREAM_MAX_CHARS: int = 262_144
    # B2: where ordered token chunks are relayed from agent to SSE stream: memory (one process) | redis (shared).
    STREAM_RELAY_BACKEND: str = "memory"
    # U10: interactive-session relay: memory (one API worker) | redis (several workers, uses REDIS_URL).
    SESSIONS_RELAY_BACKEND: str = "memory"
    # A6/D5: let AMD (ROCm) machines serve sealed / sealed_pro when their Sealed verdict is current. OWNER decision.
    AMD_SEALED_ENABLED: bool = False

    # Phase 9 — Telemetry & Time-series at scale
    TIMESCALE_ENABLED: bool = False
    TIMESCALE_URL: str = ""
    TELEMETRY_BATCH_MAX_SIZE: int = 500
    TELEMETRY_RETENTION_DAYS: int = 30
    ALERT_TEMP_THRESHOLD_C: float = 85.0
    ALERT_MEM_UTIL_THRESHOLD_PCT: float = 95.0

    # U19 observability. Empty token = /v1/ops/metrics disabled (404).
    OBSERVABILITY_METRICS_TOKEN: str = ""
    JOB_PROGRESS_MAX_POINTS: int = 100
    JOB_PROGRESS_MIN_INTERVAL_SECONDS: float = 2.0
    JOB_LIVE_STREAM_INTERVAL_SECONDS: float = 3.0
    JOB_LIVE_STREAM_MAX_SECONDS: int = 300
    OBS_CLAIM_LATENCY_WINDOW_SECONDS: int = 3600

    # U16 acceptable use. LEGAL SIGN-OFF REQUIRED before enabling either flag.
    AUP_REQUIRED: bool = False
    AUP_CURRENT_VERSION: str = "draft-0"
    ABUSE_ENFORCE: bool = False  # False = observe-only (flags only, nothing stopped)
    ABUSE_UTIL_THRESHOLD_PCT: float = 98.0
    ABUSE_UTIL_WINDOW_SECONDS: int = 900
    ABUSE_MIN_SAMPLES: int = 5
    ABUSE_REPORT_MAX_DESCRIPTION: int = 4000
    # U16 BYOC runtime signals (observe-only unless ABUSE_ENFORCE). OWNER DECISION on the numbers.
    ABUSE_BYOC_IDLE_IO_WINDOW_SECONDS: int = 600  # full GPU util + zero I/O + no progress for this long
    ABUSE_BYOC_IO_DELTA_MAX_BYTES: int = 1024  # I/O growth at or below this over the window counts as zero
    ABUSE_BYOC_POWER_WINDOW_SECONDS: int = 600
    ABUSE_BYOC_POWER_MIN_SAMPLES: int = 8
    ABUSE_BYOC_POWER_TOLERANCE_W: float = 1.0  # max-min power spread treated as identical
    ABUSE_BYOC_POWER_MIN_W: float = 60.0  # ignore an idle card

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

    # U1 Kit registry: raw Ed25519 PUBLIC key, 64 hex chars. Unset = no Kit can be published or used (fail closed).
    # The private key is never a setting; custody is an OWNER DECISION (docs/architecture/U1-job-spec-v2-design.md).
    KIT_SIGNING_PUBLIC_KEY: str = ""

    # W6 agent updates: raw Ed25519 PUBLIC key (64 hex chars) that signs update manifests. Unset = no release can be published
    # (fail closed). The private key is never a setting (L7); custody is an OWNER DECISION.
    AGENT_RELEASE_SIGNING_PUBLIC_KEY: str = ""

    # U15 pricing units and quotes. OFF by default; MONEY-ADJACENT, needs owner approval and independent
    # review before enabling (docs/architecture/U15-pricing-units-design.md). Every rate is an OWNER DECISION.
    PRICING_UNITS_ENABLED: bool = False
    UNIT_QUOTE_TTL_SECONDS: int = 900  # OWNER DECISION
    UNIT_QUOTE_REFERENCE_PRICE_PER_HOUR: str = "1.00"  # OWNER DECISION: used when no machine is given
    UNIT_QUOTE_EGRESS_PER_GB: str = "0"  # OWNER DECISION: 0 = not charged
    UNIT_QUOTE_STORAGE_PER_GB_MONTH: str = "0"  # OWNER DECISION: 0 = not charged
    UNIT_QUOTE_MIN_SAMPLES: int = 5  # OWNER DECISION
    UNIT_QUOTE_RATIO_MIN: str = "0.5"  # OWNER DECISION
    UNIT_QUOTE_RATIO_MAX: str = "2.0"  # OWNER DECISION

    # Token withdrawals (B1, ADR-0004 stage 2). OFF by default. EVERY numeric default below is a
    # conservative PLACEHOLDER that NEEDS AN OWNER DECISION (docs/architecture/B1-withdrawals-design.md).
    # The rate is TOKEN_CREDITS_PER_TOKEN above (same as deposits). The hot-wallet key is NEVER a
    # setting: the signer reads it from a KMS/HSM (see app/services/withdrawal_signer.py).
    WITHDRAWALS_ENABLED: bool = False
    WITHDRAWALS_MIN_CREDITS: str = "10.00"  # OWNER DECISION: minimum per request
    WITHDRAWALS_MAX_PER_TX_CREDITS: str = "100.00"  # OWNER DECISION: per-transaction cap
    WITHDRAWALS_DAILY_USER_CREDITS: str = "250.00"  # OWNER DECISION: per user per rolling 24h
    WITHDRAWALS_DAILY_GLOBAL_CREDITS: str = "2000.00"  # OWNER DECISION: all users per rolling 24h
    WITHDRAWALS_CONFIRMATIONS: int = 12  # OWNER DECISION: blocks to wait before "paid out"
    WITHDRAWALS_TIMEOUT_SECONDS: int = 3600  # OWNER DECISION: no receipt after this long -> resolve
    WITHDRAWALS_HOT_WALLET_ADDRESS: str = ""  # public address only; the key lives in the KMS/HSM
    WITHDRAWALS_KMS_KEY_ID: str = ""  # KMS/HSM key reference (an id, never key material)
    WITHDRAWALS_KMS_REGION: str = ""  # AWS region of the KMS key (empty: boto3 default chain)
    WITHDRAWALS_GAS_LIMIT: int = 100_000
    WITHDRAWALS_MAX_GAS_PRICE_WEI: int = 10_000_000_000  # OWNER DECISION (gas): refuse above this
    WITHDRAWALS_RECON_FROM_BLOCK: int = 0  # first block the payout reconciliation reads
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
    # U6: share of completed Kit jobs (whose manifest has a `verification` section) that get a reference re-run on
    # another provider. 0 disables. The manifest's sample_fraction can only lower it. Owner decision to raise.
    KIT_VERIFICATION_SAMPLE_RATE: float = 0.02
    KIT_VERIFICATION_SWEEP_INTERVAL_SECONDS: int = 60
    KIT_VERIFICATION_MAX_FILE_BYTES: int = 512 * 1024 * 1024
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

    # --- Data plane (U2, ADR-0006 section 9). OFF by default. ---
    # OWNER DECISIONS (not made here): the storage provider, the retention policy and the size
    # limits. Every value below is a placeholder setting, never a product decision.
    DATA_PLANE_ENABLED: bool = False
    # Any S3-compatible store (AWS S3, Cloudflare R2, MinIO, Backblaze ...). Empty = not configured = refused.
    DATA_PLANE_ENDPOINT: str = ""
    DATA_PLANE_BUCKET: str = ""
    DATA_PLANE_ACCESS_KEY: str = ""
    DATA_PLANE_SECRET_KEY_ID: str = ""  # secret access key of the platform bucket
    DATA_PLANE_REGION: str = "us-east-1"
    DATA_PLANE_ADDRESSING_STYLE: str = "path"  # path | virtual
    DATA_PLANE_MAX_FILE_BYTES: int = 1024**4  # 1 TiB, placeholder
    DATA_PLANE_ACCOUNT_QUOTA_BYTES: int = 100 * 1024**3  # 100 GiB per account, placeholder
    DATA_PLANE_PART_SIZE_BYTES: int = 64 * 1024 * 1024  # raised automatically to stay under 10 000 parts
    DATA_PLANE_UPLOAD_TTL_SECONDS: int = 7 * 86400  # an unfinished upload is aborted after this
    DATA_PLANE_PART_URL_TTL_SECONDS: int = 900
    DATA_PLANE_DOWNLOAD_TTL_SECONDS: int = 300  # default for a download link
    DATA_PLANE_DOWNLOAD_MAX_TTL_SECONDS: int = 3600
    DATA_PLANE_RETENTION_DEFAULT_DAYS: int = 0  # 0 = keep until the owner deletes
    DATA_PLANE_RETENTION_MAX_DAYS: int = 0  # 0 = no maximum
    DATA_PLANE_GC_GRACE_SECONDS: int = 86400  # an unreferenced object is deleted after this
    DATA_PLANE_CROSS_ACCOUNT_DEDUP: bool = False  # off: dedupe only inside one account (no hash oracle)
    DATA_PLANE_INLINE_VERIFY_MAX_BYTES: int = 256 * 1024 * 1024  # larger files are verified by the worker
    DATA_PLANE_SWEEP_INTERVAL_SECONDS: int = 300
    DATA_PLANE_SWEEP_BATCH: int = 200
    # --- Fan-out (U4, docs/architecture/U4-fan-out-design.md) ---
    FANOUT_MAX_PARTS: int = 1000
    FANOUT_DEFAULT_MAX_PARALLEL: int = 20
    FANOUT_CHILD_MAX_RETRIES: int = 3
    FANOUT_MAX_PART_ATTEMPTS: int = 3
    FANOUT_STRAGGLER_MULTIPLE: float = 3.0  # 0 disables straggler copies
    FANOUT_STRAGGLER_MIN_DONE: int = 3
    FANOUT_MERGE_MAX_ATTEMPTS: int = 10
    FANOUT_SWEEP_INTERVAL_SECONDS: int = 15

    # --- Multi-GPU and datacenter multi-node (U13, docs/architecture/U13-multi-gpu-design.md) ---
    # A multi-GPU (or multi-node) job with no fitting host stays QUEUED this long before it fails no_provider.
    MULTI_GPU_MAX_WAIT_SECONDS: int = 1800
    # Multi-node is OFF until the owner decides which hosts qualify (OWNER DECISION). Off = multi-node jobs are refused.
    MULTI_NODE_ENABLED: bool = False
    MULTI_NODE_MAX_NODES: int = 4
    MULTI_NODE_MAX_LATENCY_MS: float = 2.0  # round trip, pairwise, both directions
    MULTI_NODE_MIN_BANDWIDTH_MBPS: float = 10_000.0  # 10 Gbit/s, pairwise, both directions
    MULTI_NODE_BENCHMARK_MAX_AGE_SECONDS: int = 86_400  # older measurements are ignored
    MULTI_NODE_BENCHMARK_NONCE_TTL_SECONDS: int = 300
    MULTI_NODE_MASTER_PORT: int = 29500
    # Platform per-job cap for the WHOLE multi-node run when the customer gives no max_cost; split across ranks.
    MULTI_NODE_DEFAULT_MAX_COST: str = "10.00"
    MULTI_NODE_MIN_RANK_COST: str = "0.01"
    # --- Privacy across modes (U18, docs/architecture/U18-privacy-modes-design.md). All default off (L1). ---
    PRIVACY_FANOUT_ENABLED: bool = False  # private (sealed / sealed_pro / confidential) fan-out; needs PRIVACY_ENABLED too
    PRIVACY_STREAMING_ENABLED: bool = False  # end-to-end sealed chat streaming; needs PRIVACY_ENABLED too
    PRIVACY_STREAM_MAX_CHARS: int = 1_048_576  # cap on the sealed envelope text one private stream may relay
    # Fixed block size (bytes) the agent pads every sealed chunk's plaintext to (u32 BE length + text + zeros) so chunk sizes
    # do not leak token lengths. The relay then requires ciphertext length (minus the 16 byte tag) to be a multiple of this.
    # 0 = no padding, raw plaintext. Must equal the agent's PRIVACY_STREAM_PAD_BYTES; the customer's reader strips the frame.
    PRIVACY_STREAM_PAD_BYTES: int = 256
    # --- Windows VM tier (U14, docs/architecture/U14-windows-vm-design.md). Owner go/no-go; default off. ---
    WINDOWS_VM_ENABLED: bool = False

    # --- CPU-only Kits (resource_defaults.gpu_count 0; docs/operations/remaining-code-work-master-plan.md item 5). ---
    # Billing is per GPU-second, so a CPU-only Kit has no honest GPU price. Default OFF: a gpu_count 0 Kit job is REFUSED
    # (cpu_only_kits_disabled) instead of silently billing one GPU slot. When ON the job is placed only on machines that
    # advertise ``cpu_capable`` in their heartbeat health, runs with no --gpus, takes one scheduler slot, and its quote
    # prices that slot at the machine's asking rate x CPU_ONLY_RATE_FRACTION (quote path only; billing functions are
    # unchanged and settle from the quote). OWNER DECISION: the fraction and whether CPU hosts are paid at all.
    CPU_ONLY_KITS_ENABLED: bool = False
    CPU_ONLY_RATE_FRACTION: float = 0.1

    # --- BYOC (U8, docs/architecture/U8-byoc-design.md). Fail closed: empty allowlist or no scanner = BYOC refused. ---
    BYOC_REGISTRY_ALLOWLIST: str = ""  # comma separated "host" or "host/org" prefixes; OWNER DECISION
    BYOC_SCANNER: str = ""  # "" = unconfigured (BYOC refused), "trivy", "clamav", "trivy+clamav" (both must be clean)
    BYOC_CLAMAV_BIN: str = "clamscan"  # or "clamdscan" (daemon; needs the exported files readable by clamd)
    BYOC_DOCKER_BIN: str = "docker"
    BYOC_CLAMAV_MAX_IMAGE_BYTES: int = 4 * 1024 ** 3  # refuse (error) rather than scan a bigger export
    BYOC_CLAMAV_TIMEOUT_SECONDS: int = 900
    BYOC_CLAMAV_TMP_DIR: str = ""  # "" = system temp dir
    BYOC_TRIVY_BIN: str = "trivy"
    BYOC_SCAN_TIMEOUT_SECONDS: int = 300
    BYOC_SCAN_MAX_CRITICAL: int = 0
    BYOC_SCAN_TTL_SECONDS: int = 86400
    BYOC_ENV_ALLOWLIST: str = ""  # extra env names (comma separated) allowed beside the built-in list
    BYOC_ALLOW_EGRESS: bool = False  # True lets a spec declare exact host:port egress (B5 proxy)
    BYOC_MAX_GPUS: int = 1
    BYOC_MAX_CPUS: int = 8
    BYOC_MAX_MEMORY_MB: int = 32768
    BYOC_MAX_RUNTIME_SECONDS: int = 21600
    # --- Services / endpoints (U11, docs/architecture/U11-services-design.md). OWNER DECISIONS: conservative defaults. ---
    SERVICES_ENABLED: bool = False
    SERVICE_MAX_REPLICAS_LIMIT: int = 4
    SERVICE_MAX_PER_CUSTOMER: int = 3
    SERVICE_DEFAULT_SPEND_CAP: str = "50.00"
    SERVICE_MAX_SPEND_CAP: str = "500.00"
    SERVICE_REPLICA_WINDOW_SECONDS: int = 3600
    SERVICE_REPLICA_MAX_COST: str = "10.00"
    SERVICE_HEARTBEAT_TIMEOUT_SECONDS: int = 30
    SERVICE_SWEEP_INTERVAL_SECONDS: int = 10
    SERVICE_REQUEST_TIMEOUT_SECONDS: int = 60
    SERVICE_MAX_REQUEST_BYTES: int = 1_048_576
    SERVICE_MAX_RESPONSE_BYTES: int = 4_194_304
    SERVICE_POLL_MAX_WAIT_SECONDS: int = 20
    SERVICE_SCALE_UP_QUEUE_PER_REPLICA: int = 4
    SERVICE_SCALE_UP_LATENCY_SECONDS: int = 10
    SERVICE_SCALE_DOWN_IDLE_SECONDS: int = 300
    SERVICE_IDLE_SCALE_TO_ZERO_SECONDS: int = 900
    SERVICE_SCALE_COOLDOWN_SECONDS: int = 60
    SERVICE_METER_TOLERANCE: float = 0.10
    SERVICE_REQUEST_RETENTION_SECONDS: int = 86400
    # Billing mode: "uptime" (default: replica job billed for its metered uptime) or "per_request" (replica job billed for
    # the GPU-seconds attributable to served requests, capped at uptime). OWNER DECISION; unknown values fall back to uptime.
    SERVICE_BILLING_MODE: str = "uptime"
    # OWNER DECISION: multiplier on the existing per-GPU-second rate applied to request seconds (1 = the same rate).
    SERVICE_PER_REQUEST_RATE_MULTIPLIER: str = "1"
    SERVICE_REPLICA_START_TIMEOUT_SECONDS: int = 600  # replica 'starting' (never healthy) longer than this is replaced
    SERVICE_MAX_CONCURRENCY_LIMIT: int = 64  # cap of the per-service max_concurrency setting
    # per_request mode: how many times the result-path metering is retried by the worker sweep before the capped fallback
    # (billing the MINIMUM of uptime and the last known request seconds; never more than either).
    SERVICE_METERING_MAX_RETRIES: int = 5
    # --- Pipelines (U9, docs/architecture/U9-pipelines-design.md) ---
    PIPELINE_MAX_STEPS: int = 20  # hard ceiling 50 in code
    PIPELINE_MAX_PARALLEL_STEPS: int = 8
    PIPELINE_MAX_STEP_ATTEMPTS: int = 5
    PIPELINE_MAX_RESTARTS: int = 10
    PIPELINE_MAX_ACTIVE_PER_USER: int = 20
    PIPELINE_SWEEP_INTERVAL_SECONDS: int = 15

    DATA_PLANE_BYO_ENABLED: bool = False
    DATA_PLANE_BYO_MAX_TTL_SECONDS: int = 3600
    # Bring your own bucket. Customer credentials are encrypted at rest with a key derived from this (>= 32 chars, else BYO is refused).
    DATA_PLANE_CREDENTIAL_KEY: str = ""

    @property
    def data_plane_configured(self) -> bool:
        return bool(
            self.DATA_PLANE_ENABLED
            and self.DATA_PLANE_ENDPOINT
            and self.DATA_PLANE_BUCKET
            and self.DATA_PLANE_ACCESS_KEY
            and self.DATA_PLANE_SECRET_KEY_ID
        )

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

    # Interactive sessions (U10). Read via getattr in services/sessions.py; declared here so they can be set.
    SESSIONS_ALLOW_BYOC_IMAGE: bool = False  # custom images in sessions stay refused until the U8 scan covers them
    SESSIONS_MAX_LIVE_PER_USER: int = 3
    SESSIONS_REAP_INTERVAL_SECONDS: int = 30

    # Stake sync: ComputeStakeVault (on-chain) -> provider_machines.stake_bonded (A1 / P1).
    # When enabled the chain is the only source of stake (R4): the admin "record stake" route refuses.
    STAKE_SYNC_ENABLED: bool = False
    STAKE_SYNC_RPC_URL: str = ""
    STAKE_SYNC_CHAIN_ID: int = 56
    STAKE_SYNC_VAULT_ADDRESS: str = ""
    STAKE_SYNC_INTERVAL_SECONDS: int = 300
    # Read the vault this many blocks behind the head so a reorganisation can never grant stake.
    STAKE_SYNC_CONFIRMATIONS: int = 15

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
        if self.STAKE_SYNC_ENABLED:
            if not self.STAKE_SYNC_RPC_URL.startswith(("http://", "https://")):
                insecure.append("STAKE_SYNC_RPC_URL")
            if not self.STAKE_SYNC_VAULT_ADDRESS or not _ADDRESS_RE.fullmatch(self.STAKE_SYNC_VAULT_ADDRESS):
                insecure.append("STAKE_SYNC_VAULT_ADDRESS")
            if self.STAKE_SYNC_CHAIN_ID <= 0 or self.STAKE_SYNC_CHAIN_ID == _LOCAL_CHAIN_ID:
                insecure.append("STAKE_SYNC_CHAIN_ID")
            if self.STAKE_SYNC_CONFIRMATIONS < 0:
                insecure.append("STAKE_SYNC_CONFIRMATIONS")
            if self.STAKE_SYNC_INTERVAL_SECONDS < 5:
                insecure.append("STAKE_SYNC_INTERVAL_SECONDS")
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
    def withdrawals_configured(self) -> bool:
        """True only when withdrawals are switched on AND every setting they need is valid.
        The admin pause is checked separately (it lives in the database)."""
        import decimal

        if not self.WITHDRAWALS_ENABLED or not self.CHAIN_RPC_URL.strip() or not self.WITHDRAWALS_KMS_KEY_ID.strip():
            return False
        try:
            rate = decimal.Decimal(self.TOKEN_CREDITS_PER_TOKEN)
            lo = decimal.Decimal(self.WITHDRAWALS_MIN_CREDITS)
            tx = decimal.Decimal(self.WITHDRAWALS_MAX_PER_TX_CREDITS)
            du = decimal.Decimal(self.WITHDRAWALS_DAILY_USER_CREDITS)
            dg = decimal.Decimal(self.WITHDRAWALS_DAILY_GLOBAL_CREDITS)
        except (decimal.InvalidOperation, ValueError):
            return False
        a = self.WITHDRAWALS_HOT_WALLET_ADDRESS.strip()
        valid_address = len(a) == 42 and a.startswith("0x")
        return (
            rate > 0
            and 0 < lo <= tx <= du <= dg
            and valid_address
            and self.WITHDRAWALS_CONFIRMATIONS >= 1
            and self.WITHDRAWALS_TIMEOUT_SECONDS > 0
        )

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
    def frontend_base_url(self) -> str:
        """Primary web origin for links in emails. FRONTEND_URL may be a comma-separated
        CORS allow-list; only its first entry is a valid link base."""
        first = next((o.strip() for o in self.FRONTEND_URL.split(",") if o.strip()), "")
        return first.rstrip("/")

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
