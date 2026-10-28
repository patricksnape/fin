"""Account institution and group: metadata on `open`, path fallbacks, and the write path."""

import subprocess
from datetime import date
from decimal import Decimal

import pytest
from beancount import loader
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from fin import account_admin, config, demo, gitops, ledger, ledger_settings, registry
from fin.analytics import networth
from fin.api.app import create_app
from fin.cli import app as cli

runner = CliRunner()


def synthetic(text: str) -> ledger.Ledger:
    entries, errors, options = loader.load_string(text)
    return ledger.Ledger(entries, errors, options, path=None)


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    root = tmp_path_factory.mktemp("registry") / "repo"
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


def reg_of(root):
    return registry.of(ledger.load(config.main_ledger(root)))


# ---- resolution --------------------------------------------------------------------------------


def test_metadata_wins_and_the_path_is_the_fallback():
    led = synthetic(
        """
2024-01-01 open Assets:Revolut:Main
2024-01-01 open Assets:Broker:Fund
2024-01-01 open Assets:Pension:Fund
2024-01-01 open Assets:UBS:Savings
  group: "investments"
  institution: "UBS Bank"
2024-01-01 open Liabilities:Mortgage:Bank
2024-01-01 open Liabilities:Visa:Card
2024-01-01 open Assets:Wallet:Cash:CHF
2024-01-01 open Assets:Broker:Odd
  group: "Cash"
2024-01-01 open Assets:Broker:Bad
  group: "Mortgage"
2024-01-02 * "x"
  Assets:Revolut:Main  10 CHF
  Assets:Broker:Fund   5 VT {1 CHF}
  Assets:Broker:Odd    1 CHF
  Equity:Opening       -11 CHF
"""
    )
    r = registry.of(led)
    assert (r.group("Assets:Revolut:Main"), r.get("Assets:Revolut:Main").guessed) == ("Cash", True)
    assert (r.group("Assets:Broker:Fund"), r.get("Assets:Broker:Fund").guessed) == (
        "Investments",
        True,
    )
    assert r.group("Assets:Pension:Fund") == "Pension" and not r.get("Assets:Pension:Fund").guessed
    s = r.get("Assets:UBS:Savings")
    assert (s.group, s.institution, s.guessed) == ("Investments", "UBS Bank", False)
    m = r.get("Liabilities:Mortgage:Bank")
    assert (m.group, m.choice, m.role) == ("Liabilities", "Mortgage", "mortgage")
    assert (
        r.group("Liabilities:Visa:Card") == "Liabilities"
        and r.role("Liabilities:Visa:Card") is None
    )
    assert r.group("Assets:Wallet:Cash:CHF") == "Cash"
    assert r.group("Assets:Broker:Odd") == "Cash"  # explicit beats the guess (it holds CHF only)
    bad = r.get("Assets:Broker:Bad")
    assert bad.problem and "Mortgage" in bad.problem and bad.group == "Investments"  # falls back
    assert r.institution("Assets:Revolut:Main") == "Revolut"
    assert r.get("Assets:Never:Opened").group == "Investments"  # unknown accounts still resolve


def test_demo_accounts_are_explicit_and_the_card_belongs_to_ubs(root):
    r = reg_of(root)
    assert not [a.account for a in r.balance_sheet() if a.guessed or a.problem]
    card = r.get("Liabilities:UBS:CreditCard")
    assert (card.institution, card.group) == ("UBS", "Liabilities")
    mortgage = r.get("Liabilities:Mortgage:UBS")
    assert (mortgage.institution, mortgage.group, mortgage.role) == (
        "UBS",
        "Liabilities",
        "mortgage",
    )
    assert (
        r.role("Assets:Property:Home") == "property" and r.role("Assets:Vehicle:Car") == "vehicle"
    )


# ---- the numbers follow the settings -----------------------------------------------------------


def test_changing_a_group_moves_its_value_between_net_worth_groups(root):
    led = ledger.load(config.main_ledger(root))
    before = networth.net_worth(led)
    last = before["date"].max()
    now = before[before["date"] == last].set_index("group")["value"]
    account_admin.set_account(root, "Assets:Wise:EUR", {"group": "Other"})
    led2 = ledger.load(config.main_ledger(root))
    after = networth.net_worth(led2)
    then = after[after["date"] == last].set_index("group")["value"]
    moved = then["Other"] - now["Other"]
    assert moved > 0 and abs((now["Cash"] - then["Cash"]) - moved) <= Decimal(
        "0.01"
    )  # cents rounding
    assert abs(then.sum() - now.sum()) <= Decimal("0.01")  # nothing created or lost


