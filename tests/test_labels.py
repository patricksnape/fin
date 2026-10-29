"""Editing labels writes plain Beancount back by ref, atomically, and the analytics follow."""

import subprocess
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from fin import config, demo, gitops, labels, ledger
from fin.api.app import create_app


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


def block(root, ref):
    """The text of one transaction, as written in its ledger file."""
    for f in root.joinpath("ledger").rglob("*.beancount"):
        if f'ref: "{ref}"' in f.read_text():
            return f.read_text().split(f'ref: "{ref}"')[1].split("\n\n")[0]
    raise AssertionError(ref)


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    root = tmp_path_factory.mktemp("labels") / "repo"
    demo.create_demo(root)
    return root


@pytest.fixture(autouse=True)
def pristine(root):
    yield
    git(root, "reset", "-q", "--hard", "HEAD")
    git(root, "clean", "-qfd")


@pytest.fixture
def client(root):
    return TestClient(create_app(root, backend=object()))


def txn(client, **eq):
    rows = client.get("/api/transactions?month=all&limit=5000").json()["rows"]
    return [r for r in rows if all(r[k] == v for k, v in eq.items())]


def test_recategorising_rewrites_one_posting_and_clears_the_review_tag(root, client):
    review = txn(client, needs_review=True, kind="expense")[0]
    r = client.put(
        "/api/transactions/labels",
        json={"ref": review["ref"], "category": "Expenses:Food:Dining"},
    )
    assert r.status_code == 200 and r.json()["changed"] == 1
    after = txn(client, ref=review["ref"])[0]
    assert after["category"] == "Expenses:Food:Dining" and not after["needs_review"]
    assert Decimal(after["amount"]) == Decimal(review["amount"])
    diff = git(root, "show", "--stat", "--format=%s", "HEAD")
    assert "labels: 1 transaction" in diff and diff.count("|") == 1  # one file touched
    assert not gitops.is_dirty(root)


def test_metadata_labels_are_set_replaced_and_cleared(root, client):
    t = txn(client, kind="expense", needs_review=False, major=False, recurring=False)[0]

    def put(body):
        return client.put("/api/transactions/labels", json={"ref": t["ref"], **body})

    assert put({"cadence": "one_off", "necessity": "essential", "major": True}).status_code == 200
    a = txn(client, ref=t["ref"])[0]
    assert (a["cadence"], a["necessity"], a["major"]) == ("one_off", "essential", True)
    put({"cadence": "regular"})  # replaces, does not duplicate
    seg = block(root, t["ref"])
    assert seg.count("cadence:") == 1 and "major: TRUE" in seg
    put({"cadence": None, "major": None})  # back to automatic
    seg = block(root, t["ref"])
    assert "cadence:" not in seg and "major:" not in seg and 'necessity: "essential"' in seg


def test_major_flag_takes_a_purchase_out_of_spend(root, client):
    t = txn(client, kind="expense", needs_review=False, major=False, recurring=False)[0]
    month = t["date"][:7]
    led = ledger.load(config.main_ledger(root))
    before = client.get("/api/spending?view=cash&months=240").json()["major"]
    client.put("/api/transactions/labels", json={"ref": t["ref"], "major": True})
    after = client.get("/api/spending?view=cash&months=240").json()["major"]
    assert len(after) == len(before) + 1 and month and led


def test_apply_to_similar_edits_every_listed_ref_in_one_commit(root, client):
    t = txn(client, payee="Migros", kind="expense")[0]
    sim = client.get(f"/api/transactions/similar?ref={t['ref']}").json()
    assert sim["payee"] == "Migros" and len(sim["rows"]) > 5
    assert t["ref"] not in {r["ref"] for r in sim["rows"]}
    assert {r["payee"] for r in sim["rows"]} == {"Migros"}
    refs = [r["ref"] for r in sim["rows"]]
    head = git(root, "rev-list", "--count", "HEAD")
    r = client.put(
        "/api/transactions/labels",
        json={"ref": t["ref"], "also": refs, "category": "Expenses:Food:Dining"},
    )
    assert r.json()["changed"] == len(refs) + 1
    assert {x["category"] for x in txn(client, payee="Migros", kind="expense")} == {
        "Expenses:Food:Dining"
    }
    now = git(root, "rev-list", "--count", "HEAD")
    assert int(now) == int(head) + 1


