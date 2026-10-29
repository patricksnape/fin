"""FastAPI app: read-only report endpoints, pinned dashboards, and the chat stream."""

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import pandas as pd
from beancount.core import data
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from fin import account_admin, cards, config, labels, ledger, ledger_settings, registry, review
from fin import settings as app_settings
from fin.agent.backends import get_backend
from fin.agent.session import Session
from fin.analytics import (
    accounts,
    allocation,
    backtest,
    fire,
    fixed,
    networth,
    recurring,
    spend,
    trends,
)
from fin.api.context import Context
from fin.api.serialize import to_jsonable
from fin.privacy.redact import load_policy


class AgentSettingsIn(BaseModel):
    model: str
    effort: app_settings.Effort


class FireIn(BaseModel):
    withdrawal_rate: Decimal | None = None
    real_return: Decimal | None = None
    target_spend: Decimal | None = None
    birth_year: int | None = None
    pension_access_age: int | None = None
    target_age: int | None = None


class ChatIn(BaseModel):
    message: str
    conversation_id: str = "default"


def _pivot(df: pd.DataFrame, months: int) -> dict:
    """Long rows (month, key, amount) -> wide rows per month, newest `months` kept."""
    wide = (
        df.pivot(index="month", columns="key", values="amount")
        .fillna(Decimal(0))
        .sort_index()
        .tail(months)
    )
    keys = sorted(wide.columns, key=lambda k: -wide[k].sum())
    rows = [
        {
            "month": m.strftime("%Y-%m"),
            **{k: wide.loc[m, k] for k in keys},
            "total": wide.loc[m].sum(),
        }
        for m in wide.index
    ]
    return {"keys": keys, "rows": rows}


class AccountIn(BaseModel):
    account: str
    group: str | None = None
    institution: str | None = None
    currencies: list[str] = []
    cadence: str | None = None
    necessity: str | None = None
    since: date | None = None


class CardName(BaseModel):
    label: str = ""


class ReviewConfirm(BaseModel):
    payee_key: str
    category: str | None = None
    merchant_type: str | None = None
    per_type: bool = False
    remember: bool = True
    refs: list[str] | None = None  # settle only these payments


class AccountEdit(BaseModel):
    group: str | None = None
    institution: str | None = None


class LabelEdit(BaseModel):
    LABELS: ClassVar[tuple[str, ...]] = (
        "category", "cadence", "necessity", "major", "tags_add", "tags_remove"
    )  # fmt: skip
    ref: str
    also: list[str] = []
    category: str | None = None
    cadence: str | None = None
    necessity: str | None = None
    major: bool | None = None
    remember: bool = False
    remember_for_merchant_type: bool = False  # the rule only applies to this merchant type
    tags_add: list[str] = []
    tags_remove: list[str] = []


def _card_options(t: pd.DataFrame) -> list[dict]:
    """The cards payments were made on, for a filter: named ones by name, others by short id."""
    has = t[t["card"].notna()]
    if has.empty:
        return []
    shown = has["card_label"].fillna(has["card"])
    return [{"value": v, "count": int(n)} for v, n in shown.value_counts().items()]


def _tag_summary(t: pd.DataFrame) -> list[dict]:
    """Every tag you have used (not the system's `review`), with what it adds up to."""
    out: dict[str, dict] = {}
    for tags, kind, amount, day in zip(t["tags"], t["kind"], t["amount"], t["date"], strict=True):
        for tag in tags:
            if tag == "review":
                continue
            s = out.setdefault(
                tag, {"tag": tag, "count": 0, "spend": Decimal(0), "first": day, "last": day}
            )
            s["count"] += 1
            s["first"], s["last"] = min(s["first"], day), max(s["last"], day)
            if kind == "expense":
                s["spend"] -= amount
    return sorted(out.values(), key=lambda s: s["tag"])


