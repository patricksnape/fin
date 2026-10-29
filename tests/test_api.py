import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from fin import demo
from fin.api.app import create_app


class Scripted:
    """Backend stand-in: calls gateway tools the way a model would."""

    async def run(self, message, session, state):
        yield {"type": "assistant", "text": "Here is your breakdown."}
        handle = session.call(
            "run_query", {"query": {"group_by": ["category"], "measures": ["sum"]}}
        )["handle"]
        yield {"type": "tool", "name": "run_query"}
        session.call(
            "make_chart",
            {
                "handle": handle,
                "chart_type": "pie",
                "x": "category",
                "y": "sum",
                "title": "By category",
            },
        )
        session.call("say", {"template": "Total {{" + handle + ".sum.total}}"})
        state["turns"] = state.get("turns", 0) + 1


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    root = tmp_path_factory.mktemp("api") / "repo"
    demo.create_demo(root)
    return root


@pytest.fixture
def client(root):
    return TestClient(create_app(root, backend=Scripted()))


def test_meta(client):
    m = client.get("/api/meta").json()
    assert m["base_currency"] == "CHF" and m["as_of"] == "2026-10-14" and m["chat"] is True


def test_overview_money_is_strings(client):
    o = client.get("/api/overview").json()
    assert isinstance(o["net_worth"]["total"], str)
    assert Decimal(o["net_worth"]["total"]) > 200000
    assert set(o["net_worth"]["by_group"]) == {
        "Cash",
        "Investments",
        "Pension",
        "Other",
        "Liabilities",
        "Property",
        "Vehicles",
    }
    assert len(o["net_worth_series"]) == 25 and "core_monthly_spend" in o["spend"]


def test_spending_payload(client):
    s = client.get("/api/spending?months=12").json()
    assert len(s["by_cadence"]["rows"]) == 12
    assert {"recurring", "regular"} <= set(s["by_cadence"]["keys"])
    # every category group can be broken down into the accounts that make it up
    assert all(k.count(":") >= 1 for k in s["by_account"]["keys"])
    assert {":".join(k.split(":")[:2]) for k in s["by_account"]["keys"]} == set(
        s["by_category"]["keys"]
    )
    assert {r["payee"] for r in s["major"]} == {"Küchen Schmid AG", "IKEA", "Digitec"}
    assert any(r["id"].startswith("ubs hypothek") for r in s["recurring"])
    kinds = {a["kind"] for a in s["alerts"]}
    assert "lapsed" in kinds
    cash = client.get("/api/spending?view=cash").json()
    assert cash["view"] == "cash"
    assert client.get("/api/spending?view=bogus").status_code == 400


def test_networth_allocation_cashflow(client):
    nw = client.get("/api/networth").json()
    assert nw["rows"][0]["date"] == "2024-10-31" and "total" in nw["rows"][-1]
    a = client.get("/api/allocation").json()
    assert {r["asset_class"] for r in a["by_asset_class"]} == {"equity", "cash", "bonds"}
    assert a["employer"]["commodity"] == "ACME" and Decimal(a["employer"]["weight"]) > 0
    cf = client.get("/api/cashflow?months=6").json()
    assert len(cf["rows"]) == 6


def test_chat_streams_events_and_keeps_session(client):
    def ask():
        with client.stream(
            "POST", "/api/chat", json={"message": "breakdown?", "conversation_id": "t1"}
        ) as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            return [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]

    events = ask()
    assert [e["type"] for e in events] == ["assistant", "tool", "chart", "text", "done"]
    assert "CHF" in events[3]["text"]
    assert events[2]["option"]["series"][0]["type"] == "pie"
    second = ask()  # same conversation: handles continue (r2), events are not replayed
    assert [e["type"] for e in second] == ["assistant", "tool", "chart", "text", "done"]
    assert "[?]" not in second[3]["text"]


def test_chat_errors_become_events(root):
    class Boom:
        async def run(self, message, session, state):
            raise RuntimeError("model unavailable")
            yield

    c = TestClient(create_app(root, backend=Boom()))
    with c.stream("POST", "/api/chat", json={"message": "hi"}) as r:
        events = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
    assert [e["type"] for e in events] == ["error", "done"]


def test_chat_without_backend_is_503(root, monkeypatch):
    monkeypatch.setenv("FIN_AGENT_BACKEND", "none")
    assert TestClient(create_app(root)).post("/api/chat", json={"message": "hi"}).status_code == 503


