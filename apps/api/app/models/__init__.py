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
]
