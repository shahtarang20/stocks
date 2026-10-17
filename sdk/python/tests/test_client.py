"""Unit tests for the ComputeCoin SDK using httpx.MockTransport (offline)."""

import json

import httpx
import pytest

from computecoin import (
    ComputeCoinClient,
    ConflictError,
    EndpointNotAvailableError,
    PaymentRequiredError,
    ValidationError,
)
from computecoin.errors import AuthenticationError


def make_client(handler) -> ComputeCoinClient:
    transport = httpx.MockTransport(handler)
    return ComputeCoinClient("https://api.test", transport=transport)


def test_login_shapes_request_and_sets_token():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"access_token": "acc", "refresh_token": "ref", "expires_in": 900},
        )

    client = make_client(handler)
    pair = client.login("a@b.com", "pw")

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.test/v1/auth/login"
    assert captured["body"] == {"email": "a@b.com", "password": "pw"}
    assert pair.access_token == "acc"
    # Token is now attached for authenticated calls.
    assert client._access_token == "acc"


def test_create_project_sends_auth_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "proj_1", "name": "demo"})

    transport = httpx.MockTransport(handler)
    client = ComputeCoinClient("https://api.test", access_token="tok123", transport=transport)
    project = client.create_project("demo")

    assert captured["auth"] == "Bearer tok123"
    assert captured["body"] == {"name": "demo"}
    assert project.id == "proj_1"


def test_chat_completions_404_raises_not_available():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not implemented"})

    client = make_client(handler)
    with pytest.raises(EndpointNotAvailableError) as exc_info:
        client.chat_completions(
            model="llama", messages=[{"role": "user", "content": "hi"}]
        )
    assert exc_info.value.status_code == 404
    # Must not fabricate output.
    assert "not available" in str(exc_info.value)


def test_chat_completions_501_raises_not_available():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(501, text="Not Implemented")

    client = make_client(handler)
    with pytest.raises(EndpointNotAvailableError):
        client.embeddings(model="e5", input="hello")


def test_chat_completions_request_shape():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "cmpl_1",
                "model": "llama",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
            },
        )

    client = make_client(handler)
    result = client.chat_completions(
        model="llama",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.2,
    )
    assert captured["url"] == "https://api.test/v1/chat/completions"
    assert captured["body"]["model"] == "llama"
    assert captured["body"]["temperature"] == 0.2
    assert result.choices[0]["message"]["content"] == "hi"


def test_401_raises_authentication_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad token"})

    client = make_client(handler)
    with pytest.raises(AuthenticationError):
        client.list_projects()


# -- Phase 3: marketplace / quotes / reservations ------------------------- #


def test_search_machines_shapes_query_and_parses_items():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("Authorization")
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "machine_id": "m_1",
                        "gpu_model": "RTX 4090",
                        "gpu_count": 1,
                        "vram_mb": 24576,
                        "cpu_model": "EPYC",
                        "cpu_cores": 16,
                        "ram_mb": 65536,
                        "region": "us-east",
                        "verification_status": "detected",
                        "price_per_hour": "1.50",
                        "currency": "USDC_CREDITS",
                        "supported_workloads": ["text-generation"],
                        "available_slots": 1,
                        "max_concurrent_jobs": 1,
                        "reliability": {"completed_jobs": 0, "success_rate": None},
                        "last_heartbeat_at": "2026-01-01T00:00:00Z",
                    }
                ],
                "page": 1,
                "page_size": 20,
                "total": 1,
            },
        )

    client = make_client(handler)
    page = client.search_machines(
        gpu_model="RTX 4090",
        min_vram_mb=16000,
        max_price_per_hour="2.00",
        workload="text-generation",
        page=1,
        page_size=20,
    )

    assert captured["method"] == "GET"
    assert captured["url"].startswith("https://api.test/v1/marketplace/machines?")
    # Public endpoint: no Authorization header sent.
    assert captured["auth"] is None
    assert captured["params"]["gpu_model"] == "RTX 4090"
    assert captured["params"]["min_vram_mb"] == "16000"
    assert captured["params"]["max_price_per_hour"] == "2.00"
    assert captured["params"]["workload"] == "text-generation"
    # Unset params are omitted entirely.
    assert "region" not in captured["params"]

    assert page.total == 1
    assert page.page == 1
    machine = page.items[0]
    assert machine.machine_id == "m_1"
    # Money stays a string.
    assert machine.price_per_hour == "1.50"
    assert isinstance(machine.price_per_hour, str)
    # Honest: no fabricated rating.
    assert machine.reliability is not None
    assert machine.reliability.success_rate is None


