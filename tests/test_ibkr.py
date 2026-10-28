from datetime import date
from decimal import Decimal as D

import pytest

from fin.importers import base, ibkr


def _attrs(**kw) -> str:
    return " ".join(f'{k}="{v}"' for k, v in kw.items())


def _report(body: str, start="20240101", end="20241230", cash="", positions="", prices="") -> str:
    return f"""<FlexQueryResponse queryName="fin" type="AF"><FlexStatements count="1">
<FlexStatement accountId="U0000000" fromDate="{start}" toDate="{end}">
<AccountInformation accountId="U0000000" currency="CHF" name="Ada Example" street="1 Secret Street"
  primaryEmail="ada@example.com" dateOpened="20180101"/>
<CashReport>{cash}</CashReport>
<OpenPositions>{positions}</OpenPositions>
<PriorPeriodPositions>{prices}</PriorPeriodPositions>
{body}
</FlexStatement></FlexStatements></FlexQueryResponse>"""


def _buy(tid, sym, qty, price, comm="-1", day="20240105", time="093000"):
    proceeds = -D(qty) * D(price)
    return f"<Trade {_attrs(assetCategory='STK', levelOfDetail='EXECUTION', buySell='BUY', symbol=sym, currency='USD', quantity=qty, tradePrice=price, proceeds=proceeds, ibCommission=comm, ibCommissionCurrency='USD', netCash=proceeds + D(comm), tradeDate=day, dateTime=f'{day};{time}', tradeID=tid, transactionID='x' + tid)}/>"


def _sell(tid, sym, qty, price, lots, comm="-1", day="20240301"):
    proceeds = D(qty) * D(price)
    rows = "".join(
        f"<Lot {_attrs(levelOfDetail='CLOSED_LOT', buySell='SELL', symbol=sym, quantity=-D(n), openDateTime=opened)}/>"
        for n, opened in lots
    )
    return f"<Trade {_attrs(assetCategory='STK', levelOfDetail='EXECUTION', buySell='SELL', symbol=sym, currency='USD', quantity=-D(qty), tradePrice=price, proceeds=proceeds, ibCommission=comm, ibCommissionCurrency='USD', netCash=proceeds + D(comm), tradeDate=day, dateTime=f'{day};150000', tradeID=tid, transactionID='x' + tid)}/>{rows}"


def _files(tmp_path, *reports):
    src = tmp_path / "sources" / "ibkr"
    src.mkdir(parents=True, exist_ok=True)
    out = []
    for i, text in enumerate(reports):
        path = src / f"2024-0{i + 1}-01_2024-0{i + 1}-28.xml"
        path.write_bytes(ibkr.sanitize(text))
        out.append(path)
    return out


def _by_ref(txns):
    return {t.ref: base.render(t) for t in txns}


def test_sanitize_drops_personal_details_and_the_other_brokers_account():
    text = _report(
        f"<Transfers><Transfer {_attrs(account='12345678', accountName='Ada', symbol='VT')}/></Transfers>"
    )
    kept = ibkr.sanitize(text).decode()
    for gone in (
        "Ada Example",
        "Secret Street",
        "ada@example.com",
        "12345678",
        'accountName="Ada"',
    ):
        assert gone not in kept
    assert 'dateOpened="20180101"' in kept and 'accountId="U0000000"' in kept


def test_buys_and_a_sale_of_the_lots_ibkr_names(tmp_path):
    trades = "<Trades>" + _buy("1", "VT", "10", "100") + _buy("2", "VT", "5", "110", day="20240110")
    trades += (
        _sell("3", "VT", "7", "120", [("5", "20240110;093000"), ("2", "20240105;093000")])
        + "</Trades>"
    )
    out = _by_ref(ibkr.parse_files(_files(tmp_path, _report(trades))))
    assert 'Assets:IBKR:VT  10 VT {100 USD, 2024-01-05, "b1"}' in out["ibkr:trade:1"]
    assert "Assets:IBKR:Cash:USD  -1001 USD" in out["ibkr:trade:1"]
    assert "Expenses:Fees:Broker  1 USD" in out["ibkr:trade:1"]
    sale = out["ibkr:trade:3"]
    assert 'Assets:IBKR:VT  -5 VT {"b2"} @ 120 USD' in sale  # the newer lot first, as IBKR says
    assert 'Assets:IBKR:VT  -2 VT {"b1"} @ 120 USD' in sale
    assert sale.rstrip().endswith("Income:Investments:Gains:VT")


def test_a_sale_of_a_lot_fin_does_not_hold_is_an_error(tmp_path):
    trades = (
        "<Trades>"
        + _buy("1", "VT", "1", "100")
        + _sell("2", "VT", "2", "120", [("2", "20240105;093000")])
    )
    with pytest.raises(base.ImportFailed, match="holds no such lot"):
        ibkr.parse_files(_files(tmp_path, _report(trades + "</Trades>")))


