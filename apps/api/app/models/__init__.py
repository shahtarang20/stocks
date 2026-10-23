"""Model package - import all models so metadata is fully populated."""

from app.models.billing import (
    BillingAccount,
    Dispute,
    Invoice,
    LedgerEntry,
    LedgerTransaction,
    Payment,
    ProviderPayout,
    Refund,
    UsageRecord,
)
from app.models.enums import Role, UserStatus
from app.models.jobs import (
    Artifact,
    Job,
    JobAssignment,
    JobAttempt,
    JobEvent,
    JobLogMetadata,
)
from app.models.marketplace import (
    PricingRule,
    Quote,
    ResourceReservation,
)
from app.models.model_registry import Model, ModelVersion
from app.models.org import (
    ApiKey,
    Organization,
    OrganizationMember,
    Project,
)
from app.models.provider import (
    EnrollmentToken,
    HardwareInventory,
    Provider,
    ProviderCapability,
    ProviderMachine,
)
from app.models.system import AuditLog, SystemConfiguration
from app.models.user import (
    EmailVerificationToken,
    PasswordResetToken,
    User,
    UserRole,
    UserSession,
)

from app.models.telemetry import (
    JobTelemetry,
    MachineTelemetry,
    TelemetryAlert,
)
from app.models.settlement import (
    EscrowStatus,
    OnChainEscrow,
    OnChainTransaction,
    OnChainTxStatus,
    OnChainTxType,
)

# Phase B — verification, reputation, spend controls and webhooks.
from app.models.verification import (
    RedundancyCheck,
    VerificationChallenge,
    VerificationResult,
)
from app.models.reputation import (
    DisputeEvidence,
    ProviderReputation,
    ReputationEvent,
    SlashEvent,
)
from app.models.spend import SpendAlert, SpendLimit
from app.models.webhooks import WebhookDelivery, WebhookEndpoint

# Phase C — enterprise (orgs/SSO/Connect) and bulk/training workloads.
from app.models.enterprise import (
    ConnectAccount,
    OrganizationInvitation,
    SsoConnection,
)
from app.models.workloads import Batch, Dataset, TrainingCheckpoint

# Phase D — on-chain reconciliation audit trail.
from app.models.reconciliation import ChainReconciliationRun

__all__ = [
    "Role",
    "UserStatus",
    "User",
    "UserRole",
    "UserSession",
    "EmailVerificationToken",
    "PasswordResetToken",
    "Organization",
    "OrganizationMember",
    "Project",
    "ApiKey",
    "AuditLog",
    "SystemConfiguration",
    "Provider",
    "ProviderMachine",
    "HardwareInventory",
    "ProviderCapability",
    "EnrollmentToken",
    "PricingRule",
    "Quote",
    "ResourceReservation",
    "Model",
    "ModelVersion",
    "Job",
    "JobEvent",
    "JobAssignment",
    "JobAttempt",
    "JobLogMetadata",
    "Artifact",
    "BillingAccount",
    "LedgerTransaction",
    "LedgerEntry",
    "UsageRecord",
    "Invoice",
    "Payment",
    "Refund",
    "ProviderPayout",
    "Dispute",
    "MachineTelemetry",
    "JobTelemetry",
    "TelemetryAlert",
    "EscrowStatus",
    "OnChainEscrow",
    "OnChainTransaction",
    "OnChainTxStatus",
    "OnChainTxType",
    # Phase B
    "VerificationChallenge",
    "VerificationResult",
    "RedundancyCheck",
    "ProviderReputation",
    "ReputationEvent",
    "SlashEvent",
    "DisputeEvidence",
    "SpendLimit",
    "SpendAlert",
    "WebhookEndpoint",
    "WebhookDelivery",
    # Phase C
    "OrganizationInvitation",
    "SsoConnection",
    "ConnectAccount",
    "Batch",
    "Dataset",
    "TrainingCheckpoint",
    # Phase D
    "ChainReconciliationRun",
]