def test_create_quote_returns_breakdown_strings():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "quote_id": "q_1",
                "machine_id": "m_1",
                "customer_id": "c_1",
                "workload_type": "text-generation",
                "gpu_count": 1,
                "estimated_minutes": 60,
                "currency": "USDC_CREDITS",
                "breakdown": {
                    "price_per_hour": "1.50",
                    "estimated_hours": "1.00",
                    "gpu_cost": "1.50",
                    "platform_fee_rate": "0.10",
                    "platform_fee": "0.15",
                    "total": "1.65",
                },
                "max_authorized_spend": "5.00",
                "status": "active",
                "expires_at": "2026-01-01T00:02:00Z",
                "created_at": "2026-01-01T00:00:00Z",
            },
        )

    transport = httpx.MockTransport(handler)
    client = ComputeCoinClient(
        "https://api.test", access_token="tok123", transport=transport
    )
    quote = client.create_quote(
        machine_id="m_1",
        workload_type="text-generation",
        estimated_minutes=60,
        gpu_count=1,
        max_authorized_spend="5.00",
    )

    assert captured["url"] == "https://api.test/v1/quotes"
    assert captured["auth"] == "Bearer tok123"
    assert captured["body"] == {
        "machine_id": "m_1",
        "workload_type": "text-generation",
        "estimated_minutes": 60,
        "gpu_count": 1,
        "max_authorized_spend": "5.00",
    }
    # Every money field is a string, never coerced to float.
    assert quote.breakdown.total == "1.65"
    assert quote.breakdown.platform_fee == "0.15"
    assert quote.breakdown.gpu_cost == "1.50"
    assert isinstance(quote.breakdown.total, str)
    assert quote.max_authorized_spend == "5.00"
    assert quote.status == "active"


def test_reserve_quote_409_no_capacity_raises_conflict_with_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "no_capacity"})

    transport = httpx.MockTransport(handler)
    client = ComputeCoinClient(
        "https://api.test", access_token="tok123", transport=transport
    )
    with pytest.raises(ConflictError) as exc_info:
        client.reserve_quote("q_1")

    err = exc_info.value
    assert err.status_code == 409
    # The server's reason code is surfaced, not a generic crash.
    assert err.code == "no_capacity"
    assert err.body == {"detail": "no_capacity"}


def test_reserve_quote_404_raises_not_available():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "quote not found"})

    transport = httpx.MockTransport(handler)
    client = ComputeCoinClient(
        "https://api.test", access_token="tok123", transport=transport
    )
    with pytest.raises(EndpointNotAvailableError) as exc_info:
        client.reserve_quote("missing")
    assert exc_info.value.status_code == 404


def test_reserve_quote_success_returns_reservation():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {}
        return httpx.Response(
            201,
            json={
                "reservation_id": "r_1",
                "quote_id": "q_1",
                "machine_id": "m_1",
                "slots": 1,
                "status": "active",
                "expires_at": "2026-01-01T00:15:00Z",
                "created_at": "2026-01-01T00:00:00Z",
            },
        )

    transport = httpx.MockTransport(handler)
    client = ComputeCoinClient(
        "https://api.test", access_token="tok123", transport=transport
    )
    reservation = client.reserve_quote("q_1")
    assert reservation.reservation_id == "r_1"
    assert reservation.status == "active"
    assert reservation.slots == 1


