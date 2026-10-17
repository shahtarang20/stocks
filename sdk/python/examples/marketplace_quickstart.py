"""ComputeCoin marketplace quickstart (Phase 3).

Flow: search machines -> create a quote -> print the itemized breakdown ->
reserve. Guarded by ``if __name__ == "__main__"`` so importing this module
performs no network calls. Run it against a live/staging API:

    COMPUTECOIN_BASE_URL=https://api.staging.example \
    COMPUTECOIN_EMAIL=dev@example.com COMPUTECOIN_PASSWORD=... \
    python examples/marketplace_quickstart.py

Money values (price_per_hour, gpu_cost, platform_fee, total, ...) are decimal
STRINGS -- the SDK never coerces them to float. Credits ("USDC_CREDITS") are
simulated test credits in Phase 3; there is no real payment rail.
"""

from __future__ import annotations

import os

from computecoin import (
    ComputeCoinClient,
    ConflictError,
    EndpointNotAvailableError,
)


def main() -> None:
    base_url = os.environ.get("COMPUTECOIN_BASE_URL", "https://api.computecoin.example")
    email = os.environ.get("COMPUTECOIN_EMAIL", "dev@example.com")
    password = os.environ.get("COMPUTECOIN_PASSWORD", "change-me")

    with ComputeCoinClient(base_url) as client:
        # 1. Search is PUBLIC -- no auth needed.
        page = client.search_machines(
            workload="text-generation",
            max_price_per_hour="5.00",
            page_size=20,
        )
        print(f"found {page.total} machine(s)")
        if not page.items:
            print("no machines available; nothing to quote")
            return

        machine = page.items[0]
        rating = (
            machine.reliability.success_rate
            if machine.reliability and machine.reliability.success_rate is not None
            else "No history yet"
        )
        print(
            f"machine {machine.machine_id}: {machine.gpu_model} "
            f"@ {machine.price_per_hour} {machine.currency}/hr "
            f"({machine.available_slots} slot(s), reliability: {rating})"
        )

        # 2. Creating a quote needs a customer token.
        client.login(email, password)

        try:
            quote = client.create_quote(
                machine_id=machine.machine_id,
                workload_type="text-generation",
                estimated_minutes=60,
                gpu_count=1,
                max_authorized_spend="10.00",
            )
        except EndpointNotAvailableError as exc:
            print("quotes not available yet:", exc)
            return
        except ConflictError as exc:
            # e.g. budget_exceeded
            print(f"could not quote ({exc.code}): {exc}")
            return

        b = quote.breakdown
        print(f"\nQuote {quote.quote_id} (expires {quote.expires_at}):")
        print(f"  price/hour     {b.price_per_hour} {quote.currency}")
        print(f"  est. hours     {b.estimated_hours}")
        print(f"  gpu cost       {b.gpu_cost}")
        print(f"  platform fee   {b.platform_fee} (rate {b.platform_fee_rate})")
        print(f"  TOTAL          {b.total} {quote.currency}  (simulated credits)")

        # 3. Reserve -- a 404/409 must print a clear message, never crash.
        try:
            reservation = client.reserve_quote(quote.quote_id)
            print(
                f"\nreserved: {reservation.reservation_id} "
                f"({reservation.slots} slot(s), expires {reservation.expires_at})"
            )
        except ConflictError as exc:
            # no_capacity / quote_expired / quote_already_reserved / budget_exceeded
            print(f"\ncould not reserve ({exc.code}): {exc}")
        except EndpointNotAvailableError as exc:
            print("\nreservations not available yet:", exc)


if __name__ == "__main__":
    main()
