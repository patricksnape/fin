import json
import types
from datetime import date
from decimal import Decimal
from itertools import pairwise

import pytest
from typer.testing import CliRunner

from fin import config, gitops, importers, ledger
from fin.cli import app
from fin.importers import base, swisscard

runner = CliRunner()

# invented data in the shape the portal returns
RAW = {
    "dateMin": "2026-10-01",
    "dateMax": "2026-10-31",
    "response": {
        "summary": {"count": 3, "postedAmountSum": 63.4},
        "transactions": [
            {
                "id": "T1",
                "postingDate": "2026-10-03T00:00:00",
                "effectiveDate": "2026-10-03T00:00:00",
                "description": "COOP-1234 ZUERICH",
                "state": "POSTED",
                "amount": {"value": 45.5, "currency": "CHF"},
                "cardId": "c1",
                "merchant": {
                    "name": "Coop",
                    "category": "GROCERY STORES, SUPERMARKETS",
                    "city": "Zuerich",
                    "country": "CH",
                },
            },
            {
                "id": "T2",
                "postingDate": "2026-10-04T00:00:00",
                "effectiveDate": "2026-10-04T00:00:00",
                "description": "Refund Kiosk",
                "state": "POSTED",
                "amount": {"value": -7.9, "currency": "CHF"},
                "cardId": "c1",
                "merchant": {"name": "Kiosk"},
                "transactionDataEnrichment": {"category": "SERVICES"},
                "currencyConversion": {
                    "localAmount": {"value": 8.5, "currency": "EUR"},
                    "exchangeRate": 0.93,
                    "markupFeePercentage": 1.5,
                },
            },
            {
                "id": "T3",
                "description": "Pending shop",
                "state": "AUTHORIZED",
                "amount": {"value": 20, "currency": "CHF"},
                "authorizationDateTime": "2026-10-05T10:00:00Z",
                "cardId": "c1",
            },
        ],
    },
}


@pytest.fixture
def opened(data):
    with (data / "ledger" / "accounts.beancount").open("a") as f:
        f.write(
            "2020-01-01 open Liabilities:Swisscard:MilesMore CHF\n"
            "2020-01-01 open Expenses:Uncategorized CHF\n"
            "2020-01-01 open Income:Uncategorized CHF\n"
        )
    return data


def test_parse_skips_pending_and_signs_charges_as_debt():
    txns = swisscard.parse(RAW)
    assert [t.ref for t in txns] == ["swisscard:T1", "swisscard:T2"]
    assert txns[0].amount == Decimal("-45.5") and txns[0].payee == "Coop"
    assert txns[0].narration == "COOP-1234 ZUERICH"
    assert txns[1].amount == Decimal("7.9")  # a refund raises the account
    assert txns[0].date == date(2026, 10, 3)


def test_unknown_state_is_an_error():
    bad = json.loads(json.dumps(RAW))
    bad["response"]["transactions"][0]["state"] = "WEIRD"
    with pytest.raises(base.ImportFailed, match="unknown Swisscard transaction state"):
        swisscard.parse(bad)


def test_import_is_idempotent(opened):
    txns = swisscard.parse(RAW)
    assert base.write_new(opened, "swisscard", txns) == (2, 0)
    assert base.write_new(opened, "swisscard", txns) == (0, 2)
    path = opened / "ledger" / "imported" / "swisscard" / "2026" / "2026-10.beancount"
    assert path.read_text().count('ref: "swisscard:') == 2
    assert not ledger.load(config.main_ledger(opened)).errors


def test_failed_check_rolls_back(data):  # accounts are not open, so bean-check fails
    with pytest.raises(base.ImportFailed):
        base.write_new(data, "swisscard", swisscard.parse(RAW))
    assert not list((data / "ledger" / "imported").glob("swisscard/**/*.beancount"))


def test_month_ranges():
    r = list(swisscard.month_ranges(date(2026, 8, 15), date(2026, 10, 27)))
    assert r == [
        (date(2026, 8, 15), date(2026, 8, 31)),
        (date(2026, 9, 1), date(2026, 9, 30)),
        (date(2026, 10, 1), date(2026, 10, 27)),
    ]