# -- Phase 4: jobs -------------------------------------------------------- #


def _job_body(**overrides):
    body = {
        "id": "job_1",
        "customer_id": "c_1",
        "workload_type": "text-generation",
        "model": "echo-sim-v1",
        "input": {"prompt": "Hello", "max_tokens": 64},
        "status": "created",
        "execution_mode": "simulated",
        "gpu_count": 1,
        "estimated_minutes": 5,
        "max_cost": "5.00",
        "max_runtime_seconds": 300,
        "retry_policy": {"max_retries": 1},
        "attempts_made": 0,
        "final_cost": None,
        "created_at": "2026-01-01T00:00:00Z",
    }
    body.update(overrides)
    return body


def test_create_job_shapes_body_and_sends_idempotency_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("Authorization")
        captured["idempotency"] = request.headers.get("Idempotency-Key")
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json=_job_body())

    transport = httpx.MockTransport(handler)
    client = ComputeCoinClient(
        "https://api.test", access_token="tok123", transport=transport
    )
    job = client.create_job(
        workload_type="text-generation",
        model="echo-sim-v1",
        input={"prompt": "Hello", "max_tokens": 64},
        gpu_count=1,
        estimated_minutes=5,
        max_cost="5.00",
        max_runtime_seconds=300,
        retry_policy={"max_retries": 1},
        idempotency_key="idem-abc",
    )

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.test/v1/jobs"
    assert captured["auth"] == "Bearer tok123"
    # The optional Idempotency-Key header is sent when provided.
    assert captured["idempotency"] == "idem-abc"
    assert captured["body"] == {
        "workload_type": "text-generation",
        "model": "echo-sim-v1",
        "input": {"prompt": "Hello", "max_tokens": 64},
        "gpu_count": 1,
        "estimated_minutes": 5,
        # Money stays a string in the request body.
        "max_cost": "5.00",
        "max_runtime_seconds": 300,
        "retry_policy": {"max_retries": 1},
    }
    assert isinstance(captured["body"]["max_cost"], str)
    # Honesty stamp surfaced on the returned Job.
    assert job.execution_mode == "simulated"
    assert job.status == "created"


def test_create_job_omits_idempotency_header_when_not_given():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["idempotency"] = request.headers.get("Idempotency-Key")
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json=_job_body())

    transport = httpx.MockTransport(handler)
    client = ComputeCoinClient(
        "https://api.test", access_token="tok123", transport=transport
    )
    client.create_job(
        workload_type="text-generation",
        model="echo-sim-v1",
        input={"prompt": "Hi"},
        gpu_count=1,
        estimated_minutes=5,
        max_cost="5.00",
        max_runtime_seconds=300,
    )
    assert captured["idempotency"] is None
    # machine_id / retry_policy omitted entirely when not supplied.
    assert "machine_id" not in captured["body"]
    assert "retry_policy" not in captured["body"]


def test_create_job_422_invalid_input_raises_validation_error_with_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "invalid_input"})

    transport = httpx.MockTransport(handler)
    client = ComputeCoinClient(
        "https://api.test", access_token="tok123", transport=transport
    )
    with pytest.raises(ValidationError) as exc_info:
        client.create_job(
            workload_type="text-generation",
            model="echo-sim-v1",
            input={},
            gpu_count=1,
            estimated_minutes=5,
            max_cost="5.00",
            max_runtime_seconds=300,
        )
    err = exc_info.value
    assert err.status_code == 422
    # The server's reason code is surfaced -- not a crash, not a fake success.
    assert err.code == "invalid_input"
    assert err.body == {"detail": "invalid_input"}


def test_create_job_409_budget_exceeded_raises_conflict_with_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "budget_exceeded"})

    transport = httpx.MockTransport(handler)
    client = ComputeCoinClient(
        "https://api.test", access_token="tok123", transport=transport
    )
    with pytest.raises(ConflictError) as exc_info:
        client.create_job(
            workload_type="text-generation",
            model="echo-sim-v1",
            input={"prompt": "Hi"},
            gpu_count=1,
            estimated_minutes=5,
            max_cost="0.01",
            max_runtime_seconds=300,
        )
    assert exc_info.value.code == "budget_exceeded"


