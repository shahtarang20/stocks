"""Phase 6 — inference endpoint tests.

Tests the OpenAI-compatible ``/v1/chat/completions`` and ``/v1/embeddings``
endpoints end-to-end through the ASGI test client, using the same SQLite
in-memory database as the rest of the test suite.

Setup: creates a user, an approved simulated model version, and an online
provider machine so the inference service can resolve and route requests.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.models.model_registry import Model, ModelVersion
from app.models.provider import (
    HardwareInventory,
    Provider,
    ProviderCapability,
    ProviderMachine,
)
from app.models.user import User
from tests.helpers import auth_headers, register_verify_login


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def authed_user(client: AsyncClient) -> dict:
    """Register, verify and login a customer user.  Returns the token response."""
    return await register_verify_login(client, "inference-tester@example.com")


@pytest_asyncio.fixture
async def setup_model_and_machine(client: AsyncClient, authed_user: dict):
    """Seed an approved model version and an online machine in the DB."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # --- Provider user + provider ---
        provider_data = await register_verify_login(
            client, "provider-inference@example.com"
        )
        user_result = await session.execute(
            select(User).where(User.email == "provider-inference@example.com")
        )
        provider_user = user_result.scalar_one()

        provider = Provider(
            user_id=provider_user.id,
            display_name="TestInferenceProvider",
            status="active",
        )
        session.add(provider)
        await session.flush()

        # --- Machine ---
        import datetime as dt

        machine = ProviderMachine(
            provider_id=provider.id,
            hostname="inference-gpu-01",
            agent_version="0.1.0",
            status="registered",
            availability="online",
            asking_price_per_hour=1.50,
            max_concurrent_jobs=4,
            region="us-east-1",
            signing_key="test-signing-key-inference",
            secret_hash="test-secret-hash-inference",
            last_heartbeat_at=dt.datetime.now(dt.timezone.utc),
        )
        session.add(machine)
        await session.flush()

        # --- Hardware ---
        hw = HardwareInventory(
            machine_id=machine.id,
            verification_status="detected",
            cpu_model="AMD EPYC 7763",
            cpu_cores=16,
            ram_mb=65536,
            disk_gb=500,
            gpu_model="NVIDIA A100",
            gpu_count=2,
            vram_mb=81920,
        )
        session.add(hw)

        # --- Capability ---
        cap = ProviderCapability(
            machine_id=machine.id,
            supported_workloads=["text-generation", "batch-inference", "embeddings"],
            verification_status="detected",
        )
        session.add(cap)

        # --- Model + Version ---
        model = Model(
            name="echo-sim",
            display_name="Echo Simulator",
            trust_status="approved",
        )
        session.add(model)
        await session.flush()

        model_version = ModelVersion(
            model_id=model.id,
            version_tag="echo-sim-v1",
            runtime="simulated",
            supported_workloads=["text-generation", "batch-inference", "embeddings"],
            trust_status="approved",
        )
        session.add(model_version)
        await session.commit()


