"""Reading one action out of the model's reply.

The reply is ``{"action": "<name>", ...arguments}``. Weak models wrap it in a sentence or a
code fence, so the first JSON object in the reply is what counts. For ``write_file`` the
file may follow the object in a fenced block instead of being a JSON string: escaping a
whole source file correctly is where weak models fail most.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_FENCED = re.compile(r"```[a-zA-Z0-9_+-]*[ \t]*\n(.*?)\n?```", re.DOTALL)
_DECODER = json.JSONDecoder()


class BadAction(Exception):
    """The reply holds no usable action. The text goes back to the model."""


@dataclass(frozen=True)
class Action:
    name: str
    args: dict[str, Any]

    def text(self, name: str, *, default: str | None = None, limit: int = 2000) -> str:
        """A required text argument (optional when a default is given)."""
        value = self.args.get(name, default)
        if not isinstance(value, str) or (default is None and not value.strip()):
            raise BadAction(f'the action "{self.name}" needs a text argument "{name}"')
        if len(value) > limit:
            raise BadAction(f'the argument "{name}" is too long (limit {limit} characters)')
        return value


def _first_object(reply: str) -> tuple[dict[str, Any], int] | None:
    position = reply.find("{")
    for _ in range(20):
        if position < 0:
            return None
        try:
            value, end = _DECODER.raw_decode(reply, position)
        except ValueError:
            position = reply.find("{", position + 1)
            continue
        if isinstance(value, dict) and ("action" in value or "tool" in value):
            return value, end
        position = reply.find("{", position + 1)
    return None


def parse_action(reply: str) -> Action:
    found = _first_object(reply)
    if found is None:
        raise BadAction('the reply holds no JSON object with an "action"')
    data, end = found
    name = data.get("action", data.get("tool"))
    if not isinstance(name, str) or not re.fullmatch(r"[a-z_]{2,30}", name.strip()):
        raise BadAction('"action" must be the name of one of the actions')
    nested = data.get("args", data.get("arguments"))
    args = dict(nested) if isinstance(nested, dict) else {}
    args.update({k: v for k, v in data.items() if k not in ("action", "tool", "args", "arguments")})
    if name.strip() == "write_file" and not isinstance(args.get("content"), str):
        block = _FENCED.search(reply, end)
        if block:
            args["content"] = block.group(1)
    return Action(name.strip(), args)