def test_create_job_success_returns_job_with_execution_mode():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201, json=_job_body(status="queued", execution_mode="simulated")
        )

    transport = httpx.MockTransport(handler)
    client = ComputeCoinClient(
        "https://api.test", access_token="tok123", transport=transport
    )
    job = client.create_job(
        workload_type="text-generation",
        model="echo-sim-v1",
        input={"prompt": "Hi"},
        gpu_count=1,
        estimated_minutes=5,
        max_cost="5.00",
        max_runtime_seconds=300,
    )
    assert job.id == "job_1"
    assert job.execution_mode == "simulated"
    # Money stays a string on the way out too.
    assert job.max_cost == "5.00"
    assert isinstance(job.max_cost, str)
    assert job.final_cost is None


def test_get_job_events_parses_fsm_history():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/jobs/job_1/events"
        return httpx.Response(
            200,
            json=[
                {
                    "job_id": "job_1",
                    "seq": 1,
                    "from_state": None,
                    "to_state": "created",
                    "actor": "customer",
                    "created_at": "2026-01-01T00:00:00Z",
                },
                {
                    "job_id": "job_1",
                    "seq": 2,
                    "from_state": "created",
                    "to_state": "validating",
                    "actor": "system",
                    "created_at": "2026-01-01T00:00:01Z",
                },
            ],
        )

    transport = httpx.MockTransport(handler)
    client = ComputeCoinClient(
        "https://api.test", access_token="tok123", transport=transport
    )
    events = client.get_job_events("job_1")
    assert len(events) == 2
    assert events[0].seq == 1
    assert events[0].to_state == "created"
    assert events[0].from_state is None
    assert events[1].to_state == "validating"


def test_cancel_job_returns_ack():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "job_id": "job_1",
                "status": "cancelled",
                "cancellation_requested": True,
            },
        )

    transport = httpx.MockTransport(handler)
    client = ComputeCoinClient(
        "https://api.test", access_token="tok123", transport=transport
    )
    ack = client.cancel_job("job_1")
    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/jobs/job_1/cancel"
    assert ack.job_id == "job_1"
    assert ack.cancellation_requested is True


def test_wait_for_job_polls_until_terminal_offline():
    states = iter(["running", "verifying", "completed"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_job_body(status=next(states)))

    transport = httpx.MockTransport(handler)
    client = ComputeCoinClient(
        "https://api.test", access_token="tok123", transport=transport
    )
    # poll=0 keeps the test instant and fully offline.
    job = client.wait_for_job("job_1", poll=0, timeout=5)
    assert job.status == "completed"


def test_get_artifact_download_url_returns_url():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/jobs/job_1/artifacts/art_1/download"
        return httpx.Response(
            200,
            json={"url": "https://store.test/signed/art_1", "expires_at": "2026-01-01T00:05:00Z"},
        )

    transport = httpx.MockTransport(handler)
    client = ComputeCoinClient(
        "https://api.test", access_token="tok123", transport=transport
    )
    dl = client.get_artifact_download_url("job_1", "art_1")
    assert dl.url == "https://store.test/signed/art_1"


# -- Phase 5: billing ----------------------------------------------------- #


def _auth_client(handler) -> ComputeCoinClient:
    transport = httpx.MockTransport(handler)
    return ComputeCoinClient(
        "https://api.test", access_token="tok123", transport=transport
    )


def test_get_billing_account_parses_money_strings():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "currency": "USDC_CREDITS",
                "available": "998.35",
                "reserved": "1.65",
                "balance": "1000.00",
            },
        )

    client = _auth_client(handler)
    account = client.get_billing_account()

    assert captured["method"] == "GET"
    assert captured["path"] == "/v1/billing/account"
    assert captured["auth"] == "Bearer tok123"
    # Every money field is a string, never coerced to float.
    assert account.available == "998.35"
    assert account.reserved == "1.65"
    assert account.balance == "1000.00"
    assert isinstance(account.available, str)
    assert account.currency == "USDC_CREDITS"