# ---------------------------------------------------------------------------
# Chat Completions Tests
# ---------------------------------------------------------------------------
class TestChatCompletions:
    """POST /v1/chat/completions"""

    @pytest.mark.asyncio
    async def test_oversized_prompt_rejected(
        self, client: AsyncClient, authed_user: dict, setup_model_and_machine
    ):
        """An over-long message content is rejected at validation (DoS guard, M2)."""
        headers = auth_headers(authed_user["access_token"])
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "echo-sim-v1",
                "messages": [{"role": "user", "content": "x" * 200_000}],
            },
            headers=headers,
        )
        assert resp.status_code == 422

    async def test_basic_chat_completion(
        self, client: AsyncClient, authed_user: dict, setup_model_and_machine
    ):
        """A valid request returns an OpenAI-compatible chat completion."""
        headers = auth_headers(authed_user["access_token"])
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "echo-sim-v1",
                "messages": [
                    {"role": "user", "content": "Hello, ComputeCoin!"}
                ],
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()

        # OpenAI-compatible shape.
        assert data["object"] == "chat.completion"
        assert data["model"] == "echo-sim-v1"
        assert isinstance(data["id"], str)
        assert data["id"].startswith("chatcmpl-")
        assert isinstance(data["created"], int)

        # Choices.
        assert len(data["choices"]) == 1
        choice = data["choices"][0]
        assert choice["index"] == 0
        assert choice["message"]["role"] == "assistant"
        assert "Hello, ComputeCoin!" in choice["message"]["content"]
        assert choice["finish_reason"] == "stop"

        # Usage.
        usage = data["usage"]
        assert usage["prompt_tokens"] > 0
        assert usage["completion_tokens"] > 0
        assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]

        # ComputeCoin extension — execution_mode is always surfaced.
        cc = data["computecoin"]
        assert cc["execution_mode"] == "simulated"
        assert cc["model_version"] == "echo-sim-v1"

    @pytest.mark.asyncio
    async def test_chat_multiple_messages(
        self, client: AsyncClient, authed_user: dict, setup_model_and_machine
    ):
        """Multi-turn conversation works and echoes the last user message."""
        headers = auth_headers(authed_user["access_token"])
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "echo-sim-v1",
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "First message"},
                    {"role": "assistant", "content": "I heard you."},
                    {"role": "user", "content": "Second message"},
                ],
            },
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        # Should echo "Second message" (the last user message).
        assert "Second message" in data["choices"][0]["message"]["content"]

    @pytest.mark.asyncio
    async def test_chat_n_parameter(
        self, client: AsyncClient, authed_user: dict, setup_model_and_machine
    ):
        """n > 1 produces multiple choices."""
        headers = auth_headers(authed_user["access_token"])
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "echo-sim-v1",
                "messages": [{"role": "user", "content": "test"}],
                "n": 3,
            },
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["choices"]) == 3
        for i, choice in enumerate(data["choices"]):
            assert choice["index"] == i

    @pytest.mark.asyncio
    async def test_chat_model_not_found(
        self, client: AsyncClient, authed_user: dict, setup_model_and_machine
    ):
        """Unknown model returns a structured 404 error."""
        headers = auth_headers(authed_user["access_token"])
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "nonexistent-model",
                "messages": [{"role": "user", "content": "hello"}],
            },
            headers=headers,
        )
        assert resp.status_code == 404
        data = resp.json()
        assert "error" in data["detail"]
        assert data["detail"]["error"]["code"] == "model_not_found"

    @pytest.mark.asyncio
    async def test_chat_streaming_not_supported(
        self, client: AsyncClient, authed_user: dict, setup_model_and_machine
    ):
        """stream=true returns a 400 error (not yet supported)."""
        headers = auth_headers(authed_user["access_token"])
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "echo-sim-v1",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
            headers=headers,
        )
        assert resp.status_code == 400
        data = resp.json()
        assert "error" in data["detail"]
        assert data["detail"]["error"]["code"] == "streaming_not_supported"

    @pytest.mark.asyncio
    async def test_chat_requires_auth(
        self, client: AsyncClient, setup_model_and_machine
    ):
        """Unauthenticated requests are rejected."""
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "echo-sim-v1",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_chat_empty_messages_rejected(
        self, client: AsyncClient, authed_user: dict, setup_model_and_machine
    ):
        """Empty messages array is rejected by validation."""
        headers = auth_headers(authed_user["access_token"])
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "echo-sim-v1", "messages": []},
            headers=headers,
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_chat_simulated_stamp(
        self, client: AsyncClient, authed_user: dict, setup_model_and_machine
    ):
        """The simulated prefix is always in the response content."""
        headers = auth_headers(authed_user["access_token"])
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "echo-sim-v1",
                "messages": [{"role": "user", "content": "prove this is real"}],
            },
            headers=headers,
        )
        assert resp.status_code == 200
        content = resp.json()["choices"][0]["message"]["content"]
        assert "[ComputeCoin Simulated]" in content
        assert "NOT real model inference" in content

    @pytest.mark.asyncio
    async def test_chat_deterministic(
        self, client: AsyncClient, authed_user: dict, setup_model_and_machine
    ):
        """Same input produces the same simulated output (deterministic)."""
        headers = auth_headers(authed_user["access_token"])
        payload = {
            "model": "echo-sim-v1",
            "messages": [{"role": "user", "content": "deterministic test"}],
        }
        resp1 = await client.post("/v1/chat/completions", json=payload, headers=headers)
        resp2 = await client.post("/v1/chat/completions", json=payload, headers=headers)
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        c1 = resp1.json()["choices"][0]["message"]["content"]
        c2 = resp2.json()["choices"][0]["message"]["content"]
        assert c1 == c2


