"""Tracing tests for :mod:`penny.observability.otel`.

A real OTEL ``TracerProvider`` backed by an in-memory exporter is injected so
we can assert the emitted span tree (names, GenAI/Langfuse attributes,
parentage, usage) without an OTLP round-trip. Covers the agent-loop subscriber
delegation, the categorizer helpers, and the enabled/disabled gate.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent_harness.core.events import (
    InMemoryEventBus,
    MessageEnd,
    MessageStart,
    ModelStart,
    RunEnd,
    RunStart,
    ToolExecEnd,
    ToolExecStart,
)
from agent_harness.core.models import Message, TextBlock, Usage
from agent_harness.core.tools import ToolResult
import pytest

from penny import observability
from penny.observability import otel as ot


@pytest.fixture
def exporter(monkeypatch):
    """Inject an in-memory-backed TracerProvider and force tracing on."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    monkeypatch.setattr(ot, "_enabled", True)
    monkeypatch.setattr(ot, "_provider", provider)
    monkeypatch.setenv("PENNY_LANGFUSE_ENVIRONMENT", "development")
    yield exp
    provider.shutdown()


def _assistant(text: str) -> Message:
    return Message(
        role="assistant",
        content=[TextBlock(text=text)],
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )


class _RunResult:
    def __init__(self, output):
        self.output = output


def _by_name(exp):
    spans = exp.get_finished_spans()
    by_id = {s.context.span_id: s for s in spans}
    tree = {}
    for s in spans:
        parent = by_id.get(s.parent.span_id) if s.parent else None
        tree[s.name] = (s, parent.name if parent else None)
    return spans, tree


async def test_agent_run_trace_via_subscriber(exporter):
    bus = InMemoryEventBus()
    task = observability.start_run_trace_task(
        bus, source="chat", session_id="conv-42", prompt="how much did I spend?"
    )
    assert task is not None

    await bus.publish(RunStart(run_id="r1", agent_name="penny", prompt="how much?"))
    await bus.publish(ModelStart(model_name="gemini-3.6-flash"))
    await bus.publish(MessageStart(message_id="m1"))
    await bus.publish(
        ToolExecStart(
            tool_call_id="c1", tool_name="run_sql", arguments={"sql": "select 1"}
        )
    )
    await bus.publish(
        ToolExecEnd(
            tool_call_id="c1",
            result=ToolResult(content=[], structured_content={"rows": [[1]]}),
        )
    )
    await bus.publish(
        MessageEnd(
            message_id="m1",
            final=_assistant("You spent $42."),
            usage=Usage(input_tokens=100, output_tokens=12),
        )
    )
    await bus.publish(
        RunEnd(
            run_id="r1",
            result=_RunResult("You spent $42."),
            usage=Usage(input_tokens=100, output_tokens=12),
            duration_ms=1234,
        )
    )
    await bus.close()
    await task

    _, tree = _by_name(exporter)
    assert "penny-agent-run" in tree
    assert "chat gemini-3.6-flash" in tree
    assert "execute_tool run_sql" in tree

    root, _ = tree["penny-agent-run"]
    assert root.attributes["session.id"] == "conv-42"
    assert root.attributes["langfuse.observation.input"] == "how much did I spend?"
    assert tuple(root.attributes["langfuse.trace.tags"]) == ("chat",)
    assert root.attributes["langfuse.environment"] == "development"

    gen, gen_parent = tree["chat gemini-3.6-flash"]
    assert gen_parent == "penny-agent-run"
    assert gen.attributes["gen_ai.request.model"] == "gemini-3.6-flash"
    assert gen.attributes["gen_ai.usage.input_tokens"] == 100
    assert gen.attributes["gen_ai.usage.output_tokens"] == 12

    tool, tool_parent = tree["execute_tool run_sql"]
    assert tool_parent == "penny-agent-run"
    assert tool.attributes["gen_ai.tool.name"] == "run_sql"


async def test_categorizer_span_nests_generations(exporter):
    with observability.categorizer_span(
        "categorize-transactions", input={"transaction_count": 2}, session_id="sync-1"
    ):
        with observability.llm_generation("categorize:m", model="m", input="p1") as gen:
            gen.update(output="r1", usage_details={"input": 3, "output": 4})

    _, tree = _by_name(exporter)
    assert "categorize-transactions" in tree
    cat, _ = tree["categorize-transactions"]
    assert cat.attributes["session.id"] == "sync-1"
    assert cat.attributes["langfuse.observation.input"] == '{"transaction_count": 2}'

    gen, parent = tree["categorize:m"]
    assert parent == "categorize-transactions"
    assert gen.attributes["gen_ai.request.model"] == "m"
    assert gen.attributes["gen_ai.usage.input_tokens"] == 3
    assert gen.attributes["gen_ai.usage.output_tokens"] == 4
    assert gen.attributes["gen_ai.completion"] == "r1"


