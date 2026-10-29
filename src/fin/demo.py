"""Deterministic fake ledger for developing the UI and analytics without real data.

Written in the shape the real importers will produce: imported/<source>/<YYYY>/<YYYY-MM>.beancount,
every transaction carrying a unique `ref`. Everything here is invented.
"""

import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

from fin import scaffold
from fin.gitops import commit_all

D = Decimal
START = date(2024, 11, 1)
END = date(2026, 10, 15)
OPENING = date(2024, 10, 31)
SEED = 42

# (account, cadence, necessity)
EXPENSES = [
    ("Expenses:Housing:MortgageInterest", "recurring", "essential"),
    ("Expenses:Housing:Maintenance", "regular", "essential"),
    ("Expenses:Housing:Renovation", "one_off", "discretionary"),
    ("Expenses:Insurance:Home", "recurring", "essential"),
    ("Expenses:Housing:Utilities", "recurring", "essential"),
    ("Expenses:Insurance:Health", "recurring", "essential"),
    ("Expenses:Insurance:Liability", "recurring", "essential"),
    ("Expenses:Insurance:Car", "recurring", "essential"),
    ("Expenses:Food:Groceries", "regular", "essential"),
    ("Expenses:Food:Dining", "regular", "discretionary"),
    ("Expenses:Transport:Public", "regular", "essential"),
    ("Expenses:Phone", "recurring", "essential"),
    ("Expenses:Government:Serafe", "recurring", "essential"),
    ("Expenses:Health:Pharmacy", "regular", "essential"),
    ("Expenses:Shopping:Clothing", "one_off", "discretionary"),
    ("Expenses:Shopping:Electronics", "one_off", "discretionary"),
    ("Expenses:Shopping:Home", "one_off", "discretionary"),
    ("Expenses:Subscriptions:Streaming", "recurring", "discretionary"),
    ("Expenses:Subscriptions:Software", "recurring", "discretionary"),
    ("Expenses:Subscriptions:Gym", "recurring", "discretionary"),
    ("Expenses:Travel:Flights", "one_off", "discretionary"),
    ("Expenses:Travel:Hotels", "one_off", "discretionary"),
    ("Expenses:Social:AHV", "recurring", "essential"),
    ("Expenses:Social:ALV", "recurring", "essential"),
    ("Expenses:Social:NBU", "recurring", "essential"),
    ("Expenses:Fees:Wise", "regular", "essential"),
    ("Expenses:Fees:Broker", "regular", "essential"),
    ("Expenses:Taxes:Income", "regular", "essential"),
    ("Expenses:Taxes:RSU-Withholding", "regular", "essential"),
    ("Expenses:Taxes:Withholding:US", "regular", "essential"),
    ("Expenses:Uncategorized", "regular", "discretionary"),
]

OTHER_ACCOUNTS = [
    ("Assets:UBS:Checking", ""),
    ("Assets:Wise:CHF", ""),
    ("Assets:Wise:EUR", ""),
    ("Assets:IBKR:Cash:USD", ""),
    ("Assets:IBKR:VT", '"FIFO"'),
    ("Assets:IBKR:BND", '"FIFO"'),
    ("Assets:Schwab:ACME", '"STRICT"'),
    ("Assets:Schwab:Cash:USD", ""),
    ("Assets:Pension:BVG", ""),
    ("Assets:Property:Home", '"STRICT"'),
    ("Assets:Vehicle:Car", '"STRICT"'),
    ("Liabilities:Mortgage:UBS", ""),
    ("Assets:Receivable:ESPP-Contributions", ""),
    ("Assets:Transfers:Pending", ""),
    ("Liabilities:Swisscard:MilesMore", ""),
    ("Liabilities:UBS:CreditCard", ""),
    ("Income:Employer:Salary", ""),
    ("Income:Employer:Bonus", ""),
    ("Income:Employer:RSU", ""),
    ("Income:Employer:ESPP-Discount", ""),
    ("Income:Investments:Dividends:VT", ""),
    ("Income:Investments:Gains:VT", ""),
    ("Income:Investments:Gains:ACME", ""),
    ("Equity:Opening-Balances", ""),
    ("Equity:Clearing:ShareComp", ""),
]

