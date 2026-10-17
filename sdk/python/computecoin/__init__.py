"""ComputeCoin Python SDK.

A thin, typed client over the ComputeCoin HTTP API.

Some endpoints (notably the OpenAI-compatible inference routes) may not be
implemented on the backend yet. When the server returns 404/501, the client
raises :class:`EndpointNotAvailableError` -- it never fabricates a result.
"""

from .client import ComputeCoinClient
from .errors import (
    ApiError,
    AuthenticationError,
    ComputeCoinError,
    ConflictError,
    EndpointNotAvailableError,
    PaymentRequiredError,
    RateLimitError,
    ValidationError,
)
from .models import (
    ApiKey,
    ArtifactDownload,
    BillingAccount,
    CancelAck,
    ChatCompletion,
    Dispute,
    EmbeddingResponse,
    Invoice,
    InvoiceLineItem,
    Job,
    JobArtifact,
    JobEvent,
    JobLogs,
    LedgerEntry,
    MachinePage,
    MachineReliability,
    MarketplaceMachine,
    Payout,
    Project,
    ProviderEarnings,
    Quote,
    QuoteBreakdown,
    Reservation,
    TokenPair,
    Usage,
    UsageRecord,
)

__version__ = "0.1.0"

__all__ = [
    "ComputeCoinClient",
    "ComputeCoinError",
    "ApiError",
    "AuthenticationError",
    "ConflictError",
    "EndpointNotAvailableError",
    "PaymentRequiredError",
    "RateLimitError",
    "ValidationError",
    "Project",
    "ApiKey",
    "TokenPair",
    "ChatCompletion",
    "EmbeddingResponse",
    "MarketplaceMachine",
    "MachineReliability",
    "MachinePage",
    "Quote",
    "QuoteBreakdown",
    "Reservation",
    "Job",
    "JobEvent",
    "JobArtifact",
    "ArtifactDownload",
    "JobLogs",
    "CancelAck",
    "BillingAccount",
    "LedgerEntry",
    "UsageRecord",
    "Usage",
    "InvoiceLineItem",
    "Invoice",
    "Dispute",
    "ProviderEarnings",
    "Payout",
]
