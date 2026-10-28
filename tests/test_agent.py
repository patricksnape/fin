import json
import re
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from fin import config, demo, ledger
from fin.agent import engine
from fin.agent import spec as S
from fin.agent.session import Session, tool_specs
from fin.analytics import recurring, spend
from fin.privacy.redact import Policy, load_policy

EXTRA = """2026-09-20 * "Hans Muster" "Rent share IBAN CH9300762011623852957, tel +41 79 123 45 67"
  ref: "manual:leak-1"
  Expenses:Shopping:Home  30.00 CHF
  Assets:UBS:Checking
"""


def make_session(root, level=None) -> Session:
    led = ledger.load(root / "ledger" / "main.beancount")
    settings = config.load_settings(root, led)
    confirmed, rejected = recurring.load_state(led)
    data = engine.Data(led, settings, spend.build(led, settings, confirmed, rejected))
    policy = load_policy(root)
    if level:
        policy.level = level
    return Session(data, root, policy)


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "repo"
    demo.create_demo(root)
    (root / "ledger" / "manual" / "2026.beancount").write_text(EXTRA)
    (root / "privacy.yaml").write_text(
        "level: blind\nredact_names: [Jane Doe]\npeople: [Hans Muster]\n"
    )
    return root


QUESTIONS = [
    ("schema", {}),
    ("list_accounts", {}),
    ("list_payees", {"contains": "Hans"}),
    ("run_query", {"query": {"group_by": ["category"], "measures": ["sum", "share_of_total"]}}),
    (
        "run_query",
        {
            "query": {
                "filters": {"account_prefix": "Expenses:Food"},
                "group_by": ["quarter"],
                "measures": ["sum", "pct_change_yoy"],
            }
        },
    ),
    ("run_query", {"query": {"scope": "major", "group_by": ["payee"]}}),
    ("run_query", {"query": {"group_by": ["payee"], "limit": 50}}),
    (
        "run_query",
        {
            "query": {
                "group_by": ["cadence", "month"],
                "measures": ["sum", "running_total", "avg", "count"],
            }
        },
    ),
    ("run_query", {"report": {"report": "networth"}}),
    ("run_query", {"report": {"report": "cashflow"}}),
    ("run_query", {"report": {"report": "allocation", "by": "currency"}}),
    ("run_query", {"report": {"report": "spend_headline"}}),
    (
        "make_chart",
        {
            "handle": "r5",
            "chart_type": "line",
            "x": "month",
            "y": "sum",
            "series": "cadence",
            "title": "Spend",
        },
    ),
    ("make_table", {"handle": "r8", "sort_by": "value"}),
    ("say", {"template": "Latest NW {{r6.value.last}}, core {{r9.value[core_monthly_spend]}}"}),
]


def run_questions(root, level=None):
    s = make_session(root, level)
    for tool, args in QUESTIONS:
        s.call(tool, args)
    return s


def numeric_secrets(s: Session) -> set[str]:
    """Every figure the person could see, in the formats an LLM payload might carry them."""
    out = set()
    values = [r.amount for r in s.data.model.postings.itertuples()]
    for res in s.results.values():
        for m in res.measures:
            if res.units[m] == "money":
                values += [Decimal(v) for v in res.table[m] if not pd.isna(v)]
    for v in values:
        v = abs(Decimal(v)).quantize(Decimal("0.01"))
        if v < 100:
            continue
        out |= {
            f"{v:.2f}",
            f"{v:,.2f}",
            f"{v:,.2f}".replace(",", "'"),
            f"{v:,.2f}".replace(",", "’"),
        }
        out.add(f"{int(v)}")
    return {x for x in out if not re.fullmatch(r"(?:19|20)\d\d", x)}


def payloads(root) -> list[str]:
    lines = (root / "state" / "llm_audit.jsonl").read_text().splitlines()
    return [json.dumps(json.loads(line)["to_model"], ensure_ascii=False) for line in lines]


def test_blind_payloads_contain_no_figures(root):
    s = run_questions(root)
    secrets = numeric_secrets(s)
    assert len(secrets) > 100
    for text in payloads(root):
        # only the schema's JSON-schema bounds (limit <= 200) and years/counts are numbers
        for token in re.findall(r"[\d][\d,'’.]*", text):
            assert token not in secrets, f"leaked {token!r} in {text[:200]}"
        assert not re.search(r"\d[.,']\d{2}\b", text), text[:300]


