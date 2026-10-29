import json
from datetime import date
from decimal import Decimal as D

import pytest

from fin.importers import base, schwab


def _vest(day, award, gross, withheld, fmv, sym="ACME"):
    net = gross - withheld
    return {
        "typeName": "LapseViewModel", "eventDate": day, "action": "Lapse", "symbol": sym,
        "quantity": str(gross), "description": "Restricted Stock Lapse",
        "transactionDetails": [{
            "awardDate": "01/01/2020", "awardName": "RSU", "marketValue": f"${fmv}", "salePrice": "",
            "shareSoldWithheld": str(withheld), "netShares": str(net),
            "totalTaxes": f"${withheld * D(fmv):,}", "typeName": "LapseDetailViewModel",
            "fieldNameToDisplayValueLookup": {"awardId": award, "fairMarketValuePrice": f"${fmv}"},
        }],
    }  # fmt: skip


def _row(day, action, sym="", qty="", amount="", description=""):
    return {"transactionDate": day, "action": action, "symbol": sym, "shareQuantity": qty, "amount": amount, "description": description}  # fmt: skip


def _archive(root, kind, rows, start="2023-01-01", end="2023-12-31"):
    src = root / "sources" / "schwab"
    src.mkdir(parents=True, exist_ok=True)
    path = src / f"{start}_{end}_{kind}.json"
    path.write_text(
        json.dumps({"kind": kind, "fromDate": start, "toDate": end, "rows": rows[::-1]})
    )
    return path


def test_vest_books_net_shares_tax_and_gross_income(tmp_path):
    f = _archive(tmp_path, "eac", [_vest("02/15/2023", "111", 10, 4, "150.25")])
    [t] = schwab.parse_files([f])
    text = base.render(t)
    assert 'Assets:Schwab:ACME  6 ACME {150.25 USD, 2023-02-15, "v111-20230215"}' in text
    assert "Expenses:Taxes:RSU-Withholding  601.00 USD" in text
    assert "Income:Employer:RSU  -1502.50 USD" in text
    assert t.ref == "schwab:vest:111:20230215" and t.meta["award"] == "111"


def test_transfer_out_takes_the_oldest_lots_at_cost(tmp_path):
    files = [
        _archive(tmp_path, "eac", [_vest("02/15/2023", "1", 10, 4, "100"), _vest("05/15/2023", "1", 10, 4, "120")]),
        _archive(tmp_path, "brokerage", [
            _row("02/17/2023 as of 02/15/2023", "Stock Plan Activity", "ACME", "6"),
            _row("05/22/2023", "Security Transfer", "ACME", "-8", description="ACME CLASS A"),
        ]),
    ]  # fmt: skip
    out = schwab.parse_files(files)
    move = next(t for t in out if t.narration.startswith("transfer out"))
    text = base.render(move)
    assert 'Assets:Schwab:ACME  -6 ACME {"v1-20230215"}' in text
    assert 'Assets:Schwab:ACME  -2 ACME {"v1-20230515"}' in text
    assert "Assets:Transfers:Pending  840 USD" in text  # 6 x 100 + 2 x 120
    assert not any("Stock Plan Activity" in t.narration for t in out)  # already booked as the vest


def test_lots_from_before_the_brokerage_history_left_before_it(tmp_path):
    files = [
        _archive(tmp_path, "eac", [_vest("08/15/2022", "1", 5, 2, "90"), _vest("02/15/2023", "1", 5, 2, "100")], start="2022-01-01"),
        _archive(tmp_path, "brokerage", [_row("02/22/2023", "Security Transfer", "ACME", "-3")], start="2022-10-28"),
    ]  # fmt: skip
    out = schwab.parse_files(files)
    bridge = next(t for t in out if t.ref.startswith("schwab:before-history:"))
    assert bridge.date == date(2022, 10, 28)
    assert "Assets:Transfers:Pending  270 USD" in base.render(bridge)
    move = next(t for t in out if t.narration.startswith("transfer out"))
    assert 'Assets:Schwab:ACME  -3 ACME {"v1-20230215"}' in base.render(move)


def test_moving_more_than_fin_holds_is_an_error(tmp_path):
    files = [
        _archive(tmp_path, "eac", [_vest("02/15/2023", "1", 5, 2, "100")]),
        _archive(tmp_path, "brokerage", [_row("02/22/2023", "Security Transfer", "ACME", "-4")]),
    ]
    with pytest.raises(base.ImportFailed, match="fin holds 3"):
        schwab.parse_files(files)


