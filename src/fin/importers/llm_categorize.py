"""Let a model pick expense accounts for merchants the mapping cannot place precisely.

What leaves the machine, per merchant: its (redacted) name, the card network's merchant type and the
portal's coarse category, plus your expense account names. No amounts, dates or descriptions, and
nothing about people you pay person-to-person (they are never asked about). It
works per merchant, not per transaction, and each confident answer is saved as a payee rule in the
ledger, so a merchant is only ever asked about once and later imports stay deterministic.

Targets are transactions still on Expenses:Uncategorized or on an account listed under `llm_refine`
in rules/categories.yaml (too generic, e.g. Expenses:Shopping:General). Medium-confidence answers
are applied but keep #review; low-confidence ones are left alone. A model may propose a new account
only inside an expense group you already have.
"""

import asyncio
import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from beancount.core import data

from fin import account_admin, config, labels, ledger, registry, settings
from fin.analytics.recurring import normalize_payee
from fin.importers import categorize
from fin.privacy.redact import ledger_people, load_policy

BATCH = 60
MAX_NEW_ACCOUNTS = 12
NEVER = ("Expenses:Uncategorized", "Expenses:Taxes", "Expenses:Social", "Expenses:Fees")

SYSTEM = """You categorize merchants for one person's expense tracking. For each merchant you see \
its name, the card network's merchant type and a coarse category. You never see amounts.
Pick the single best account from the provided list. Only if none fits, propose a new account named \
Expenses:<Group>:<Name> where <Group> is a group that already appears in the list (CamelCase, \
letters only). Be honest about confidence: "high" only when the merchant clearly belongs there, \
"medium" when it plausibly does, "low" when you are guessing (use account null for low).
Reply with JSON only: {"results": [{"id": <int>, "account": "<account or null>", \
"confidence": "high|medium|low"}]} with one entry per id."""


@dataclass
class Report:
    asked: int = 0
    applied_high: int = 0
    applied_medium: int = 0
    skipped: int = 0
    by_rule: int = 0
    new_accounts: list[str] = field(default_factory=list)
    by_account: dict[str, int] = field(default_factory=dict)


PRIVATE_ENV = {
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}


def private_options(system: str, model: str, cwd: str):
    """A one-shot, tool-less session that leaves no transcript on this machine and opts out of
    telemetry and other non-essential traffic. Retention on Anthropic's side is set by your plan or
    organization, not per request."""
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        system_prompt=system,
        tools=[],
        setting_sources=[],
        max_turns=1,
        model=model,
        cwd=cwd,
        extra_args={"no-session-persistence": None},
        env=PRIVATE_ENV,
    )


def claude_ask(system: str, user: str, model: str) -> str:
    """One private completion through the Claude Agent SDK."""
    from claude_agent_sdk import AssistantMessage, TextBlock, query

    async def go(cwd: str) -> str:
        out = []
        async for msg in query(prompt=user, options=private_options(system, model, cwd)):
            if isinstance(msg, AssistantMessage):
                out += [b.text for b in msg.content if isinstance(b, TextBlock)]
        return "".join(out)

    with tempfile.TemporaryDirectory(prefix="fin-cat-") as cwd:
        return asyncio.run(go(cwd))


def refine_accounts(maps: dict) -> set[str]:
    return {labels.UNCATEGORIZED, *maps.get("llm_refine", ["Expenses:Shopping:General"])}


def _parse(text: str) -> list[dict]:
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < a:
        return []
    try:
        return json.loads(text[a : b + 1]).get("results", [])
    except ValueError:
        return []


def valid_account(account, opened: set[str], never: tuple[str, ...] = NEVER) -> bool:
    if (
        not isinstance(account, str)
        or not account.startswith("Expenses:")
        or account.startswith(never)
    ):
        return False
    if account in opened:
        return True
    groups = {a.split(":")[1] for a in opened if a.startswith("Expenses:") and a.count(":") >= 2}
    return (
        bool(registry.NAME.fullmatch(account))
        and account.count(":") >= 2
        and (account.split(":")[1] in groups)
    )


def people_keys(entries) -> set[str]:
    return {normalize_payee(p) for p in ledger_people(entries)}


def collect(root: Path, led, maps: dict) -> dict[str, dict]:
    """Still-generic transactions grouped by normalized payee. Payments to people are left out:
    their names must not leave the machine, and a model could not place them anyway."""
    refine = refine_accounts(maps)
    out: dict[str, dict] = {}
    people = people_keys(led.entries)
    for e in led.entries:
        if not isinstance(e, data.Transaction) or not (e.meta or {}).get("ref"):
            continue
        spent = [p for p in e.postings if p.account.startswith("Expenses:")]
        if len(spent) != 1 or spent[0].account not in refine:
            continue
        key = normalize_payee(e.payee or "")
        if not key or key in people:
            continue
        g = out.setdefault(
            key,
            {
                "payee": e.payee,
                "refs": [],
                "merchant": e.meta.get("merchant_category"),
                "portal": e.meta.get("portal_category"),
            },
        )
        g["refs"].append(e.meta["ref"])
    return out