def test_parse_foreign_amount_and_value_date():
    raw = json.loads(json.dumps(RAW))
    raw["response"]["transactions"][1]["effectiveDate"] = "2026-10-02T00:00:00"
    t = swisscard.parse(raw)[1]
    assert t.meta == {
        "card": "c1",
        "value_date": "2026-10-02",
        "portal_category": "SERVICES",
        "original_amount": "8.5 EUR",
        "fx_rate": "0.93",
    }


def test_fetch_range_splits_full_responses():
    days = [date(2026, 10, d) for d in range(1, 9)]
    calls = []

    def post(lo, hi):
        calls.append((lo, hi))
        n = sum(lo <= d <= hi for d in days) * 10  # 10 transactions per day, cap 50
        return {"transactions": [{}] * min(n, swisscard.PAGE_CAP)}

    got = list(swisscard.fetch_range(post, days[0], days[-1]))
    assert all(len(b["transactions"]) < swisscard.PAGE_CAP for _, _, b in got)
    assert got[0][0] == days[0] and got[-1][1] == days[-1]
    for (_, h1, _), (l2, _, _) in pairwise(got):
        assert (l2 - h1).days == 1  # contiguous, no gaps or overlaps


def test_fetch_range_halves_a_cut_off_response():
    def post(lo, hi):
        if (hi - lo).days > 3:  # the portal cuts long replies off mid-JSON
            raise swisscard.TruncatedResponse
        return {"transactions": [{}]}

    got = list(swisscard.fetch_range(post, date(2025, 10, 1), date(2025, 10, 31)))
    assert got[0][0] == date(2025, 10, 1) and got[-1][1] == date(2025, 10, 31)
    assert all((h - lo).days <= 3 for lo, h, _ in got)
    for (_, h1, _), (l2, _, _) in pairwise(got):
        assert (l2 - h1).days == 1

    def always_cut(lo, hi):
        raise swisscard.TruncatedResponse

    with pytest.raises(base.ImportFailed, match="incomplete"):
        list(swisscard.fetch_range(always_cut, date(2025, 10, 1), date(2025, 10, 2)))


def test_fetch_range_single_full_day_fails():
    with pytest.raises(base.ImportFailed, match="single day"):
        list(
            swisscard.fetch_range(
                lambda lo, hi: {"transactions": [{}] * 50}, date(2026, 10, 1), date(2026, 10, 1)
            )
        )


def test_default_start_is_incremental(tmp_path):
    root = tmp_path
    assert base.default_start(root, "swisscard", today=date(2026, 10, 27)) == date(2026, 7, 29)
    (root / "sources" / "swisscard").mkdir(parents=True)
    for name in ("2026-09-01_2026-09-30.json", "2026-10-01_2026-10-20.json"):
        (root / "sources" / "swisscard" / name).write_text("{}")
    assert base.default_start(root, "swisscard", today=date(2026, 10, 27)) == date(2026, 10, 6)


def fake_source(raw):
    """A source whose 'fetch' just archives a canned response, like the browser flow would."""

    def fetch(root, start, end, log=print):
        d = root / "sources" / "fake"
        d.mkdir(parents=True, exist_ok=True)
        (d / "2026-10-01_2026-10-31.json").write_text(json.dumps(raw))
        return [d]

    return types.SimpleNamespace(
        SOURCE="swisscard",
        fetch=fetch,
        source_files=lambda root: sorted((root / "sources" / "fake").glob("*_*.json")),
        parse_files=lambda files: [
            t for f in files for t in swisscard.parse(json.loads(f.read_text()))
        ],
    )


def test_sync_fetches_imports_commits_and_is_idempotent(opened, monkeypatch):
    gitops.commit_all(opened, "open accounts")
    monkeypatch.setattr(importers, "SOURCES", {"fake": "x"})
    monkeypatch.setattr(importers, "get", lambda name: fake_source(RAW))
    r = runner.invoke(app, ["sync"])
    assert r.exit_code == 0, r.output
    assert "imported 2 new" in r.output
    assert not gitops.is_dirty(opened)
    r = runner.invoke(app, ["sync"])
    assert "imported 0 new (2 already in the ledger)" in r.output


def test_sync_reports_a_failing_source_and_leaves_the_ledger_alone(opened, monkeypatch):
    bad = json.loads(json.dumps(RAW))
    bad["response"]["transactions"][0]["state"] = "WEIRD"
    monkeypatch.setattr(importers, "SOURCES", {"fake": "x"})
    monkeypatch.setattr(importers, "get", lambda name: fake_source(bad))
    r = runner.invoke(app, ["sync"])
    assert r.exit_code == 1
    assert "FAILED" in r.output
    assert not base.existing_refs(opened)