def test_blind_payloads_have_no_names_ibans_or_phones(root):
    run_questions(root)
    joined = " ".join(payloads(root))
    for bad in ("Hans Muster", "Jane", "CH9300762011623852957", "123 45 67"):
        assert bad not in joined
    assert "[person]" in joined


def test_audit_log_records_every_call(root):
    run_questions(root)
    assert len(payloads(root)) == len(QUESTIONS)


def test_person_sees_real_numbers_in_events(root):
    s = run_questions(root)
    kinds = [e["type"] for e in s.events]
    assert kinds == ["chart", "table", "text"]
    text = s.events[-1]["text"]
    assert "CHF" in text and "[?]" not in text
    assert re.search(r"CHF [\d,]+\.\d{2}", text)
    assert all("value" in e["columns"] for e in s.events if e["type"] == "table")


def test_relative_level_shows_ratios_but_no_money(root):
    s = run_questions(root, "relative")
    secrets = numeric_secrets(s)
    blob = " ".join(payloads(root))
    assert '"share_of_total"' in blob
    for token in re.findall(r"[\d][\d,'’.]*", blob):
        assert token not in secrets


def test_full_level_exposes_values_for_local_models(root):
    s = make_session(root, "full")
    out = s.call("run_query", {"query": {"group_by": ["category"], "measures": ["sum"]}})
    assert "rows" in out and isinstance(out["rows"][0]["sum"], float)


def test_raw_bql_and_unknown_fields_are_rejected(root):
    s = make_session(root)
    for bad in (
        {"query": {"raw_bql": "SELECT 1"}},
        {"query": {"group_by": ["str(sum(position))"]}},
        {"query": {"group_by": ["month", "year"]}},
        {"query": {"measures": ["pct_change_yoy"]}},
        {"query": {}, "report": {"report": "networth"}},
        {},
    ):
        assert "error" in s.call("run_query", bad), bad
    assert "error" in s.call("nope", {})


def test_chart_validation_errors_do_not_create_events(root):
    s = make_session(root)
    s.call("run_query", {"query": {"group_by": ["category"]}})
    assert "error" in s.call(
        "make_chart", {"handle": "r1", "chart_type": "bar", "x": "month", "y": "sum", "title": "t"}
    )
    assert "error" in s.call(
        "make_chart",
        {"handle": "r9", "chart_type": "bar", "x": "category", "y": "sum", "title": "t"},
    )
    assert s.events == []


def test_placeholder_values_match_engine(root):
    s = make_session(root)
    s.call("run_query", {"query": {"group_by": ["category"], "measures": ["sum"]}})
    s.call(
        "say",
        {
            "template": "{{r1.sum[Expenses:Food]}} / {{r1.sum.total}} / {{r2.sum.total}} / {{r1.sum.bogus}}"
        },
    )
    t = s.results["r1"].table.set_index("category")["sum"]
    food = f"CHF {t['Expenses:Food']:,.2f}"
    total = f"CHF {t.sum():,.2f}"
    assert s.events[-1]["text"] == f"{food} / {total} / [?] / [?]"


def test_pinned_tile_stores_no_data_and_rerenders(root):
    s = run_questions(root)
    out = s.call("pin", {"chart_id": "c1", "name": "Spend by cadence"})
    saved = (root / "dashboards" / out["saved_as"]).read_text()
    assert not re.search(r"\d+\.\d{2}", saved)
    fresh = make_session(root)
    event = fresh.render_pinned("Spend by cadence")
    assert event["type"] == "chart" and event["option"]["series"]


def test_engine_measures(root):
    s = make_session(root)
    r = engine.run_query(
        s.data, S.QuerySpec(group_by=["category"], measures=["sum", "share_of_total"])
    )
    assert abs(r.table["share_of_total"].sum() - 1) < Decimal("0.001")
    r = engine.run_query(s.data, S.QuerySpec(group_by=["month"], measures=["sum", "running_total"]))
    assert r.table["running_total"].iloc[-1] == r.table["sum"].sum()
    r = engine.run_query(s.data, S.QuerySpec(scope="major", group_by=["payee"]))
    assert set(r.table["payee"]) == {"Küchen Schmid AG", "IKEA", "Digitec"}
    r = engine.run_query(s.data, S.QuerySpec(group_by=["payee"], limit=3))
    assert len(r.table) == 3 and list(r.table["sum"]) == sorted(r.table["sum"], reverse=True)