def payload(groups: dict[str, dict], policy) -> tuple[list[str], list[dict]]:
    keys = sorted(groups)
    items = [
        {
            "id": i,
            "merchant": policy.redact(groups[k]["payee"]),
            "merchant_type": groups[k]["merchant"] and policy.redact(str(groups[k]["merchant"])),
            "category": groups[k]["portal"] and policy.redact(str(groups[k]["portal"])),
        }
        for i, k in enumerate(keys)
    ]
    return keys, items


def run(
    root: Path,
    ask: Callable[[str, str, str], str] = claude_ask,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> Report:
    maps = categorize.load_map(root)
    led = ledger.load(config.main_ledger(root))
    never = NEVER + tuple(config.load_settings(root, led).non_spend_prefixes)
    opened = {e.account for e in led.entries if isinstance(e, data.Open)}
    rules = labels.read_rules(led)
    groups = collect(root, led, maps)
    report = Report()
    edits: dict[str, dict] = {}

    for key in list(groups):  # a payee rule always beats a generic mapping
        rule = labels.rule_for(rules, key, groups[key]["merchant"])
        cat = (rule or {}).get("category")
        if cat in opened and cat not in refine_accounts(maps):
            edits.update(
                {ref: {"category": cat, "categorized": "rule"} for ref in groups[key]["refs"]}
            )
            report.by_rule += len(groups.pop(key)["refs"])

    policy = load_policy(root).with_ledger(led.entries)
    keys, items = payload(groups, policy)
    # accounts that are not spending (business, taxes) or that carry a redacted name are not offered
    accounts = sorted(
        a
        for a in opened
        if a.startswith("Expenses:") and not a.startswith(never) and policy.redact(a) == a
    )
    offered, people = set(accounts), people_keys(led.entries)
    examples = [
        {"merchant": policy.redact(k.replace("|", " / ")), "account": a}
        for k, a in labels.rule_examples(led)
        if a in offered and k.split("|")[0] not in people
    ]
    if dry_run:
        log(f"Would send {len(items)} merchants (names, merchant type, category; no amounts).")
        log("System prompt:\n" + SYSTEM + "\n\nAccounts offered:\n" + "\n".join(accounts))
        log(f"\nYour past decisions shown as examples ({len(examples)}):")
        log("\n".join(json.dumps(x) for x in examples))
        for it in items:
            log(json.dumps(it))
        return report

    model = settings.load().agent.model
    rule_writes: dict[str, str] = {}
    for i in range(0, len(items), BATCH):
        chunk = items[i : i + BATCH]
        user = (
            "Accounts:\n"
            + "\n".join(accounts)
            + (
                "\n\nThe user's past decisions (follow their habits):\n" + json.dumps(examples)
                if examples
                else ""
            )
            + "\n\nMerchants:\n"
            + json.dumps(chunk)
        )
        answers = {r.get("id"): r for r in _parse(ask(SYSTEM, user, model))}
        for it in chunk:
            a = answers.get(it["id"], {})
            report.asked += 1
            account, conf = a.get("account"), a.get("confidence")
            if conf not in ("high", "medium") or not valid_account(
                account, offered | set(report.new_accounts), never
            ):
                report.skipped += 1
                continue
            if account not in opened | set(report.new_accounts):
                if len(report.new_accounts) >= MAX_NEW_ACCOUNTS:
                    report.skipped += 1
                    continue
                report.new_accounts.append(account)
            key = keys[it["id"]]
            change = {"category": account, "categorized": "model"}
            if conf == "medium":
                change["tags_add"] = ["review"]
                report.applied_medium += len(groups[key]["refs"])
            else:
                rule_writes[key] = account
                report.applied_high += len(groups[key]["refs"])
            edits.update({ref: dict(change) for ref in groups[key]["refs"]})
            report.by_account[account] = report.by_account.get(account, 0) + len(
                groups[key]["refs"]
            )

    for account in report.new_accounts:
        cadence, necessity = categorize.CHART.get(account, ("regular", "discretionary"))
        account_admin.add_account(
            root, account, cadence=cadence, necessity=necessity, since=categorize.OPENED_ON
        )
    if edits:
        labels.apply(root, edits, f"llm categorize: {len(edits)} transaction(s)")
    for key, account in rule_writes.items():
        labels.record_rule(root, key, {"category": account}, source="model")
    return report