def test_unknown_source_is_rejected(data):
    r = runner.invoke(app, ["sync", "nope"])
    assert r.exit_code == 1 and "unknown source" in r.output


def test_parse_keeps_source_categories_as_meta():
    t1, t2 = swisscard.parse(RAW)
    assert t1.meta["merchant_category"] == "GROCERY STORES, SUPERMARKETS"
    assert t2.meta == {
        "card": "c1",
        "portal_category": "SERVICES",
        "original_amount": "8.5 EUR",
        "fx_rate": "0.93",
    }


def _accounts(root):
    led = ledger.load(config.main_ledger(root))
    return {
        e.account: [p.account for p in e.postings]
        for e in led.entries
        if hasattr(e, "postings") and e.meta.get("ref")
    }


def test_mapping_categorizes_known_and_leaves_the_rest_for_review(opened):
    from fin.importers import categorize

    gitops.commit_all(opened, "open accounts")
    base.write_new(opened, "swisscard", swisscard.parse(RAW))
    assert categorize.apply_mapping(opened, "swisscard") == 1
    led = ledger.load(config.main_ledger(opened))
    by_ref = {e.meta["ref"]: e for e in led.entries if hasattr(e, "postings") and "ref" in e.meta}
    coop, kiosk = by_ref["swisscard:T1"], by_ref["swisscard:T2"]
    assert "Expenses:Food:Groceries" in [p.account for p in coop.postings]
    assert "review" not in coop.tags
    assert "Expenses:Uncategorized" in [p.account for p in kiosk.postings]  # SERVICES is unmapped
    assert "review" in kiosk.tags
    assert (opened / "rules" / "categories.yaml").exists()
    assert categorize.apply_mapping(opened, "swisscard") == 0  # nothing left to do
    assert not ledger.load(config.main_ledger(opened)).errors


def test_payee_rule_beats_the_mapping(opened):
    from fin import labels
    from fin.importers import categorize

    for acct in ("Expenses:Food:Dining",):
        (opened / "ledger" / "accounts.beancount").open("a").write(
            f'2020-01-01 open {acct} CHF\n  cadence: "regular"\n  necessity: "discretionary"\n'
        )
    labels.record_rule(opened, "coop", {"category": "Expenses:Food:Dining"})
    base.write_new(opened, "swisscard", swisscard.parse(RAW))
    categorize.apply_mapping(opened, "swisscard")  # leaves Coop to the payee rule
    assert labels.apply_rules(opened) == 1
    led = ledger.load(config.main_ledger(opened))
    coop = next(
        e for e in led.entries if hasattr(e, "postings") and e.meta.get("ref") == "swisscard:T1"
    )
    assert "Expenses:Food:Dining" in [p.account for p in coop.postings]


def test_card_bill_payment_goes_to_transfers_not_expenses():
    raw = json.loads(json.dumps(RAW))
    raw["response"]["transactions"][1]["transactionDataEnrichment"] = {"category": "PAYMENT"}
    assert swisscard.parse(raw)[1].counter == "Assets:Transfers:Pending"
    assert "Assets:Transfers:Pending  -7.9 CHF" in base.render(swisscard.parse(raw)[1])


def _fake_ask(answers):
    """A stand-in model: `answers` maps merchant name -> (account, confidence). Records the prompts."""
    seen = []

    def ask(system, user, model):
        seen.append(user)
        items = json.loads(user.split("Merchants:\n")[1])
        res = []
        for it in items:
            acct, conf = answers.get(it["merchant"], (None, "low"))
            res.append({"id": it["id"], "account": acct, "confidence": conf})
        return "Sure!\n" + json.dumps({"results": res})

    ask.seen = seen
    return ask


def _generic_ledger(opened):
    """Coop mapped to the generic account, as the MCC mapping would leave it."""
    from fin.importers import categorize

    gitops.commit_all(opened, "open accounts")
    raw = json.loads(json.dumps(RAW))
    raw["response"]["transactions"][0]["merchant"]["category"] = "DEPARTMENT STORES"
    base.write_new(opened, "swisscard", swisscard.parse(raw))
    categorize.apply_mapping(opened, "swisscard")


