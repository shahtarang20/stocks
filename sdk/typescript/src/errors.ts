/** Error hierarchy for the ComputeCoin SDK. */

export class ComputeCoinError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ComputeCoinError";
  }
}

export class ApiError extends ComputeCoinError {
  readonly statusCode: number;
  readonly body: unknown;

  constructor(statusCode: number, message: string, body?: unknown) {
    super(`HTTP ${statusCode}: ${message}`);
    this.name = "ApiError";
    this.statusCode = statusCode;
    this.body = body;
  }
}

export class AuthenticationError extends ApiError {
  constructor(statusCode: number, message: string, body?: unknown) {
    super(statusCode, message, body);
    this.name = "AuthenticationError";
  }
}

export class RateLimitError extends ApiError {
  constructor(statusCode: number, message: string, body?: unknown) {
    super(statusCode, message, body);
    this.name = "RateLimitError";
  }
}

/**
 * 409 -- the request conflicts with current server state.
 *
 * Used by the marketplace reservation flow. The server communicates the
 * specific reason via the `detail` field (`no_capacity`, `quote_expired`,
 * `quote_already_reserved`, `budget_exceeded`), surfaced here as `code` so
 * callers can branch on it. A 409 always throws this error carrying the
 * server's detail -- it is never turned into a fabricated success.
 */
export class ConflictError extends ApiError {
  readonly code: string | undefined;

  constructor(statusCode: number, message: string, body?: unknown, code?: string) {
    super(statusCode, message, body);
    this.name = "ConflictError";
    this.code = code;
  }
}

/**
 * 422 -- the request failed server-side validation.
 *
 * Used by the job-creation flow. The server communicates the specific reason
 * via the `code`/`detail` field (`invalid_input`, `model_not_supported`),
 * surfaced here as `code` so callers can branch on it instead of parsing a
 * message string. A 422 always throws this typed error carrying the server's
 * detail -- it is never turned into a fabricated success.
 */
export class ValidationError extends ApiError {
  readonly code: string | undefined;

  constructor(statusCode: number, message: string, body?: unknown, code?: string) {
    super(statusCode, message, body);
    this.name = "ValidationError";
    this.code = code;
  }
}

/**
 * 402 -- the request requires funds the account does not have.
 *
 * Used by the Phase 5 billing / job-submit flows: when a customer has
 * insufficient credits to cover a hold, the server returns 402 with a reason
 * (`insufficient_credits`), surfaced here as `code`. A 402 always throws this
 * typed error carrying the server's detail -- it is never turned into a
 * fabricated success (no overspend, no fake top-up).
 */
export class PaymentRequiredError extends ApiError {
  readonly code: string | undefined;

  constructor(statusCode: number, message: string, body?: unknown, code?: string) {
    super(statusCode, message, body);
    this.name = "PaymentRequiredError";
    this.code = code;
  }
}

/**
 * 404/501 -- the endpoint is not implemented / has no capacity yet.
 * Thrown instead of returning fabricated output.
 */
export class EndpointNotAvailableError extends ApiError {
  constructor(statusCode: number, message: string, body?: unknown) {
    super(statusCode, message, body);
    this.name = "EndpointNotAvailableError";
  }
}
