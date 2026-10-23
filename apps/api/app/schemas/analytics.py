"""Usage-analytics response schemas (Phase B4).

Money crosses the wire as a DECIMAL STRING (e.g. ``"12.34"``) alongside the
authoritative integer minor units. JSON numbers are IEEE doubles in most
clients, so emitting money as a float would silently round customer invoices.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, Field

# Dimensions a caller may group usage by. ``api_key`` is deliberately absent:
# nothing in the schema attributes a job to the API key that created it, so an
# api-key breakdown could only be guessed at.
USAGE_GROUPINGS = ("project", "model", "day", "total")


class UsageBucket(BaseModel):
    """One row of the breakdown."""

    key: str = Field(description="Grouping value: project id, model name, or ISO date.")
    label: str | None = Field(default=None, description="Human-readable name, when known.")
    job_count: int
    actual_seconds: int
    estimated_cost_minor: int
    estimated_cost: str
    actual_cost_minor: int
    actual_cost: str


class UsageSummary(BaseModel):
    """Totals for the requested window, plus the ledger cross-check."""

    job_count: int
    actual_seconds: int
    estimated_cost_minor: int
    estimated_cost: str
    actual_cost_minor: int
    actual_cost: str
    # Net customer spend over the same window straight from the ledger. It can
    # differ from metered cost (open holds, refunds), which is exactly why both
    # are reported rather than one being derived from the other.
    ledger_net_spend_minor: int
    ledger_net_spend: str


class UsageReport(BaseModel):
    customer_id: uuid.UUID
    project_id: uuid.UUID | None
    group_by: str
    start: dt.datetime | None
    end: dt.datetime | None
    currency: str
    summary: UsageSummary
    buckets: list[UsageBucket]
