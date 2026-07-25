"""A run emitting ModelUsage events writes matching ledger rows for the ctx user."""

from __future__ import annotations

import uuid

from agent_harness.core.events import InMemoryEventBus, ModelUsage
from agent_harness.core.models import Cost, Usage
import pytest

from penny.api.persistence.engine import create_web_schema
from penny.billing import metering
from penny.billing.session import BillingSession
from penny.billing.usage_subscriber import start_usage_subscriber_task
from penny.tenancy.context import RequestContext


def _ctx() -> RequestContext:
    return RequestContext(
        user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        household_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
    )


def _usage(cents: int) -> ModelUsage:
    return ModelUsage(
        model_name="gemini-3.6-flash",
        usage=Usage(input_tokens=100, output_tokens=50),
        cost=Cost(input_cost=cents / 100),
    )


@pytest.mark.asyncio
async def test_two_usage_events_write_two_ledger_rows(isolated_db: None) -> None:
    create_web_schema()
    ctx = _ctx()
    bus = InMemoryEventBus()
    task = start_usage_subscriber_task(bus, ctx)
    assert task is not None

    await bus.publish(_usage(30))
    await bus.publish(_usage(45))
    await bus.close()
    await task

    with BillingSession().begin(ctx) as s:
        assert metering.spend_cents(s, ctx) == 75
        from penny.billing.models import UsageEvent

        assert s.query(UsageEvent).count() == 2
