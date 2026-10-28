"""Chat backend on the Claude Agent SDK. The model gets only the gateway tools: all built-in
tools (files, shell, web) are disabled and no user/project settings are loaded."""

import json
import tempfile
from collections.abc import AsyncIterator
from functools import cache

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from fin.agent.session import Session, tool_specs
from fin.importers.llm_categorize import PRIVATE_ENV
from fin.privacy.redact import strip_figures

SERVER = "fin"

SYSTEM = """You are a careful analyst for one person's finances. You work blind: you never see \
amounts, balances or account numbers, only names, labels, dates and the shape of results.

How to work:
1. Call schema once if unsure what exists, then run_query to compute a result locally. It returns a \
handle (r1, r2, ...) with labels and columns, never numbers.
2. Show results with make_chart, make_table and say. say takes a template whose {{placeholders}} are \
filled with real numbers locally, e.g. {{r1.sum.total}}, {{r1.sum.change_pct}}, {{r1.sum[Expenses:Food]}}.
3. Never write numbers or amounts in your own replies; you cannot know them. Any figure in your text \
is removed. Put every figure inside say templates or tables. Keep your own text to one short sentence.
4. If a result lists partial_periods, mention that those periods are incomplete. In placeholders, \
first/last/change/avg already skip them. You cannot see which period a figure belongs to, so never \
describe a figure's period in words unless it comes from a labelled placeholder like {{r1.sum[2026-Q3]}}.
5. For "top N", "biggest increase" or "what grew most" questions: use order_by and limit, and for \
growth use compare (presets like ytd_vs_prior_year, or explicit months) which gives current, baseline, \
change and change_pct per group. Name the winners with rank placeholders, e.g. \
{{r1.label[#1]}} grew by {{r1.change[#1]}} ({{r1.change_pct[#1]}}). The rows are in the order you asked \
for, so rank 1 is the first row. Respect any warnings the result lists (incomplete or pre-data periods).
6. Spend queries exclude major purchases, taxes and payroll deductions by default (scope: spend). \
Use scope major or all when asked.
Only use the provided tools."""


def _server(session: Session):
    tools = []
    for spec in tool_specs():

        async def handler(args, _name=spec["name"]):
            return {
                "content": [
                    {"type": "text", "text": json.dumps(session.call(_name, args), default=str)}
                ]
            }

        tools.append(tool(spec["name"], spec["description"], spec["input_schema"])(handler))
    return create_sdk_mcp_server(SERVER, tools=tools)


@cache
def _workdir() -> str:
    """One empty directory per process: the CLI files a session under its working directory, so
    `resume` only finds the conversation again from the same one."""
    return tempfile.mkdtemp(prefix="fin-agent-")


def build_options(session: Session, state: dict, max_turns: int = 12) -> ClaudeAgentOptions:
    """Model and effort come from `state["agent"]` (set from Settings per message); with none, the
    SDK's own defaults apply."""
    agent = state.get("agent") or {}
    return ClaudeAgentOptions(
        system_prompt=SYSTEM,
        mcp_servers={SERVER: _server(session)},
        strict_mcp_config=True,
        tools=[],
        allowed_tools=[f"mcp__{SERVER}__{s['name']}" for s in tool_specs()],
        setting_sources=[],
        max_turns=max_turns,
        resume=state.get("sdk_session"),
        cwd=_workdir(),
        model=agent.get("model"),
        effort=agent.get("effort"),
        env=PRIVATE_ENV,
    )


class ClaudeSdkBackend:
    async def run(self, message: str, session: Session, state: dict) -> AsyncIterator[dict]:
        async for msg in query(prompt=message, options=build_options(session, state)):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        yield {"type": "assistant", "text": strip_figures(block.text)}
                    elif isinstance(block, ToolUseBlock):
                        yield {"type": "tool", "name": block.name.removeprefix(f"mcp__{SERVER}__")}
            elif isinstance(msg, ResultMessage):
                state["sdk_session"] = msg.session_id
                if msg.is_error:
                    yield {
                        "type": "error",
                        "message": strip_figures(str(msg.result or "model error"))[:300],
                    }