def test_llm_refines_generic_account_and_saves_a_rule(opened):
    from fin.importers import llm_categorize

    _generic_ledger(opened)
    ask = _fake_ask({"Coop": ("Expenses:Shopping:Gifts", "high")})
    with (opened / "ledger" / "accounts.beancount").open("a") as f:
        f.write(
            '2020-01-01 open Expenses:Shopping:Clothing CHF\n  cadence: "one_off"\n  necessity: "discretionary"\n'
        )
    r = llm_categorize.run(opened, ask=ask)
    assert r.new_accounts == ["Expenses:Shopping:Gifts"] and r.applied_high == 1
    led = ledger.load(config.main_ledger(opened))
    coop = next(
        e for e in led.entries if hasattr(e, "postings") and e.meta.get("ref") == "swisscard:T1"
    )
    assert "Expenses:Shopping:Gifts" in [p.account for p in coop.postings]
    from fin import labels

    assert labels.read_rules(led)["coop"]["category"] == "Expenses:Shopping:Gifts"
    assert not led.errors


def test_llm_never_sees_amounts_or_descriptions(opened):
    from fin.importers import llm_categorize

    _generic_ledger(opened)
    ask = _fake_ask({})
    llm_categorize.run(opened, ask=ask)
    sent = "\n".join(ask.seen)
    for secret in ("45.5", "45,5", "COOP-1234", "2026-10", "ZUERICH"):
        assert secret not in sent
    assert "Coop" in sent  # merchant names are what it works from


def test_llm_never_sees_people_or_business_accounts(opened):
    """Person-to-person payments are not asked about, their payees are redacted wherever they
    appear, and non-spend accounts (here one named after a person) are not offered."""
    from fin.importers import llm_categorize

    _generic_ledger(opened)
    with (opened / "ledger" / "accounts.beancount").open("a") as f:
        f.write(
            "2020-01-01 open Expenses:Business:Erika CHF\n"
            '2026-10-02 * "Erika Beispiel" "Payment UBS TWINT · TWINT-Acc.:+41790000000"\n'
            '  ref: "ubs:P1"\n'
            "  Expenses:Uncategorized  20.00 CHF\n"
            "  Liabilities:Swisscard:MilesMore\n"
            '2026-10-03 * "Erika Beispiel Shop" "online"\n'
            '  ref: "ubs:P2"\n'
            "  Expenses:Uncategorized  20.00 CHF\n"
            "  Liabilities:Swisscard:MilesMore\n"
        )
    (opened / "config.toml").write_text('non_spend_prefixes = ["Expenses:Business"]\n')
    ask = _fake_ask({})
    llm_categorize.run(opened, ask=ask)
    sent = "\n".join(ask.seen)
    assert "Erika" not in sent and "Expenses:Business" not in sent
    assert "[person] Shop" in sent  # a merchant that contains a person's name is still asked
    r = llm_categorize.run(
        opened, ask=_fake_ask({"[person] Shop": ("Expenses:Business:Erika", "high")})
    )
    assert r.applied_high == 0  # not even when the model names a non-spend account itself


def test_llm_medium_keeps_review_and_low_or_bad_accounts_are_ignored(opened):
    from fin.importers import llm_categorize

    _generic_ledger(opened)
    r = llm_categorize.run(opened, ask=_fake_ask({"Coop": ("Expenses:Shopping:Gifts", "medium")}))
    assert r.applied_medium == 1
    led = ledger.load(config.main_ledger(opened))
    coop = next(
        e for e in led.entries if hasattr(e, "postings") and e.meta.get("ref") == "swisscard:T1"
    )
    assert "review" in coop.tags
    from fin import labels

    assert "coop" not in labels.read_rules(led)  # tentative answers do not become rules
    for bad in ("Expenses:Taxes:Evil", "Expenses:Brand:New", "Assets:Cash", None):
        r = llm_categorize.run(opened, ask=_fake_ask({"Kiosk": (bad, "high")}))
        assert r.applied_high == 0


def test_llm_dry_run_sends_nothing(opened):
    from fin.importers import llm_categorize

    _generic_ledger(opened)

    def boom(*a):
        raise AssertionError("must not call the model")

    lines = []
    llm_categorize.run(opened, ask=boom, dry_run=True, log=lines.append)
    assert any("Would send" in ln for ln in lines)