def test_dashboards_rerun_pinned_tiles(root):
    c = TestClient(create_app(root, backend=Scripted()))
    assert c.get("/api/dashboards").json() == {"tiles": []}
    with c.stream("POST", "/api/chat", json={"message": "x", "conversation_id": "p"}) as r:
        list(r.iter_lines())
    from fin.agent.session import Session  # pin through the gateway, as the model would

    convo = c.app.state.conversations["p"]
    convo["session"].call("pin", {"chart_id": "c1", "name": "By category"})
    tiles = c.get("/api/dashboards").json()["tiles"]
    assert tiles[0]["title"] == "By category" and tiles[0]["option"]["series"]
    assert isinstance(convo["session"], Session)


def test_strip_figures():
    from fin.privacy.redact import strip_figures

    assert (
        strip_figures("about 1,234.50 in 2026-03 and Q1, 12 times, 500 CHF")
        == "about [?] in 2026-03 and Q1, 12 times, [?] CHF"
    )


def test_empty_ledger_is_reported_not_crashed(tmp_path):
    from fin import ledger, scaffold

    root = tmp_path / "empty"
    scaffold.init_data(root)
    with pytest.raises(ledger.EmptyLedger):
        ledger.as_of(ledger.load(root / "ledger" / "main.beancount"))
    c = TestClient(create_app(root, backend=Scripted()))
    meta = c.get("/api/meta").json()
    assert (
        meta["empty"] is True and meta["base_currency"] == "CHF" and meta["data_dir"] == str(root)
    )
    for path in (
        "/api/overview",
        "/api/spending",
        "/api/networth",
        "/api/allocation",
        "/api/cashflow",
        "/api/dashboards",
    ):
        r = c.get(path)
        assert r.status_code == 409 and r.json()["empty"] is True, path
    assert c.post("/api/chat", json={"message": "hi"}).status_code == 409
    assert c.get("/api/settings").status_code == 200  # settings do not need data

    # the first transaction makes the app live, with no restart
    (root / "ledger" / "accounts.beancount").write_text(
        (root / "ledger" / "accounts.beancount").read_text()
        + '2026-01-01 open Expenses:Food CHF\n  cadence: "regular"\n  necessity: "essential"\n2026-01-01 open Assets:Bank CHF\n'
    )
    (root / "ledger" / "manual" / "2026.beancount").write_text(
        '2026-01-05 * "Coop" "Groceries"\n  ref: "manual:1"\n  Expenses:Food  42.50 CHF\n  Assets:Bank\n'
    )
    assert c.get("/api/meta").json()["empty"] is False
    assert c.get("/api/spending").status_code == 200


def test_a_chat_can_be_cleared_and_sees_changes_made_after_it_started(tmp_path):
    root = tmp_path / "repo"
    demo.create_demo(root)
    c = TestClient(create_app(root, backend=Scripted()))

    def ask(convo):
        with c.stream("POST", "/api/chat", json={"message": "x", "conversation_id": convo}) as r:
            list(r.iter_lines())

    ask("a")
    first = c.app.state.conversations["a"]["session"]
    assert first.data.transactions["category"].eq("Expenses:Uncategorized").any()
    rows = c.get("/api/transactions?month=all&review=true").json()["rows"]
    c.put(
        "/api/transactions/labels",
        json={"ref": rows[0]["ref"], "category": "Expenses:Food:Dining"},
    )
    ask("a")  # the same conversation, after relabelling: it sees the new category
    t = c.app.state.conversations["a"]["session"].data.transactions
    assert t.loc[t["ref"] == rows[0]["ref"], "category"].iloc[0] == "Expenses:Food:Dining"
    assert c.app.state.conversations["a"]["session"] is first  # but it is still the same chat
    assert "r2" in first.results  # earlier results are kept for follow-ups

    assert c.delete("/api/chat/a").json() == {"cleared": True}
    assert "a" not in c.app.state.conversations
    assert c.delete("/api/chat/a").json() == {"cleared": False}  # clearing twice is harmless
    ask("a")  # a fresh conversation under the same id starts again at r1
    assert list(c.app.state.conversations["a"]["session"].results) == ["r1"]