def test_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(ot, "_enabled", False)
    monkeypatch.setattr(ot, "_provider", None)
    assert observability.start_run_trace_task(InMemoryEventBus(), source="chat") is None
    with observability.categorizer_span("x") as span:
        with observability.llm_generation("y", model=None) as gen:
            gen.update(output="z", usage_details={"input": 1, "output": 1})
        assert span is not None
    observability.flush()  # harmless when disabled


def test_enabled_gate(monkeypatch):
    monkeypatch.setattr(ot, "_enabled", None)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("PENNY_LANGFUSE_ENABLED", raising=False)
    assert observability.is_enabled() is False

    monkeypatch.setattr(ot, "_enabled", None)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-x")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-x")
    assert observability.is_enabled() is True

    monkeypatch.setattr(ot, "_enabled", None)
    monkeypatch.setenv("PENNY_LANGFUSE_ENABLED", "false")
    assert observability.is_enabled() is False


# --- canonical trace id / url ------------------------------------------------

_KNOWN_ID = "d2cb03fd69383bb608ed3348f60ffb35"


def test_current_trace_id_disabled_is_none(monkeypatch):
    """Strict no-op: id/url are None when tracing is off, with no SDK import."""
    monkeypatch.setattr(ot, "_enabled", False)

    def _boom():  # pragma: no cover - must never be reached
        raise AssertionError("get_client should not be called when disabled")

    monkeypatch.setitem(__import__("sys").modules, "langfuse", None)
    assert observability.current_trace_id() is None
    assert observability.current_trace_url() is None


def test_current_trace_id_from_sdk(monkeypatch):
    """When enabled, the id comes from the Langfuse SDK's get_current_trace_id."""
    monkeypatch.setattr(ot, "_enabled", True)

    class _FakeClient:
        def get_current_trace_id(self):
            return _KNOWN_ID

    fake = type("langfuse", (), {"get_client": staticmethod(lambda: _FakeClient())})
    monkeypatch.setitem(__import__("sys").modules, "langfuse", fake)

    assert observability.current_trace_id() == _KNOWN_ID


def test_current_trace_url_format(monkeypatch):
    """URL is host + /trace/{canonical id}; host honors LANGFUSE_HOST."""
    monkeypatch.setattr(ot, "_enabled", True)
    monkeypatch.setenv("LANGFUSE_HOST", "https://eu.cloud.langfuse.com/")

    class _FakeClient:
        def get_current_trace_id(self):
            return _KNOWN_ID

    fake = type("langfuse", (), {"get_client": staticmethod(lambda: _FakeClient())})
    monkeypatch.setitem(__import__("sys").modules, "langfuse", fake)

    assert (
        observability.current_trace_url()
        == f"https://eu.cloud.langfuse.com/trace/{_KNOWN_ID}"
    )


def test_current_trace_id_falls_back_to_otel_span(monkeypatch):
    """If the SDK yields no id, fall back to the active OTEL span context.

    The fallback equals the SDK id (both are ``format(trace_id, "032x")``).
    """
    monkeypatch.setattr(ot, "_enabled", True)

    class _FakeClient:
        def get_current_trace_id(self):
            return None  # e.g. SDK has no active span of its own

    fake = type("langfuse", (), {"get_client": staticmethod(lambda: _FakeClient())})
    monkeypatch.setitem(__import__("sys").modules, "langfuse", fake)

    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("span") as span:
        raw = format(span.get_span_context().trace_id, "032x")
        assert observability.current_trace_id() == raw
        assert observability.current_trace_url() == f"{ot._trace_host()}/trace/{raw}"


def test_current_trace_id_no_active_span_is_none(monkeypatch):
    """Enabled but no active span anywhere ⇒ None (no link)."""
    monkeypatch.setattr(ot, "_enabled", True)

    class _FakeClient:
        def get_current_trace_id(self):
            return None

    fake = type("langfuse", (), {"get_client": staticmethod(lambda: _FakeClient())})
    monkeypatch.setitem(__import__("sys").modules, "langfuse", fake)

    # No active recording span → INVALID_SPAN has trace_id 0 → None.
    assert observability.current_trace_id() is None
    assert observability.current_trace_url() is None