def test_currency_trade_is_a_conversion(tmp_path):
    fx = f"<Trades><Trade {_attrs(assetCategory='CASH', levelOfDetail='EXECUTION', buySell='BUY', symbol='USD.CHF', currency='CHF', quantity='1000', tradePrice='0.9', proceeds='-900.12', ibCommission='-2', ibCommissionCurrency='CHF', netCash='0', tradeDate='20240102', dateTime='20240102;100000', tradeID='9', transactionID='x9')}/></Trades>"
    [t] = ibkr.parse_files(_files(tmp_path, _report(fx)))
    text = base.render(t)
    assert "Assets:IBKR:Cash:USD  1000 USD @@ 900.12 CHF" in text
    assert "Assets:IBKR:Cash:CHF  -900.12 CHF" in text
    assert "Expenses:Fees:Broker  2 CHF" in text and "Assets:IBKR:Cash:CHF  -2 CHF" in text


def test_cash_rows_detail_only_dated_when_posted(tmp_path):
    def row(tid, kind, amount, detail="DETAIL", **kw):
        return f"<CashTransaction {_attrs(type=kind, currency='USD', amount=amount, levelOfDetail=detail, transactionID=tid, reportDate='20240311', dateTime='20240308;202000', **kw)}/>"

    rows = "<CashTransactions>" + row(
        "1", "Dividends", "12.5", symbol="VT", description="VT CASH DIVIDEND"
    )
    rows += row("1s", "Dividends", "12.5", detail="SUMMARY", symbol="VT")
    rows += row(
        "2", "Withholding Tax", "-1.88", symbol="VT", description="VT CASH DIVIDEND - US Tax"
    )
    rows += row("3", "Broker Interest Received", "3.1") + row("4", "Deposits/Withdrawals", "500")
    rows += row("5", "Other Fees", "-1") + "</CashTransactions>"
    txns = {t.ref: t for t in ibkr.parse_files(_files(tmp_path, _report(rows)))}
    assert set(txns) == {f"ibkr:cash:{i}" for i in "12345"}
    assert txns["ibkr:cash:1"].counter == "Income:Investments:Dividends:VT"
    assert txns["ibkr:cash:1"].date == date(2024, 3, 11)
    assert txns["ibkr:cash:2"].counter == "Expenses:Taxes:Withholding:US"
    assert txns["ibkr:cash:3"].counter == ibkr.INTEREST
    assert (
        txns["ibkr:cash:4"].counter == ibkr.CLEARING and txns["ibkr:cash:4"].narration == "deposit"
    )
    assert txns["ibkr:cash:5"].counter == ibkr.FEES


def test_unknown_cash_type_is_an_error(tmp_path):
    row = f"<CashTransactions><CashTransaction {_attrs(type='Bond Interest', currency='USD', amount='1', levelOfDetail='DETAIL', transactionID='1', reportDate='20240311')}/></CashTransactions>"
    with pytest.raises(base.ImportFailed, match="unknown IBKR cash transaction type"):
        ibkr.parse_files(_files(tmp_path, _report(row)))


def _transfer(lots, tid="70", sym="VT", day="20240201"):
    qty = sum(D(q) for q, _, _ in lots)
    rows = "".join(
        f"<TransferLot {_attrs(symbol=sym, quantity=q, cost=c, openDateTime=o, direction='IN', type='ACATS')}/>"
        for q, c, o in lots
    )
    return f"<Transfers><Transfer {_attrs(type='ACATS', direction='IN', assetCategory='STK', symbol=sym, currency='USD', quantity=qty, date=day, transactionID=tid, deliveringBroker='0164')}/>{rows}</Transfers>"


def test_shares_transferred_in_keep_their_cost_and_date_then_sell(tmp_path):
    body = _transfer([("3", "300", "20230315"), ("7", "1000", "20230615")])  # 1000/7 does not end
    body += "<Trades>" + _sell(
        "5", "VT", "4", "150", [("3", "20230315"), ("1", "20230615")], day="20240301"
    )
    txns = _by_ref(ibkr.parse_files(_files(tmp_path, _report(body + "</Trades>"))))
    t = txns["ibkr:transfer:70"]
    assert 'Assets:IBKR:VT  3 VT {100 USD, 2023-03-15, "t70-1"}' in t
    assert 'Assets:IBKR:VT  7 VT {# 1000 USD, 2023-06-15, "t70-2"}' in t
    assert "Assets:Transfers:Pending  -1300 USD" in t
    assert 'Assets:IBKR:VT  -1 VT {"t70-2"} @ 150 USD' in txns["ibkr:trade:5"]


