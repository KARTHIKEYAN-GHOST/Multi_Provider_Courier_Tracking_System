"""The agent's loop: ask the model for one action, do it, show the result, repeat.

The conversation is kept short on purpose. Old results are cut down to their beginning:
the model needs the last few in full, and a free model's context is small.
"""

from __future__ import annotations

from typing import Protocol

import structlog

from ..llm import ModelError
from .actions import BadAction, parse_action
from .tools import Toolbox

log = structlog.get_logger()

_KEEP_FULL = 6  # the most recent exchanges stay as they are
_STUB = 400
_SILENT_STEPS = 3  # this many steps in a row without an answer: the model cannot be used now


class AgentGaveUp(Exception):
    """The steps ran out before the work passed its checks. The text holds no secret."""


class Model(Protocol):
    name: str

    def complete(self, system: str, messages: list[dict[str, str]]) -> str: ...


def _shortened(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """The task, old exchanges cut to their beginning, the recent ones in full."""
    head, tail = messages[:1], messages[1:]
    old, recent = tail[: -2 * _KEEP_FULL], tail[-2 * _KEEP_FULL :]
    cut = [
        m if len(m["content"]) <= _STUB else {**m, "content": m["content"][:_STUB] + "\n[... shortened ...]"}
        for m in old
    ]
    return head + cut + recent


class Agent:
    def __init__(self, model: Model, system: str, *, max_steps: int = 40) -> None:
        self._model, self._system, self._max_steps = model, system, max_steps
        self.used = model.name
        self.steps = 0

    def run(self, task: str, tools: Toolbox, *, label: str = "") -> str:
        """Works on the task until ``finish`` is accepted. Returns the summary."""
        messages = [{"role": "user", "content": task}]
        last = "no step was made"
        silent = 0
        for step in range(1, self._max_steps + 1):
            self.steps = step
            try:
                reply = self._model.complete(self._system, _shortened(messages))
            except ModelError as error:
                if not error.retryable:
                    raise
                last = f"the model gave no usable answer: {error}"
                log.info(
                    "agent_step", adapter=label, step=step, action="(no answer)", reason=str(error)[:200]
                )
                silent += 1
                if silent >= _SILENT_STEPS:
                    # Waiting longer only keeps the adapter's lookups on hold: give the task back.
                    raise AgentGaveUp(
                        f"the model gave no answer {silent} times in a row (after {step} steps); "
                        f"the last reason: {str(error)[:500]}"
                    ) from None
                continue
            silent = 0
            self.used = str(getattr(self._model, "last_model", self._model.name))
            try:
                action = parse_action(reply)
                result, done = tools.run(action)
                name = action.name
            except BadAction as error:
                name, done = "(unusable)", False
                result = (
                    f"That was not a usable action: {error}. Answer with exactly one JSON object, "
                    'for example {"action": "list_files"}.'
                )
            log.info("agent_step", adapter=label, step=step, action=name, model=self.used)
            if done:
                return tools.summary
            last = f"{name}: {result[:600]}"
            left = self._max_steps - step
            if left in (10, 5, 2):
                result += f"\n\n({left} steps are left. Get the checks to pass and use finish.)"
            messages += [
                {"role": "assistant", "content": reply[:60_000]},
                {"role": "user", "content": result[:40_000]},
            ]
        raise AgentGaveUp(f"not finished after {self._max_steps} steps; the last step was {last[:1000]}")
