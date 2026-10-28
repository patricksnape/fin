import json

import pytest
from fastapi.testclient import TestClient

from fin import demo, settings
from fin.agent.backends.claude_sdk import build_options
from fin.api.app import create_app


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    root = tmp_path_factory.mktemp("settings") / "repo"
    demo.create_demo(root)
    return root


def test_defaults_are_haiku_medium():
    s = settings.load()
    assert (s.agent.model, s.agent.effort) == ("claude-haiku-5-5", "medium")


def test_save_and_load_round_trip():
    settings.save(
        settings.AppSettings(agent=settings.AgentSettings(model="claude-opus-5-5", effort="high"))
    )
    assert settings.load().agent.model == "claude-opus-5-5"
    assert json.loads(settings.path().read_text())["agent"]["effort"] == "high"


def test_unreadable_file_falls_back_to_defaults():
    settings.path().write_text("not json {")
    assert settings.load().agent.model == "claude-haiku-5-5"
    settings.path().write_text('{"agent": {"model": "x", "effort": "turbo"}}')
    assert settings.load().agent.effort == "medium"


def test_api_get_put_and_validation(root):
    c = TestClient(create_app(root, backend=object()))
    got = c.get("/api/settings").json()
    assert got["agent"] == {"model": "claude-haiku-5-5", "effort": "medium"}
    assert {m["id"] for m in got["models"]} == settings.MODEL_IDS and len(got["efforts"]) == 4
    put = c.put("/api/settings", json={"model": "claude-sonnet-5-5", "effort": "low"})
    assert put.status_code == 200 and put.json()["agent"]["model"] == "claude-sonnet-5-5"
    assert c.get("/api/settings").json()["agent"]["effort"] == "low"
    assert c.put("/api/settings", json={"model": "gpt-4", "effort": "low"}).status_code == 422
    assert (
        c.put("/api/settings", json={"model": "claude-haiku-5-5", "effort": "turbo"}).status_code
        == 422
    )
    assert (
        c.get("/api/settings").json()["agent"]["model"] == "claude-sonnet-5-5"
    )  # bad input changed nothing


def test_chat_uses_the_saved_settings_for_each_message(root):
    seen = []

    class Spy:
        async def run(self, message, session, state):
            seen.append(dict(state["agent"]))
            return
            yield

    c = TestClient(create_app(root, backend=Spy()))

    def ask():
        with c.stream("POST", "/api/chat", json={"message": "hi", "conversation_id": "s"}) as r:
            list(r.iter_lines())

    ask()
    c.put("/api/settings", json={"model": "claude-opus-5-5", "effort": "max"})
    ask()
    assert seen == [
        {"model": "claude-haiku-5-5", "effort": "medium"},
        {"model": "claude-opus-5-5", "effort": "max"},
    ]


def test_backend_options_carry_model_and_effort(root):
    from fin.agent.session import Session
    from fin.api.context import Context
    from fin.privacy.redact import load_policy

    session = Session(Context(root).data(), root, load_policy(root))
    opts = build_options(session, {"agent": {"model": "claude-haiku-5-5", "effort": "medium"}})
    assert (opts.model, opts.effort) == ("claude-haiku-5-5", "medium")
    assert opts.tools == [] and opts.setting_sources == []  # built-in tools stay off
    assert build_options(session, {}).model is None  # no settings: the SDK default applies