# ---------------------------------------------------------------------------
# Embedding Tests
# ---------------------------------------------------------------------------
class TestEmbeddings:
    """POST /v1/embeddings"""

    @pytest.mark.asyncio
    async def test_basic_embedding(
        self, client: AsyncClient, authed_user: dict, setup_model_and_machine
    ):
        """A valid request returns an OpenAI-compatible embedding response."""
        headers = auth_headers(authed_user["access_token"])
        resp = await client.post(
            "/v1/embeddings",
            json={"model": "echo-sim-v1", "input": "Hello, world!"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()

        # Shape.
        assert data["object"] == "list"
        assert data["model"] == "echo-sim-v1"
        assert len(data["data"]) == 1

        emb = data["data"][0]
        assert emb["object"] == "embedding"
        assert emb["index"] == 0
        assert isinstance(emb["embedding"], list)
        assert len(emb["embedding"]) == 256  # EMBEDDING_DIM
        # Check they are floats.
        assert all(isinstance(v, (int, float)) for v in emb["embedding"])

        # Usage.
        assert data["usage"]["prompt_tokens"] > 0
        assert data["usage"]["total_tokens"] > 0

        # ComputeCoin extension.
        assert data["computecoin"]["execution_mode"] == "simulated"

    @pytest.mark.asyncio
    async def test_batch_embedding(
        self, client: AsyncClient, authed_user: dict, setup_model_and_machine
    ):
        """Batch input (list of strings) returns one embedding per item."""
        headers = auth_headers(authed_user["access_token"])
        inputs = ["Hello", "World", "ComputeCoin"]
        resp = await client.post(
            "/v1/embeddings",
            json={"model": "echo-sim-v1", "input": inputs},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]) == len(inputs)
        for i, emb in enumerate(data["data"]):
            assert emb["index"] == i
            assert len(emb["embedding"]) == 256

    @pytest.mark.asyncio
    async def test_embedding_deterministic(
        self, client: AsyncClient, authed_user: dict, setup_model_and_machine
    ):
        """Same input produces the same embedding vector (deterministic hash)."""
        headers = auth_headers(authed_user["access_token"])
        payload = {"model": "echo-sim-v1", "input": "deterministic embedding"}
        resp1 = await client.post("/v1/embeddings", json=payload, headers=headers)
        resp2 = await client.post("/v1/embeddings", json=payload, headers=headers)
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        emb1 = resp1.json()["data"][0]["embedding"]
        emb2 = resp2.json()["data"][0]["embedding"]
        assert emb1 == emb2

    @pytest.mark.asyncio
    async def test_embedding_different_inputs_different_vectors(
        self, client: AsyncClient, authed_user: dict, setup_model_and_machine
    ):
        """Different inputs produce different embeddings."""
        headers = auth_headers(authed_user["access_token"])
        resp1 = await client.post(
            "/v1/embeddings",
            json={"model": "echo-sim-v1", "input": "apple"},
            headers=headers,
        )
        resp2 = await client.post(
            "/v1/embeddings",
            json={"model": "echo-sim-v1", "input": "banana"},
            headers=headers,
        )
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        emb1 = resp1.json()["data"][0]["embedding"]
        emb2 = resp2.json()["data"][0]["embedding"]
        assert emb1 != emb2

    @pytest.mark.asyncio
    async def test_embedding_l2_normalized(
        self, client: AsyncClient, authed_user: dict, setup_model_and_machine
    ):
        """Embedding vectors are approximately L2-normalized."""
        import math

        headers = auth_headers(authed_user["access_token"])
        resp = await client.post(
            "/v1/embeddings",
            json={"model": "echo-sim-v1", "input": "normalization check"},
            headers=headers,
        )
        assert resp.status_code == 200
        vec = resp.json()["data"][0]["embedding"]
        norm = math.sqrt(sum(x * x for x in vec))
        # Should be very close to 1.0 (within floating-point tolerance).
        assert abs(norm - 1.0) < 0.01

    @pytest.mark.asyncio
    async def test_embedding_model_not_found(
        self, client: AsyncClient, authed_user: dict, setup_model_and_machine
    ):
        """Unknown model returns 404."""
        headers = auth_headers(authed_user["access_token"])
        resp = await client.post(
            "/v1/embeddings",
            json={"model": "nonexistent-embedding-model", "input": "hello"},
            headers=headers,
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_embedding_requires_auth(
        self, client: AsyncClient, setup_model_and_machine
    ):
        """Unauthenticated embedding requests are rejected."""
        resp = await client.post(
            "/v1/embeddings",
            json={"model": "echo-sim-v1", "input": "hello"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_embedding_empty_input_rejected(
        self, client: AsyncClient, authed_user: dict, setup_model_and_machine
    ):
        """Empty input array is rejected with 400."""
        headers = auth_headers(authed_user["access_token"])
        resp = await client.post(
            "/v1/embeddings",
            json={"model": "echo-sim-v1", "input": []},
            headers=headers,
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["detail"]["error"]["code"] == "empty_input"

    @pytest.mark.asyncio
    async def test_embedding_batch_cap_exceeded(
        self, client: AsyncClient, authed_user: dict, setup_model_and_machine
    ):
        """Batch input exceeding max allowed (100) is rejected with 400."""
        headers = auth_headers(authed_user["access_token"])
        resp = await client.post(
            "/v1/embeddings",
            json={"model": "echo-sim-v1", "input": ["item"] * 101},
            headers=headers,
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["detail"]["error"]["code"] == "batch_size_exceeded"

    @pytest.mark.asyncio
    async def test_rate_limiting_enforced(
        self, client: AsyncClient, authed_user: dict, setup_model_and_machine, monkeypatch
    ):
        """Exceeding INFERENCE_RATE_LIMIT returns 429."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "INFERENCE_RATE_LIMIT", 2)
        headers = auth_headers(authed_user["access_token"])
        # Send 2 requests (allowed)
        for _ in range(2):
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "echo-sim-v1", "messages": [{"role": "user", "content": "hi"}]},
                headers=headers,
            )
            assert resp.status_code == 200

        # 3rd request should trigger 429
        resp3 = await client.post(
            "/v1/chat/completions",
            json={"model": "echo-sim-v1", "messages": [{"role": "user", "content": "hi"}]},
            headers=headers,
        )
        assert resp3.status_code == 429
        data = resp3.json()
        assert data["detail"]["error"]["code"] == "rate_limit_exceeded"


# ---------------------------------------------------------------------------
# Simulated execution honesty
# ---------------------------------------------------------------------------
class TestSimulatedHonesty:
    """Verify that simulated responses are always honestly stamped."""

    @pytest.mark.asyncio
    async def test_chat_never_claims_hardware(
        self, client: AsyncClient, authed_user: dict, setup_model_and_machine
    ):
        """execution_mode is 'simulated', never 'hardware', for simulated models."""
        headers = auth_headers(authed_user["access_token"])
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "echo-sim-v1",
                "messages": [{"role": "user", "content": "test"}],
            },
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["computecoin"]["execution_mode"] == "simulated"
        # The system_fingerprint should indicate ComputeCoin.
        assert "computecoin" in resp.json()["system_fingerprint"].lower()

    @pytest.mark.asyncio
    async def test_embedding_never_claims_hardware(
        self, client: AsyncClient, authed_user: dict, setup_model_and_machine
    ):
        """execution_mode is 'simulated' for embeddings too."""
        headers = auth_headers(authed_user["access_token"])
        resp = await client.post(
            "/v1/embeddings",
            json={"model": "echo-sim-v1", "input": "test"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["computecoin"]["execution_mode"] == "simulated"