# institution (when it differs from the second part of the name) and net worth group
ACCOUNT_META = {
    "Assets:UBS:Checking": (None, "Cash"),
    "Assets:Wise:CHF": (None, "Cash"),
    "Assets:Wise:EUR": (None, "Cash"),
    "Assets:IBKR:Cash:USD": (None, "Cash"),
    "Assets:IBKR:VT": (None, "Investments"),
    "Assets:IBKR:BND": (None, "Investments"),
    "Assets:Schwab:ACME": (None, "Investments"),
    "Assets:Schwab:Cash:USD": (None, "Cash"),
    "Assets:Pension:BVG": (None, "Pension"),
    "Assets:Property:Home": (None, "Property"),
    "Assets:Vehicle:Car": (None, "Vehicles"),
    "Liabilities:Mortgage:UBS": ("UBS", "Mortgage"),
    "Assets:Receivable:ESPP-Contributions": (None, "Other"),
    "Assets:Transfers:Pending": (None, "Other"),
    "Liabilities:Swisscard:MilesMore": (None, "Liabilities"),
    "Liabilities:UBS:CreditCard": (None, "Liabilities"),
}

# which card pays for what: train tickets and the pharmacy go on the UBS card
CARDS = {
    "swisscard": ("swisscard", "Liabilities:Swisscard:MilesMore", "Swisscard", 8),
    "ubs": ("ubscard", "Liabilities:UBS:CreditCard", "UBS", 12),
}

COMMODITIES = [
    ("VT", "Total World Stock ETF", "equity"),
    ("BND", "Total Bond ETF", "bonds"),
    ("ACME", "Acme Corp (employer stock)", "equity"),
    ("HOME", "Home (own appraisal)", "property"),
    ("CAR", "Car (own estimate)", "vehicle"),
]


MONEY = ("CHF", "USD", "EUR")


ALWAYS_MAJOR = {"Expenses:Housing:Renovation"}
EMPLOYER_STOCK = "ACME"


def q(x) -> Decimal:
    return D(x).quantize(D("0.01"), ROUND_HALF_EVEN)


@dataclass
class Posting:
    account: str
    amount: Decimal | None = None
    ccy: str = "CHF"
    suffix: str = ""  # cost / price annotation, e.g. "{95.00 USD}" or "@@ 100.00 CHF"

    def render(self) -> str:
        if self.amount is None:
            return f"  {self.account}"
        amount = q(self.amount) if self.ccy in MONEY else self.amount
        suffix = f" {self.suffix}" if self.suffix else ""
        return f"  {self.account}  {amount} {self.ccy}{suffix}"


@dataclass
class Txn:
    date: date
    source: str
    payee: str
    narration: str
    postings: list[Posting]
    tags: tuple[str, ...] = ()
    ref: str = field(default="")
    meta: tuple[tuple[str, str], ...] = ()

    def render(self) -> str:
        tags = "".join(f" #{t}" for t in self.tags)
        head = f'{self.date} * "{self.payee}" "{self.narration}"{tags}'
        extra = [f'  {k}: "{v}"' for k, v in self.meta]
        lines = [head, f'  ref: "{self.ref}"', *extra] + [p.render() for p in self.postings]
        return "\n".join(lines)


def P(account, amount=None, ccy="CHF", suffix=""):
    return Posting(account, None if amount is None else D(amount), ccy, suffix)


def _months():
    d = START
    while d <= END:
        yield d
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)