def create_app(root: Path | None = None, backend=None) -> FastAPI:
    root = root or config.data_dir()
    ctx = Context(root)
    app = FastAPI(title="fin")
    app.state.conversations = {}
    app.state.backend = backend

    def ok(payload) -> JSONResponse:
        return JSONResponse(to_jsonable(payload))

    @app.exception_handler(ledger.EmptyLedger)
    def empty_ledger(_, exc):
        return JSONResponse({"detail": str(exc), "empty": True}, status_code=409)

    @app.get("/api/meta")
    def meta():
        led = ctx.load_ledger()
        chat = app.state.backend is not None or get_backend() is not None
        if not ledger.has_transactions(led):
            return ok(
                {
                    "empty": True,
                    "base_currency": ledger_settings.read(led).base_currency,
                    "privacy_level": load_policy(root).level,
                    "chat": chat,
                    "data_dir": str(root),
                }
            )
        d = ctx.data()
        return ok(
            {
                "empty": False,
                "base_currency": d.settings.base_currency,
                "as_of": d.model.as_of,
                "first_date": d.model.postings["date"].min(),
                "privacy_level": load_policy(root).level,
                "chat": chat,
                "data_dir": str(root),
            }
        )

    @app.get("/api/settings")
    def get_settings():
        return ok(
            {
                "agent": app_settings.load().agent,
                "models": app_settings.MODELS,
                "efforts": app_settings.EFFORTS,
                "privacy_level": load_policy(root).level,
                "settings_file": str(app_settings.path()),
            }
        )

    @app.put("/api/settings")
    def put_settings(body: AgentSettingsIn):
        if body.model not in app_settings.MODEL_IDS:
            raise HTTPException(422, f"unknown model {body.model}")
        app_settings.save(
            app_settings.AppSettings(agent=app_settings.AgentSettings(**body.model_dump()))
        )
        return get_settings()

    @app.get("/api/overview")
    def overview():
        d = ctx.data()
        nw = networth.net_worth(d.led, d.settings.base_currency)
        totals = networth.total_by_date(nw)
        latest = nw[nw["date"] == nw["date"].max()].set_index("group")["value"]
        by_date = totals.set_index("date")["value"]
        cf = d.cashflow
        last_full = cf.iloc[-2] if len(cf) > 1 else cf.iloc[-1]
        return ok(
            {
                "net_worth": {
                    "total": by_date.iloc[-1],
                    "by_group": latest.to_dict(),
                    "change_1m": by_date.iloc[-1] - by_date.iloc[-2],
                    "change_12m": by_date.iloc[-1] - by_date.iloc[max(len(by_date) - 13, 0)],
                },
                "spend": spend.headline(d.model),
                "last_full_month": {
                    "month": last_full["month"],
                    "income": last_full["income"],
                    "net": last_full["net"],
                    "savings_rate": last_full["savings_rate"],
                },
                "net_worth_series": totals,
            }
        )

    @app.get("/api/spending")
    def spending(view: str = "run_rate", months: int = 24):
        if view not in ("run_rate", "cash"):
            raise HTTPException(400, "view must be run_rate or cash")
        d = ctx.data()
        m = d.model
        return ok(
            {
                "view": view,
                "headline": spend.headline(m),
                "monthly_headline": spend.monthly_headline(m, view),
                "partial_month": m.as_of.strftime("%Y-%m"),
                "by_cadence": _pivot(spend.spend_by(m, "cadence", view), months),
                "by_necessity": _pivot(spend.spend_by(m, "necessity", view), months),
                "by_category": _pivot(spend.spend_by(m, "category", view), months),
                "by_account": _pivot(spend.spend_by(m, "account", view), months),
                "major": spend.major_purchases(m),
                "recurring": [
                    s
                    for s in sorted(m.series, key=lambda s: -s.typical_amount)
                    if s.applies or s.status == "proposed"
                ],
                "alerts": recurring.alerts(m.series, m.as_of),
            }
        )

    @app.get("/api/transactions")
    def transactions_endpoint(
        month: str = "latest",
        q: str = "",
        kind: str = "all",
        review: bool = False,
        flag: str = "",
        category: str = "",
        depth: int = 2,
        account: str = "",
        tag: str = "",
        cadence: str = "",
        necessity: str = "",
        card: str = "",
        limit: int = 1000,
        start: str = "",
        end: str = "",
        exclude: str = "",
    ):
        """Every transaction of a month (or all, or the months start..end), with filters. `summary` covers the whole month so
        the numbers do not move as you filter; `matching` is what the filters left."""
        d = ctx.data()
        t = d.transactions.assign(ym=[f"{x.year}-{x.month:02d}" for x in d.transactions["date"]])
        per_month = (
            t.groupby("ym")
            .agg(count=("ref", "size"), needs_review=("needs_review", "sum"))
            .reset_index()
            .rename(columns={"ym": "month"})
        )
        latest = f"{d.model.as_of.year}-{d.model.as_of.month:02d}"
        if month == "latest":
            month = latest
        elif month != "all" and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(400, "month must be latest, all, or YYYY-MM")
        if kind not in ("all", "expense", "income", "transfer", "investment", "asset", "opening"):
            raise HTTPException(400, "unknown kind")
        if flag not in ("", "recurring", "major"):
            raise HTTPException(400, "flag must be recurring or major")
        for bound in (start, end):
            if bound and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", bound):
                raise HTTPException(400, "start and end must be YYYY-MM")
        if start or end:  # a run of months, e.g. what a chart on another page was showing
            month = "all"
        scope = t if month == "all" else t[t["ym"] == month]
        if start:
            scope = scope[scope["ym"] >= start]
        if end:
            scope = scope[scope["ym"] <= end]
        summary = {
            "count": len(scope),
            "income": scope.loc[scope["kind"] == "income", "amount"].sum(),
            "expenses": scope.loc[scope["kind"] == "expense", "amount"].sum(),
            "needs_review": int(scope["needs_review"].sum()),
            "needs_review_spend": -scope.loc[
                scope["needs_review"] & (scope["kind"] == "expense"), "amount"
            ].sum(),
        }
        rows = scope
        if q:
            needle = q.lower()
            hay = (
                rows[["payee", "narration", "ref", "category", "account"]]
                .fillna("")
                .agg(" ".join, axis=1)
                .str.lower()
            )
            rows = rows[hay.str.contains(needle, regex=False)]
        if kind != "all":
            rows = rows[rows["kind"] == kind]
        if review:
            rows = rows[rows["needs_review"]]
        if card:  # a card's name, or its short id when it has none
            rows = rows[(rows["card_label"] == card) | (rows["card"] == card)]
        if flag:
            rows = rows[rows["recurring" if flag == "recurring" else "major"]]
        wanted = [x.strip() for x in tag.split(",") if x.strip()]
        if wanted:
            rows = rows[rows["tags"].map(lambda ts: any(w in ts for w in wanted))]
        if cadence:
            rows = rows[rows["cadence"] == cadence]
        if necessity:
            rows = rows[rows["necessity"] == necessity]
        if account.startswith("inst:"):  # every account of an institution, assets and debts alike
            reg = registry.of(ctx.load_ledger())
            names = {
                a.account
                for a in reg.balance_sheet()
                if a.institution == account.removeprefix("inst:")
            }
            rows = rows[rows["postings"].map(lambda ps: any(p["account"] in names for p in ps))]
        elif account:  # an account, or an account prefix such as Assets:UBS
            rows = rows[
                rows["postings"].map(
                    lambda ps: any(
                        p["account"] == account or p["account"].startswith(account + ":")
                        for p in ps
                    )
                )
            ]
        skip = [x.strip() for x in exclude.split(",") if x.strip()]
        if skip:  # categories to leave out, each with everything below it
            rows = rows[
                ~rows["category"].map(lambda c: any(c == k or c.startswith(k + ":") for k in skip))
            ]
        # where the expenses went, grouped at `depth` levels (2 = Expenses:Food, 3 = Expenses:Food:Dining).
        # Counted before the category filter, so everything stays on offer for picking more.
        depth = min(max(depth, 2), 5)
        spent = rows[rows["kind"] == "expense"]
        spent_by = (
            spent.assign(group=spent["category"].map(lambda c: ":".join(c.split(":")[:depth])))
            .groupby("group")
            .agg(amount=("amount", "sum"), count=("ref", "size"))
            .reset_index()
        )
        spent_by["amount"] = -spent_by["amount"]
        spent_by = spent_by.sort_values("amount", ascending=False)
        picked = [c.strip() for c in category.split(",") if c.strip()]
        if picked:  # one or several categories; each also covers everything below it
            rows = rows[
                rows["category"].map(lambda c: any(c == k or c.startswith(k + ":") for k in picked))
            ]
        rows = rows.sort_values("date", ascending=False, kind="stable")
        return ok(
            {
                "month": month,
                "tags": _tag_summary(t),
                "by_category": spent_by.rename(columns={"group": "category"}).to_dict("records"),
                "months": per_month.to_dict("records"),
                "cards": _card_options(t),
                "review_total": int(t["needs_review"].sum()),
                "summary": summary,
                "matching": len(rows),
                "truncated": len(rows) > limit,
                "rows": rows.drop(columns="ym").head(limit),
            }
        )

    @app.get("/api/transactions/similar")
    def similar_transactions(ref: str):
        """The other expenses from the same payee, for "apply to all of them"."""
        d = ctx.data()
        t = d.transactions
        me = t[t["ref"] == ref]
        if me.empty:
            raise HTTPException(404, f"no transaction {ref}")
        key = recurring.normalize_payee(me.iloc[0]["payee"] or "")
        mask = (
            (t["kind"] == "expense")
            & (t["ref"] != ref)
            & (t["payee"].map(lambda p: recurring.normalize_payee(p or "")) == key)
            & bool(key)
        )
        cols = ["ref", "date", "payee", "category", "amount", "cadence", "necessity", "major"]
        return ok({"payee": me.iloc[0]["payee"], "rows": t[mask][cols].sort_values("date")})

    @app.get("/api/account-settings")
    def account_settings():
        led = ctx.load_ledger()
        reg = registry.of(led)
        counts: dict[str, int] = {}
        for e in led.entries:
            if hasattr(e, "postings"):
                for name in {p.account for p in e.postings}:
                    counts[name] = counts.get(name, 0) + 1
        rows = [
            {
                "account": a.account,
                "kind": "liability" if a.account.startswith("Liabilities:") else "asset",
                "institution": a.institution,
                "group": a.choice,
                "explicit_group": a.explicit_group,
                "explicit_institution": a.explicit_institution,
                "guessed": a.guessed,
                "problem": a.problem,
                "currencies": a.currencies,
                "opened": a.opened,
                "closed": a.closed,
                "transactions": counts.get(a.account, 0),
            }
            for a in sorted(reg.balance_sheet(), key=lambda a: (a.institution, a.account))
        ]
        return ok(
            {
                "accounts": rows,
                "groups": {
                    "asset": registry.ASSET_GROUPS,
                    "liability": registry.LIABILITY_GROUPS,
                },
                "institutions": sorted({r["institution"] for r in rows}),
                "cadences": labels.CADENCES,
                "necessities": labels.NECESSITIES,
            }
        )

    @app.post("/api/account-settings")
    def add_account(body: AccountIn):
        try:
            text = account_admin.add_account(
                root,
                body.account,
                body.group,
                body.institution,
                tuple(body.currencies),
                body.cadence,
                body.necessity,
                body.since,
            )
        except ValueError as err:
            raise HTTPException(400, str(err)) from err
        return ok({"written": text})

    @app.put("/api/account-settings/{account}")
    def set_account(account: str, body: AccountEdit):
        changes = {k: getattr(body, k) for k in body.model_fields_set}
        try:
            account_admin.set_account(root, account, changes)
        except KeyError as err:
            raise HTTPException(404, f"no account {account}") from err
        except ValueError as err:
            raise HTTPException(400, str(err)) from err
        return ok({"updated": account})

    @app.post("/api/account-settings/{account}/close")
    def close_account(account: str):
        try:
            account_admin.close_account(root, account)
        except KeyError as err:
            raise HTTPException(404, f"no account {account}") from err
        except ValueError as err:
            raise HTTPException(400, str(err)) from err
        return ok({"closed": account})

    @app.get("/api/labels/options")
    def label_options():
        led = ctx.load_ledger()
        accts = sorted(
            e.account
            for e in led.entries
            if isinstance(e, data.Open) and e.account.startswith(("Expenses:", "Income:"))
        )
        return ok(
            {"categories": accts, "cadences": labels.CADENCES, "necessities": labels.NECESSITIES}
        )

    @app.put("/api/transactions/labels")
    def edit_labels(body: LabelEdit):
        """Change labels on a transaction and, optionally, on others, in one commit."""
        changes = {k: getattr(body, k) for k in body.model_fields_set if k in LabelEdit.LABELS}
        try:
            for key in labels.TAG_KEYS:
                if key in changes:
                    changes[key] = list(
                        dict.fromkeys(labels.normalize_tag(t) for t in changes[key])
                    )
        except ValueError as err:
            raise HTTPException(400, str(err)) from err
        changes = {k: v for k, v in changes.items() if v != []}
        if not changes:
            raise HTTPException(400, "nothing to change")
        refs = [body.ref, *body.also]

        def d_payee(ref: str) -> str:
            t = ctx.data().transactions
            return str(t.loc[t["ref"] == ref, "payee"].iloc[0] or "")

        mtype = None
        if body.remember and body.remember_for_merchant_type:  # check before changing anything
            t = ctx.data().transactions
            mtype = t.loc[t["ref"] == body.ref, "merchant_type"].iloc[0] or None
            if not mtype:
                raise HTTPException(400, "this transaction has no merchant type")
        try:
            n = labels.apply(root, {r: changes for r in dict.fromkeys(refs)})
        except KeyError as err:
            raise HTTPException(404, f"no transaction {err.args[0]}") from err
        except ValueError as err:
            raise HTTPException(400, str(err)) from err
        rule = None
        applied = 0
        if body.remember:
            payee = recurring.normalize_payee(d_payee(body.ref))
            if not payee:
                raise HTTPException(400, "this transaction has no payee to make a rule for")
            kept = {k: v for k, v in changes.items() if v is not None and k not in labels.TAG_KEYS}
            if kept:
                try:
                    labels.record_rule(root, payee, kept, merchant_type=mtype)
                    applied = labels.apply_rules(root)
                except ValueError as err:
                    raise HTTPException(400, str(err)) from err
                rule = labels.rule_key(payee, mtype)
        return ok({"changed": n, "rule": rule, "rule_applied": applied})

    @app.get("/api/cards")
    def card_list():
        return ok(cards.summary(ctx.load_ledger(), root))

    @app.put("/api/cards/{source}/{card}")
    def card_name(source: str, card: str, body: CardName):
        try:
            cards.set_label(root, source, card, body.label)
        except ValueError as err:
            raise HTTPException(400, str(err)) from err
        return ok({"card": card, "label": body.label or None})

    @app.get("/api/review")
    def review_queue():
        """Merchants with something to decide (tentative guesses and uncategorized), one row each."""
        rows = review.queue(ctx.load_ledger())
        return ok({"merchants": rows, "transactions": sum(r["count"] for r in rows)})

    @app.get("/api/review/details")
    def review_details(payee_key: str):
        """The full information on each payment of a merchant, including the source's own record."""
        rows = review.details(root, payee_key)
        if not rows:
            raise HTTPException(404, f"nothing to review for {payee_key}")
        return ok(rows)

    @app.post("/api/review/confirm")
    def review_confirm(body: ReviewConfirm):
        """Settle a merchant: keep or change its category, clear review, and (optionally) remember the rule."""
        try:
            return ok(
                review.confirm(
                    root,
                    body.payee_key,
                    body.category,
                    body.merchant_type,
                    body.per_type,
                    body.remember,
                    body.refs,
                )
            )
        except KeyError as err:
            raise HTTPException(404, f"nothing to review for {err.args[0]}") from err
        except ValueError as err:
            raise HTTPException(400, str(err)) from err

    @app.get("/api/rules")
    def label_rules():
        rules = labels.read_rules(ctx.load_ledger())
        return ok([{"payee": k, "labels": v} for k, v in sorted(rules.items())])

    @app.delete("/api/rules/{payee}")
    def forget_rule(payee: str):
        if payee not in labels.read_rules(ctx.load_ledger()):
            raise HTTPException(404, f"no rule for {payee}")
        labels.record_rule(root, payee, {})
        return ok({"forgotten": payee})

    @app.get("/api/accounts")
    def accounts_overview():
        return ok(accounts.accounts(ctx.data()))

    @app.get("/api/accounts/ledger")
    def account_ledger(account: str, month: str = "all", limit: int = 2000):
        """One account's postings in order with the running balance. `month` narrows the rows, and
        `balance_before` carries in what the account held at the start of it."""
        if month != "all" and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(400, "month must be all or YYYY-MM")
        d = ctx.data()
        try:
            full = accounts.account_ledger(d, account)
        except KeyError:
            raise HTTPException(404, f"no postings to {account}") from None
        view = full if month == "all" else accounts.month_view(full, month)
        rows = view["rows"]
        info = next(
            (
                a
                for i in accounts.accounts(d)["institutions"]
                for a in i["accounts"]
                if a["account"] == account
            ),
            None,
        )
        return ok(
            {
                "account": account,
                "info": info,
                "month": month,
                "months": accounts.months_with_activity(full),
                "balances": full["balances"],
                "balance_before": view.get("balance_before", {}),
                "matching": len(rows),
                "truncated": len(rows) > limit,
                "rows": rows[-limit:],
            }
        )

    @app.get("/api/trends")
    def trends_report():
        d = ctx.data()
        return ok(
            {
                "through": trends.complete_through(d),
                "annual": trends.annual(d),
                "overlay": trends.overlay(d),
                "rolling": trends.rolling(d),
                "categories": trends.categories_by_year(d),
            }
        )

    def _fire_inputs(d, overrides: FireIn) -> tuple[fire.Assumptions, fire.Assumptions]:
        """(saved in the ledger, saved with the try-it-out overrides applied)."""
        saved = fire.from_ledger(d)
        changes = {k: v for k, v in overrides.model_dump().items() if v is not None}
        try:
            for key, value in changes.items():
                ledger_settings.check_fire(key.replace("_", "-"), Decimal(value))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return saved, replace(saved, **changes)

    @app.get("/api/fire")
    def fire_report(
        include_pension: bool = False,
        include_home: bool = False,
        withdrawal_rate: Decimal | None = None,
        real_return: Decimal | None = None,
        target_spend: Decimal | None = None,
        birth_year: int | None = None,
        pension_access_age: int | None = None,
        target_age: int | None = None,
    ):
        """FIRE status. Any assumption passed here is tried out without saving it."""
        d = ctx.data()
        tried = FireIn(**{k: v for k, v in locals().items() if k in FireIn.model_fields})
        saved, used = _fire_inputs(d, tried)
        return ok(
            {
                "status": fire.status(d, used, include_pension, include_home),
                "saved": saved,
                "differs_from_saved": used != saved,
                "limits": {
                    k.replace("-", "_"): list(v) for k, v in ledger_settings.FIRE_PARAMS.items()
                },
            }
        )

    @app.get("/api/fire/backtest")
    def fire_backtest(
        years: int = 50,
        equity: str = "us",
        equity_share: Decimal = Decimal(1),
        spend: Decimal | None = None,
        start: Decimal | None = None,
    ):
        """How often the money would have lasted `years` years in each historical window, starting
        from your investable assets (or `start`) and a fixed real spend (target spend, your last 12
        months, or `spend`)."""
        if equity not in backtest.EQUITIES:
            raise HTTPException(400, f"equity must be one of {', '.join(backtest.EQUITIES)}")
        if not 1 <= years <= 80:
            raise HTTPException(400, "years must be between 1 and 80")
        if not 0 <= equity_share <= 1:
            raise HTTPException(400, "equity_share must be between 0 and 1")
        d = ctx.data()
        jst = backtest.load(root)
        if jst is None:
            return ok(
                {
                    "ready": False,
                    "reason": "The market history file is not in sources/market-history/",
                }
            )
        st = fire.status(d, fire.from_ledger(d))
        if not st["ready"]:
            return ok({"ready": False, "reason": st["reason"]})
        pot = start if start is not None else st["assets"]["investable"]
        yearly = spend if spend is not None else st["basis"]["annual_spend"]
        if pot <= 0 or yearly <= 0:
            return ok({"ready": False, "reason": "Needs investable assets and a spend above zero"})
        held = allocation.allocation(
            allocation.holdings(d.led, d.settings.base_currency), "currency"
        )
        usd = held[held["currency"] == "USD"]["weight"].sum() if len(held) else Decimal(0)
        result = backtest.run(jst, float(pot), float(yearly), years, equity, float(equity_share))
        return ok(
            {
                **result,
                "start": pot,
                "spend": yearly,
                "usd_share": usd,
                "choices": backtest.EQUITIES,
                "dataset_ends": 2020,
            }
        )

    @app.put("/api/fire/settings")
    def fire_save(body: FireIn):
        """Save assumptions into the ledger (one dated line each, only what changed)."""
        d = ctx.data()
        saved, _used = _fire_inputs(d, body)
        try:
            for key, value in body.model_dump().items():
                if value is not None and getattr(saved, key) != value:
                    ledger_settings.record_fire(root, key.replace("_", "-"), value)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return fire_report()

    @app.get("/api/networth")
    def net_worth():
        d = ctx.data()
        nw = networth.net_worth(d.led, d.settings.base_currency)
        wide = nw.pivot(index="date", columns="group", values="value")[networth.GROUPS]
        wide["total"] = wide.sum(axis=1)
        rows = [{"date": i, **r} for i, r in zip(wide.index, wide.to_dict("records"), strict=True)]
        return ok(
            {
                "groups": networth.GROUPS,
                "rows": rows,
                "fixed": fixed.fixed_assets(d.led, d.settings.base_currency),
            }
        )

    @app.get("/api/cashflow")
    def cashflow_report(months: int = 24):
        return ok({"rows": ctx.data().cashflow.tail(months)})

    @app.get("/api/allocation")
    def allocation_report():
        d = ctx.data()
        h = allocation.holdings(d.led, d.settings.base_currency)
        emp = d.settings.employer_commodity
        return ok(
            {
                "holdings": h,
                "by_asset_class": allocation.allocation(
                    h, "asset_class", d.settings.targets or None
                ),
                "by_currency": allocation.allocation(h, "currency"),
                "employer": {"commodity": emp, "weight": allocation.concentration(h, emp)}
                if emp
                else None,
            }
        )

    @app.get("/api/dashboards")
    def dashboards():
        d = ctx.data()
        session = Session(d, root, load_policy(root))
        tiles = []
        for path in sorted((root / "dashboards").glob("*.json")):
            saved = json.loads(path.read_text())
            try:
                event = session.render_pinned(saved["title"])
                tiles.append({"title": saved["title"], "option": event["option"]})
            except (ValueError, KeyError) as e:
                tiles.append({"title": saved["title"], "error": str(e)})
        return ok({"tiles": tiles})

    @app.post("/api/chat")
    async def chat(body: ChatIn):
        be = app.state.backend or get_backend()
        if be is None:
            raise HTTPException(
                503,
                "No model backend configured (install the 'agent' extra and set FIN_AGENT_BACKEND=claude_sdk)",
            )
        convo = app.state.conversations.setdefault(
            body.conversation_id,
            {"session": Session(ctx.data(), root, load_policy(root)), "state": {}},
        )

        # read per message so a change in Settings applies to the next question, and so questions
        # asked after you relabel or tag see the ledger as it is now
        convo["state"].update(agent=app_settings.load().agent.model_dump())
        convo["session"].refresh(ctx.data())

        async def stream() -> AsyncIterator[str]:
            session = convo["session"]
            sent = len(session.events)  # earlier turns' events were already streamed
            try:
                async for event in be.run(body.message, session, convo["state"]):
                    yield f"data: {json.dumps(to_jsonable(event))}\n\n"
                    while sent < len(session.events):
                        yield f"data: {json.dumps(to_jsonable(session.events[sent]))}\n\n"
                        sent += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - surface backend failures in the chat instead of a dead stream
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)[:300]})}\n\n"
            while sent < len(session.events):
                yield f"data: {json.dumps(to_jsonable(session.events[sent]))}\n\n"
                sent += 1
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.delete("/api/chat/{conversation_id}")
    def clear_chat(conversation_id: str):
        """Forget a conversation: its results, charts and the model's memory of it."""
        return ok({"cleared": app.state.conversations.pop(conversation_id, None) is not None})

    dist = Path(
        os.environ.get("FIN_WEB_DIST") or Path(__file__).resolve().parents[3] / "web" / "dist"
    )
    if dist.is_dir() and not os.environ.get("FIN_NO_STATIC"):
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}")
        def spa(path: str):
            return FileResponse(dist / "index.html")

    return app
