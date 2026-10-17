"""ComputeCoin billing quickstart (Phase 5).

Flow: check the billing account balance -> top up simulated credits -> look at
the invoice for an existing (already-settled) job -> check provider earnings.
Guarded by ``if __name__ == "__main__"`` so importing this module performs no
network calls. Run it against a live/staging API:

    COMPUTECOIN_BASE_URL=https://api.staging.example \
    COMPUTECOIN_EMAIL=dev@example.com COMPUTECOIN_PASSWORD=... \
    COMPUTECOIN_JOB_ID=job_123 \
    python examples/billing_quickstart.py

HONESTY / MONEY NOTE: all amounts are decimal STRINGS (e.g. "50.00"), never
floats -- the SDK sends and keeps them verbatim so no precision is lost. The
currency "USDC_CREDITS" is a SIMULATED test credit: there is no real payment
rail (PAYMENT_PROVIDER_CONFIGURATION=simulated), the top-up below moves only
simulated credits, and an estimated amount is never reported as a received /
actual amount (estimated vs actual stay separate on the invoice).
"""

from __future__ import annotations

import os

from computecoin import (
    ComputeCoinClient,
    ConflictError,
    EndpointNotAvailableError,
    PaymentRequiredError,
)


def main() -> None:
    base_url = os.environ.get("COMPUTECOIN_BASE_URL", "https://api.computecoin.example")
    email = os.environ.get("COMPUTECOIN_EMAIL", "dev@example.com")
    password = os.environ.get("COMPUTECOIN_PASSWORD", "change-me")
    job_id = os.environ.get("COMPUTECOIN_JOB_ID")  # an existing, settled job

    with ComputeCoinClient(base_url) as client:
        client.login(email, password)

        try:
            # 1. Check the current balance. available = spendable, reserved =
            #    current holds, balance = available + reserved (all strings).
            account = client.get_billing_account()
            print(
                f"balance: available={account.available} "
                f"reserved={account.reserved} total={account.balance} "
                f"{account.currency} (simulated)"
            )

            # 2. Top up simulated credits. The amount is a decimal string.
            account = client.purchase_credits("50.00")
            print(f"after top-up: available={account.available} {account.currency}")

            # 3. Inspect the invoice for an existing settled job. Estimated vs
            #    actual are reported separately -- never conflated.
            if job_id:
                for inv in client.list_invoices():
                    if inv.job_id == job_id:
                        print(
                            f"invoice {inv.invoice_id}: estimated={inv.estimated_total} "
                            f"actual={inv.actual_total} fee={inv.platform_fee} "
                            f"status={inv.status}"
                        )
                        for item in inv.line_items:
                            print(f"    {item.label}: {item.amount}")
                        break
            else:
                print("set COMPUTECOIN_JOB_ID to inspect a settled job's invoice")

            # 4. Provider view: LIVE accrued earnings from the ledger. A payout
            #    beyond `available` is refused with 409 insufficient_earnings.
            try:
                earnings = client.get_provider_earnings()
                print(
                    f"provider earnings: available={earnings.available} "
                    f"lifetime={earnings.lifetime} {earnings.currency} (simulated)"
                )
            except EndpointNotAvailableError:
                print("provider earnings not available for this account")

        except PaymentRequiredError as exc:
            # 402: insufficient_credits -- never an overspend, never a fake top-up.
            print(f"payment required ({exc.code}): {exc}")
        except ConflictError as exc:
            # 409: e.g. insufficient_earnings on a payout request.
            print(f"conflict ({exc.code}): {exc}")
        except EndpointNotAvailableError as exc:
            print("billing not available yet:", exc)


if __name__ == "__main__":
    main()