def test_bad_edits_change_nothing(root, client):
    t = txn(client, kind="expense")[0]
    before = git(root, "rev-parse", "HEAD")
    for body in (
        {"ref": t["ref"], "category": "Expenses:Nonexistent"},
        {"ref": t["ref"], "category": "Assets:UBS:Checking"},
        {"ref": t["ref"], "cadence": "sometimes"},
        {"ref": t["ref"], "necessity": "meh"},
        {"ref": t["ref"]},
    ):
        assert client.put("/api/transactions/labels", json=body).status_code == 400, body
    assert (
        client.put("/api/transactions/labels", json={"ref": "nope:1", "major": True}).status_code
        == 404
    )
    # a valid edit alongside an unknown ref must not be half applied
    r = client.put(
        "/api/transactions/labels", json={"ref": t["ref"], "also": ["nope:1"], "major": True}
    )
    assert r.status_code == 404
    assert not gitops.is_dirty(root)
    assert git(root, "rev-parse", "HEAD") == before


def test_a_split_transaction_category_is_ambiguous(root):
    led = ledger.load(config.main_ledger(root))
    from beancount.core import data

    multi = [
        e
        for e in led.entries
        if isinstance(e, data.Transaction)
        and sum(p.account.startswith("Expenses:") for p in e.postings) > 1
    ]
    if not multi:
        pytest.skip("demo has no split expense transactions")
    with pytest.raises(ValueError, match="ambiguous"):
        labels.apply(root, {multi[0].meta["ref"]: {"category": "Expenses:Food:Dining"}})
    assert not gitops.is_dirty(root)


def test_options_list_open_expense_and_income_accounts(client):
    o = client.get("/api/labels/options").json()
    assert "Expenses:Food:Groceries" in o["categories"]
    assert all(c.startswith(("Expenses:", "Income:")) for c in o["categories"])
    assert o["cadences"] == ["recurring", "regular", "one_off"]


NEW = """
2026-10-16 * "TWINT P2P" "Card payment" #review
  ref: "test:{n}"
  Expenses:Uncategorized  12.50 CHF
  Liabilities:Swisscard:MilesMore  -12.50 CHF
"""


def add_import(root, n):
    """What an importer would write: a fresh, unlabelled payment."""
    path = root / "ledger" / "manual" / "incoming.beancount"
    path.write_text((path.read_text() if path.exists() else "") + NEW.format(n=n))
    git(root, "add", "-A")
    git(root, "commit", "-qm", "import")
    return f"test:{n}"


def twint(client):
    return txn(client, payee="TWINT P2P")[0]


def test_a_remembered_rule_labels_the_next_import(root, client):
    t = twint(client)
    r = client.put(
        "/api/transactions/labels",
        json={
            "ref": t["ref"],
            "category": "Expenses:Food:Dining",
            "cadence": "one_off",
            "remember": True,
        },
    ).json()
    assert r["rule"] == "twint p p"
    text = (root / "ledger" / "manual" / "rules.beancount").read_text()
    assert 'custom "fin-rule" "twint p p"' in text and 'category: "Expenses:Food:Dining"' in text
    assert client.get("/api/rules").json() == [
        {"payee": "twint p p", "labels": {"category": "Expenses:Food:Dining", "cadence": "one_off"}}
    ]
    new = add_import(root, 1)
    assert txn(client, ref=new)[0]["needs_review"]
    assert labels.apply_rules(root) >= 1
    got = txn(client, ref=new)[0]
    assert got["category"] == "Expenses:Food:Dining" and got["cadence"] == "one_off"
    assert not got["needs_review"]
    assert labels.apply_rules(root) == 0  # nothing left to do
    assert not gitops.is_dirty(root)