def test_review_queue_and_confirm_round_trip(tmp_path):
    root = tmp_path / "repo"
    demo.create_demo(root)
    with TestClient(create_app(root)) as c:
        q = c.get("/api/review").json()
        assert q["merchants"], "the demo ledger has some uncategorized transactions"
        first = q["merchants"][0]
        assert first["needs_category"]
        bad = c.post("/api/review/confirm", json={"payee_key": first["payee_key"]})
        assert bad.status_code == 400  # still no category chosen
        ok = c.post(
            "/api/review/confirm",
            json={"payee_key": first["payee_key"], "category": "Expenses:Food:Dining"},
        )
        assert ok.status_code == 200, ok.text
        left = {m["payee_key"] for m in c.get("/api/review").json()["merchants"]}
        assert first["payee_key"] not in left
        gone = c.post(
            "/api/review/confirm",
            json={"payee_key": first["payee_key"], "category": "Expenses:Food:Dining"},
        )
        assert gone.status_code == 404  # nothing left to review for that merchant


def test_review_details_endpoint(tmp_path):
    root = tmp_path / "repo"
    demo.create_demo(root)
    with TestClient(create_app(root)) as c:
        first = c.get("/api/review").json()["merchants"][0]
        rows = c.get("/api/review/details", params={"payee_key": first["payee_key"]}).json()
        assert len(rows) == first["count"] and rows[0]["ref"] in first["refs"]
        assert (
            c.get("/api/review/details", params={"payee_key": "no such merchant"}).status_code
            == 404
        )


def test_cards_are_named_and_filterable(tmp_path):
    root = tmp_path / "repo"
    demo.create_demo(root)
    path = next(root.glob("ledger/imported/swisscard/*/*.beancount"))
    text = path.read_text().replace(
        '  ref: "swisscard:', '  card: "AA11BB22"\n  ref: "swisscard:', 1
    )
    path.write_text(text)
    with TestClient(create_app(root)) as c:
        listed = c.get("/api/cards").json()
        assert listed == [
            {
                "source": "swisscard",
                "card": "AA11BB22",
                "label": None,
                "count": 1,
                "first": listed[0]["first"],
                "last": listed[0]["last"],
            }
        ]
        assert (
            c.put("/api/cards/swisscard/AA11BB22", json={"label": "Anna (Visa)"}).status_code == 200
        )
        t = c.get("/api/transactions?month=all&card=Anna%20(Visa)").json()
        assert t["matching"] == 1 and t["rows"][0]["card_label"] == "Anna (Visa)"
        assert t["cards"] == [{"value": "Anna (Visa)", "count": 1}]
        assert c.get("/api/transactions?month=all&card=Nobody").json()["matching"] == 0


def test_review_confirm_can_settle_only_some_payments(tmp_path):
    root = tmp_path / "repo"
    demo.create_demo(root)
    with TestClient(create_app(root)) as c:
        many = next(m for m in c.get("/api/review").json()["merchants"] if m["count"] > 1)
        keep, rest = many["refs"][0], many["refs"][1:]
        ok = c.post(
            "/api/review/confirm",
            json={
                "payee_key": many["payee_key"],
                "category": "Expenses:Food:Dining",
                "remember": False,
                "refs": rest,
            },
        )
        assert ok.status_code == 200, ok.text
        left = next(
            m
            for m in c.get("/api/review").json()["merchants"]
            if m["payee_key"] == many["payee_key"]
        )
        assert left["refs"] == [keep]  # the payment that was not picked is still pending


def test_review_details_meta_is_plain_text(tmp_path):
    root = tmp_path / "repo"
    demo.create_demo(root)
    with TestClient(create_app(root)) as c:
        for m in c.get("/api/review").json()["merchants"][:5]:
            for row in c.get("/api/review/details", params={"payee_key": m["payee_key"]}).json():
                assert all(isinstance(v, str) for v in row["meta"].values())
                assert not any(k.startswith("__") for k in row["meta"])


def test_transactions_can_be_limited_to_a_run_of_months(tmp_path):
    root = tmp_path / "repo"
    demo.create_demo(root)
    with TestClient(create_app(root)) as c:
        months = sorted(m["month"] for m in c.get("/api/transactions?month=all").json()["months"])
        lo, hi = months[2], months[4]
        out = c.get("/api/transactions", params={"start": lo, "end": hi}).json()
        got = {str(t["date"])[:7] for t in out["rows"]}
        assert got and got <= {m for m in months if lo <= m <= hi}
        assert c.get("/api/transactions", params={"start": "2026-13"}).status_code == 400
