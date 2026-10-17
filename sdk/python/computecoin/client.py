"""The ComputeCoin HTTP client.

Thin typed wrapper over httpx. The transport is injectable (pass your own
``httpx.Client``) so it is trivial to unit-test with a ``MockTransport`` and
never touch the network.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import httpx

from .errors import (
    ApiError,
    AuthenticationError,
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
    Job,
    JobArtifact,
    JobEvent,
    JobLogs,
    LedgerEntry,
    MachinePage,
    MarketplaceMachine,
    Payout,
    Project,
    ProviderEarnings,
    Quote,
    Reservation,
    TokenPair,
    Usage,
)

# FSM states that are terminal -- a job never leaves these (see phase4 contract).
TERMINAL_JOB_STATES = frozenset(
    {"completed", "failed", "cancelled", "timed_out", "disputed"}
)

DEFAULT_BASE_URL = "https://api.computecoin.example"


class ComputeCoinClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        access_token: Optional[str] = None,
        api_key: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._access_token = access_token
        self._api_key = api_key
        self._client = httpx.Client(
            base_url=self.base_url, transport=transport, timeout=timeout
        )

    # -- lifecycle ----------------------------------------------------------- #
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ComputeCoinClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals ----------------------------------------------------------- #
    def _headers(self, auth: bool) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if auth:
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            elif self._access_token:
                headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        auth: bool = True,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> Any:
        headers = self._headers(auth)
        if extra_headers:
            headers.update(extra_headers)
        resp = self._client.request(
            method, path, json=json, params=params, headers=headers
        )
        return self._handle(resp)

    @staticmethod
    def _handle(resp: httpx.Response) -> Any:
        if resp.is_success:
            if resp.content:
                return resp.json()
            return None

        # Try to extract a useful message without assuming JSON.
        try:
            body = resp.json()
            message = body.get("detail") or body.get("message") or str(body)
        except Exception:
            body = resp.text
            message = resp.text or resp.reason_phrase

        status = resp.status_code
        if status in (404, 501):
            raise EndpointNotAvailableError(
                status,
                f"endpoint not available yet (no capacity / not implemented): {message}",
                body,
            )
        if status in (401, 403):
            raise AuthenticationError(status, message, body)
        if status == 402:
            code = None
            if isinstance(body, dict):
                code = body.get("detail") or body.get("code")
            raise PaymentRequiredError(status, message, body, code=code)
        if status == 429:
            raise RateLimitError(status, message, body)
        if status == 409:
            code = None
            if isinstance(body, dict):
                code = body.get("detail") or body.get("code")
            raise ConflictError(status, message, body, code=code)
        if status == 422:
            code = None
            if isinstance(body, dict):
                code = body.get("code") or body.get("detail")
            raise ValidationError(status, message, body, code=code)
        raise ApiError(status, message, body)

    def set_access_token(self, token: str) -> None:
        self._access_token = token

    # -- auth ---------------------------------------------------------------- #
    def register(self, email: str, password: str) -> dict[str, Any]:
        return self._request(
            "POST", "/v1/auth/register",
            json={"email": email, "password": password}, auth=False,
        )

    def login(self, email: str, password: str) -> TokenPair:
        data = self._request(
            "POST", "/v1/auth/login",
            json={"email": email, "password": password}, auth=False,
        )
        pair = TokenPair.model_validate(data)
        self._access_token = pair.access_token
        return pair

    def refresh(self, refresh_token: str) -> TokenPair:
        data = self._request(
            "POST", "/v1/auth/refresh",
            json={"refresh_token": refresh_token}, auth=False,
        )
        pair = TokenPair.model_validate(data)
        self._access_token = pair.access_token
        return pair

    # -- projects ------------------------------------------------------------ #
    def list_projects(self) -> list[Project]:
        data = self._request("GET", "/v1/projects")
        return [Project.model_validate(p) for p in data]

    def create_project(self, name: str) -> Project:
        data = self._request("POST", "/v1/projects", json={"name": name})
        return Project.model_validate(data)

    def create_api_key(self, project_id: str, name: Optional[str] = None) -> ApiKey:
        data = self._request(
            "POST", f"/v1/projects/{project_id}/api-keys",
            json={"name": name},
        )
        return ApiKey.model_validate(data)

    # -- OpenAI-compatible inference ----------------------------------------- #
    # These may 404/501 until the backend ships them; we surface that clearly.
    def chat_completions(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> ChatCompletion:
        payload = {"model": model, "messages": messages, **kwargs}
        data = self._request("POST", "/v1/chat/completions", json=payload)
        return ChatCompletion.model_validate(data)

    def embeddings(
        self,
        *,
        model: str,
        input: Any,
        **kwargs: Any,
    ) -> EmbeddingResponse:
        payload = {"model": model, "input": input, **kwargs}
        data = self._request("POST", "/v1/embeddings", json=payload)
        return EmbeddingResponse.model_validate(data)

    # -- Phase 3: marketplace (PUBLIC) --------------------------------------- #
    def search_machines(
        self,
        *,
        gpu_model: Optional[str] = None,
        min_vram_mb: Optional[int] = None,
        max_price_per_hour: Optional[str] = None,
        region: Optional[str] = None,
        workload: Optional[str] = None,
        verification_status: Optional[str] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
    ) -> MachinePage:
        """Browse online, priced, available machines. No auth required.

        ``max_price_per_hour`` is a decimal string (money), matching the rest
        of the marketplace contract.
        """
        params: dict[str, Any] = {
            "gpu_model": gpu_model,
            "min_vram_mb": min_vram_mb,
            "max_price_per_hour": max_price_per_hour,
            "region": region,
            "workload": workload,
            "verification_status": verification_status,
            "page": page,
            "page_size": page_size,
        }
        # Drop unset params so we send a clean query string.
        params = {k: v for k, v in params.items() if v is not None}
        data = self._request(
            "GET", "/v1/marketplace/machines", params=params, auth=False
        )
        return MachinePage.model_validate(data)

    def get_marketplace_machine(self, machine_id: str) -> MarketplaceMachine:
        data = self._request(
            "GET", f"/v1/marketplace/machines/{machine_id}", auth=False
        )
        return MarketplaceMachine.model_validate(data)

    # -- Phase 3: quotes (customer auth) ------------------------------------- #
    def create_quote(
        self,
        *,
        machine_id: str,
        workload_type: str,
        estimated_minutes: int,
        gpu_count: int = 1,
        max_authorized_spend: Optional[str] = None,
    ) -> Quote:
        """Create an itemized, time-limited, price-snapshotted quote.

        ``max_authorized_spend`` is a decimal string (money). The returned
        quote's ``breakdown`` keeps every line (gpu_cost, platform_fee, total)
        as a string -- no float coercion.
        """
        payload: dict[str, Any] = {
            "machine_id": machine_id,
            "workload_type": workload_type,
            "estimated_minutes": estimated_minutes,
            "gpu_count": gpu_count,
        }
        if max_authorized_spend is not None:
            payload["max_authorized_spend"] = max_authorized_spend
        data = self._request("POST", "/v1/quotes", json=payload)
        return Quote.model_validate(data)

    def get_quote(self, quote_id: str) -> Quote:
        data = self._request("GET", f"/v1/quotes/{quote_id}")
        return Quote.model_validate(data)

    def list_quotes(self) -> list[Quote]:
        data = self._request("GET", "/v1/quotes")
        return [Quote.model_validate(q) for q in data]

    # -- Phase 3: reservations (customer auth) ------------------------------- #
    def reserve_quote(self, quote_id: str) -> Reservation:
        """Atomically convert a quote into a capacity reservation.

        On a 409 the server reports a specific reason (``no_capacity``,
        ``quote_expired``, ``quote_already_reserved``, ``budget_exceeded``);
        :meth:`_handle` raises :class:`ConflictError` carrying that ``code`` and
        the server ``body`` -- it never returns a fabricated success.
        """
        data = self._request("POST", f"/v1/quotes/{quote_id}/reserve", json={})
        return Reservation.model_validate(data)

    def list_reservations(self) -> list[Reservation]:
        data = self._request("GET", "/v1/reservations")
        return [Reservation.model_validate(r) for r in data]

    def release_reservation(self, reservation_id: str) -> dict[str, Any]:
        """Release a held reservation. Idempotent-ish: releasing an already
        released reservation returns 200/204 rather than erroring."""
        return self._request(
            "POST", f"/v1/reservations/{reservation_id}/release", json={}
        )

    # -- Phase 4: jobs (customer auth) --------------------------------------- #
    def create_job(
        self,
        *,
        workload_type: str,
        model: str,
        input: dict[str, Any],
        gpu_count: int,
        estimated_minutes: int,
        max_cost: str,
        max_runtime_seconds: int,
        machine_id: Optional[str] = None,
        retry_policy: Optional[dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Job:
        """Submit a job (POST /v1/jobs).

        ``max_cost`` is a decimal string (money); it is sent verbatim, never
        coerced to float. When ``idempotency_key`` is given it is sent as the
        ``Idempotency-Key`` header, so the same (customer, key) returns the SAME
        job rather than creating a duplicate.

        On a 422 the server reports ``invalid_input`` / ``model_not_supported``
        and on a 409 ``budget_exceeded``; these surface as the typed
        :class:`~computecoin.errors.ValidationError` /
        :class:`~computecoin.errors.ConflictError` carrying ``.code`` -- the
        call never crashes and NEVER fabricates a success.
        """
        payload: dict[str, Any] = {
            "workload_type": workload_type,
            "model": model,
            "input": input,
            "gpu_count": gpu_count,
            "estimated_minutes": estimated_minutes,
            "max_cost": max_cost,
            "max_runtime_seconds": max_runtime_seconds,
        }
        if machine_id is not None:
            payload["machine_id"] = machine_id
        if retry_policy is not None:
            payload["retry_policy"] = retry_policy

        extra_headers = None
        if idempotency_key is not None:
            extra_headers = {"Idempotency-Key": idempotency_key}

        data = self._request(
            "POST", "/v1/jobs", json=payload, extra_headers=extra_headers
        )
        return Job.model_validate(data)

    def get_job(self, job_id: str) -> Job:
        data = self._request("GET", f"/v1/jobs/{job_id}")
        return Job.model_validate(data)

    def list_jobs(self) -> list[Job]:
        data = self._request("GET", "/v1/jobs")
        return [Job.model_validate(j) for j in data]

    def get_job_events(self, job_id: str) -> list[JobEvent]:
        """Ordered FSM history (append-only job_events) for a job."""
        data = self._request("GET", f"/v1/jobs/{job_id}/events")
        return [JobEvent.model_validate(e) for e in data]

    def get_job_logs(self, job_id: str) -> JobLogs:
        data = self._request("GET", f"/v1/jobs/{job_id}/logs")
        # The backend may return either a bare list of lines or {"lines": [...]}.
        if isinstance(data, list):
            return JobLogs(lines=data)
        return JobLogs.model_validate(data)

    def get_job_artifacts(self, job_id: str) -> list[JobArtifact]:
        data = self._request("GET", f"/v1/jobs/{job_id}/artifacts")
        return [JobArtifact.model_validate(a) for a in data]

    def get_artifact_download_url(
        self, job_id: str, artifact_id: str
    ) -> ArtifactDownload:
        """Fetch a short-lived signed download URL for an artifact (owner only)."""
        data = self._request(
            "GET", f"/v1/jobs/{job_id}/artifacts/{artifact_id}/download"
        )
        return ArtifactDownload.model_validate(data)

    def cancel_job(self, job_id: str) -> CancelAck:
        """Request cancellation; honored at the next safe point (-> CANCELLED)."""
        data = self._request("POST", f"/v1/jobs/{job_id}/cancel", json={})
        if data is None:
            return CancelAck(job_id=job_id, cancellation_requested=True)
        return CancelAck.model_validate(data)

    def wait_for_job(
        self,
        job_id: str,
        *,
        poll: float = 2.0,
        timeout: float = 300.0,
    ) -> Job:
        """Poll :meth:`get_job` until the job reaches a terminal FSM state.

        Terminal states are ``completed``/``failed``/``cancelled``/``timed_out``/
        ``disputed`` (lowercase, per the Phase 4 contract). Raises
        :class:`TimeoutError` if the job is still non-terminal after ``timeout``
        seconds. Uses the injected transport, so tests stay fully offline.
        """
        deadline = time.monotonic() + timeout
        while True:
            job = self.get_job(job_id)
            if (job.status or "").lower() in TERMINAL_JOB_STATES:
                return job
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"job {job_id} did not reach a terminal state within "
                    f"{timeout}s (last status: {job.status!r})"
                )
            time.sleep(poll)

    # -- Phase 5: billing (customer auth) ------------------------------------ #
    # The MONEY layer. Every amount in and out is a decimal STRING, sent/kept
    # verbatim -- never coerced to float. Currency "USDC_CREDITS" is a SIMULATED
    # test credit (no real payment rail). A 402 insufficient_credits raises
    # PaymentRequiredError.code and a 409 (e.g. insufficient_earnings) raises
    # ConflictError.code -- the call never crashes and NEVER fabricates a
    # success (no overspend, no fake payout).

    def get_billing_account(self) -> BillingAccount:
        """Current billing account snapshot (spendable, reserved, total)."""
        data = self._request("GET", "/v1/billing/account")
        return BillingAccount.model_validate(data)

    def purchase_credits(self, amount: str) -> BillingAccount:
        """Top up simulated credits (``amount`` is a decimal string, sent
        verbatim). Returns the updated account."""
        data = self._request(
            "POST", "/v1/billing/credits/purchase", json={"amount": amount}
        )
        return BillingAccount.model_validate(data)

    def get_ledger(self) -> list[LedgerEntry]:
        """The customer's own double-entry ledger entries (append-only)."""
        data = self._request("GET", "/v1/billing/ledger")
        return [LedgerEntry.model_validate(e) for e in data]

    def get_usage(self) -> Usage:
        """Spend summary + per-job usage records (estimated vs actual separate)."""
        data = self._request("GET", "/v1/usage")
        return Usage.model_validate(data)

    def list_invoices(self) -> list[Invoice]:
        """Itemized invoices, one per settled job."""
        data = self._request("GET", "/v1/invoices")
        return [Invoice.model_validate(i) for i in data]

    def get_invoice(self, invoice_id: str) -> Invoice:
        data = self._request("GET", f"/v1/invoices/{invoice_id}")
        return Invoice.model_validate(data)

    def open_dispute(self, job_id: str, reason: str) -> Dispute:
        """Open a dispute (status ``open``) on a settled job."""
        data = self._request(
            "POST", f"/v1/jobs/{job_id}/dispute", json={"reason": reason}
        )
        return Dispute.model_validate(data)

    def list_disputes(self) -> list[Dispute]:
        data = self._request("GET", "/v1/disputes")
        return [Dispute.model_validate(d) for d in data]

    def get_dispute(self, dispute_id: str) -> Dispute:
        data = self._request("GET", f"/v1/disputes/{dispute_id}")
        return Dispute.model_validate(data)

    # -- Phase 5: provider billing (provider auth) --------------------------- #
    def get_provider_earnings(self) -> ProviderEarnings:
        """LIVE accrued earnings from the ledger (available + lifetime)."""
        data = self._request("GET", "/v1/provider/earnings")
        return ProviderEarnings.model_validate(data)

    def request_payout(self, amount: str) -> Payout:
        """Request a payout against accrued earnings (``amount`` is a decimal
        string, sent verbatim).

        On a 409 the server reports ``insufficient_earnings`` (amount >
        available); :meth:`_handle` raises :class:`ConflictError` carrying that
        ``code`` -- the call never fabricates a settled payout.
        """
        data = self._request(
            "POST", "/v1/provider/payouts", json={"amount": amount}
        )
        return Payout.model_validate(data)

    def list_payouts(self) -> list[Payout]:
        data = self._request("GET", "/v1/provider/payouts")
        return [Payout.model_validate(p) for p in data]