def test_rules_do_not_override_a_category_that_was_already_chosen(root, client):
    labels.record_rule(root, "twint p p", {"category": "Expenses:Food:Dining"})
    labels.apply_rules(root)
    mine = txn(client, payee="TWINT P2P")[0]
    client.put(
        "/api/transactions/labels", json={"ref": mine["ref"], "category": "Expenses:Shopping:Home"}
    )
    labels.apply_rules(root)
    assert txn(client, ref=mine["ref"])[0]["category"] == "Expenses:Shopping:Home"


def test_latest_rule_wins_and_forgetting_stops_it(root, client):
    labels.record_rule(root, "twint p p", {"category": "Expenses:Food:Dining"})
    labels.record_rule(root, "twint p p", {"category": "Expenses:Shopping:Home"})
    assert labels.read_rules(ledger.load(config.main_ledger(root)))["twint p p"]["category"] == (
        "Expenses:Shopping:Home"
    )
    assert client.delete("/api/rules/twint p p").status_code == 200
    assert client.get("/api/rules").json() == []
    new = add_import(root, 2)
    assert labels.apply_rules(root) == 0
    assert txn(client, ref=new)[0]["category"] == "Expenses:Uncategorized"
    assert client.delete("/api/rules/twint p p").status_code == 404


def test_label_only_rule_fills_missing_labels_but_keeps_explicit_ones(root, client):
    mine = txn(client, payee="Migros")
    client.put(
        "/api/transactions/labels", json={"ref": mine[0]["ref"], "necessity": "discretionary"}
    )
    labels.record_rule(root, "migros", {"necessity": "essential"})
    assert labels.apply_rules(root) == len(mine) - 1
    got = txn(client, payee="Migros")
    assert {x["necessity"] for x in got if x["ref"] != mine[0]["ref"]} == {"essential"}
    assert next(x for x in got if x["ref"] == mine[0]["ref"])["necessity"] == "discretionary"
    assert {x["category"] for x in got} == {x["category"] for x in mine}  # category untouched


def test_bad_rules_are_refused(root, client):
    for payee, found in (("Bad Payee", {}), ("x", {"category": "Expenses:Nope"}), ("", {})):
        with pytest.raises(ValueError):
            labels.record_rule(root, payee, found or {"cadence": "sometimes"})
    assert not gitops.is_dirty(root)
    t = txn(client, kind="expense")[0]
    r = client.put(
        "/api/transactions/labels",
        json={"ref": t["ref"], "category": "Expenses:Nope", "remember": True},
    )
    assert r.status_code == 400


# ---- tags --------------------------------------------------------------------------------------


def header(root, ref):
    for f in root.joinpath("ledger").rglob("*.beancount"):
        lines = f.read_text().split("\n")
        for i, ln in enumerate(lines):
            if f'ref: "{ref}"' in ln:
                return lines[i - 1]
    raise AssertionError(ref)


def test_tags_are_added_to_many_transactions_as_plain_beancount_tags(root, client):
    picks = txn(client, kind="expense", needs_review=False)[:3]
    refs = [p["ref"] for p in picks]
    r = client.put(
        "/api/transactions/labels",
        json={"ref": refs[0], "also": refs[1:], "tags_add": ["Canada 2026", "#holiday"]},
    )
    assert r.status_code == 200 and r.json()["changed"] == 3
    for ref in refs:
        assert header(root, ref).endswith(" #canada-2026 #holiday")
        assert {"canada-2026", "holiday"} <= set(txn(client, ref=ref)[0]["tags"])
    # adding again does not duplicate; removing takes one off
    client.put("/api/transactions/labels", json={"ref": refs[0], "tags_add": ["holiday"]})
    assert header(root, refs[0]).count("#holiday") == 1
    client.put("/api/transactions/labels", json={"ref": refs[0], "tags_remove": ["holiday"]})
    assert header(root, refs[0]).endswith(" #canada-2026")
    assert header(root, refs[1]).endswith(" #canada-2026 #holiday")
    assert git(root, "log", "-2", "--format=%s").count("labels:") == 2


