"""ComputeCoin Python SDK quickstart.

Guarded by ``if __name__ == "__main__"`` so importing this module performs no
network calls. Run it directly against a live/staging API:

    COMPUTECOIN_BASE_URL=https://api.staging.example python examples/quickstart.py
"""

from __future__ import annotations

import os

from computecoin import ComputeCoinClient, EndpointNotAvailableError


def main() -> None:
    base_url = os.environ.get("COMPUTECOIN_BASE_URL", "https://api.computecoin.example")
    email = os.environ.get("COMPUTECOIN_EMAIL", "dev@example.com")
    password = os.environ.get("COMPUTECOIN_PASSWORD", "change-me")

    with ComputeCoinClient(base_url) as client:
        # Auth
        client.register(email, password)
        pair = client.login(email, password)
        print("logged in; access token len:", len(pair.access_token))

        # Projects + API keys
        project = client.create_project(name="quickstart")
        key = client.create_api_key(project.id, name="default")
        print("project:", project.id, "api key prefix:", key.prefix)

        # OpenAI-compatible inference may not be live yet.
        try:
            completion = client.chat_completions(
                model="llama-3.1-8b",
                messages=[{"role": "user", "content": "Hello!"}],
            )
            print("completion:", completion.choices[0])
        except EndpointNotAvailableError as exc:
            # The SDK never fabricates a result; it tells you the feature isn't live.
            print("chat not available yet:", exc)


if __name__ == "__main__":
    main()
