"""Per-machine app preferences (not in the data repo, so they are never synced or encrypted).

Stored in ~/.config/fin/settings.json; override the location with $FIN_SETTINGS (used by tests).
"""

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

Effort = Literal["low", "medium", "high", "max"]

MODELS: list[dict[str, str]] = [
    {
        "id": "claude-haiku-5-5",
        "label": "Haiku 5.5",
        "note": "Fastest and cheapest; fine for turning a question into a query and chart",
    },
    {
        "id": "claude-sonnet-5-5",
        "label": "Sonnet 5.5",
        "note": "Balanced; better at ambiguous or multi-step questions",
    },
    {
        "id": "claude-opus-5-5",
        "label": "Opus 5.5",
        "note": "Strongest reasoning; slower and uses more of your plan",
    },
    {"id": "claude-fable-5-1", "label": "Fable 5.1", "note": "Newest and most capable; slowest"},
]
EFFORTS: list[dict[str, str]] = [
    {"id": "low", "label": "Low", "note": "Minimal thinking, quickest replies"},
    {"id": "medium", "label": "Medium", "note": "Moderate thinking"},
    {"id": "high", "label": "High", "note": "Deep reasoning"},
    {"id": "max", "label": "Max", "note": "Maximum effort"},
]
MODEL_IDS = {m["id"] for m in MODELS}


class AgentSettings(BaseModel):
    model: str = "claude-haiku-5-5"
    effort: Effort = "medium"


class AppSettings(BaseModel):
    agent: AgentSettings = AgentSettings()


def path() -> Path:
    return Path(os.environ.get("FIN_SETTINGS") or Path.home() / ".config" / "fin" / "settings.json")


def load() -> AppSettings:
    """Saved settings, or the defaults when missing or unreadable."""
    try:
        return AppSettings.model_validate(json.loads(path().read_text()))
    except (OSError, ValueError, ValidationError):
        return AppSettings()


def save(settings: AppSettings) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(settings.model_dump(), indent=2) + "\n")