def test_model_session_is_private():
    from fin.importers import llm_categorize

    o = llm_categorize.private_options("sys", "claude-haiku-5-5", "/tmp/x")
    assert "no-session-persistence" in o.extra_args
    assert o.tools == [] and o.setting_sources == [] and o.max_turns == 1
    assert o.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"


# ---- provenance, typed rules, the review queue ----------------------------------------------


def _txn(opened, ref):
    led = ledger.load(config.main_ledger(opened))
    return next(e for e in led.entries if hasattr(e, "postings") and e.meta.get("ref") == ref)


def test_provenance_is_recorded_and_cleared_by_a_manual_choice(opened):
    from fin import labels
    from fin.importers import categorize

    gitops.commit_all(opened, "open accounts")
    base.write_new(opened, "swisscard", swisscard.parse(RAW))
    categorize.apply_mapping(opened, "swisscard")
    assert _txn(opened, "swisscard:T1").meta["categorized"] == "mapping"
    labels.apply(
        opened, {"swisscard:T1": {"category": "Expenses:Uncategorized"}}
    )  # a person's edit
    assert "categorized" not in _txn(opened, "swisscard:T1").meta
    assert not ledger.load(config.main_ledger(opened)).errors


def test_rule_can_be_narrowed_to_a_merchant_type(opened):
    from fin import labels

    for acct in ("Expenses:Shopping:Media", "Expenses:Shopping:Gifts"):
        (opened / "ledger" / "accounts.beancount").open("a").write(
            f'2020-01-01 open {acct} CHF\n  cadence: "one_off"\n  necessity: "discretionary"\n'
        )
    labels.record_rule(opened, "coop", {"category": "Expenses:Shopping:Gifts"})
    labels.record_rule(
        opened, "coop", {"category": "Expenses:Shopping:Media"}, merchant_type="BOOK STORES"
    )
    rules = labels.read_rules(ledger.load(config.main_ledger(opened)))
    assert set(rules) == {"coop", "coop|BOOK STORES"}
    assert labels.rule_for(rules, "coop", "BOOK STORES")["category"] == "Expenses:Shopping:Media"
    assert labels.rule_for(rules, "coop", "GROCERY")["category"] == "Expenses:Shopping:Gifts"
    assert labels.rule_for(rules, "coop", None)["category"] == "Expenses:Shopping:Gifts"
    labels.record_rule(opened, "coop|BOOK STORES", {})  # forget just the typed rule
    assert set(labels.read_rules(ledger.load(config.main_ledger(opened)))) == {"coop"}


def test_review_queue_groups_by_merchant_and_confirm_writes_a_rule(opened):
    from fin import labels, review
    from fin.importers import llm_categorize

    _generic_ledger(opened)
    llm_categorize.run(opened, ask=_fake_ask({"Coop": ("Expenses:Shopping:Gifts", "medium")}))
    led = ledger.load(config.main_ledger(opened))
    rows = {r["payee_key"]: r for r in review.queue(led)}
    assert (
        rows["coop"]["source"] == "model" and rows["coop"]["category"] == "Expenses:Shopping:Gifts"
    )
    assert rows["kiosk"]["needs_category"]  # unmapped, still uncategorized
    assert rows["coop"]["merchant_types"] == ["DEPARTMENT STORES"]

    out = review.confirm(opened, "coop")  # accept the model's guess as it stands
    assert out["rule"] == "coop"
    t = _txn(opened, "swisscard:T1")
    assert "review" not in t.tags and "categorized" not in t.meta
    led = ledger.load(config.main_ledger(opened))
    assert labels.read_rules(led)["coop"]["category"] == "Expenses:Shopping:Gifts"
    assert "coop" not in {r["payee_key"] for r in review.queue(led)}
    assert not led.errors


def test_confirm_needs_a_real_category_and_can_be_typed(opened):
    from fin import labels, review

    _generic_ledger(opened)
    import pytest as _pytest

    with _pytest.raises(ValueError, match="choose a category"):
        review.confirm(opened, "kiosk")
    with _pytest.raises(ValueError, match="merchant type"):
        review.confirm(opened, "kiosk", category="Expenses:Food:Dining", per_type=True)
    (opened / "ledger" / "accounts.beancount").open("a").write(
        '2020-01-01 open Expenses:Food:Dining CHF\n  cadence: "regular"\n  necessity: "discretionary"\n'
    )
    # Coop is already categorized from its merchant type, so it is not pending; Kiosk has none
    out = review.confirm(opened, "kiosk", category="Expenses:Food:Dining")
    assert out["rule"] == "kiosk"
    assert "kiosk" in labels.read_rules(ledger.load(config.main_ledger(opened)))