def test_tool_specs_cover_every_tool():
    names = {t["name"] for t in tool_specs()}
    assert names == {
        "schema",
        "list_accounts",
        "list_payees",
        "run_query",
        "make_chart",
        "make_table",
        "say",
        "pin",
    }
    assert all(t["input_schema"]["type"] == "object" for t in tool_specs())


def test_redaction_patterns():
    p = Policy(names=["Jane Doe"], people=["Hans Muster"])
    cases = {
        "CHF 1'234.50 to Hans Muster": "[amount] to [person]",
        "12.50 EUR Coop": "[amount] Coop",
        "Rent 2400": "Rent [#]",
        "2026-03-01 2026-Q1": "2026-03-01 2026-Q1",
        "jane doe": "[name]",
        "CH93 0076 2011 6238 5295 7": "[iban]",
    }
    for text, want in cases.items():
        assert p.redact(text) == want, text


def test_percent_sign_is_not_doubled(root):
    s = make_session(root)
    s.call("run_query", {"query": {"group_by": ["quarter"], "measures": ["sum", "pct_change_yoy"]}})
    s.call("say", {"template": "{{r1.pct_change_yoy.last}}% vs last year"})
    text = s.events[-1]["text"]
    assert "%%" not in text and text.endswith("% vs last year")


def test_charts_fold_long_tails_into_other(root):
    s = make_session(root)
    s.call("run_query", {"query": {"group_by": ["payee"]}})
    s.call("run_query", {"query": {"group_by": ["category", "month"]}})
    s.call(
        "make_chart", {"handle": "r1", "chart_type": "pie", "x": "payee", "y": "sum", "title": "t"}
    )
    s.call(
        "make_chart",
        {
            "handle": "r2",
            "chart_type": "stacked_bar",
            "x": "month",
            "y": "sum",
            "series": "category",
            "title": "t",
        },
    )
    pie, bars = (e["option"] for e in s.events)
    names = [d["name"] for d in pie["series"][0]["data"]]
    assert len(names) == 7 and names[-1] == "Other"
    assert [x["name"] for x in bars["series"]][-1] == "Other" and len(bars["series"]) == 6
    total = s.results["r1"].table["sum"].sum()
    assert abs(sum(d["value"] for d in pie["series"][0]["data"]) - float(total)) < 0.5


def test_stats_skip_incomplete_periods_unless_named(root):
    s = make_session(root)
    s.call(
        "run_query",
        {"query": {"filters": {"account_prefix": "Expenses:Food"}, "group_by": ["quarter"]}},
    )
    res = s.results["r1"].table.set_index("quarter")["sum"]
    assert "2026-Q4" in s.call("run_query", {"query": {"group_by": ["quarter"]}})["partial_periods"]
    s.call(
        "say",
        {"template": "{{r1.sum.last}} | {{r1.sum[2026-Q4]}} | {{r1.sum.first}} | {{r1.sum.total}}"},
    )
    last, partial, first, total = s.events[-1]["text"].split(" | ")
    money = lambda v: f"CHF {v:,.2f}"
    assert last == money(res["2026-Q3"])  # last finished quarter, not the half-filled 2026-Q4
    assert partial == money(res["2026-Q4"])  # naming the period still works
    assert first == money(res["2025-Q1"])  # 2024-Q4 starts mid-quarter too
    assert total == money(res.sum())  # totals include everything


def _q(s, **kw):
    return engine.run_query(s.data, S.QuerySpec(**kw))


def test_order_by_any_measure_then_limit_after_ordering(root):
    s = make_session(root)
    top = _q(
        s, group_by=["category"], measures=["sum", "count"], order_by={"measure": "count"}, limit=3
    )
    full = _q(s, group_by=["category"], measures=["sum", "count"])
    assert list(top.table["count"]) == sorted(full.table["count"], reverse=True)[:3]
    asc = _q(
        s, group_by=["category"], measures=["sum"], order_by={"measure": "sum", "direction": "asc"}
    )
    assert list(asc.table["sum"]) == sorted(asc.table["sum"])


