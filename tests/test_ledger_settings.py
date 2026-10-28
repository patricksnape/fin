"""Settings that change the numbers live in the ledger, where any Beancount tool can see them."""

from datetime import date
from decimal import Decimal

import pytest

from fin import config, demo, ledger, ledger_settings
from fin.analytics import allocation, recurring, spend
from fin.gitops import git, is_dirty


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "repo"
    demo.create_demo(root)
    return root


def load(root):
    return ledger.load(root / "ledger" / "main.beancount")


def add(root, rel, text):
    path = root / "ledger" / rel
    path.write_text((path.read_text() + "\n" if path.exists() else "") + text)


def test_settings_come_from_the_ledger(root):
    s = ledger_settings.read(load(root))
    assert s.base_currency == "CHF"
    assert s.major_threshold == Decimal(2000)
    assert s.major_accounts == ["Expenses:Housing:Renovation"]
    assert s.employer_commodity == "ACME"
    assert s.targets == {
        "equity": Decimal("0.75"),
        "bonds": Decimal("0.15"),
        "cash": Decimal("0.10"),
    }
    assert s.confirmed == {
        "serafe|Expenses:Government:Serafe",
        "axa versicherungen|Expenses:Insurance:Liability",
        "jetbrains|Expenses:Subscriptions:Software",
        "mobiliar|Expenses:Insurance:Home",
    }
    assert not s.problems


def test_other_beancount_tools_see_the_same_settings(root):
    """BQL reads the account and commodity metadata; the custom directives are plain ledger entries."""
    main = root / "ledger" / "main.beancount"
    (flag,) = ledger.query(
        main, "SELECT open_meta('Expenses:Housing:Renovation', 'major') FROM postings LIMIT 1"
    )[1][0]
    assert flag is True
    (emp,) = ledger.query(main, "SELECT commodity_meta('ACME', 'employer') FROM postings LIMIT 1")[
        1
    ][0]
    assert emp is True
    customs = {e.type for e in load(root).entries if type(e).__name__ == "Custom"}
    assert customs == {"fin-major-threshold", "fin-allocation-target", "fin-recurring", "fin-fire"}
    assert "fin-allocation-target" in (root / "ledger" / "settings.beancount").read_text()


def test_config_toml_no_longer_holds_them(root):
    assert config.stale_config_keys(root) == []
    (root / "config.toml").write_text(
        "major_threshold = 5000\ntargets = {equity = 1}\nnon_spend_prefixes = []\n"
    )
    assert config.stale_config_keys(root) == ["major_threshold", "targets"]
    # and they are ignored: the ledger wins
    s = config.load_settings(root, load(root))
    assert s.major_threshold == Decimal(2000) and s.non_spend_prefixes == []


def test_latest_directive_wins(root):
    add(
        root,
        "manual/changes.beancount",
        '2026-05-01 custom "fin-major-threshold" 1500 CHF\n\n2026-06-01 custom "fin-allocation-target" "equity" 0.60\n',
    )
    s = ledger_settings.read(load(root))
    assert s.major_threshold == Decimal(1500) and s.targets["equity"] == Decimal("0.60")


def test_the_threshold_changes_what_counts_as_major(root):
    led = load(root)
    base = spend.build(led, config.load_settings(root, led))
    assert set(spend.major_purchases(base)["payee"]) == {"Küchen Schmid AG", "IKEA", "Digitec"}
    add(root, "manual/changes.beancount", '2026-01-01 custom "fin-major-threshold" 2200 CHF\n')
    led = load(root)
    raised = spend.build(led, config.load_settings(root, led))
    assert set(spend.major_purchases(raised)["payee"]) == {
        "Küchen Schmid AG",
        "IKEA",
    }  # the 2,100 laptop is no longer major


def test_a_transaction_can_be_marked_major_in_the_ledger(root):
    path = next((root / "ledger" / "imported" / "swisscard" / "2026").glob("2026-01.beancount"))
    text = path.read_text()
    assert "Digitec" in text
    path.write_text(text.replace('"Digitec" "Laptop"', '"Digitec" "Laptop"\n  major: FALSE', 1))
    led = load(root)
    m = spend.build(led, config.load_settings(root, led))
    assert "Digitec" not in set(spend.major_purchases(m)["payee"])