def test_the_tag_filter_and_summary_add_up(client):
    picks = txn(client, kind="expense", needs_review=False)[:4]
    client.put(
        "/api/transactions/labels",
        json={"ref": picks[0]["ref"], "also": [p["ref"] for p in picks[1:]], "tags_add": ["trip"]},
    )
    r = client.get("/api/transactions?month=all&tag=trip&limit=5000").json()
    assert r["matching"] == 4 and {x["ref"] for x in r["rows"]} == {p["ref"] for p in picks}
    summary = next(t for t in r["tags"] if t["tag"] == "trip")
    assert summary["count"] == 4
    assert Decimal(summary["spend"]) == -sum(Decimal(x["amount"]) for x in r["rows"])
    assert (summary["first"], summary["last"]) == (
        min(x["date"] for x in r["rows"]),
        max(x["date"] for x in r["rows"]),
    )
    assert "review" not in {t["tag"] for t in r["tags"]}
    both = client.get("/api/transactions?month=all&tag=trip,nothing&limit=5000").json()
    assert both["matching"] == 4
    assert client.get("/api/transactions?month=all&tag=nothing").json()["matching"] == 0


def test_the_demo_trips_are_tagged(client):
    tags = {t["tag"]: t for t in client.get("/api/transactions").json()["tags"]}
    assert {"rome-2025", "lisbon-2025", "paris-2026", "vienna-2026"} <= set(tags)
    assert tags["paris-2026"]["count"] >= 6 and Decimal(tags["paris-2026"]["spend"]) > 500


def test_tagging_keeps_links_comments_and_other_tags_and_works_on_any_kind(root, client):
    path = root / "ledger" / "manual" / "odd.beancount"
    path.write_text(
        '2026-10-16 * "Cafe" "Coffee" #old ^receipt-1 ; keep this\n  ref: "odd:1"\n'
        "  Expenses:Food:Dining  4.00 CHF\n  Assets:UBS:Checking  -4.00 CHF\n"
    )
    git(root, "add", "-A")
    git(root, "commit", "-qm", "odd")
    client.put("/api/transactions/labels", json={"ref": "odd:1", "tags_add": ["new"]})
    assert header(root, "odd:1") == '2026-10-16 * "Cafe" "Coffee" #old #new ^receipt-1 ; keep this'
    client.put("/api/transactions/labels", json={"ref": "odd:1", "tags_remove": ["old", "new"]})
    assert header(root, "odd:1") == '2026-10-16 * "Cafe" "Coffee" ^receipt-1 ; keep this'
    transfer = txn(client, kind="transfer")[0]
    assert (
        client.put(
            "/api/transactions/labels", json={"ref": transfer["ref"], "tags_add": ["moving"]}
        ).status_code
        == 200
    )
    assert "moving" in txn(client, ref=transfer["ref"])[0]["tags"]


def test_bad_tags_are_refused(root, client):
    t = txn(client, kind="expense")[0]
    head = git(root, "rev-parse", "HEAD")
    for body in (
        {"tags_add": ["!!"]},
        {"tags_add": [""]},
        {"tags_add": []},
        {"tags_remove": ["a b!"]},
    ):
        assert (
            client.put("/api/transactions/labels", json={"ref": t["ref"], **body}).status_code
            == 400
        )
    assert git(root, "rev-parse", "HEAD") == head and not gitops.is_dirty(root)