def test_share_of_total_is_of_everything_even_when_limited(root):
    s = make_session(root)
    top = _q(s, group_by=["category"], measures=["sum", "share_of_total"], limit=2)
    total = _q(s, group_by=["category"]).table["sum"].sum()
    assert top.table["share_of_total"].iloc[0] == (top.table["sum"].iloc[0] / total).quantize(
        Decimal("0.0001")
    )
    assert top.table["share_of_total"].sum() < 1


def test_compare_ytd_matches_manual_sums(root):
    s = make_session(root)
    r = _q(
        s,
        group_by=["category"],
        compare={"preset": "ytd_vs_prior_year"},
        order_by={"measure": "change"},
    )
    assert r.meta["compare"] == {"current": "2026-01..2026-09", "baseline": "2025-01..2025-09"}
    rows = s.data.model.postings
    rr = engine.spend.monthly_rows(s.data.model, "run_rate")
    rr = rr[rr["in_spend"]]
    for _, row in r.table.iterrows():
        cat = rr[rr["category"] == row["category"]]
        cur = Decimal(
            cat[(cat["month"] >= date(2026, 1, 1)) & (cat["month"] <= date(2026, 9, 1))][
                "amount"
            ].sum()
        )
        base = Decimal(
            cat[(cat["month"] >= date(2025, 1, 1)) & (cat["month"] <= date(2025, 9, 1))][
                "amount"
            ].sum()
        )
        assert row["current"] == cur.quantize(Decimal("0.01")) and row["baseline"] == base.quantize(
            Decimal("0.01")
        )
        assert row["change"] == row["current"] - row["baseline"]
    assert list(r.table["change"]) == sorted(r.table["change"], reverse=True) and len(rows) > 0


def test_compare_presets_resolve_to_complete_periods():
    d = date(2026, 10, 14)  # October is unfinished
    assert engine.last_complete_month(d) == date(2026, 9, 1)
    assert engine.last_complete_month(date(2026, 10, 31)) == date(2026, 10, 1)
    cases = {
        "last_12m_vs_prior_12m": (
            (date(2025, 10, 1), date(2026, 9, 1)),
            (date(2024, 10, 1), date(2025, 9, 1)),
        ),
        "last_month_vs_prior_month": ((date(2026, 9, 1),) * 2, (date(2026, 8, 1),) * 2),
        "last_quarter_vs_prior_quarter": (
            (date(2026, 7, 1), date(2026, 9, 1)),
            (date(2026, 4, 1), date(2026, 6, 1)),
        ),
        "last_quarter_vs_year_ago": (
            (date(2026, 7, 1), date(2026, 9, 1)),
            (date(2025, 7, 1), date(2025, 9, 1)),
        ),
    }
    for preset, want in cases.items():
        assert engine.resolve_compare(S.Compare(preset=preset), d) == want, preset
    mid = engine.resolve_compare(
        S.Compare(preset="last_quarter_vs_prior_quarter"), date(2026, 5, 20)
    )
    assert mid[0] == (
        date(2026, 1, 1),
        date(2026, 3, 1),
    )  # May is unfinished, so Q1 is the last full quarter


def test_compare_warns_about_pre_data_baseline(root):
    s = make_session(root)
    r = _q(
        s,
        group_by=["category"],
        compare={
            "current": {"from_month": "2025-01", "to_month": "2025-03"},
            "baseline": {"from_month": "2024-01", "to_month": "2024-03"},
        },
    )
    assert any("before your data" in n for n in r.notes)
    described = s.call(
        "run_query",
        {"query": {"group_by": ["category"], "compare": {"preset": "last_12m_vs_prior_12m"}}},
    )
    assert (
        described["compare"]["current"] == "2025-10..2026-09"
        and described["row_order"] == "ranked by current, largest first"
    )


def test_new_spec_rules_reject_misuse(root):
    s = make_session(root)
    bad = [
        {"group_by": ["month"], "compare": {"preset": "ytd_vs_prior_year"}},
        {"measures": ["change"]},
        {"compare": {"preset": "ytd_vs_prior_year"}, "measures": ["sum"]},
        {"group_by": ["month"], "order_by": {"measure": "sum"}},
        {"group_by": ["month"], "limit": 3},
        {"group_by": ["category"], "measures": ["running_total"]},
        {
            "compare": {
                "current": {"from_month": "2026-05", "to_month": "2026-01"},
                "baseline": {"from_month": "2025-01", "to_month": "2025-02"},
            }
        },
    ]
    for q in bad:
        assert "error" in s.call("run_query", {"query": q}), q