def test_forward_split_keeps_cost_and_dates(tmp_path):
    body = "<Trades>" + _buy("1", "AAPL", "10", "400", comm="0") + "</Trades>"
    body += f"<CorporateActions><CorporateAction {_attrs(type='FS', levelOfDetail='DETAIL', symbol='AAPL', quantity='30', description='AAPL(US0378331005) SPLIT 4 FOR 1 (AAPL, APPLE INC, US0378331005)', reportDate='20240831', transactionID='80')}/></CorporateActions>"
    txns = _by_ref(ibkr.parse_files(_files(tmp_path, _report(body))))
    split = txns["ibkr:ca:80"]
    assert 'Assets:IBKR:AAPL  -10 AAPL {"b1"}' in split
    assert 'Assets:IBKR:AAPL  40 AAPL {100 USD, 2024-01-05, "b1s"}' in split


def test_other_corporate_actions_stop_the_import(tmp_path):
    ca = f"<CorporateActions><CorporateAction {_attrs(type='TC', levelOfDetail='DETAIL', symbol='X', quantity='1', description='X RENAMED', reportDate='20240831', transactionID='81')}/></CorporateActions>"
    with pytest.raises(base.ImportFailed, match="needs a manual booking"):
        ibkr.parse_files(_files(tmp_path, _report(ca)))


def test_commission_credits_come_from_the_cash_report(tmp_path):
    cash = f"<CashReportCurrency {_attrs(currency='USD', levelOfDetail='Currency', commissionCreditsRedemption='5.4', endingCash='5.4')}/>"
    [t] = ibkr.parse_files(_files(tmp_path, _report("", cash=cash)))
    assert (t.date, t.amount, t.counter) == (date(2024, 12, 30), D("5.4"), ibkr.FEES)


def test_windows_do_not_overlap_and_overlapping_files_count_once(tmp_path):
    assert ibkr.windows(date(2024, 1, 1), date(2025, 1, 1)) == [
        (date(2024, 1, 1), date(2024, 12, 30)),
        (date(2024, 12, 31), date(2025, 1, 1)),
    ]
    trades = "<Trades>" + _buy("1", "VT", "1", "100") + "</Trades>"
    assert len(ibkr.parse_files(_files(tmp_path, _report(trades), _report(trades)))) == 1
    assert ibkr.default_start(tmp_path) == date(2024, 2, 29)  # after the last archived window


def test_derived_prices_and_closing_balances(tmp_path):
    cash = f"<CashReportCurrency {_attrs(currency='USD', levelOfDetail='Currency', endingCash='12.345678901')}/>"
    positions = f"<OpenPosition {_attrs(levelOfDetail='SUMMARY', symbol='VT', position='10', markPrice='110', currency='USD', reportDate='20241230')}/>"
    prices = f"<PriorPeriodPosition {_attrs(symbol='VT', date='20241227', price='108.5', currency='USD')}/>"
    trades = "<Trades>" + _buy("1", "VT", "10", "100") + _buy("2", "AMD", "1", "100") + "</Trades>"
    text = ibkr.derived(
        _files(tmp_path, _report(trades, cash=cash, positions=positions, prices=prices))
    )
    assert "2024-12-27 price VT  108.5 USD" in text and "2024-12-30 price VT  110 USD" in text
    assert "2024-12-31 balance Assets:IBKR:Cash:USD  12.35 USD" in text
    assert "2024-12-31 balance Assets:IBKR:VT  10 VT" in text
    assert "2024-12-31 balance Assets:IBKR:AMD  0 AMD" in text  # sold out, so IBKR lists nothing


def test_import_end_to_end_balances_agree(data, monkeypatch):
    from fin import cli, config, ledger, prices

    monkeypatch.setattr(prices, "fetch_rates", lambda ccy, quote, a, b: {a: D("0.9")})
    trades = "<Trades>" + _buy("1", "VT", "10", "100") + "</Trades>"
    cash = f"<CashReportCurrency {_attrs(currency='USD', levelOfDetail='Currency', endingCash='-1001')}/>"
    positions = f"<OpenPosition {_attrs(levelOfDetail='SUMMARY', symbol='VT', position='10', markPrice='110', currency='USD', reportDate='20241230')}/>"
    _files(data, _report(trades, cash=cash, positions=positions))
    cli._fetch_and_import(data, "ibkr", fetch=False)
    led = ledger.load(config.main_ledger(data))
    assert not led.errors
    assert sum(1 for e in led.entries if type(e).__name__ == "Balance") == 2


def test_secret_from_bitwarden(monkeypatch):
    import subprocess

    from fin import secrets

    calls = []

    def fake(args, **kw):
        calls.append(args[1:3])
        out = '{"status": "unlocked"}' if args[1] == "status" else "tok-123\n"
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    monkeypatch.setattr(secrets.shutil, "which", lambda _: "/usr/bin/bw")
    monkeypatch.setattr(secrets.subprocess, "run", fake)
    assert secrets.get("bitwarden:fin IBKR Flex") == "tok-123"
    assert ["get", "password"] in calls
    with pytest.raises(secrets.SecretError, match="unknown secret location"):
        secrets.get("plain-token")