def test_payee_key_ignores_invoice_codes():
    from fin.analytics.recurring import normalize_payee

    assert (
        normalize_payee("Spotify P32B87DC9F") == normalize_payee("Spotify P3FC407C99") == "spotify"
    )
    assert normalize_payee("APPLE.COM/BILL") == "apple com bill"
    assert normalize_payee("7-Eleven") == "eleven" and normalize_payee("Migros M2") == "migros m"
    assert normalize_payee("AB12CD34") == "ab cd"  # nothing else left: keep the letters


def test_income_can_be_categorized_and_reviewed(tmp_path):
    from fin import config, gitops, labels, ledger, review, scaffold

    root = tmp_path / "r"
    scaffold.init_data(root)
    with (root / "ledger" / "accounts.beancount").open("a") as f:
        f.write(
            '2020-01-01 open Assets:Bank CHF\n  group: "Cash"\n2020-01-01 open Income:Uncategorized\n'
            "2020-01-01 open Income:Employer:Bonus\n"
            '2020-01-01 open Expenses:Business:Reimbursable\n  cadence: "one_off"\n  necessity: "essential"\n'
            '\n2026-03-25 * "Employer" "credit" #review\n  ref: "t:1"\n  Assets:Bank  80.00 CHF\n  Income:Uncategorized\n'
            '\n2026-03-26 * "Employer" "salary" #review\n  ref: "t:2"\n  Assets:Bank  9000.00 CHF\n  Income:Uncategorized\n'
        )
    gitops.commit_all(root, "setup")
    led = ledger.load(config.main_ledger(root))
    assert [r["payee_key"] for r in review.queue(led)] == [
        "employer"
    ]  # money in waits for review too
    labels.apply(
        root,
        {
            "t:1": {"category": "Expenses:Business:Reimbursable"},
            "t:2": {"category": "Income:Employer:Bonus"},
        },
    )
    led = ledger.load(config.main_ledger(root))
    accounts = {
        e.meta["ref"]: [p.account for p in e.postings]
        for e in led.entries
        if hasattr(e, "postings")
    }
    assert accounts == {
        "t:1": ["Assets:Bank", "Expenses:Business:Reimbursable"],
        "t:2": ["Assets:Bank", "Income:Employer:Bonus"],
    }
    assert not led.errors and not review.queue(led)


def test_a_rule_with_an_income_category_fills_an_uncategorized_credit(data):
    from datetime import date

    from fin import config, ledger
    from fin.importers import base

    with (data / "ledger" / "accounts.beancount").open("a") as f:
        f.write(
            "".join(
                f"2019-01-01 open {a}\n"
                for a in ("Assets:UBS:Checking", "Income:Uncategorized", "Income:Employer:Salary")
            )
        )
    base.write_new(data, "ubs", [base.RawTxn(
        date=date(2020, 1, 25), account="Assets:UBS:Checking", amount=Decimal(5000), currency="CHF",
        payee="ACME AG", narration="", ref="ubs:pay", counter="Income:Uncategorized",
    )])  # fmt: skip
    labels.record_rule(data, "acme ag", {"category": "Income:Employer:Salary"})
    assert labels.apply_rules(data) == 1
    led = ledger.load(config.main_ledger(data))
    assert not led.errors
    assert any(
        p.account == "Income:Employer:Salary"
        for e in led.entries
        if hasattr(e, "postings")
        for p in e.postings
    )
    assert labels.apply_rules(data) == 0


def test_add_meta_adds_lines_once_and_commits(root, client):
    ref = txn(client, kind="expense")[0]["ref"]
    n = labels.add_meta(root, {ref: {"award_date": "2023-03-20"}}, "meta: test")
    assert n == 1 and '  award_date: "2023-03-20"' in block(root, ref)
    assert git(root, "log", "-1", "--format=%s").strip() == "meta: test"
    assert labels.add_meta(root, {ref: {"award_date": "2020-01-01"}}, "meta: again") == 0  # already there
    assert '2020-01-01' not in block(root, ref)
    with pytest.raises(KeyError):
        labels.add_meta(root, {"nope:1": {"award_date": "2023-03-20"}}, "meta: none")