def test_rank_placeholders_name_rows_the_model_never_saw(root):
    s = make_session(root)
    out = s.call(
        "run_query",
        {
            "query": {
                "group_by": ["category"],
                "compare": {"preset": "ytd_vs_prior_year"},
                "order_by": {"measure": "change"},
                "limit": 3,
            }
        },
    )
    assert out["row_order"] == "ranked by change, largest first"
    s.call(
        "say",
        {
            "template": "{{r1.label[#1]}} rose {{r1.change[#1]}} ({{r1.change_pct[#1]}}); then {{r1.label[#2]}}; {{r1.label[#9]}} {{r1.label}} {{r1.change[#0]}}"
        },
    )
    t = s.results["r1"].table
    top = t.iloc[0]
    expect = f"{top['category']} rose CHF {top['change']:,.2f} ({top['change_pct'] * 100:.1f}%); then {t.iloc[1]['category']}; [?] [?] [?]"
    assert s.events[-1]["text"] == expect


def test_blind_payloads_stay_amount_free_with_rankings_and_comparisons(root):
    s = make_session(root)
    for q in (
        {
            "group_by": ["category"],
            "compare": {"preset": "ytd_vs_prior_year"},
            "order_by": {"measure": "change"},
            "limit": 5,
        },
        {
            "group_by": ["payee"],
            "compare": {"preset": "last_quarter_vs_year_ago"},
            "order_by": {"measure": "change_pct", "direction": "asc"},
            "limit": 10,
        },
        {
            "group_by": ["category"],
            "measures": ["sum", "count"],
            "order_by": {"measure": "count"},
            "limit": 5,
        },
    ):
        s.call("run_query", {"query": q})
    secrets = numeric_secrets(s)
    for text in payloads(root):
        for token in re.findall(r"[\d][\d,'’.]*", text):
            assert token not in secrets, f"leaked {token!r}"
        assert not re.search(r"\d[.,']\d{2}\b", text)


TWINT = """2026-09-21 * "Erika Beispiel" "Payment UBS TWINT · TWINT-Acc.:+41790000000"
  ref: "ubs:p2p-1"
  Expenses:Shopping:Home  15.00 CHF
  Assets:UBS:Checking
"""


def test_people_paid_by_twint_are_redacted_everywhere(root):
    with (root / "ledger" / "manual" / "2026.beancount").open("a") as f:
        f.write("\n" + TWINT)
    s = make_session(root)
    sent = json.dumps(
        [
            s.call("list_payees", {}),
            s.call("run_query", {"query": {"group_by": ["payee"]}}),
            s.call("list_payees", {"contains": "erika"}),
        ]
    )
    assert "Erika" not in sent and "[person]" in sent
    assert "Erika" not in (root / "state" / "llm_audit.jsonl").read_text().split('"to_model"')[-1]


def test_unexpected_errors_do_not_reach_the_model(root, monkeypatch):
    s = make_session(root)

    def boom(*a, **k):
        raise RuntimeError("CHF 12.50 at Secret Shop")

    monkeypatch.setattr(engine, "run", boom)
    out = s.call("run_query", {"query": {"group_by": ["month"]}})
    assert out == {"error": "internal error (RuntimeError)"}


def test_redaction_matches_whole_words_and_names_with_digits():
    p = Policy(people=["Ann", "Muster 1234"])
    assert p.redact("Annual fee, paid Ann") == "Annual fee, paid [person]"
    assert p.redact("to Muster 1234") == "to [person]"
    assert Policy().redact("nothing to hide") == "nothing to hide"


def test_chat_backend_is_private_and_resumable():
    pytest.importorskip("claude_agent_sdk")
    from fin.agent.backends import claude_sdk

    s = Session.__new__(Session)
    a = claude_sdk.build_options(s, {})
    b = claude_sdk.build_options(s, {"sdk_session": "abc"})
    assert a.cwd == b.cwd and b.resume == "abc"  # the CLI files sessions per working directory
    assert a.tools == [] and a.setting_sources == [] and a.strict_mcp_config
    assert a.env["DISABLE_TELEMETRY"] == "1"