def test_the_ubs_card_is_in_net_worth_and_under_ubs(root, client):
    inst = {i["name"]: i for i in client.get("/api/accounts").json()["institutions"]}
    names = {a["account"] for a in inst["UBS"]["accounts"]}
    assert {
        "Assets:UBS:Checking",
        "Liabilities:UBS:CreditCard",
        "Liabilities:Mortgage:UBS",
    } <= names
    assert "Mortgage" not in inst


def test_institution_filter_includes_debts_as_well_as_assets(root, client):
    rows = client.get("/api/transactions?month=all&account=inst:UBS&limit=5000").json()["rows"]
    seen = {p["account"] for r in rows for p in r["postings"]}
    assert {"Assets:UBS:Checking", "Liabilities:UBS:CreditCard", "Liabilities:Mortgage:UBS"} <= seen
    none = client.get("/api/transactions?month=all&account=inst:Nope").json()
    assert none["matching"] == 0


# ---- adding, editing, closing ------------------------------------------------------------------


def test_adding_a_credit_card_writes_a_plain_open_line(root, client):
    text = account_admin.add_account(
        root,
        "Liabilities:Revolut:Card",
        institution="Revolut Ltd",
        currencies=("chf",),
        since=date(2026, 1, 1),
    )
    assert text == (
        '2026-01-01 open Liabilities:Revolut:Card CHF\n  institution: "Revolut Ltd"\n  group: "Liabilities"\n'
    )
    assert text in (root / account_admin.ACCOUNTS_FILE).read_text()
    a = reg_of(root).get("Liabilities:Revolut:Card")
    assert (a.institution, a.group, a.guessed) == ("Revolut Ltd", "Liabilities", False)
    rows = {r["account"]: r for r in client.get("/api/account-settings").json()["accounts"]}
    assert rows["Liabilities:Revolut:Card"]["transactions"] == 0
    assert git(root, "log", "-1", "--format=%s").strip() == "account: open Liabilities:Revolut:Card"
    assert not gitops.is_dirty(root)


def test_an_asset_needs_a_group_unless_the_name_or_currency_settles_it(root):
    with pytest.raises(ValueError, match="choose a group"):
        account_admin.add_account(root, "Assets:Broker:Fund")
    assert 'group: "Cash"' in account_admin.add_account(
        root, "Assets:Bank:Main", currencies=("CHF",)
    )
    assert 'group: "Pension"' in account_admin.add_account(root, "Assets:Pension:Pillar3a")
    assert 'group: "Investments"' in account_admin.add_account(
        root, "Assets:Broker:Fund", group="investments"
    )
    assert not gitops.is_dirty(root)


def test_expense_categories_carry_cadence_and_necessity(root):
    text = account_admin.add_account(
        root, "Expenses:Pets:Vet", cadence="one_off", necessity="essential", since=date(2026, 1, 1)
    )
    assert 'cadence: "one_off"' in text and 'necessity: "essential"' in text and "group" not in text
    assert 'cadence: "regular"' in account_admin.add_account(root, "Expenses:Pets:Food")
    assert account_admin.add_account(root, "Income:Gifts:Birthday").startswith(str(date.today()))


def test_bad_accounts_are_refused_and_nothing_is_written(root):
    head = git(root, "rev-parse", "HEAD")
    for name, kw in (
        ("liabilities:ubs:card", {}),
        ("Savings", {}),
        ("Assets:UBS:Checking", {}),  # exists
        ("Liabilities:X:Y", {"group": "Cash"}),  # an asset group on a debt
        ("Assets:X:Y", {"group": "Mortgage"}),
        ("Assets:X:Z", {"group": "Cash", "currencies": ("not a currency",)}),
        ("Expenses:X:Y", {"cadence": "sometimes"}),
        ("Income:X:Y", {"group": "Cash"}),
        ("Equity:X:Y", {}),
    ):
        with pytest.raises(ValueError):
            account_admin.add_account(root, name, **kw)
    assert git(root, "rev-parse", "HEAD") == head and not gitops.is_dirty(root)