class Generator:
    def __init__(self):
        self.rng = random.Random(SEED)
        self.txns: list[Txn] = []
        self.card: dict[tuple[str, int, int], Decimal] = defaultdict(lambda: D(0))
        self.prices = self._price_series()
        self.mortgage = D(700000)  # what is still owed, for the interest each month
        self.vt_buys: list[tuple[date, int]] = []
        self.espp_cleared = D(0)
        self.vest_lots: dict[date, Decimal] = {}

    # ---- helpers -----------------------------------------------------------------------------
    def _price_series(self):
        rng = random.Random(SEED + 1)
        cur = {"USDCHF": 0.89, "EURCHF": 0.94, "VT": 100.0, "BND": 72.0, "ACME": 100.0}
        vol = {"USDCHF": 0.012, "EURCHF": 0.008, "VT": 0.035, "BND": 0.012, "ACME": 0.07}
        drift = {"USDCHF": 0.0, "EURCHF": 0.0, "VT": 0.008, "BND": 0.001, "ACME": 0.01}
        series = {}
        d = date(2024, 10, 1)
        while d <= END:
            for k in cur:
                cur[k] *= 1 + drift[k] + rng.gauss(0, vol[k])
            series[(d.year, d.month)] = {
                k: q(v) if k in ("VT", "BND", "ACME") else D(f"{v:.4f}") for k, v in cur.items()
            }
            d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        return series

    def px(self, sym: str, d: date) -> Decimal:
        return self.prices[(d.year, d.month)][sym]

    def add(self, d, source, payee, narration, postings, tags=(), meta=()):
        if START <= d <= END or (source == "opening" and d == OPENING):
            self.txns.append(
                Txn(d, source, payee, narration, postings, tuple(tags), meta=tuple(meta))
            )

    def day(self, month: date, lo=1, hi=28) -> date:
        return month.replace(day=self.rng.randint(lo, hi))

    def card_charge(self, d, payee, narration, account, amount, tags=(), card="swisscard"):
        amount = q(amount)
        source, liability, _, _ = CARDS[card]
        self.add(d, source, payee, narration, [P(account, amount), P(liability, -amount)], tags)
        if d <= END:
            self.card[(card, d.year, d.month)] += amount

    # ---- generation --------------------------------------------------------------------------
    def build(self):
        self.opening()
        for m in _months():
            self.salary(m)
            self.fixed_bills(m)
            self.groceries(m)
            self.card_spending(m)
            self.investing(m)
        self.annual_events()
        self.trips()
        self.share_comp()
        self.card_payments()
        self.ibkr_dividends_and_sale()
        self.uncategorized()
        self.txns.sort(key=lambda t: (t.date, t.source))
        counters: dict[str, int] = defaultdict(int)
        for t in self.txns:
            counters[t.source] += 1
            t.ref = f"{t.source}:{counters[t.source]:05d}"

    def opening(self):
        d = OPENING
        eq = "Equity:Opening-Balances"
        self.add(
            d,
            "opening",
            "Opening balance",
            "UBS checking",
            [P("Assets:UBS:Checking", 40000), P(eq)],
        )
        self.add(
            d, "opening", "Opening balance", "BVG pension", [P("Assets:Pension:BVG", 80000), P(eq)]
        )
        self.add(
            d,
            "opening",
            "Opening balance",
            "Home and mortgage",
            [
                P("Assets:Property:Home", 1, "HOME", "{1000000 CHF, 2020-06-01}"),
                P("Liabilities:Mortgage:UBS", -700000),
                P(eq),
            ],
        )
        self.add(
            d,
            "opening",
            "Opening balance",
            "VT lot",
            [P("Assets:IBKR:VT", 200, "VT", "{95.00 USD, 2023-06-01}"), P(eq)],
        )
        self.add(
            d,
            "opening",
            "Opening balance",
            "BND lot",
            [P("Assets:IBKR:BND", 150, "BND", "{75.00 USD, 2023-06-01}"), P(eq)],
        )

    def salary(self, m: date):
        d = m.replace(day=25)
        gross = D(11500) if d >= date(2026, 1, 1) else D(11000)
        espp = D(500)
        vest_month = m.month in (2, 5, 8, 11)
        postings = [P("Income:Employer:Salary", -gross)]
        deductions = []
        bonus = D(8000) if m.month == 3 else D(0)
        if bonus:
            postings.append(P("Income:Employer:Bonus", -bonus))
        base = gross + bonus
        ahv, alv, nbu = q(base * D("0.053")), q(base * D("0.011")), q(base * D("0.009"))
        bvg = D(600)
        for acct, amt in (
            ("Expenses:Social:AHV", ahv),
            ("Expenses:Social:ALV", alv),
            ("Expenses:Social:NBU", nbu),
            ("Assets:Pension:BVG", bvg),
            ("Assets:Receivable:ESPP-Contributions", espp),
        ):
            deductions.append(P(acct, amt))
        net = base - ahv - alv - nbu - bvg - espp
        postings += deductions
        if vest_month:
            postings += [
                P("Equity:Clearing:ShareComp", 4000),
                P("Equity:Clearing:ShareComp", -4000),
            ]
        postings.append(P("Assets:UBS:Checking", net))
        self.add(d, "payslip", "Employer AG", f"Payslip {m:%Y-%m}", postings)

    def fixed_bills(self, m: date):
        health = D(402) if m >= date(2026, 1, 1) else D(380)
        ubs = "Assets:UBS:Checking"
        bills = [
            (
                m.replace(day=3),
                "Health Insurer AG",
                "Health insurance",
                "Expenses:Insurance:Health",
                health,
            ),
            (m.replace(day=10), "Sunrise", "Mobile plan", "Expenses:Phone", D("49.00")),
            (
                m.replace(day=12),
                "ewz",
                "Utilities",
                "Expenses:Housing:Utilities",
                q(self.rng.uniform(90, 150)),
            ),
        ]
        if m >= date(2026, 4, 1):
            bills.append(
                (
                    m.replace(day=14),
                    "AXA Versicherungen",
                    "Car insurance",
                    "Expenses:Insurance:Car",
                    D(95),
                )
            )
        for d, payee, narr, acct, amt in bills:
            self.add(d, "ubs", payee, narr, [P(acct, amt), P(ubs, -amt)])
        # mortgage: interest on what is still owed is an expense, principal reduces the debt
        interest, principal = q(self.mortgage * D("0.016") / 12), D(1000)
        self.add(
            m.replace(day=1),
            "ubs",
            "UBS Hypothek",
            "Mortgage payment",
            [
                P("Expenses:Housing:MortgageInterest", interest),
                P("Liabilities:Mortgage:UBS", principal),
                P(ubs, -(interest + principal)),
            ],
        )
        self.mortgage -= principal
        # card subscriptions
        spotify = D("14.90") if m >= date(2026, 4, 1) else D("12.90")
        self.card_charge(
            m.replace(day=6),
            "Netflix",
            "Subscription",
            "Expenses:Subscriptions:Streaming",
            D("13.90"),
        )
        self.card_charge(
            m.replace(day=7), "Spotify", "Subscription", "Expenses:Subscriptions:Streaming", spotify
        )
        if m <= date(2026, 3, 1):
            self.card_charge(
                m.replace(day=9),
                "Fitnesspark",
                "Gym membership",
                "Expenses:Subscriptions:Gym",
                D("79.00"),
            )

    def groceries(self, m: date):
        for _ in range(self.rng.randint(9, 13)):
            d = self.day(m)
            payee = self.rng.choice(["Coop", "Migros", "Aldi Suisse", "Denner"])
            amt = q(self.rng.uniform(18, 140))
            self.add(
                d,
                "ubs",
                payee,
                "Groceries",
                [P("Expenses:Food:Groceries", amt), P("Assets:UBS:Checking", -amt)],
            )

    def card_spending(self, m: date):
        rng = self.rng
        for _ in range(rng.randint(5, 9)):
            self.card_charge(
                self.day(m),
                rng.choice(
                    ["Restaurant Kronenhalle", "Sprüngli", "Hiltl", "Starbucks", "Zeughauskeller"]
                ),
                "Dining",
                "Expenses:Food:Dining",
                rng.uniform(14, 95),
            )
        for _ in range(rng.randint(2, 4)):
            self.card_charge(
                self.day(m),
                "SBB",
                "Train ticket",
                "Expenses:Transport:Public",
                rng.uniform(12, 60),
                card="ubs",
            )
        if rng.random() < 0.5:
            self.card_charge(
                self.day(m),
                "Apotheke Dr. Meier",
                "Pharmacy",
                "Expenses:Health:Pharmacy",
                rng.uniform(15, 70),
                card="ubs",
            )
        if rng.random() < 0.35:
            self.card_charge(
                self.day(m),
                rng.choice(["Zalando", "H&M", "Uniqlo"]),
                "Clothing",
                "Expenses:Shopping:Clothing",
                rng.uniform(40, 220),
            )

    def investing(self, m: date):
        d = m.replace(day=5)
        chf = D(1500)
        rate = self.px("USDCHF", d)
        usd = q(chf / rate)
        self.add(
            d,
            "ibkr",
            "Interactive Brokers",
            "Deposit",
            [
                P("Assets:UBS:Checking", -chf),
                P("Assets:IBKR:Cash:USD", usd, "USD", f"@@ {q(chf)} CHF"),
            ],
        )
        buy_d = d + timedelta(days=3)
        price = self.px("VT", buy_d)
        n = int((usd - 1) // price)
        if n > 0:
            self.vt_buys.append((buy_d, n))
            self.add(
                buy_d,
                "ibkr",
                "Interactive Brokers",
                f"Buy {n} VT",
                [
                    P("Assets:IBKR:VT", n, "VT", f"{{{price} USD}}"),
                    P("Expenses:Fees:Broker", 1, "USD"),
                    P("Assets:IBKR:Cash:USD", -(n * price + 1), "USD"),
                ],
            )

    def annual_events(self):
        ubs = "Assets:UBS:Checking"
        for y in (2025, 2026):
            self.add(
                date(y, 1, 20),
                "ubs",
                "Serafe",
                "Radio/TV fee",
                [P("Expenses:Government:Serafe", 335), P(ubs, -335)],
            )
            self.add(
                date(y, 3, 10),
                "ubs",
                "AXA Versicherungen",
                "Liability insurance",
                [P("Expenses:Insurance:Liability", 210), P(ubs, -210)],
            )
            self.add(
                date(y, 3, 12),
                "ubs",
                "Mobiliar",
                "Home insurance",
                [P("Expenses:Insurance:Home", 520), P(ubs, -520)],
            )
            self.card_charge(
                date(y, 5, 3),
                "JetBrains",
                "Software subscription",
                "Expenses:Subscriptions:Software",
                120,
            )
        for d in (date(2025, 3, 31), date(2025, 9, 30), date(2026, 3, 31), date(2026, 9, 30)):
            self.add(
                d,
                "ubs",
                "Steueramt Kanton Zürich",
                "Tax installment",
                [P("Expenses:Taxes:Income", 6000), P(ubs, -6000)],
            )
        # major purchases (excluded from monthly spend)
        self.add(
            date(2026, 3, 18),
            "ubs",
            "Küchen Schmid AG",
            "Kitchen renovation",
            [P("Expenses:Housing:Renovation", 18000), P(ubs, -18000)],
        )
        # the car is an asset (its value falls; see the price directives), not an expense
        self.add(
            date(2026, 4, 2),
            "ubs",
            "Garage Müller AG",
            "Car purchase",
            [P("Assets:Vehicle:Car", 1, "CAR", "{24500 CHF}"), P(ubs, -24500)],
        )
        for d, payee, narr, amt in (
            (date(2025, 5, 14), "Hauswart Müller", "Repairs", 640),
            (date(2026, 2, 20), "Elektro Frei AG", "Electrical repair", 380),
        ):
            self.add(d, "ubs", payee, narr, [P("Expenses:Housing:Maintenance", amt), P(ubs, -amt)])
        self.add(
            date(2025, 6, 12),
            "ubs",
            "IKEA",
            "Sofa and shelves",
            [P("Expenses:Shopping:Home", 2400), P(ubs, -2400)],
        )
        self.card_charge(
            date(2026, 1, 9), "Digitec", "Laptop", "Expenses:Shopping:Electronics", 2100
        )
        # a refund
        self.card_charge(date(2026, 6, 17), "Zalando", "Refund", "Expenses:Shopping:Clothing", -120)

    def trips(self):
        for m, city in (
            (date(2025, 3, 1), "Rome"),
            (date(2025, 8, 1), "Lisbon"),
            (date(2026, 2, 1), "Paris"),
            (date(2026, 8, 1), "Vienna"),
        ):
            tag = (
                f"{city.lower()}-{m.year}",
            )  # a trip is a tag, so its spending adds up in one place
            self.card_charge(
                m.replace(day=2),
                "Swiss Intl Air Lines",
                f"Flights {city}",
                "Expenses:Travel:Flights",
                self.rng.uniform(280, 520),
                tag,
            )
            chf = D(600)
            d = m.replace(day=9)
            rate = self.px("EURCHF", d)
            fee = D("4.50")
            eur = q((chf - fee) / rate)
            self.add(
                d - timedelta(days=2),
                "wise",
                "Wise",
                "Top up",
                [P("Assets:UBS:Checking", -chf), P("Assets:Wise:CHF", chf)],
            )
            self.add(
                d,
                "wise",
                "Wise",
                "Convert CHF to EUR",
                [
                    P("Assets:Wise:CHF", -chf),
                    P("Assets:Wise:EUR", eur, "EUR", f"@@ {q(chf - fee)} CHF"),
                    P("Expenses:Fees:Wise", fee),
                ],
            )
            remaining = eur * D("0.88")
            hotel = q(remaining * D("0.55"))
            self.add(
                d + timedelta(days=1),
                "wise",
                f"Hotel {city}",
                "Hotel",
                [P("Expenses:Travel:Hotels", hotel, "EUR"), P("Assets:Wise:EUR", -hotel, "EUR")],
                tag,
            )
            left = remaining - hotel
            for i in range(4):
                amt = q(left / 4)
                self.add(
                    d + timedelta(days=2 + i),
                    "wise",
                    f"{city} Café",
                    "Dining",
                    [P("Expenses:Food:Dining", amt, "EUR"), P("Assets:Wise:EUR", -amt, "EUR")],
                    tag,
                )

    def share_comp(self):
        vests = [
            date(2024, 11, 15),
            date(2025, 2, 14),
            date(2025, 5, 15),
            date(2025, 8, 15),
            date(2025, 11, 14),
            date(2026, 2, 13),
            date(2026, 5, 15),
            date(2026, 8, 14),
        ]
        for d in vests:
            fmv = self.px("ACME", d)
            self.vest_lots[d] = fmv
            granted = date(d.year - 1, 11, 15) if d.month >= 11 else date(d.year - 1, 5, 15)
            self.add(
                d,
                "schwab",
                "Acme Corp",
                "vest 40 ACME (14 withheld for tax)",
                [
                    P("Assets:Schwab:ACME", 26, "ACME", f"{{{fmv} USD, {d}}}"),
                    P("Expenses:Taxes:RSU-Withholding", 14 * fmv, "USD"),
                    P("Income:Employer:RSU", -(40 * fmv), "USD"),
                ],
                meta=(
                    ("award", f"A{granted:%Y%m}"),
                    ("award_date", granted.isoformat()),
                    ("award_name", "RSU"),
                ),
            )
        accrued = D(0)
        payslips = sorted(t.date for t in self.txns if t.source == "payslip")
        for pd in (date(2025, 3, 31), date(2025, 9, 30), date(2026, 3, 31), date(2026, 9, 30)):
            accrued = D(500) * sum(1 for p in payslips if p <= pd) - self.espp_cleared
            rate = self.px("USDCHF", pd)
            fmv = self.px("ACME", pd)
            price = q(fmv * D("0.85"))
            avail_usd = accrued / rate
            qty = int(avail_usd // price)
            cost_usd = qty * price
            used = q(cost_usd * rate)
            self.espp_cleared += used
            self.add(
                pd,
                "schwab",
                "Acme Corp",
                "ESPP purchase",
                [
                    P("Assets:Schwab:ACME", qty, "ACME", f"{{{fmv} USD, {pd}}}"),
                    P("Income:Employer:ESPP-Discount", -(qty * (fmv - price)), "USD"),
                    P(
                        "Assets:Receivable:ESPP-Contributions",
                        -used,
                        "CHF",
                        f"@@ {q(cost_usd)} USD",
                    ),
                ],
                meta=(("offering_start", (pd - timedelta(days=182)).isoformat()),),
            )
            leftover = accrued - used
            self.add(
                pd + timedelta(days=3),
                "payslip",
                "Employer AG",
                "ESPP leftover refund",
                [
                    P("Assets:Receivable:ESPP-Contributions", -q(leftover)),
                    P("Assets:UBS:Checking", q(leftover)),
                ],
            )
            self.espp_cleared += q(leftover)
        # one sale from the Feb 2025 vest lot, wired to UBS
        lot_date = date(2025, 2, 14)
        sd = date(2026, 6, 10)
        price = self.px("ACME", sd)
        proceeds = 26 * price
        self.add(
            sd,
            "schwab",
            "Acme Corp",
            "Sale of 26 ACME",
            [
                P(
                    "Assets:Schwab:ACME",
                    -26,
                    "ACME",
                    f"{{{self.vest_lots[lot_date]} USD, {lot_date}}} @ {price} USD",
                ),
                P("Assets:Schwab:Cash:USD", proceeds, "USD"),
                P("Income:Investments:Gains:ACME"),
            ],
        )
        chf = q(proceeds * self.px("USDCHF", sd))
        self.add(
            sd + timedelta(days=3),
            "schwab",
            "Acme Corp",
            "Wire to UBS",
            [
                P("Assets:Schwab:Cash:USD", -proceeds, "USD", f"@@ {chf} CHF"),
                P("Assets:UBS:Checking", chf),
            ],
        )

    def ibkr_dividends_and_sale(self):
        for y in (2024, 2025, 2026):
            for mo in (3, 6, 9, 12):
                d = date(y, mo, 20)
                if not (START <= d <= END):
                    continue
                units = 200 + sum(n for bd, n in self.vt_buys if bd <= d)
                div = q(units * D("0.80"))
                wht = q(div * D("0.15"))
                self.add(
                    d,
                    "ibkr",
                    "Interactive Brokers",
                    "VT dividend",
                    [
                        P("Income:Investments:Dividends:VT", -div, "USD"),
                        P("Expenses:Taxes:Withholding:US", wht, "USD"),
                        P("Assets:IBKR:Cash:USD", div - wht, "USD"),
                    ],
                )
        d = date(2026, 6, 24)
        price = self.px("VT", d)
        self.add(
            d,
            "ibkr",
            "Interactive Brokers",
            "Sell 20 VT",
            [
                P("Assets:IBKR:VT", -20, "VT", f"{{}} @ {price} USD"),
                P("Assets:IBKR:Cash:USD", 20 * price, "USD"),
                P("Income:Investments:Gains:VT"),
            ],
        )

    def card_payments(self):
        for card, (source, liability, payee, day) in CARDS.items():
            for m in _months():
                nxt = date(m.year + (m.month == 12), m.month % 12 + 1, day)
                total = self.card.get((card, m.year, m.month), D(0))
                if total > 0:
                    self.add(
                        nxt,
                        source,
                        payee,
                        "Card payment",
                        [P(liability, total), P("Assets:UBS:Checking", -total)],
                    )

    def uncategorized(self):
        payees = [
            "SUMUP *KIOSK 4412",
            "PAYPAL *3KX9",
            "TWINT P2P",
            "ZVV TICKETAUTOMAT",
            "BAR 1930",
            "PAYPAL *LK22",
            "KIOSK HB 0113",
            "TWINT P2P",
            "PAYPAL *91AA",
            "MM RESTAURANT",
        ]
        for i, p in enumerate(payees):
            d = date(2026, 9, 1) + timedelta(days=i * 3)
            self.card_charge(
                d, p, "Card payment", "Expenses:Uncategorized", self.rng.uniform(8, 60), ("review",)
            )

    # ---- output ------------------------------------------------------------------------------
    def accounts_text(self) -> str:
        lines = []
        for acct, cadence, need in EXPENSES:
            lines += [
                f"2024-01-01 open {acct}",
                f'  cadence: "{cadence}"',
                f'  necessity: "{need}"',
                *(["  major: TRUE"] if acct in ALWAYS_MAJOR else []),
            ]
        for acct, booking in OTHER_ACCOUNTS:
            lines.append(f"2024-01-01 open {acct} {booking}".rstrip())
            if acct in ACCOUNT_META:
                inst, group = ACCOUNT_META[acct]
                lines += ([f'  institution: "{inst}"'] if inst else []) + [f'  group: "{group}"']
        return "\n".join(lines) + "\n"

    def commodities_text(self) -> str:
        return (
            "\n".join(
                f'2024-01-01 commodity {sym}\n  name: "{name}"\n  asset_class: "{cls}"'
                + ("\n  employer: TRUE" if sym == EMPLOYER_STOCK else "")
                for sym, name, cls in COMMODITIES
            )
            + "\n"
        )

    def prices_text(self) -> str:
        out = []
        for (y, m), p in sorted(self.prices.items()):
            d = date(y, m, 1)
            if d > END:
                continue
            out += [f"{d} price USD {p['USDCHF']} CHF", f"{d} price EUR {p['EURCHF']} CHF"]
            out += [f"{d} price {s} {p[s]} USD" for s in ("VT", "BND", "ACME")]
        # fixed assets are valued by hand: one price line each time you update the estimate
        out += [
            "2020-06-01 price HOME 1000000 CHF",
            "2024-10-01 price HOME 1080000 CHF",
            "2025-10-01 price HOME 1105000 CHF",
            "2026-10-01 price HOME 1140000 CHF",
            "2026-04-02 price CAR 24500 CHF",
            "2026-10-01 price CAR 21800 CHF",
        ]
        return "\n".join(out) + "\n"

    def write_price_history(self, root: Path):
        """A daily-close file for the employer stock back before the ledger's own quotes begin, so the
        demo's awards (granted a year before they vest) have a price on their award date."""
        first = self.prices[(START.year, START.month)][EMPLOYER_STOCK]
        rows = ["Date,Open,High,Low,Close,Volume"]
        day = date(2023, 1, 1)
        while day < START:
            months_before = (START.year - day.year) * 12 + START.month - day.month
            close = q(first * (D(1) - D("0.02") * months_before))
            rows.append(f"{day},{close},{close},{close},{close},1000")
            day += timedelta(days=1)
        folder = root / "sources" / "market-history"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{EMPLOYER_STOCK}.csv").write_text("\n".join(rows) + "\n")

    def write(self, root: Path):
        ledger = root / "ledger"
        (ledger / "accounts.beancount").write_text(self.accounts_text())
        (ledger / "commodities.beancount").write_text(self.commodities_text())
        (ledger / "prices.beancount").write_text(self.prices_text())
        self.write_price_history(root)
        groups: dict[Path, list[Txn]] = defaultdict(list)
        for t in self.txns:
            if t.source == "opening":
                path = ledger / "opening" / "opening.beancount"
            else:
                path = ledger / "imported" / t.source / f"{t.date:%Y}" / f"{t.date:%Y-%m}.beancount"
            groups[path].append(t)
        for path, txns in groups.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n\n".join(t.render() for t in txns) + "\n")
        # settings that change what the numbers mean live in the ledger, like any plaintext setup
        (ledger / "settings.beancount").write_text(
            "; DEMO DATA - everything in this repo is invented.\n"
            '2020-01-01 custom "fin-major-threshold" 2000 CHF\n'
            '2020-01-01 custom "fin-allocation-target" "equity" 0.75\n'
            '2020-01-01 custom "fin-allocation-target" "bonds" 0.15\n'
            '2020-01-01 custom "fin-allocation-target" "cash" 0.10\n'
            '2020-01-01 custom "fin-fire" "withdrawal-rate" 0.035\n'
            '2020-01-01 custom "fin-fire" "real-return" 0.04\n'
            '2020-01-01 custom "fin-fire" "birth-year" 1988\n'
            '2020-01-01 custom "fin-fire" "pension-access-age" 60\n'
            '2020-01-01 custom "fin-fire" "target-age" 55\n'
        )
        (ledger / "manual" / "recurring.beancount").write_text(
            f'{END} custom "fin-recurring" "confirmed" "serafe" Expenses:Government:Serafe\n\n'
            f'{END} custom "fin-recurring" "confirmed" "axa versicherungen" Expenses:Insurance:Liability\n\n'
            f'{END} custom "fin-recurring" "confirmed" "jetbrains" Expenses:Subscriptions:Software\n\n'
            f'{END} custom "fin-recurring" "confirmed" "mobiliar" Expenses:Insurance:Home\n'
        )
        (root / "config.toml").write_text("# DEMO DATA - everything in this repo is invented.\n")


def create_demo(root: Path) -> int:
    """Create a demo data repo at `root`. Returns the number of transactions."""
    scaffold.init_data(root)
    gen = Generator()
    gen.build()
    gen.write(root)
    commit_all(root, "demo data")
    return len(gen.txns)