def test_confirm_without_a_rule_labels_once(opened):
    from fin import labels, review
    from fin.importers import llm_categorize

    _generic_ledger(opened)
    llm_categorize.run(opened, ask=_fake_ask({"Coop": ("Expenses:Shopping:Gifts", "medium")}))
    out = review.confirm(opened, "coop", remember=False)
    assert out["rule"] is None
    led = ledger.load(config.main_ledger(opened))
    assert "coop" not in labels.read_rules(led)
    assert "review" not in _txn(opened, "swisscard:T1").tags


def test_model_is_shown_your_decisions_but_not_its_own_rules(opened):
    from fin import labels
    from fin.importers import llm_categorize

    _generic_ledger(opened)
    for acct in ("Expenses:Food:Dining", "Expenses:Pets"):
        (opened / "ledger" / "accounts.beancount").open("a").write(
            f'2020-01-01 open {acct} CHF\n  cadence: "regular"\n  necessity: "discretionary"\n'
        )
    labels.record_rule(opened, "migros", {"category": "Expenses:Food:Dining"})  # yours
    labels.record_rule(opened, "zooplus", {"category": "Expenses:Pets"}, source="model")  # its own
    ask = _fake_ask({})
    llm_categorize.run(opened, ask=ask)
    sent = "\n".join(ask.seen)
    assert "migros" in sent and "zooplus" not in sent


def test_review_details_include_the_sources_full_record(opened):
    from fin import review

    _generic_ledger(opened)
    d = opened / "sources" / "swisscard"
    d.mkdir(parents=True)
    (d / "2026-10-01_2026-10-31.json").write_text(json.dumps(RAW))
    base.write_new(opened, "swisscard", [])  # nothing new; just keep the ledger loaded
    rows = review.details(opened, "kiosk")
    assert len(rows) == 1 and rows[0]["ref"] == "swisscard:T2"
    assert rows[0]["record"]["description"] == "Refund Kiosk"  # the portal's own wording, untrimmed
    assert rows[0]["record"]["currencyConversion"]["localAmount"]["currency"] == "EUR"
    assert rows[0]["meta"]["original_amount"] == "8.5 EUR"
    assert swisscard.raw_record(opened, "nope") is None


# ---- which card ----------------------------------------------------------------------------


def test_parse_records_a_short_card_id():
    raw = json.loads(json.dumps(RAW))
    raw["response"]["transactions"][0]["cardId"] = "AA12BC34" + "0" * 56
    assert swisscard.parse(raw)[0].meta["card"] == "AA12BC34"


def test_backfill_adds_missing_details_to_older_imports_without_overwriting(opened):
    old = swisscard.parse(RAW)
    for t in old:
        t.meta.pop("card", None)  # as the parser did before it recorded cards
    base.write_new(opened, "swisscard", old)
    assert "card" not in _txn(opened, "swisscard:T1").meta
    new = swisscard.parse(RAW)
    assert base.backfill_meta(opened, new) == 2
    t1 = _txn(opened, "swisscard:T1")
    assert t1.meta["card"] == "c1" and t1.meta["ref"] == "swisscard:T1"
    assert base.backfill_meta(opened, new) == 0  # idempotent
    path = opened / "ledger" / "imported" / "swisscard" / "2026" / "2026-10.beancount"
    assert path.read_text().count('  card: "c1"') == 2
    assert not ledger.load(config.main_ledger(opened)).errors


def test_card_names_relabel_without_touching_the_ledger(opened):
    from fin import cards

    base.write_new(opened, "swisscard", swisscard.parse(RAW))
    led = ledger.load(config.main_ledger(opened))
    assert cards.summary(led, opened)[0]["label"] is None
    cards.set_label(opened, "swisscard", "c1", "Anna (Visa)")
    assert cards.summary(led, opened)[0]["label"] == "Anna (Visa)"
    assert cards.summary(led, opened)[0]["count"] == 2
    cards.set_label(opened, "swisscard", "c1", "")
    assert cards.summary(led, opened)[0]["label"] is None
