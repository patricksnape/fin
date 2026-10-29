import pandas as pd
import pytest

from fin.analytics import backtest


def test_a_window_runs_out_when_the_spend_outlasts_the_returns():
    flat = pd.Series(0.0, index=range(2000, 2010))
    ws = backtest.windows(flat, 5, 0.25)  # 25% of the pot a year at no return
    assert [w["failed"] for w in ws[:2]] == [2003, 2004]  # empty during the 4th year
    assert backtest.windows(flat, 5, 0.1)[0]["failed"] is None
    assert backtest.windows(flat, 5, 0.1)[0]["end"] == pytest.approx(0.5)


def test_only_complete_windows_with_data_count():
    r = pd.Series(0.05, index=[2000, 2001, 2003, 2004, 2005])  # 2002 is missing
    assert [w["start"] for w in backtest.windows(r, 3, 0.01)] == [2003]


def _jst(usd_equity, chf_per_usd, chf_cpi):
    years = list(range(2000, 2000 + len(usd_equity)))
    rows = []
    for country, eq, fx, cpi in (
        ("USA", usd_equity, [1.0] * len(years), [100.0] * len(years)),
        ("Switzerland", [0.0] * len(years), chf_per_usd, chf_cpi),
    ):
        rows += [
            {"country": country, "year": y, "eq_tr": e, "bond_tr": 0.0, "xrusd": x, "cpi": c}
            for y, e, x, c in zip(years, eq, fx, cpi, strict=True)
        ]
    return pd.DataFrame(rows)


def test_a_dollar_asset_is_converted_to_francs_and_deflated_by_swiss_inflation():
    # +10% in dollars; the dollar loses 10% against the franc; Swiss prices rise 5% the second year
    jst = _jst([0.1, 0.1, 0.1], [2.0, 1.8, 1.62], [100.0, 105.0, 110.25])
    t = backtest.real_returns(jst)["us"]
    assert t["equity_chf"].iloc[1] == pytest.approx(1.1 * 0.9 / 1.05 - 1)
    assert t["equity_usd"].iloc[1] == pytest.approx(0.1)  # a dollar spender sees no currency


def test_the_currency_section_compares_a_franc_and_a_dollar_spender():
    jst = _jst([0.1] * 12, [2.0 * 0.9**i for i in range(12)], [100.0] * 12)
    out = backtest.run(jst, 100.0, 9.0, years=8, equity="us")
    c = out["currency"]
    assert c["applies"] and c["real_return_usd"] == pytest.approx(0.1)
    assert c["real_return_chf"] < c["real_return_usd"]
    assert c["success_rate_usd"] >= c["success_rate_chf"]
    assert backtest.run(jst, 100.0, 9.0, 8, "swiss")["currency"] == {"applies": False}