def test_targets_flow_into_the_allocation_view(root):
    add(
        root,
        "manual/changes.beancount",
        '2026-06-01 custom "fin-allocation-target" "equity" 0.60\n',
    )
    led = load(root)
    s = config.load_settings(root, led)
    a = allocation.allocation(
        allocation.holdings(led, s.base_currency), "asset_class", s.targets
    ).set_index("asset_class")
    assert a.loc["equity", "target"] == Decimal("0.60")


def test_bad_directives_are_reported_not_ignored(root):
    add(
        root,
        "manual/bad.beancount",
        '2026-01-01 custom "fin-major-threshold" "lots"\n\n2026-01-02 custom "fin-recurring" "maybe" "x" Expenses:Fees:Broker\n\n2026-01-03 custom "fin-allocation-target" "equity"\n',
    )
    problems = ledger_settings.read(load(root)).problems
    assert len(problems) == 3 and all("bad fin-" in p for p in problems)


def test_as_of_ignores_directives_dated_after_the_last_transaction(root):
    before = ledger.as_of(load(root))
    add(root, "manual/later.beancount", '2030-01-01 custom "fin-major-threshold" 2000 CHF\n')
    led = load(root)
    assert ledger.as_of(led) == before == date(2026, 10, 14)
    assert spend.build(led, config.load_settings(root, led)).as_of == before


def test_confirming_a_series_writes_a_ledger_line_and_commits(root):
    assert "x|Expenses:Fees:Broker" not in recurring.load_state(load(root))[0]
    ledger_settings.record_recurring(
        root, "confirmed", "interactive brokers", "Expenses:Fees:Broker", date(2026, 10, 16)
    )
    line = (root / ledger_settings.RECURRING_FILE).read_text().strip().splitlines()[-1]
    assert (
        line
        == '2026-10-16 custom "fin-recurring" "confirmed" "interactive brokers" Expenses:Fees:Broker'
    )
    assert "interactive brokers|Expenses:Fees:Broker" in recurring.load_state(load(root))[0]
    assert not is_dirty(root) and "recurring: confirmed" in git(root, "log", "-1", "--format=%s")
    ledger_settings.record_recurring(
        root, "rejected", "interactive brokers", "Expenses:Fees:Broker", date(2026, 10, 17)
    )
    confirmed, rejected = recurring.load_state(load(root))
    assert (
        "interactive brokers|Expenses:Fees:Broker" in rejected
        and "interactive brokers|Expenses:Fees:Broker" not in confirmed
    )


def test_bad_confirmations_are_refused_before_writing(root):
    before = (root / ledger_settings.RECURRING_FILE).read_text()
    for payee, account in (
        ("x", "Expenses:Does:NotExist"),
        ('say "hi"', "Expenses:Fees:Broker"),
        ("Upper Case", "Expenses:Fees:Broker"),
    ):
        with pytest.raises(ValueError):
            ledger_settings.record_recurring(root, "confirmed", payee, account, date(2026, 10, 16))
    with pytest.raises(ValueError, match="status"):
        ledger_settings.record_recurring(root, "maybe", "x", "Expenses:Fees:Broker")
    assert (root / ledger_settings.RECURRING_FILE).read_text() == before and not is_dirty(root)


def test_a_write_that_breaks_the_ledger_is_rolled_back(root, monkeypatch):
    from types import SimpleNamespace

    real = ledger.load
    calls = []

    def failing_after_the_write(path):
        calls.append(1)
        if len(calls) == 1:  # the pre-check passes; the post-write check reports an error
            return real(path)
        return SimpleNamespace(
            errors=[SimpleNamespace(message="boom", source={"filename": "x", "lineno": 1})]
        )

    monkeypatch.setattr(ledger, "load", failing_after_the_write)
    before = (root / ledger_settings.RECURRING_FILE).read_text()
    with pytest.raises(ValueError, match="boom"):
        ledger_settings.record_recurring(
            root, "confirmed", "interactive brokers", "Expenses:Fees:Broker", date(2026, 10, 16)
        )
    assert (root / ledger_settings.RECURRING_FILE).read_text() == before and not is_dirty(root)