def test_purchase_credits_sends_amount_body_and_returns_account():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "currency": "USDC_CREDITS",
                "available": "1048.35",
                "reserved": "1.65",
                "balance": "1050.00",
            },
        )

    client = _auth_client(handler)
    account = client.purchase_credits("50.00")

    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/billing/credits/purchase"
    # Amount stays a decimal string in the request body.
    assert captured["body"] == {"amount": "50.00"}
    assert isinstance(captured["body"]["amount"], str)
    assert account.available == "1048.35"


def test_list_invoices_parses_estimated_vs_actual():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/invoices"
        return httpx.Response(
            200,
            json=[
                {
                    "invoice_id": "inv_1",
                    "job_id": "job_1",
                    "line_items": [
                        {"label": "gpu_cost", "amount": "1.50"},
                        {"label": "platform_fee", "amount": "0.15"},
                    ],
                    "estimated_total": "1.65",
                    "actual_total": "1.10",
                    "platform_fee": "0.10",
                    "currency": "USDC_CREDITS",
                    "status": "settled",
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ],
        )

    client = _auth_client(handler)
    invoices = client.list_invoices()

    assert len(invoices) == 1
    inv = invoices[0]
    # Estimated vs actual are tracked separately -- never conflated.
    assert inv.estimated_total == "1.65"
    assert inv.actual_total == "1.10"
    assert inv.estimated_total != inv.actual_total
    assert isinstance(inv.actual_total, str)
    assert inv.line_items[0].label == "gpu_cost"
    assert inv.line_items[0].amount == "1.50"
    assert inv.status == "settled"


def test_request_payout_409_insufficient_earnings_raises_conflict_with_code():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/provider/payouts"
        assert json.loads(request.content) == {"amount": "500.00"}
        return httpx.Response(409, json={"detail": "insufficient_earnings"})

    client = _auth_client(handler)
    with pytest.raises(ConflictError) as exc_info:
        client.request_payout("500.00")

    err = exc_info.value
    assert err.status_code == 409
    # The server's reason code is surfaced -- not a crash, not a fake payout.
    assert err.code == "insufficient_earnings"
    assert err.body == {"detail": "insufficient_earnings"}


def test_request_payout_success_returns_payout():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "payout_id": "po_1",
                "amount": "10.00",
                "currency": "USDC_CREDITS",
                "status": "requested",
                "created_at": "2026-01-01T00:00:00Z",
            },
        )

    client = _auth_client(handler)
    payout = client.request_payout("10.00")
    assert payout.payout_id == "po_1"
    assert payout.amount == "10.00"
    assert payout.status == "requested"


def test_purchase_credits_402_insufficient_credits_raises_payment_required():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"detail": "insufficient_credits"})

    client = _auth_client(handler)
    with pytest.raises(PaymentRequiredError) as exc_info:
        client.purchase_credits("50.00")
    err = exc_info.value
    assert err.status_code == 402
    assert err.code == "insufficient_credits"


def test_get_provider_earnings_parses_live_ledger_figures():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/provider/earnings"
        return httpx.Response(
            200,
            json={
                "currency": "USDC_CREDITS",
                "available": "12.34",
                "lifetime": "99.99",
                "recent": [
                    {
                        "entry_id": "e_1",
                        "transaction_id": "t_1",
                        "kind": "capture",
                        "direction": "credit",
                        "amount": "1.35",
                        "currency": "USDC_CREDITS",
                        "reference": "job:job_1",
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
            },
        )

    client = _auth_client(handler)
    earnings = client.get_provider_earnings()
    assert earnings.available == "12.34"
    assert earnings.lifetime == "99.99"
    assert isinstance(earnings.available, str)
    assert earnings.recent[0].amount == "1.35"
    assert earnings.recent[0].direction == "credit"
