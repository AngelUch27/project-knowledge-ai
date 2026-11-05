"""Tool-using agent loop on top of Gemini function calling.

The model decides which tools to call and in what order (search, read a
document, list the library, import a repository...). Read-only tools run at
once; *action* tools (anything that writes) are only executed when the caller
passed a matching approval, otherwise the loop stops and reports the pending
action so the user can confirm it. The loop is bounded by ``max_steps``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from typing import Any, Callable

from google.genai.types import (
    AutomaticFunctionCallingConfig,
    Content,
    FunctionDeclaration,
    GenerateContentConfig,
    Part,
    Tool,
)

logger = logging.getLogger(__name__)

MAX_AGENT_STEPS = 6
MAX_TOOL_RESULT_CHARS = 20_000


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[[dict[str, Any]], dict[str, Any]]
    action: bool = False  # True: modifies data, needs the user's confirmation
    confirm_text: Callable[[dict[str, Any]], str] | None = None


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class AgentStep:
    tool: str
    args: dict[str, Any]
    summary: str


@dataclass
class AgentResult:
    answer: str
    steps: list[AgentStep] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    pending_action: ToolCall | None = None
    pending_description: str | None = None


class Toolbox:
    def __init__(self, specs: list[ToolSpec]) -> None:
        self._specs = {spec.name: spec for spec in specs}

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def declarations(self) -> list[FunctionDeclaration]:
        return [
            FunctionDeclaration(name=s.name, description=s.description, parameters_json_schema=s.parameters)
            for s in self._specs.values()
        ]


def _is_approved(call: ToolCall, approved: list[ToolCall]) -> bool:
    same_name = [a for a in approved if a.name == call.name]
    if any(_normalized(a.args) == _normalized(call.args) for a in same_name):
        return True
    # The model may phrase the arguments slightly differently on the confirmed
    # run (e.g. trailing slash); one approval for that tool is still a clear yes.
    return len(same_name) == 1


def _normalized(args: dict[str, Any]) -> str:
    return json.dumps({k: str(v).strip().rstrip("/").lower() for k, v in sorted(args.items())}, sort_keys=True)


def _truncate(result: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(result, ensure_ascii=False, default=str)
    if len(encoded) <= MAX_TOOL_RESULT_CHARS:
        return result
    return {"truncated": True, "content": encoded[:MAX_TOOL_RESULT_CHARS]}


def _summarize(name: str, args: dict[str, Any], result: dict[str, Any]) -> str:
    if "error" in result:
        return f"{name} failed: {result['error']}"
    if "summary" in result:
        return str(result["summary"])
    return f"{name}({', '.join(f'{k}={v!r}' for k, v in args.items())})"


def run_agent(
    client,
    model: str,
    system_instruction: str,
    history: list[Content],
    question: str,
    toolbox: Toolbox,
    approved_actions: list[ToolCall] | None = None,
    max_steps: int = MAX_AGENT_STEPS,
) -> AgentResult:
    approved_actions = approved_actions or []
    result = AgentResult(answer="")
    seen_sources: set[str] = set()
    contents = list(history) + [Content(role="user", parts=[Part(text=question)])]
    tool_config = GenerateContentConfig(
        temperature=0.2,
        system_instruction=system_instruction,
        tools=[Tool(function_declarations=toolbox.declarations())],
        automatic_function_calling=AutomaticFunctionCallingConfig(disable=True),
    )

    for _ in range(max_steps):
        response = client.models.generate_content(model=model, contents=contents, config=tool_config)
        calls = list(response.function_calls or [])
        if not calls:
            result.answer = (response.text or "").strip()
            return result

        contents.append(response.candidates[0].content)
        response_parts = []
        for call in calls:
            tool_call = ToolCall(name=call.name, args=dict(call.args or {}))
            spec = toolbox.get(tool_call.name)
            if spec is None:
                tool_result: dict[str, Any] = {"error": f"Unknown tool: {tool_call.name}"}
            elif spec.action and not _is_approved(tool_call, approved_actions):
                result.pending_action = tool_call
                result.pending_description = (
                    spec.confirm_text(tool_call.args) if spec.confirm_text else f"Run {spec.name} with {tool_call.args}"
                )
                result.answer = (response.text or "").strip() or result.pending_description
                return result
            else:
                try:
                    tool_result = spec.run(tool_call.args) or {}
                except Exception as exc:  # noqa: BLE001 - the model gets the error and decides what to do
                    logger.exception("Tool %s failed", tool_call.name)
                    tool_result = {"error": f"{type(exc).__name__}: {exc}"}

            for source in tool_result.pop("_sources", []) or []:
                key = source.get("location") or source.get("uri") or source.get("filename") or source.get("title")
                if key and key not in seen_sources:
                    seen_sources.add(key)
                    result.sources.append(source)
            result.steps.append(AgentStep(tool=tool_call.name, args=tool_call.args, summary=_summarize(tool_call.name, tool_call.args, tool_result)))
            response_parts.append(Part.from_function_response(name=tool_call.name, response=_truncate(tool_result)))
        contents.append(Content(role="user", parts=response_parts))

    # Step budget exhausted: ask for the best answer with what was gathered, no more tools.
    contents.append(Content(role="user", parts=[Part(text="Answer now using only the information gathered so far. Do not call any more tools.")]))
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=GenerateContentConfig(temperature=0.2, system_instruction=system_instruction),
    )
    result.answer = (response.text or "").strip()
    return result
