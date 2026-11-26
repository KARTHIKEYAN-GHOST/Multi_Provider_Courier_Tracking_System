"""The agent: a model that works in steps, with a browser, an HTTP client and a workspace.

It is independent of the model. The model is asked for one action at a time as a plain JSON
object, so any chat model from any supplier can drive it; nothing here relies on a
supplier's own tool-calling feature.
"""

from .actions import Action, BadAction, parse_action
from .loop import Agent, AgentGaveUp
from .net import Net
from .tools import Toolbox

__all__ = ["Action", "Agent", "AgentGaveUp", "BadAction", "Net", "Toolbox", "parse_action"]
