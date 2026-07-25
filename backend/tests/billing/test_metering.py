"""Metering: ledger accrual, remaining runway, idempotent subsidy grant."""

from __future__ import annotations

import uuid

from agent_harness.core.events import ModelUsage
from agent_harness.core.models import Cost, Usage

from penny.api.persistence.engine import create_web_schema
from penny.billing import metering
from penny.billing.session import BillingSession
from penny.tenancy.context import RequestContext


def _ctx(user: str = "11111111-1111-1111-1111-111111111111") -> RequestContext:
    return RequestContext(
        user_id=uuid.UUID(user),
        household_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
    )


def _usage(cost_dollars: float) -> ModelUsage:
    return ModelUsage(
        model_name="gemini-3.6-flash",
        usage=Usage(input_tokens=1000, output_tokens=500),
        cost=Cost(input_cost=cost_dollars),
    )


def test_record_usage_decrements_remaining_exactly(isolated_db: None) -> None:
    create_web_schema()
    ctx = _ctx()
    billing = BillingSession()
    with billing.begin(ctx) as s:
        metering.grant_subsidy(s, ctx, cents=200)
    with billing.begin(ctx) as s:
        metering.record_usage(s, ctx, _usage(0.30))  # 30 cents
        metering.record_usage(s, ctx, _usage(0.45))  # 45 cents
    with billing.begin(ctx) as s:
        assert metering.spend_cents(s, ctx) == 75
        assert metering.remaining_cents(s, ctx) == 125


def test_grant_subsidy_is_idempotent(isolated_db: None) -> None:
    create_web_schema()
    ctx = _ctx()
    billing = BillingSession()
    with billing.begin(ctx) as s:
        assert metering.grant_subsidy(s, ctx, cents=200) is True
    with billing.begin(ctx) as s:
        assert metering.grant_subsidy(s, ctx, cents=200) is False  # no re-grant
    with billing.begin(ctx) as s:
        assert metering.granted_cents(s, ctx) == 200


def test_remaining_is_zero_grant_when_never_granted(isolated_db: None) -> None:
    create_web_schema()
    ctx = _ctx()
    billing = BillingSession()
    with billing.begin(ctx) as s:
        assert metering.granted_cents(s, ctx) == 0
        assert metering.remaining_cents(s, ctx) == 0


def test_usage_is_isolated_per_user(isolated_db: None) -> None:
    create_web_schema()
    alice = _ctx("11111111-1111-1111-1111-111111111111")
    bob = _ctx("33333333-3333-3333-3333-333333333333")
    billing = BillingSession()
    with billing.begin(alice) as s:
        metering.grant_subsidy(s, alice, cents=200)
        metering.record_usage(s, alice, _usage(0.50))
    with billing.begin(bob) as s:
        # Bob sees none of Alice's spend or grant (app-layer filter on SQLite).
        assert metering.spend_cents(s, bob) == 0
        assert metering.granted_cents(s, bob) == 0
