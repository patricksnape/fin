import json

import pytest

from fin.importers import base, ubs


def _tx(id, amount, head, status="booked", deal="payments", booking="2026-10-24", **extra):
    credit = not amount.startswith("-")
    return {
        "_id": id,
        "amount": amount,
        "currency": "CHF",
        "status": status,
        "debitCreditCode": "credit" if credit else "debit",
        "bookingDate": booking,
        "tradeDate": booking,
        "valueDate": extra.pop("value", booking),
        "businessTransactionId": f"BT{id}",
        "bankTransactionCode": {"proprietary": {"dealType": deal}},
        "_embedded": {
            "description": {
                "heading": {
                    "headlineElements": [
                        {"text": {"value": head[0]}},
                        *(
                            [{"separator": True}, {"text": {"value": head[1]}}]
                            if len(head) > 1
                            else []
                        ),
                    ],
                    "bylineElements": [],
                },
                "subheading": {
                    "headlineElements": [{"text": {"value": s}} for s in extra.get("sub", [])]
                },
                "bodyElements": [
                    {
                        "list": [
                            {
                                "lineItems": [
                                    {
                                        "items": [
                                            {
                                                "labelElements": [
                                                    {"text": {"value": "Reason for payment"}}
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        "items": [
                                            {"text": {"value": "Lunch"}},
                                            {"separator": True},
                                            {"text": {"value": "Ref 1"}},
                                        ]
                                    },
                                ]
                            },
                            {
                                "lineItems": [
                                    {
                                        "items": [
                                            {
                                                "labelElements": [
                                                    {"text": {"value": "Transaction no."}}
                                                ]
                                            }
                                        ]
                                    },
                                    {"items": [{"text": {"value": f"BT{id}"}}]},
                                ]
                            },
                        ]
                    }
                ],
            }
        },
    }


def _raw(*txns, name="Current"):
    return {
        "account": {"displayName": name},
        "pages": [
            {"_embedded": {"cash-transactions": list(txns[:2])}},
            {"_embedded": {"cash-transactions": list(txns[2:])}},
        ],
    }


def test_parse_signs_status_and_text():
    raw = _raw(
        _tx("a", "-12.50", ["CAFE ZUM BAR", "Payment UBS TWINT"]),
        _tx("b", "100.00", ["Employer AG"], sub=["Salary"]),
        _tx("c", "-5.00", ["Pending shop"], status="pending"),
        _tx("d", "-3.00", ["Balance closing of service prices"], deal=ubs.FEES, value="2026-10-31"),
    )
    got = {t.ref: t for t in ubs.parse(raw, "Assets:UBS:Checking")}
    assert set(got) == {
        "ubs:current:BTa",
        "ubs:current:BTb",
        "ubs:current:BTd",
    }  # pending is skipped
    a = got["ubs:current:BTa"]
    assert (str(a.amount), a.payee, a.counter) == ("-12.50", "CAFE ZUM BAR", None)
    assert a.narration == "Payment UBS TWINT · Lunch · Ref 1" and a.meta["ubs_type"] == "payments"
    assert a.meta["transaction_no"] == "BTa" and "value_date" not in a.meta
    b = got["ubs:current:BTb"]
    assert (
        str(b.amount) == "100.00" and b.counter == ubs.UNCATEGORIZED_IN and "Salary" in b.narration
    )
    assert got["ubs:current:BTd"].meta["value_date"] == "2026-10-31"


def test_unknown_status_is_an_error():
    with pytest.raises(base.ImportFailed, match="status"):
        ubs.parse(_raw(_tx("a", "-1", ["X"], status="weird")), "Assets:UBS:Checking")


def test_parse_files_maps_accounts_and_dedupes_overlap(tmp_path):
    src = tmp_path / "sources" / "ubs"
    src.mkdir(parents=True)
    for name, f in (
        ("Current", "2026-01-01_2026-10-27_current.json"),
        ("Current", "2026-09-01_2026-10-27_current.json"),
    ):
        (src / f).write_text(json.dumps(_raw(_tx("a", "-1.00", ["X"]), name=name)))
    got = ubs.parse_files(ubs.source_files(tmp_path))
    assert [(t.ref, t.account) for t in got] == [("ubs:current:BTa", "Assets:UBS:Checking")]
    (src / "2026-01-01_2026-10-27_other.json").write_text(
        json.dumps(_raw(_tx("z", "-1", ["Y"]), name="Unknown"))
    )
    with pytest.raises(base.ImportFailed, match="rules/ubs.yaml"):
        ubs.parse_files(ubs.source_files(tmp_path))


def test_account_names_can_be_overridden(tmp_path):
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "ubs.yaml").write_text('accounts:\n  "Current": "Assets:Bank:Main"\n')
    assert ubs.account_map(tmp_path)["Current"] == "Assets:Bank:Main"
    assert ubs.account_map(tmp_path)["Credit Card"] == "Liabilities:UBS:CreditCard"  # defaults kept
    (tmp_path / "rules" / "ubs.yaml").write_text('accounts:\n  "Savings": "Assets:UBS:Savings"\n')
    assert ubs.account_map(tmp_path)["Savings"] == "Assets:UBS:Savings"  # your other accounts


def test_default_start_is_incremental_for_ubs_files(tmp_path):
    from datetime import date

    src = tmp_path / "sources" / "ubs"
    src.mkdir(parents=True)
    (src / "2026-01-01_2026-10-20_current.json").write_text("{}")
    assert base.default_start(tmp_path, "ubs") == date(2026, 10, 6)


# ---- opening balances and transfers ----------------------------------------------------------


def _archive(root, name, txns, start="2024-01-01"):
    src = root / "sources" / "ubs"
    src.mkdir(parents=True, exist_ok=True)
    raw = _raw(*txns, name=name)
    raw["startDate"] = start
    (src / f"{start}_2026-10-27_{ubs.slug(name)}.json").write_text(json.dumps(raw))


def test_opening_balance_is_the_balance_before_the_oldest_transaction(tmp_path):
    older = _tx("old", "-10.00", ["A"], booking="2024-01-05") | {"runningBalance": "90.00"}
    newer = _tx("new", "5.00", ["B"], booking="2024-02-01") | {"runningBalance": "95.00"}
    _archive(tmp_path, "Current", [newer, older])  # the API lists newest first
    [(day, account, amount, ccy, ref)] = ubs.opening_balances(tmp_path)
    assert (str(day), account, str(amount), ccy) == (
        "2024-01-01",
        "Assets:UBS:Checking",
        "100.00",
        "CHF",
    )
    assert ref == "opening:Assets:UBS:Checking"


@pytest.fixture
def bank(data):
    with (data / "ledger" / "accounts.beancount").open("a") as f:
        f.write(
            "".join(
                f"2020-01-01 open {a} CHF\n"
                for a in (
                    "Assets:UBS:Checking",
                    "Assets:UBS:Savings",
                    "Liabilities:Swisscard:MilesMore",
                    "Expenses:Uncategorized",
                    "Income:Uncategorized",
                )
            )
        )
    return data


def _txn(day, ref, payee, account, amount, counter):
    return base.RawTxn(
        date=day, account=account, amount=amount, currency="CHF", payee=payee, narration="",
        ref=ref, counter=counter,
    )  # fmt: skip


def test_opening_entries_are_written_once(bank):
    from fin import config, ledger, opening

    older = _tx("old", "-10.00", ["A"], booking="2024-01-05") | {"runningBalance": "90.00"}
    _archive(bank, "Current", [older])
    assert opening.write(bank, "ubs") == 1
    assert opening.write(bank, "ubs") == 0  # idempotent
    led = ledger.load(config.main_ledger(bank))
    assert not led.errors


def test_transfers_pair_card_payments_and_own_accounts(bank):
    from datetime import date
    from decimal import Decimal as D

    from fin import config, gitops, ledger, transfers

    gitops.commit_all(bank, "open accounts")
    d = date
    base.write_new(
        bank,
        "mixed",
        [
            _txn(
                d(2026, 9, 1),
                "ubs:pay",
                "Swisscard AECS GmbH",
                "Assets:UBS:Checking",
                D(-500),
                None,
            ),
            _txn(
                d(2026, 9, 1),
                "swisscard:pay",
                "Payment",
                "Liabilities:Swisscard:MilesMore",
                D(500),
                "Assets:Transfers:Pending",
            ),
            _txn(d(2026, 9, 2), "ubs:out", "Savings top-up", "Assets:UBS:Checking", D(-200), None),
            _txn(
                d(2026, 9, 2),
                "ubs:in",
                "From Current",
                "Assets:UBS:Savings",
                D(200),
                "Income:Uncategorized",
            ),
            # an equal debit that is not a card payment must stay an expense
            _txn(d(2026, 9, 3), "ubs:shop", "Some shop", "Assets:UBS:Checking", D(-500), None),
            # a card payment with no card side
            _txn(
                d(2024, 3, 1),
                "ubs:old",
                "Swisscard AECS GmbH",
                "Assets:UBS:Checking",
                D(-80),
                None,
            ),
        ],
    )
    assert transfers.match(bank, dry_run=True) == {
        "pairs": 2,
        "ambiguous": 0,
        "card_payments_unmatched": 1,
    }
    assert transfers.match(bank)["pairs"] == 2
    assert transfers.match(bank)["pairs"] == 0  # nothing left to pair
    led = ledger.load(config.main_ledger(bank))
    assert not led.errors
    by = {e.meta["ref"]: e for e in led.entries if hasattr(e, "postings") and e.meta.get("ref")}
    assert (
        by["ubs:pay"].meta["transfer"] == "swisscard:pay"
        and by["swisscard:pay"].meta["transfer"] == "ubs:pay"
    )
    assert {p.account for p in by["ubs:in"].postings} == {
        "Assets:UBS:Savings",
        transfers.CLEARING,
    }
    assert "review" not in by["ubs:out"].tags
    assert "transfer" not in by["ubs:shop"].meta
    assert {p.account for p in by["ubs:shop"].postings} == {
        "Assets:UBS:Checking",
        "Expenses:Uncategorized",
    }


def test_ties_are_left_alone():
    from datetime import date
    from decimal import Decimal as D

    from fin import transfers

    def leg(ref, account, amount, day=1):
        return transfers.Leg(
            ref,
            date(2026, 9, day),
            account,
            D(amount),
            "CHF",
            "x",
            "Expenses:Uncategorized",
            "f",
            1,
        )

    legs = [
        leg("a", "Assets:UBS:Checking", "-100"),
        leg("b", "Assets:UBS:Savings", "100"),
        leg("c", "Assets:UBS:Savings", "100"),
    ]
    pairs, ambiguous = transfers.find_pairs(legs)
    assert pairs == [] and len(ambiguous) >= 2


def test_manual_opening_balance_for_a_debt(bank):
    from datetime import date
    from decimal import Decimal as D

    from fin import config, ledger, opening

    assert (
        opening.set_manual(bank, "Liabilities:Swisscard:MilesMore", D("-1234.50"), date(2024, 1, 1))
        == 1
    )
    assert opening.set_manual(bank, "Liabilities:Swisscard:MilesMore", D(-1), date(2024, 1, 1)) == 0
    led = ledger.load(config.main_ledger(bank))
    assert not led.errors
    got = {
        p.account: p.units.number for e in led.entries if hasattr(e, "postings") for p in e.postings
    }
    assert got["Liabilities:Swisscard:MilesMore"] == D("-1234.50") and got[
        "Equity:Opening-Balances"
    ] == D("1234.50")


def test_banks_own_charges_and_interest_need_no_review(bank):
    from fin import config, gitops, ledger

    fee = _tx("f", "-3.00", ["Balance closing of service prices"], deal=ubs.FEES)
    interest = _tx("i", "1.50", ["Interest calculation balance"], deal=ubs.FEES)
    got = {t.ref: t for t in ubs.parse(_raw(fee, interest), "Assets:UBS:Checking")}
    assert (got["ubs:current:BTf"].counter, got["ubs:current:BTf"].review) == (
        ubs.FEES_ACCOUNT,
        False,
    )
    assert (got["ubs:current:BTi"].counter, got["ubs:current:BTi"].review) == (
        ubs.INTEREST_ACCOUNT,
        False,
    )
    gitops.commit_all(bank, "open accounts")
    base.ensure_accounts(bank, ubs.NEEDED_ACCOUNTS)
    base.ensure_accounts(bank, ubs.NEEDED_ACCOUNTS)  # a second call opens nothing
    base.write_new(bank, "ubs", list(got.values()))
    led = ledger.load(config.main_ledger(bank))
    assert not led.errors
    tx = [e for e in led.entries if hasattr(e, "postings") and e.meta.get("ref")]
    assert all("review" not in e.tags for e in tx)


# ---- mortgages ---------------------------------------------------------------------------------


def _snapshot(root, balance="96000", per_year="4000"):
    (root / "rules").mkdir(exist_ok=True)
    (root / "rules" / "mortgages.yaml").write_text("pays_from: Assets:UBS:Savings\n")
    folder = root / "sources" / "ubs" / "mortgages"
    folder.mkdir(parents=True)
    (folder / "2026-10-27.json").write_text(
        json.dumps(
            {
                "mortgages": {
                    "mortgagesByRealEstates": [
                        {
                            "mortgages": [
                                {
                                    "number": "0000 00123456.ABC 0001",
                                    "mortgageAmount": {"amount": balance, "currency": "CHF"},
                                    "directAmortizationPerYear": {
                                        "amount": per_year,
                                        "currency": "CHF",
                                    },
                                    "startDate": "2021-06-01",
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )


def test_mortgage_payments_split_into_interest_and_amortization(bank):
    from datetime import date
    from decimal import Decimal as D

    from fin import config, gitops, ledger, mortgage

    gitops.commit_all(bank, "open accounts")
    pay = lambda day, ref, amount: base.RawTxn(
        date=day, account="Assets:UBS:Savings", amount=D(amount), currency="CHF",
        payee="Maturity", narration="HYPOTHEK 123456.ABC 0001", ref=ref, meta={},
    )  # fmt: skip
    base.write_new(
        bank,
        "ubs",
        [pay(date(2026, 6, 30), "ubs:p1", "-1250"), pay(date(2026, 9, 30), "ubs:p2", "-1240")],
    )
    assert mortgage.apply(bank) == {}  # nothing before the first snapshot
    _snapshot(bank)  # the bank says 96'000 are owed, repaying 4'000 a year: 1'000 a quarter
    assert mortgage.apply(bank) == {"split": 2, "opening": 1}
    assert mortgage.apply(bank) == {"split": 0, "opening": 0}  # idempotent
    led = ledger.load(config.main_ledger(bank))
    assert not led.errors
    assert mortgage.check(bank) == []  # the books agree with the bank
    p2 = next(e for e in led.entries if hasattr(e, "meta") and e.meta.get("ref") == "ubs:p2")
    got = {p.account: p.units.number for p in p2.postings}
    assert got == {
        "Assets:UBS:Savings": D(-1240),
        "Liabilities:UBS:Mortgage:Loan1": D(1000),
        mortgage.INTEREST: D(240),
    }
    assert "review" not in p2.tags and p2.meta["mortgage"] == "Liabilities:UBS:Mortgage:Loan1"


def test_mortgage_payment_below_the_amortization_is_an_error(bank):
    from datetime import date
    from decimal import Decimal as D

    from fin import gitops, mortgage

    gitops.commit_all(bank, "open accounts")
    t = base.RawTxn(
        date=date(2026, 9, 30), account="Assets:UBS:Savings", amount=D(-500), currency="CHF",
        payee="Maturity", narration="HYPOTHEK 123456.ABC 0001", ref="ubs:p", meta={},
    )  # fmt: skip
    base.write_new(bank, "ubs", [t])
    _snapshot(bank)
    with pytest.raises(base.ImportFailed, match="below zero"):
        mortgage.apply(bank)


# ---- credit card -------------------------------------------------------------------------------


def _card_tx(id, amount, details, status="BOOKED", day="2026-01-04T23:00:00Z", **extra):
    t = {
        "_id": id,
        "transactionStatus": status,
        "transactionDate": day,
        "valueDate": "2026-01-06T00:00:00Z",
        "details": details,
        "merchantName": extra.pop("merchantName", "Grocery stores"),
        "merchantGroupCode": "0009",
        "alias": "1234",
        "postingAmount": {"amount": amount, "currency": "CHF"},
        "originalAmount": extra.pop("original", {"amount": amount, "currency": "CHF"}),
    }
    return t | extra


def _card_raw(*txns):
    return {
        "kind": "card",
        "startDate": "2026-01-05",
        "fetched": "2026-10-27T10:00:00+01:00",
        "account": {"displayName": "Credit Card", "currency": "CHF", "balance": "-80.00"},
        "pages": [{"_embedded": {"transactions": list(txns)}}],
    }


def test_card_parse_dates_text_payments_and_foreign_amounts():
    raw = _card_raw(
        _card_tx("a", "-30.00", "Migros M Example         Zuerich "),
        _card_tx(
            "b",
            "-45.00",
            "Cars on Booking          London   ",
            merchantName="Car Rental Company",
            original={"amount": "-40.00", "currency": "GBP"},
            effectiveExchangeRate=1.125,
        ),
        _card_tx("c", "100.00", "DIRECT DEBIT", merchantName=None),
        _card_tx("d", "-9.00", "Pending shop", status="RESERVED"),
    )
    ids = {ubs.card_key(t): t["_id"] for t in raw["pages"][0]["_embedded"]["transactions"] if t["transactionStatus"] == "BOOKED"}  # fmt: skip
    got = {ids[t.ref.removeprefix("ubs:")]: t for t in ubs.parse_card(raw, "Liabilities:UBS:CreditCard")}  # fmt: skip
    assert set(got) == {"a", "b", "c"}  # reserved authorizations are skipped
    a = got["a"]
    assert str(a.date) == "2026-01-05"  # 23:00Z is already the next day in Zurich
    assert (str(a.amount), a.payee, a.narration) == (
        "-30.00",
        "Migros M Example",
        "Zuerich",
    )
    assert a.meta == {
        "merchant_category": "Grocery stores",
        "merchant_group": "0009",
        "card": "1234",
        "value_date": "2026-01-06",
    }
    assert got["b"].meta["original_amount"] == "-40.00 GBP" and got["b"].meta["fx_rate"] == "1.125"
    c = got["c"]
    assert (c.counter, c.review, str(c.amount)) == (ubs.CLEARING, False, "100.00")


def test_card_opening_balance_makes_the_books_match_the_bank(tmp_path):
    src = tmp_path / "sources" / "ubs"
    src.mkdir(parents=True)
    raw = _card_raw(_card_tx("a", "-30.00", "X   Y"), _card_tx("b", "-50.00", "Z   W"))
    (src / "2026-01-05_2026-10-27_creditcard.json").write_text(json.dumps(raw))
    [(day, account, amount, _ccy, ref)] = ubs.opening_balances(tmp_path)
    assert (str(day), account, str(amount), ref) == (
        "2026-01-05",
        "Liabilities:UBS:CreditCard",
        "0.00",
        "opening:Liabilities:UBS:CreditCard",
    )
    parsed = ubs.parse_files(ubs.source_files(tmp_path))
    assert sum(t.amount for t in parsed) + amount == -80  # ledger ends at the bank's -80.00


def test_card_opening_counts_an_item_on_two_overlapping_pages_once(tmp_path):
    src = tmp_path / "sources" / "ubs"
    src.mkdir(parents=True)
    a, b = _card_tx("a", "-30.00", "X   Y"), _card_tx("b", "-50.00", "Z   W")
    raw = _card_raw()
    raw["pages"] = [{"_embedded": {"transactions": [a, b]}}, {"_embedded": {"transactions": [b]}}]
    (src / "2026-01-05_2026-10-27_creditcard.json").write_text(json.dumps(raw))
    [(_d, _a, amount, _c, _r)] = ubs.opening_balances(tmp_path)
    parsed = ubs.parse_files(ubs.source_files(tmp_path))
    assert len(parsed) == 2 and sum(t.amount for t in parsed) + amount == -80


def test_pair_search_matches_brute_force():
    """The amount-bucketed search finds exactly what comparing every leg with every other does."""
    import random
    from datetime import date, timedelta
    from decimal import Decimal as D

    from fin import transfers as T

    def brute(legs):
        edges = []
        cards = [
            x for x in legs if x.counter == T.CLEARING and not x.account.startswith(T.BANK_PREFIX)
        ]
        banks = [x for x in legs if x.account.startswith(T.BANK_PREFIX)]
        for c in cards:
            for b in banks:
                gap = abs((c.date - b.date).days)
                if T._is_card_payment(b) and b.amount == -c.amount and gap <= T.CARD_WINDOW:
                    edges.append((gap, b, c))
        for i, a in enumerate(banks):
            for b in banks[i + 1 :]:
                gap = abs((a.date - b.date).days)
                if (
                    a.account != b.account
                    and a.amount == -b.amount
                    and a.amount
                    and gap <= T.OWN_WINDOW
                ):
                    edges.append((gap, *((a, b) if a.amount < 0 else (b, a))))
        edges.sort(key=lambda e: (e[0], e[1].ref, e[2].ref))
        used, pairs, ambiguous = set(), [], []
        for gap, a, b in edges:
            if a.ref in used or b.ref in used:
                continue
            rivals = [e for e in edges if e[0] == gap and {a.ref, b.ref} & {e[1].ref, e[2].ref}]
            used |= {a.ref, b.ref}
            (ambiguous.extend((a, b)) if len(rivals) > 1 else pairs.append((a, b)))
        return pairs, ambiguous

    rng = random.Random(7)
    accounts = [
        "Assets:UBS:Checking",
        "Assets:UBS:Savings",
        "Liabilities:Swisscard:MilesMore",
    ]
    for _ in range(30):
        legs = []
        for i in range(60):
            account = rng.choice(accounts)
            card = not account.startswith(T.BANK_PREFIX)
            legs.append(
                T.Leg(
                    f"r{i}", date(2026, 1, 1) + timedelta(days=rng.randrange(20)), account,
                    D(rng.choice([-1, 1]) * rng.choice([10, 20, 30])), "CHF",
                    rng.choice(["Swisscard AG", "x"]),
                    T.CLEARING if card else "Expenses:Uncategorized", "f", 1,
                )
            )  # fmt: skip
        assert T.find_pairs(legs) == brute(legs)


def test_import_opens_the_banks_own_accounts(tmp_path):
    """A fresh data repo has none of the UBS accounts; importing must not fail on them."""
    from fin import config, ledger, scaffold

    root = tmp_path / "fresh"
    scaffold.init_data(root)
    base.ensure_accounts(root, ubs.needed_accounts(root))
    led = ledger.load(config.main_ledger(root))
    opens = {e.account: e.meta for e in led.entries if type(e).__name__ == "Open"}
    assert opens["Liabilities:UBS:CreditCard"]["group"] == "Liabilities"
    assert opens["Assets:UBS:Checking"]["group"] == "Cash"
    assert not led.errors


def test_ubs_card_payments_are_recognised_by_their_text():
    from datetime import date
    from decimal import Decimal as D

    from fin import transfers as T

    def leg(amount, payee, narration):
        return T.Leg("r", date(2026, 1, 1), "Assets:UBS:Checking", D(amount), "CHF", payee,
                     "Expenses:Uncategorized", "f", 1, narration)  # fmt: skip

    assert T._is_card_payment(
        leg("-10", "UBS Switzerland AG", "c/o UBS Card Center · direct debit")
    )
    assert not T._is_card_payment(leg("10", "UBS Switzerland AG", "c/o UBS Card Center · Credit"))
    assert not T._is_card_payment(leg("-10", "UBS Switzerland AG", "Maturity"))
    on_card = leg("-10", "Swisscard charges (no detail)", "")
    on_card.account = "Liabilities:Swisscard:MilesMore"
    assert not T._is_card_payment(on_card)  # only a bank debit pays a card


def test_mapping_file_gains_new_sources_and_keeps_your_edits(tmp_path):
    import yaml

    from fin.importers import categorize

    path = tmp_path / categorize.MAP_FILE
    path.parent.mkdir(parents=True)
    path.write_text("# mine\nswisscard:\n  merchant:\n    BAKERIES: Expenses:Food:Dining\n")
    maps = categorize.load_map(tmp_path)
    assert maps["swisscard"]["merchant"] == {"BAKERIES": "Expenses:Food:Dining"}
    assert maps["ubs"]["merchant"]["Grocery stores"] == "Expenses:Food:Groceries"
    text = path.read_text()
    assert text.startswith("# mine\n") and yaml.safe_load(text) == maps
    categorize.load_map(tmp_path)
    assert path.read_text() == text  # appended once


def test_every_default_mapping_target_has_cadence_and_necessity():
    from fin.importers import categorize

    targets = {
        a for src in categorize.DEFAULT_MAP.values() for part in src.values() for a in part.values()
    }
    assert targets <= set(categorize.CHART)


def test_refs_survive_ubs_renumbering_its_items():
    """UBS hands out new `_id`s every session; refs must not change, or a re-fetch imports twice."""
    cash = _tx("a", "-1.00", ["X"])
    again = dict(cash, _id="other-session-id")
    assert (
        ubs.parse(_raw(cash), "A")[0].ref == ubs.parse(_raw(again), "A")[0].ref == "ubs:current:BTa"
    )
    card = _card_tx("a", "-3.00", "Shop   City") | {"transactionNr": 7}
    later = dict(card, _id="other-session-id")
    assert (
        ubs.parse_card(_card_raw(card), "L")[0].ref == ubs.parse_card(_card_raw(later), "L")[0].ref
    )
    other = dict(card, transactionNr=8)  # same purchase twice on one statement: two items
    assert (
        ubs.parse_card(_card_raw(card), "L")[0].ref != ubs.parse_card(_card_raw(other), "L")[0].ref
    )
    with pytest.raises(base.ImportFailed, match="transaction number"):
        ubs.parse(_raw({k: v for k, v in cash.items() if k != "businessTransactionId"}), "A")


def test_salary_credits_are_filed_as_salary():
    raw = _raw(
        _tx("s", "5000.00", ["Employer AG"], sub=["salary payment", "SALAIRE"]),
        _tx("r", "80.00", ["Employer AG"], sub=["credit", "B1JZ"]),
        _tx("x", "-10.00", ["Shop"], sub=["salary payment"]),
    )
    got = {t.ref.rsplit(":", 1)[1]: t for t in ubs.parse(raw, "Assets:UBS:Checking")}
    assert (got["BTs"].counter, got["BTs"].review) == (ubs.SALARY_ACCOUNT, False)
    assert (got["BTr"].counter, got["BTr"].review) == (
        ubs.UNCATEGORIZED_IN,
        True,
    )  # a person decides
    assert got["BTx"].counter is None  # money out is never salary
