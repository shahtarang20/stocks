"""ComputeCoin job quickstart (Phase 4).

Flow: create a text-generation job (model ``echo-sim-v1``) -> poll to a terminal
FSM state -> list artifacts -> fetch a signed download URL. Guarded by
``if __name__ == "__main__"`` so importing this module performs no network calls.
Run it against a live/staging API:

    COMPUTECOIN_BASE_URL=https://api.staging.example \
    COMPUTECOIN_EMAIL=dev@example.com COMPUTECOIN_PASSWORD=... \
    python examples/job_quickstart.py

HONESTY NOTE: on CPU-only dev/CI infrastructure (no GPU, no Docker) the backend
runs the in-process ``SimulatedExecutor``, so every job it produces is stamped
``execution_mode: "simulated"``. That output is a DETERMINISTIC transform -- it
is NOT real model inference, and the SDK surfaces the stamp rather than hiding
it. Money values (max_cost, final_cost) are decimal STRINGS, never floats.
"""

from __future__ import annotations

import os

from computecoin import (
    ComputeCoinClient,
    ConflictError,
    EndpointNotAvailableError,
    ValidationError,
)


def main() -> None:
    base_url = os.environ.get("COMPUTECOIN_BASE_URL", "https://api.computecoin.example")
    email = os.environ.get("COMPUTECOIN_EMAIL", "dev@example.com")
    password = os.environ.get("COMPUTECOIN_PASSWORD", "change-me")

    with ComputeCoinClient(base_url) as client:
        client.login(email, password)

        # 1. Submit a job. A validation / budget failure surfaces as a typed
        #    error carrying .code -- it never crashes or fabricates a success.
        try:
            job = client.create_job(
                workload_type="text-generation",
                model="echo-sim-v1",
                input={"prompt": "Hello, ComputeCoin!", "max_tokens": 64},
                gpu_count=1,
                estimated_minutes=5,
                max_cost="5.00",
                max_runtime_seconds=300,
                retry_policy={"max_retries": 1},
                idempotency_key="job-quickstart-demo-1",  # safe to retry
            )
        except ValidationError as exc:
            # invalid_input / model_not_supported
            print(f"job rejected ({exc.code}): {exc}")
            return
        except ConflictError as exc:
            # budget_exceeded
            print(f"job rejected ({exc.code}): {exc}")
            return
        except EndpointNotAvailableError as exc:
            print("jobs not available yet:", exc)
            return

        print(f"created job {job.id} (status: {job.status})")
        # execution_mode may only be stamped once the executor picks it up.
        if job.execution_mode:
            print(f"execution_mode: {job.execution_mode}")

        # 2. Poll to a terminal state (completed/failed/cancelled/timed_out).
        try:
            job = client.wait_for_job(job.id, poll=2.0, timeout=300.0)
        except TimeoutError as exc:
            print("timed out waiting for the job:", exc)
            return

        print(f"\nfinished: {job.status}  execution_mode={job.execution_mode}")
        if job.execution_mode == "simulated":
            print(
                "  NOTE: simulated output -- a deterministic transform on CPU-only "
                "infra, NOT real model inference."
            )
        if job.status != "completed":
            print(f"  error: {job.error_code} / {job.error_message}")
            return

        # 3. List artifacts and get a signed download URL for the first one.
        artifacts = client.get_job_artifacts(job.id)
        print(f"\n{len(artifacts)} artifact(s):")
        for art in artifacts:
            print(
                f"  {art.artifact_id}  kind={art.kind} size={art.size} "
                f"sha256={art.sha256}"
            )
        if artifacts:
            dl = client.get_artifact_download_url(job.id, artifacts[0].artifact_id)
            print(f"\ndownload URL (expires {dl.expires_at}): {dl.url}")


if __name__ == "__main__":
    main()
