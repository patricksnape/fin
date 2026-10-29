from datetime import date
from decimal import Decimal as D

import pytest
from beancount import loader
from beancount.core import data

from fin import depreciation

LEDGER = """
2020-01-01 open Assets:Car
2020-01-01 open Assets:Bank
2020-01-01 open Equity:Opening
2020-01-01 commodity CAR
  depreciation: 0.2
2020-03-15 * "Buy"
  Assets:Car  1 CAR {10000 CHF}
  Assets:Bank  -10000 CHF
2022-03-15 * "Pay"
  Assets:Bank  -5 CHF
  Equity:Opening  5 CHF
"""


def load(text):
    entries, errors, _ = loader.load_string(text)
    assert not errors
    return depreciation.with_prices(entries)


def test_a_depreciating_commodity_is_priced_by_its_age():
    prices = {e.date: e.amount.number for e in load(LEDGER) if isinstance(e, data.Price)}
    assert prices[date(2020, 3, 15)] == D("10000.00")
    assert prices[date(2022, 3, 15)] == pytest.approx(D(6400), abs=D(5))  # two years at 20%
    assert max(prices) == date(2022, 3, 15)  # through the last transaction, no further


def test_a_quote_of_your_own_is_left_alone():
    mine = LEDGER + "2021-06-30 price CAR 9000 CHF\n"
    assert len([e for e in load(mine) if isinstance(e, data.Price)]) == 1


def test_the_value_does_not_fall_below_the_floor():
    text = LEDGER.replace("depreciation: 0.2", "depreciation: 0.9")
    assert min(e.amount.number for e in load(text) if isinstance(e, data.Price)) == D("1000.00")
