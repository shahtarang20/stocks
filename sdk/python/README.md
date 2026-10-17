# ComputeCoin Python SDK (`computecoin`)

A thin, typed HTTP client for the ComputeCoin API, built on `httpx` + `pydantic`.

## Install

```bash
cd sdk/python
pip install -e ".[dev]"
```

## Quickstart

```python
from computecoin import ComputeCoinClient, EndpointNotAvailableError

with ComputeCoinClient("https://api.computecoin.example") as client:
    client.register("dev@example.com", "s3cret")
    pair = client.login("dev@example.com", "s3cret")

    project = client.create_project(name="my-app")
    key = client.create_api_key(project.id, name="prod")
    print(key.prefix)  # full key is only returned once, at creation

    try:
        completion = client.chat_completions(
            model="llama-3.1-8b",
            messages=[{"role": "user", "content": "Hello!"}],
        )
    except EndpointNotAvailableError as exc:
        # The server may not expose inference yet. The SDK raises instead of
        # fabricating a result.
        print("not live yet:", exc)
```

A runnable version is in [`examples/quickstart.py`](examples/quickstart.py)
(guarded so importing it makes no network calls).

## Methods

| Method | Endpoint |
| --- | --- |
| `register(email, password)` | `POST /v1/auth/register` |
| `login(email, password)` | `POST /v1/auth/login` |
| `refresh(refresh_token)` | `POST /v1/auth/refresh` |
| `list_projects()` | `GET /v1/projects` |
| `create_project(name)` | `POST /v1/projects` |
| `create_api_key(project_id, name)` | `POST /v1/projects/{id}/api-keys` |
| `chat_completions(model, messages, ...)` | `POST /v1/chat/completions` |
| `embeddings(model, input, ...)` | `POST /v1/embeddings` |
| `search_machines(...)` | `GET /v1/marketplace/machines` (public) |
| `get_marketplace_machine(id)` | `GET /v1/marketplace/machines/{id}` (public) |
| `create_quote(...)` | `POST /v1/quotes` |
| `get_quote(id)` / `list_quotes()` | `GET /v1/quotes/{id}` / `GET /v1/quotes` |
| `reserve_quote(quote_id)` | `POST /v1/quotes/{id}/reserve` |
| `list_reservations()` | `GET /v1/reservations` |
| `release_reservation(id)` | `POST /v1/reservations/{id}/release` |
| `create_job(..., idempotency_key=None)` | `POST /v1/jobs` |
| `get_job(id)` / `list_jobs()` | `GET /v1/jobs/{id}` / `GET /v1/jobs` |
| `get_job_events(id)` | `GET /v1/jobs/{id}/events` |
| `get_job_logs(id)` | `GET /v1/jobs/{id}/logs` |
| `get_job_artifacts(id)` | `GET /v1/jobs/{id}/artifacts` |
| `get_artifact_download_url(id, artifact_id)` | `GET /v1/jobs/{id}/artifacts/{artifact_id}/download` |
| `cancel_job(id)` | `POST /v1/jobs/{id}/cancel` |
| `wait_for_job(id, poll, timeout)` | polls `get_job` until terminal |
| `get_billing_account()` | `GET /v1/billing/account` |
| `purchase_credits(amount)` | `POST /v1/billing/credits/purchase` |
| `get_ledger()` | `GET /v1/billing/ledger` |
| `get_usage()` | `GET /v1/usage` |
| `list_invoices()` / `get_invoice(id)` | `GET /v1/invoices` / `GET /v1/invoices/{id}` |
| `open_dispute(job_id, reason)` | `POST /v1/jobs/{job_id}/dispute` |
| `list_disputes()` / `get_dispute(id)` | `GET /v1/disputes` / `GET /v1/disputes/{id}` |
| `get_provider_earnings()` | `GET /v1/provider/earnings` |
| `request_payout(amount)` | `POST /v1/provider/payouts` |
| `list_payouts()` | `GET /v1/provider/payouts` |

## Marketplace, quotes & reservations (Phase 3)

Search is **public** (no auth); quotes and reservations require a logged-in
customer. **All money values are decimal strings** (e.g. `"1.50"`) — the SDK
keeps them as `str` and never coerces to `float`, so the itemized breakdown
stays exact. Credits (`"USDC_CREDITS"`) are **simulated test credits** in
Phase 3; there is no real payment rail.

```python
from computecoin import ComputeCoinClient, ConflictError

with ComputeCoinClient("https://api.computecoin.example") as client:
    page = client.search_machines(workload="text-generation", max_price_per_hour="5.00")
    machine = page.items[0]

    client.login("dev@example.com", "s3cret")
    quote = client.create_quote(
        machine_id=machine.machine_id,
        workload_type="text-generation",
        estimated_minutes=60,
        gpu_count=1,
        max_authorized_spend="10.00",
    )
    b = quote.breakdown
    print(f"gpu {b.gpu_cost} + fee {b.platform_fee} = {b.total} {quote.currency}")

    try:
        reservation = client.reserve_quote(quote.quote_id)
        print("reserved:", reservation.reservation_id)
    except ConflictError as exc:
        # 409: no_capacity / quote_expired / quote_already_reserved / budget_exceeded.
        # The reason code is exposed; the SDK never fakes a success.
        print(f"could not reserve ({exc.code}): {exc}")
```

A runnable version is in
[`examples/marketplace_quickstart.py`](examples/marketplace_quickstart.py).

