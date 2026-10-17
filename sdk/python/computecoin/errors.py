"""Exception hierarchy for the ComputeCoin SDK."""

from __future__ import annotations

from typing import Optional


class ComputeCoinError(Exception):
    """Base class for all SDK errors."""


class ApiError(ComputeCoinError):
    """An API call returned a non-success status."""

    def __init__(self, status_code: int, message: str, body: Optional[object] = None) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"HTTP {status_code}: {message}")


class AuthenticationError(ApiError):
    """401/403 -- missing or invalid credentials."""


class RateLimitError(ApiError):
    """429 -- too many requests."""


class ConflictError(ApiError):
    """409 -- the request conflicts with current server state.

    Used by the marketplace reservation flow. The server communicates the
    specific reason via the ``detail`` field (``no_capacity``,
    ``quote_expired``, ``quote_already_reserved``, ``budget_exceeded``), which
    is surfaced here as :attr:`code` so callers can branch on it instead of
    parsing a message string. A 409 is never silently turned into a fake
    success -- it always raises this error carrying the server's detail.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        body: Optional[object] = None,
        code: Optional[str] = None,
    ) -> None:
        self.code = code
        super().__init__(status_code, message, body)


class ValidationError(ApiError):
    """422 -- the request failed server-side validation.

    Used by the job-creation flow. The server communicates the specific reason
    via the ``detail``/``code`` field (``invalid_input``, ``model_not_supported``),
    surfaced here as :attr:`code` so callers can branch on it instead of parsing
    a message string. A 422 always raises this typed error carrying the server's
    detail -- it is NEVER silently turned into a fabricated success.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        body: Optional[object] = None,
        code: Optional[str] = None,
    ) -> None:
        self.code = code
        super().__init__(status_code, message, body)


class PaymentRequiredError(ApiError):
    """402 -- the request requires funds the account does not have.

    Used by the Phase 5 billing/job-submit flows: when a customer has
    insufficient credits to cover a hold, the server returns 402 with a reason
    code (``insufficient_credits``), surfaced here as :attr:`code` so callers can
    branch on it. A 402 always raises this typed error carrying the server's
    detail -- the call never crashes and is NEVER turned into a fabricated
    success (no overspend, no fake top-up).
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        body: Optional[object] = None,
        code: Optional[str] = None,
    ) -> None:
        self.code = code
        super().__init__(status_code, message, body)


class EndpointNotAvailableError(ApiError):
    """404/501 -- the endpoint is not implemented / no capacity yet.

    Raised instead of returning fabricated output so callers can distinguish
    "the feature isn't live yet" from a real result.
    """