def test_cash_rows_dividend_tax_and_cash_transfer(tmp_path):
    rows = [
        _row("03/26/2024", "Qualified Dividend", "ACME", amount="$12.00"),
        _row("03/26/2024", "NRA Tax Adj", "ACME", amount="-$1.80"),
        _row("03/28/2024", "Security Transfer", amount="-$10.20", description="ACAT TOA TO ...000"),
    ]
    by = {
        t.narration: t
        for t in schwab.parse_files([_archive(tmp_path, "brokerage", rows, start="2024-01-01")])
    }
    assert by["dividend ACME"].counter == "Income:Investments:Dividends:ACME" and by[
        "dividend ACME"
    ].amount == D("12.00")
    assert by["withholding tax ACME"].counter == "Expenses:Taxes:Withholding:US"
    assert (
        by["cash transfer out"].counter == schwab.CLEARING
        and by["cash transfer out"].account == "Assets:Schwab:Cash:USD"
    )


def test_unknown_rows_are_an_error(tmp_path):
    with pytest.raises(base.ImportFailed, match="unknown Schwab brokerage action"):
        schwab.parse_files(
            [_archive(tmp_path, "brokerage", [_row("03/26/2024", "Margin Interest", amount="-$1")])]
        )
    with pytest.raises(base.ImportFailed, match="sold shares to cover"):
        v = _vest("02/15/2023", "1", 5, 2, "100")
        v["transactionDetails"][0]["salePrice"] = "$101"
        schwab.parse_files([_archive(tmp_path, "eac", [v])])


def test_overlapping_fetches_count_rows_once(tmp_path):
    v = _vest("02/15/2023", "1", 5, 2, "100")
    a = _archive(tmp_path, "eac", [v])
    b = _archive(tmp_path, "eac", [v], start="2023-02-01", end="2023-03-01")
    assert len(schwab.parse_files([a, b])) == 1


def test_windows_and_money():
    assert schwab.windows(date(2022, 10, 28), date(2024, 3, 1)) == [
        (date(2022, 10, 28), date(2022, 12, 31)), (date(2023, 1, 1), date(2023, 12, 31)), (date(2024, 1, 1), date(2024, 3, 1)),
    ]  # fmt: skip
    assert schwab._money("-$1,234.56") == D("-1234.56") and schwab._money("") == 0
    assert schwab.years_back(date(2028, 2, 29), 4) == date(2024, 2, 29)


def test_import_end_to_end(data, monkeypatch):
    from fin import cli, config, ledger, prices

    monkeypatch.setattr(prices, "fetch_rates", lambda ccy, quote, a, b: {a: D("0.9")})
    _archive(data, "eac", [_vest("02/15/2023", "1", 10, 4, "100")])
    _archive(data, "brokerage", [_row("02/22/2023", "Security Transfer", "ACME", "-6")])
    cli._fetch_and_import(data, "schwab", fetch=False)
    led = ledger.load(config.main_ledger(data))
    assert not led.errors


def _moves(*txns):
    from types import SimpleNamespace

    from beancount import loader

    head = "".join(f"2020-01-01 open {a}\n" for a in ("Assets:Schwab:ACME", "Assets:IBKR:ACME", "Assets:Transfers:Pending", "Income:Employer:RSU"))  # fmt: skip
    entries, errors, _ = loader.load_string(head + "\n".join(txns))
    assert not errors, errors
    return SimpleNamespace(entries=entries)


VEST = """2023-02-15 * "vest"
  Assets:Schwab:ACME  6 ACME {100 USD, 2023-02-15, "v1"}
  Income:Employer:RSU  -600 USD
"""
OUT = """2023-02-22 * "out"
  Assets:Schwab:ACME  -6 ACME {"v1"}
  Assets:Transfers:Pending  600 USD
"""


def _in(cost="100", qty=6, day="2023-02-24", lot_day="2023-02-15"):
    total = D(cost) * qty
    return f"""{day} * "in"
  Assets:IBKR:ACME  {qty} ACME {{{cost} USD, {lot_day}}}
  Assets:Transfers:Pending  -{total} USD
"""


def test_share_moves_check_each_lots_basis():
    from fin import transfers

    ok = transfers.share_moves(_moves(VEST, OUT, _in()), today=date(2023, 3, 1))
    assert not any(ok.values())
    wrong = transfers.share_moves(_moves(VEST, OUT, _in(cost="101")), today=date(2023, 3, 1))
    assert wrong["mismatch"] == ["ACME lot of 2023-02-15"]
    assert transfers.share_moves(_moves(VEST, OUT), today=date(2023, 2, 28))["in_flight"]
    assert transfers.share_moves(_moves(VEST, OUT), today=date(2023, 3, 30))["not_arrived"]
    assert transfers.share_moves(_moves(_in()), today=date(2023, 3, 1))["no_sender"] == [
        "ACME lot of 2023-02-15"
    ]