def test_editing_group_and_institution_rewrites_only_that_open_block(root):
    account_admin.set_account(root, "Liabilities:Swisscard:MilesMore", {"institution": "Cembra"})
    account_admin.set_account(root, "Assets:Wise:CHF", {"group": "Investments"})
    a = reg_of(root)
    assert a.institution("Liabilities:Swisscard:MilesMore") == "Cembra"
    assert a.group("Assets:Wise:CHF") == "Investments"
    diff = git(root, "diff", "-U0", "HEAD~2", "HEAD").splitlines()
    changed = [ln for ln in diff if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
    assert sorted(changed) == sorted(
        ['+  institution: "Cembra"', '-  group: "Cash"', '+  group: "Investments"']
    )
    account_admin.set_account(root, "Liabilities:Swisscard:MilesMore", {"institution": None})
    assert reg_of(root).institution("Liabilities:Swisscard:MilesMore") == "Swisscard"
    with pytest.raises(ValueError, match="group must be"):
        account_admin.set_account(root, "Liabilities:Swisscard:MilesMore", {"group": "Cash"})
    with pytest.raises(KeyError):
        account_admin.set_account(root, "Assets:Nope:Nope", {"group": "Cash"})
    assert not gitops.is_dirty(root)


def test_closing_needs_an_empty_account(root):
    with pytest.raises(ValueError, match="still holds"):
        account_admin.close_account(root, "Assets:UBS:Checking")
    account_admin.add_account(root, "Assets:Bank:Temp", currencies=("CHF",))
    account_admin.close_account(root, "Assets:Bank:Temp")
    assert reg_of(root).get("Assets:Bank:Temp").closed == date.today()
    with pytest.raises(ValueError, match="already closed"):
        account_admin.close_account(root, "Assets:Bank:Temp")
    with pytest.raises(ValueError):  # and nothing can be opened twice
        account_admin.add_account(root, "Assets:Bank:Temp", currencies=("CHF",))


def test_the_api_adds_edits_and_closes(root, client):
    r = client.post(
        "/api/account-settings", json={"account": "Liabilities:Amex:Gold", "institution": "Amex"}
    )
    assert r.status_code == 200 and 'group: "Liabilities"' in r.json()["written"]
    got = {a["account"]: a for a in client.get("/api/account-settings").json()["accounts"]}
    assert (
        got["Liabilities:Amex:Gold"]["institution"] == "Amex"
        and got["Assets:UBS:Checking"]["group"] == "Cash"
    )
    assert (
        client.put(
            "/api/account-settings/Liabilities:Amex:Gold", json={"institution": "AmEx"}
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/api/account-settings/Liabilities:Amex:Gold", json={"group": "Cash"}
        ).status_code
        == 400
    )
    assert (
        client.put("/api/account-settings/Assets:Nope:X", json={"group": "Cash"}).status_code == 404
    )
    assert client.post("/api/account-settings", json={"account": "bad name"}).status_code == 400
    assert client.post("/api/account-settings/Assets:UBS:Checking/close").status_code == 400
    assert client.post("/api/account-settings/Liabilities:Amex:Gold/close").status_code == 200
    assert client.post("/api/account-settings/Nope:X/close").status_code == 404


def test_the_cli_lists_adds_sets_and_closes(root, monkeypatch):
    monkeypatch.setenv("FIN_DATA", str(root))
    out = runner.invoke(cli, ["account", "list"]).output
    assert "UBS" in out and "Liabilities:UBS:CreditCard" in out and "guessed" not in out
    r = runner.invoke(
        cli,
        ["account", "add", "Liabilities:Zkb:Card", "--institution", "ZKB", "--since", "2026-01-01"],
    )
    assert r.exit_code == 0 and 'group: "Liabilities"' in r.output
    assert (
        runner.invoke(
            cli, ["account", "set", "Liabilities:Zkb:Card", "--institution", "Zurcher"]
        ).exit_code
        == 0
    )
    assert runner.invoke(cli, ["account", "set", "Liabilities:Zkb:Card"]).exit_code != 0
    assert runner.invoke(cli, ["account", "add", "Assets:Foo:Bar"]).exit_code != 0
    assert runner.invoke(cli, ["account", "close", "Assets:UBS:Checking"]).exit_code != 0
    assert runner.invoke(cli, ["account", "close", "Liabilities:Zkb:Card"]).exit_code == 0


# ---- doctor ------------------------------------------------------------------------------------


def test_doctor_flags_accounts_counted_by_a_guess_and_invalid_groups(root):
    def checks():
        led = ledger.load(config.main_ledger(root))
        return {n: (ok, d) for n, ok, d in ledger_settings.doctor(root, led)}

    assert checks()["every account has a group"][0] and checks()["account groups are valid"][0]
    path = root / "ledger" / "manual" / "extra.beancount"
    path.write_text(
        '2026-01-01 open Assets:Revolut:Main\n2026-01-01 open Assets:Revolut:Bad\n  group: "Mortgage"\n\n'
        '2026-02-01 * "t"\n  ref: "t:1"\n  Assets:Revolut:Main  10 CHF\n  Assets:Revolut:Bad  5 CHF\n'
        "  Assets:UBS:Checking  -15 CHF\n"
    )
    got = checks()
    ok, detail = got["every account has a group"]
    assert (
        not ok and "Assets:Revolut:Main (counted as Cash)" in detail and "fin account set" in detail
    )
    ok, detail = got["account groups are valid"]
    assert not ok and "Assets:Revolut:Bad" in detail
    account_admin.set_account(root, "Assets:Revolut:Main", {"group": "Cash"})
    assert checks()["every account has a group"][0]
