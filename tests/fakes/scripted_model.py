"""A scripted stand-in for Claude in drafting tests (tier: offline).

It is a test double, not evidence: its transport kind is "scripted", so it
can only serve `scripted-example` runs (ADR 0007). For each questionnaire
item it plays a fixed list of turns. Each turn is a list of tool calls, or
one of the strings "refuse" or "text".
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from stewardry.platform.model import TransportKind

Turn = list[tuple[str, dict[str, Any]]] | Literal["refuse", "text"]
DEFAULT: list[Turn] = [
    [("route_to_expert", {"reason": "missing", "explanation": "not covered", "passages": []})]
]


class Crash(BaseException):
    """Simulates the process dying mid-run (not an Exception, so nothing catches it)."""


@dataclass
class ScriptedModel:
    plans: dict[str, list[Turn]]
    crash_on_call: int | None = None
    kind: TransportKind = "scripted"
    payloads: list[dict[str, Any]] = field(default_factory=list)

    def send(self, payload: Mapping[str, Any], request_hash: str) -> dict[str, Any]:
        self.payloads.append(json.loads(json.dumps(payload)))
        if self.crash_on_call is not None and len(self.payloads) == self.crash_on_call:
            raise Crash
        messages = payload["messages"]
        found = re.search(r'"question_id": "([^"]+)"', messages[0]["content"])
        assert found is not None
        question_id = found[1]
        turns = self.plans.get(question_id, DEFAULT)
        index = sum(1 for m in messages if m["role"] == "assistant")
        turn = turns[min(index, len(turns) - 1)]
        usage = {"input_tokens": 100, "output_tokens": 20}
        base = {"model": "claude-opus-5-5", "usage": usage, "role": "assistant"}
        if turn == "refuse":
            return base | {
                "content": [],
                "stop_reason": "refusal",
                "stop_details": {"category": "cyber"},
            }
        if turn == "text":
            return base | {
                "content": [{"type": "text", "text": "I think the answer is yes."}],
                "stop_reason": "end_turn",
            }
        content = [
            {
                "type": "tool_use",
                "id": f"toolu_{question_id}_{index}_{n}",
                "name": name,
                "input": arguments,
            }
            for n, (name, arguments) in enumerate(turn)
        ]
        return base | {"content": content, "stop_reason": "tool_use"}