def _eac_csv(path, vests):
    import csv

    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(schwab.EAC_CSV_HEADER)
        for day, award, gross, withheld, fmv in vests:
            w.writerow([day, "Lapse", "ACME", "Restricted Stock Lapse", str(gross)] + [""] * 10)
            w.writerow(
                [""] * 8
                + [
                    "01/01/2020",
                    award,
                    f"${fmv}",
                    "",
                    str(withheld),
                    str(gross - withheld),
                    f"${withheld * D(fmv):,}",
                ]
            )
    return path


def _brokerage_json(path, rows, start="01/01/2023", end="05/31/2023"):
    names = {"transactionDate": "Date", "action": "Action", "symbol": "Symbol", "description": "Description", "shareQuantity": "Quantity", "amount": "Amount"}  # fmt: skip
    path.write_text(json.dumps({"FromDate": start, "ToDate": end, "TotalTransactionsAmount": "$0.00", "TotalFeesAndCommAmount": "$0.00",
                                "BrokerageTransactions": [{names[k]: v for k, v in r.items()} | {"Price": "", "Fees & Comm": "", "AcctgRuleCd": "1"} for r in rows]}))  # fmt: skip
    return path


def test_exports_read_like_the_sites_own_data(tmp_path):
    csv_ = _eac_csv(
        tmp_path / "EquityAwardsCenter_Transactions_1.csv", [("02/15/2023", "111", 10, 4, "150.25")]
    )
    js = _brokerage_json(
        tmp_path / "Individual_XXX000_Transactions_1.json",
        [_row("05/22/2023", "Security Transfer", "ACME", "-6")],
    )
    out = {t.ref.split(":")[1]: t for t in schwab.parse_files([csv_, js])}
    assert 'Assets:Schwab:ACME  6 ACME {150.25 USD, 2023-02-15, "v111-20230215"}' in base.render(
        out["vest"]
    )
    assert "Assets:Transfers:Pending  901.50 USD" in base.render(
        next(t for t in out.values() if t.narration.startswith("transfer"))
    )


def test_an_export_and_a_fetch_of_the_same_rows_count_once(tmp_path):
    vest, move = (
        _vest("02/15/2023", "111", 10, 4, "150.25"),
        _row("05/22/2023", "Security Transfer", "ACME", "-6"),
    )
    fetched = [
        _archive(tmp_path, "eac", [vest]),
        _archive(tmp_path, "brokerage", [move]),
    ]
    csv_ = _eac_csv(
        tmp_path / "EquityAwardsCenter_Transactions_1.csv", [("02/15/2023", "111", 10, 4, "150.25")]
    )
    js = _brokerage_json(tmp_path / "Individual_XXX000_Transactions_1.json", [move])
    assert len(schwab.parse_files(fetched + [csv_, js])) == len(schwab.parse_files(fetched)) == 2


def test_import_takes_a_downloaded_file(data, monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from fin import cli, prices

    monkeypatch.setattr(prices, "fetch_rates", lambda ccy, quote, a, b: {a: D("0.9")})
    csv_ = _eac_csv(
        tmp_path / "EquityAwardsCenter_Transactions_1.csv", [("02/15/2023", "111", 10, 4, "150.25")]
    )
    result = CliRunner().invoke(cli.app, ["import", "schwab", str(csv_)])
    assert result.exit_code == 0, result.output
    assert (
        "kept EquityAwardsCenter_Transactions_1.csv" in result.output
        and "imported 1 new" in result.output
    )
    again = CliRunner().invoke(cli.app, ["import", "schwab", str(csv_)])
    assert "imported 0 new" in again.output
    assert (data / "sources" / "schwab" / "exports" / csv_.name).exists()
    bad = tmp_path / "other.json"
    bad.write_text("{}")
    assert (
        "not a Schwab export" in CliRunner().invoke(cli.app, ["import", "schwab", str(bad)]).output
    )


def test_vest_records_the_award_date_and_name(tmp_path):
    f = _archive(tmp_path, "eac", [_vest("02/15/2023", "111", 10, 4, "150.25")])
    [t] = schwab.parse_files([f])
    assert t.meta["award_date"] == "2020-01-01" and t.meta["award_name"] == "RSU"
    assert schwab.award_meta([f]) == {
        "schwab:vest:111:20230215": {"award_date": "2020-01-01", "award_name": "RSU"}
    }
