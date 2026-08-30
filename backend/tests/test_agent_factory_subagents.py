"""``.agent/agents/*.md`` definitions become callable tools on the agent.

Mirrors the skill registry (``load_skill_registry``): ``SubagentRegistry``
reads ``.agent/agents/`` (no per-user agents dir), and
:func:`penny.agent_factory._subagent_toolset` turns each definition into one
tool via ``build_agent_tool``. ``.agent/agents/`` currently holds no
definitions, so these pin two things: (1) the registry is empty until one is
authored, and (2) a definition that does exist produces a real, named tool.
"""

from __future__ import annotations

from agent_harness.core.agent import Agent
from agent_harness.core.subagents import AgentDefinition, SubagentRegistry
from agent_harness.sessions.inmemory import InMemorySession

from penny.agent_factory import _subagent_toolset, load_subagent_registry
from tests.test_reminder_e2e import _FakeModel


def test_load_subagent_registry_is_empty_until_definitions_are_authored() -> None:
    # No backend/.agent/agents/ directory exists yet — same starting point
    # the skill registry had before its first skill.
    assert load_subagent_registry().list_agents() == []


async def test_a_defined_subagent_becomes_a_named_callable_tool(tmp_path) -> None:
    agent = Agent(
        name="penny-test",
        model=_FakeModel(),
        toolsets=[],
        session=InMemorySession(session_id="subagent-test"),
    )
    definition = AgentDefinition(
        name="researcher",
        description="Digs into a single merchant's history.",
        initial_prompt="Research the given merchant.",
        body_path=tmp_path / "researcher.md",
    )
    registry = SubagentRegistry(agents={"researcher": definition})

    toolset = _subagent_toolset(agent, registry)
    tools = await toolset.list_tools(ctx=None)

    assert toolset.name == "subagents"
    assert [t.name for t in tools] == ["researcher"]
    assert tools[0].description == definition.description