A **409** on reserve raises `ConflictError`, whose `.code` carries the server
detail (`no_capacity`, `quote_expired`, `quote_already_reserved`,
`budget_exceeded`) so you can branch on it.

## Jobs (Phase 4)

Submit and track compute jobs. `create_job` takes the job spec and an optional
`idempotency_key` (sent as the `Idempotency-Key` header, so the same
`(customer, key)` returns the SAME job instead of a duplicate). FSM states are
**lowercase** (`created`, `validating`, `queued`, `matched`, `reserved`,
`running`, `verifying`, `completed`, and terminal `failed` / `cancelled` /
`timed_out` / `disputed`). Money (`max_cost`, `final_cost`) stays a decimal
string.

```python
from computecoin import ComputeCoinClient, ValidationError, ConflictError

with ComputeCoinClient("https://api.computecoin.example") as client:
    client.login("dev@example.com", "s3cret")
    try:
        job = client.create_job(
            workload_type="text-generation",
            model="echo-sim-v1",
            input={"prompt": "Hello", "max_tokens": 64},
            gpu_count=1,
            estimated_minutes=5,
            max_cost="5.00",
            max_runtime_seconds=300,
            retry_policy={"max_retries": 1},
            idempotency_key="my-job-1",
        )
    except ValidationError as exc:
        print(f"rejected ({exc.code}): {exc}")   # invalid_input / model_not_supported
        raise
    except ConflictError as exc:
        print(f"rejected ({exc.code}): {exc}")   # budget_exceeded
        raise

    job = client.wait_for_job(job.id, poll=2.0, timeout=300.0)
    print(job.status, job.execution_mode)        # e.g. "completed" "simulated"
    for art in client.get_job_artifacts(job.id):
        url = client.get_artifact_download_url(job.id, art.artifact_id).url
```

A runnable version is in [`examples/job_quickstart.py`](examples/job_quickstart.py).

**Simulated execution (honesty stamp).** `Job.execution_mode` is `"simulated"`
or `"hardware"`. On CPU-only dev/CI infrastructure (no GPU, no Docker) the
backend runs the in-process `SimulatedExecutor`, which produces a
**deterministic transform** and stamps every job `execution_mode: "simulated"`.
That output is **NOT real model inference** — the SDK surfaces the stamp so you
always know. A failed/cancelled/timed-out job is never reported as completed.

A **422** on `create_job` raises `ValidationError` and a **409** raises
`ConflictError`; both carry the server's reason in `.code` (`invalid_input`,
`model_not_supported`, `budget_exceeded`) so you can branch on it instead of
parsing a message — the call never crashes and never fabricates a success.

## Billing, ledger, invoices & payouts (Phase 5)

The MONEY layer. **Every amount in and out is a decimal string** (e.g.
`"50.00"`) — the SDK sends and keeps it verbatim and NEVER coerces to `float`,
so nothing is rounded. The currency `"USDC_CREDITS"` is a **simulated test
credit**: there is no real payment rail (`PAYMENT_PROVIDER_CONFIGURATION=
simulated`), so `purchase_credits` only tops up simulated credits. Estimated vs
actual costs are tracked **separately** on each invoice — an estimated amount is
never reported as a received/actual amount, and a failed job is never billed.

```python
from computecoin import ComputeCoinClient, ConflictError, PaymentRequiredError

with ComputeCoinClient("https://api.computecoin.example") as client:
    client.login("dev@example.com", "s3cret")

    account = client.get_billing_account()
    print(account.available, account.reserved, account.balance)  # all strings

    account = client.purchase_credits("50.00")   # simulated top-up

    for inv in client.list_invoices():
        # estimated vs actual kept separate; never conflated.
        print(inv.invoice_id, inv.estimated_total, inv.actual_total, inv.status)

    # Provider side: live accrued earnings + payout request.
    earnings = client.get_provider_earnings()
    try:
        payout = client.request_payout("10.00")
    except ConflictError as exc:
        # 409 insufficient_earnings -> exc.code; never a fabricated payout.
        print(f"payout refused ({exc.code}): {exc}")
```

A runnable version is in [`examples/billing_quickstart.py`](examples/billing_quickstart.py).

Billing error mappings are typed, so you branch on `.code` instead of parsing a
message: a **402** raises `PaymentRequiredError` (`.code` =
`insufficient_credits`) and a **409** raises `ConflictError` (`.code` =
`insufficient_earnings`, etc.). The call never crashes and never fabricates a
success — no overspend, no fake top-up, no fake payout.

Types added: `BillingAccount`, `LedgerEntry`, `UsageRecord`, `Usage`,
`Invoice`/`InvoiceLineItem`, `Dispute`, `ProviderEarnings`, `Payout` — every
money field is a `str`.

## Endpoints not yet implemented

The OpenAI-compatible inference routes (`/v1/chat/completions`,
`/v1/embeddings`) may not be live on the backend yet. When the server returns
**404** or **501**, the client raises `EndpointNotAvailableError` with a clear
"not available yet / no capacity" message. **It never fabricates output.**

Other error mappings: `401/403 -> AuthenticationError`, `429 -> RateLimitError`,
anything else -> `ApiError`.

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

Tests use `httpx.MockTransport`, so they run fully offline and assert on request
shaping and on the 404/501 -> `EndpointNotAvailableError` behavior.
