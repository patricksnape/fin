"""Chat backends. A backend turns a user message into events by calling Session tools."""

import os
from typing import Protocol


class Backend(Protocol):
    def run(self, message: str, session, state: dict):
        """Async generator of events: {"type": "assistant", "text": ...}, {"type": "tool", ...}."""


def get_backend() -> Backend | None:
    name = os.environ.get("FIN_AGENT_BACKEND", "claude_sdk")
    if name == "claude_sdk":
        try:
            from fin.agent.backends.claude_sdk import ClaudeSdkBackend
        except ImportError:
            return None
        return ClaudeSdkBackend()
    return None
