"""How often would the money have lasted? A historical backtest of a fixed real spend.

For every start year in the Jordà-Schularick-Taylor data (annual returns, 1870-2020, kept in the
data repo under sources/market-history/), take the same spend out of the same starting pot at the
start of each of the next N years and let the rest grow by that year's real return. A window
succeeds when the pot is still positive after N years. Only complete windows count.

Returns are real, in the currency you spend. You hold dollar assets and spend francs, so a US
equity return is turned into francs with that year's change of the franc/dollar rate and deflated
by Swiss inflation. The same windows are also run for someone who spends dollars, which shows what
the currency alone changed.
"""

from __future__ import annotations

import functools
from pathlib import Path

import numpy as np
import pandas as pd

SOURCE = Path("sources") / "market-history" / "JSTdatasetR6.dta"
EQUITIES = {
    "us": "US equity (like VOO), converted to francs",
    "world": "18 countries equally weighted, converted to francs (a stand-in for VT)",
    "swiss": "Swiss equity, in francs",
}
SPEND_STEPS = (0.6, 0.8, 1.0, 1.2, 1.4, 1.6)


def load(root: Path) -> pd.DataFrame | None:
    path = root / SOURCE
    return _read(path, path.stat().st_mtime_ns) if path.exists() else None


@functools.lru_cache(maxsize=2)
def _read(path: Path, _mtime: int) -> pd.DataFrame:
    return pd.read_stata(path)


def _by_country(jst: pd.DataFrame, column: str) -> pd.DataFrame:
    return jst.pivot(index="year", columns="country", values=column).sort_index()


def real_returns(jst: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Per equity choice, a table by year with the columns equity_chf, equity_usd, bond_chf and
    bond_usd: real returns for someone spending francs / dollars. equity_usd is empty for Swiss
    equity (no dollar view)."""
    eq, bond = _by_country(jst, "eq_tr"), _by_country(jst, "bond_tr")
    cpi, fx = _by_country(jst, "cpi"), _by_country(jst, "xrusd")  # fx: local currency per dollar
    infl = cpi / cpi.shift(1)
    chf_per_usd = fx["Switzerland"]
    franc_gain = chf_per_usd / chf_per_usd.shift(1)  # a dollar asset is worth this much more
    real_chf, real_usd = infl["Switzerland"], infl["USA"]

    # every country's equity return in dollars: local return, then the local currency against the dollar
    in_usd = (1 + eq) * fx.shift(1) / fx - 1
    world_usd = in_usd.where(eq.notna()).mean(axis=1, skipna=True)
    world_usd = world_usd.where(in_usd.count(axis=1) >= 3)

    bonds = pd.DataFrame(
        {
            "bond_chf": (1 + bond["Switzerland"]) / real_chf - 1,
            "bond_usd": (1 + bond["USA"]) / real_usd - 1,
        }
    )
    out = {}
    for key, usd in (("us", eq["USA"]), ("world", world_usd)):
        out[key] = pd.DataFrame(
            {
                "equity_chf": (1 + usd) * franc_gain / real_chf - 1,
                "equity_usd": (1 + usd) / real_usd - 1,
            }
        ).join(bonds)
    out["swiss"] = pd.DataFrame(
        {"equity_chf": (1 + eq["Switzerland"]) / real_chf - 1, "equity_usd": np.nan}
    ).join(bonds)
    return out


def _portfolio(table: pd.DataFrame, side: str, equity_share: float) -> pd.Series:
    """The yearly real return of the mix, rebalanced each year; needs the bond column only when
    some of the mix is in bonds."""
    eq = table[f"equity_{side}"]
    if equity_share >= 1:
        return eq
    return equity_share * eq + (1 - equity_share) * table[f"bond_{side}"]


def windows(returns: pd.Series, years: int, spend: float, start: float = 1.0) -> list[dict]:
    """One simulation per complete window of `years` consecutive years."""
    r = returns.dropna()
    have = set(r.index)
    out = []
    for first in sorted(have):
        span = range(first, first + years)
        if not all(y in have for y in span):
            continue
        pot, path, failed = start, [], None
        for y in span:
            if failed is None:
                pot = (pot - spend) * (1 + r[y])
                if pot <= 0:
                    failed, pot = y, 0.0
            path.append(pot)
        out.append({"start": first, "failed": failed, "end": path[-1], "path": path})
    return out


def _rate(ws: list[dict]) -> float | None:
    return round(sum(w["failed"] is None for w in ws) / len(ws), 4) if ws else None


def _geomean(r: pd.Series) -> float:
    return float(np.exp(np.log1p(r.dropna()).mean()) - 1)


def run(
    jst: pd.DataFrame,
    start: float,
    spend: float,
    years: int = 50,
    equity: str = "us",
    equity_share: float = 1.0,
) -> dict:
    """The backtest for a starting pot and a yearly real spend. All money in the table is real,
    francs for the franc spender."""
    tables = real_returns(jst)
    table = tables[equity]
    chf = _portfolio(table, "chf", equity_share)
    ws = windows(chf, years, spend / start)
    if not ws:
        return {"ready": False, "reason": f"No {years}-year window in the data"}
    rate = _rate(ws)
    ends = np.array([w["end"] for w in ws])
    paths = np.array([w["path"] for w in ws]) * start
    fan = [
        {
            "year": i + 1,
            "p10": float(np.percentile(paths[:, i], 10)),
            "p50": float(np.percentile(paths[:, i], 50)),
            "p90": float(np.percentile(paths[:, i], 90)),
        }
        for i in range(years)
    ]
    failed = sorted((w for w in ws if w["failed"] is not None), key=lambda w: w["start"])
    out = {
        "ready": True,
        "years": years,
        "equity": equity,
        "equity_share": equity_share,
        "windows": len(ws),
        "first_start": ws[0]["start"],
        "last_start": ws[-1]["start"],
        "success_rate": rate,
        "failed_starts": [
            {
                "start": w["start"],
                "ran_out_in": w["failed"],
                "after_years": w["failed"] - w["start"] + 1,
            }
            for w in failed
        ],
        "end_p10": float(np.percentile(ends, 10) * start),
        "end_median": float(np.median(ends) * start),
        "fan": fan,
        "by_spend": [
            {
                "multiple": m,
                "spend": spend * m,
                "success_rate": _rate(windows(chf, years, spend * m / start)),
            }
            for m in SPEND_STEPS
        ],
    }
    out["currency"] = _currency(table, years, spend / start, equity_share, rate)
    return out


def _currency(table: pd.DataFrame, years: int, spend: float, share: float, chf_rate) -> dict:
    """What the franc/dollar rate alone did: the same windows for a franc and for a dollar spender."""
    if table["equity_usd"].isna().all():
        return {"applies": False}
    both = table.dropna(subset=["equity_chf", "equity_usd"])
    usd_ws = windows(_portfolio(table, "usd", share), years, spend)
    return {
        "applies": True,
        "years_of_data": [int(both.index.min()), int(both.index.max())],
        "real_return_chf": _geomean(both["equity_chf"]),
        "real_return_usd": _geomean(both["equity_usd"]),
        "success_rate_chf": chf_rate,
        "success_rate_usd": _rate(usd_ws),
    }
