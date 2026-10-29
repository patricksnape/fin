"""Edit a transaction's labels in the ledger text, found by its `ref`.

Labels are plain Beancount: the category is the expense account of the posting, and cadence,
necessity and major are metadata lines on the transaction. Editing rewrites only the
transaction's own lines in its file, then keeps the change only if the ledger still loads.
"""

import re
from pathlib import Path

from beancount.core import data

from fin import gitops

CADENCES = ("recurring", "regular", "one_off")
NECESSITIES = ("essential", "discretionary")
META_KEYS = ("cadence", "necessity", "major")
_META_LINE = re.compile(r"^(\s+)(cadence|necessity|major):")
_POSTING = re.compile(r"^  [A-Z][A-Za-z0-9-]*:")
TAG_KEYS = ("tags_add", "tags_remove")
# How an automatic category was chosen. Absent means a person chose it: choosing a category by hand
# (any edit that sets `category` without `categorized`) clears it.
PROVENANCE = ("mapping", "model", "rule")
_CAT_LINE = re.compile(r"^\s+categorized:")
_TAG = re.compile(r"[a-z0-9][a-z0-9_/.-]*")


def normalize_tag(tag: str) -> str:
    """`#Canada 2026` -> `canada-2026`: what can be written as a Beancount tag."""
    out = re.sub(r"\s+", "-", tag.strip().lstrip("#").strip().lower())
    if not _TAG.fullmatch(out):
        raise ValueError(f"{tag!r} cannot be a tag: use letters, digits, - _ / .")
    return out


def _retag(header: str, add: list[str], remove: list[str]) -> str:
    """Add and remove `#tags` on a transaction's first line, leaving links and comments alone."""
    q = header.rfind('"')
    if q < 0:
        raise ValueError("a transaction without a narration cannot be tagged")
    head, tail = header[: q + 1], header[q + 1 :]
    tail, sep, comment = tail.partition(";")
    toks = tail.split()
    have = [t[1:] for t in toks if t.startswith("#")]
    rest = [t for t in toks if not t.startswith("#")]
    new = [t for t in have if t not in remove] + [t for t in add if t not in have]
    return (
        head
        + "".join(f" #{t}" for t in new)
        + "".join(f" {t}" for t in rest)
        + (f" ;{comment}" if sep else "")
    )


def _validate(changes: dict, opened: set[str]) -> None:
    unknown = set(changes) - {"category", "categorized", *META_KEYS, *TAG_KEYS}
    if unknown:
        raise ValueError(f"unknown label: {', '.join(sorted(unknown))}")
    cat = changes.get("category")
    if "category" in changes and (
        cat is None or not cat.startswith(("Expenses:", "Income:")) or cat not in opened
    ):
        raise ValueError(f"{cat} is not an open expense or income account")
    if changes.get("categorized") not in (*PROVENANCE, None):
        raise ValueError(f"categorized must be one of {', '.join(PROVENANCE)}")
    if changes.get("cadence", "regular") not in (*CADENCES, None):
        raise ValueError(f"cadence must be one of {', '.join(CADENCES)}")
    if changes.get("necessity", "essential") not in (*NECESSITIES, None):
        raise ValueError(f"necessity must be one of {', '.join(NECESSITIES)}")
    for key in TAG_KEYS:
        if not all(isinstance(t, str) and _TAG.fullmatch(t) for t in changes.get(key, [])):
            raise ValueError("tags must be normalized (see labels.normalize_tag)")
    if not isinstance(changes.get("major"), bool | None):
        raise ValueError("major must be true, false or null")  # noqa: TRY004


def _block_end(lines: list[str], start: int) -> int:
    end = start + 1
    while end < len(lines) and lines[end].strip():
        end += 1
    return end


def _rewrite(block: list[str], changes: dict, ref: str) -> list[str]:
    out = list(block)
    if "category" in changes:
        # the one expense or income posting (a refund or reimbursement may move between the two)
        spots = [i for i, ln in enumerate(out) if ln.strip().startswith(("Expenses:", "Income:"))]
        if len(spots) != 1:
            raise ValueError(
                f"{ref} has {len(spots)} expense/income postings, so its category is ambiguous"
            )
        i = spots[0]
        out[i] = re.sub(r"(?:Expenses|Income):\S+", changes["category"], out[i], count=1)
        out[0] = re.sub(r"\s+#review\b", "", out[0])  # a category chosen by hand is reviewed
    # after the category: choosing a category clears #review, unless a tag is asked for explicitly
    if changes.get("tags_add") or changes.get("tags_remove"):
        out[0] = _retag(out[0], changes.get("tags_add", []), changes.get("tags_remove", []))
    wanted = [k for k in META_KEYS if k in changes]
    if wanted:
        # drop the old value wherever it was written (transaction or posting)
        out = [
            ln
            for ln in out
            if not (_META_LINE.match(ln) and _META_LINE.match(ln).group(2) in wanted)
        ]
        new = []
        for key in wanted:
            value = changes[key]
            if value is None:
                continue
            new.append(
                f"  {key}: " + (("TRUE" if value else "FALSE") if key == "major" else f'"{value}"')
            )
        first_posting = next(i for i, ln in enumerate(out) if _POSTING.match(ln))
        out[first_posting:first_posting] = new
    if "category" in changes or "categorized" in changes:
        out = [ln for ln in out if not _CAT_LINE.match(ln)]
        if changes.get("categorized"):
            first_posting = next(i for i, ln in enumerate(out) if _POSTING.match(ln))
            out.insert(first_posting, f'  categorized: "{changes["categorized"]}"')
    return out


def apply(root: Path, edits: dict[str, dict], message: str | None = None) -> int:
    """Apply `{ref: changes}` in one commit. All of it or none: if the ledger would no longer
    load cleanly, every file is put back. Returns the number of transactions changed."""
    from fin import config, ledger

    if not edits:
        return 0
    with gitops.write_lock(root):
        led = ledger.load(config.main_ledger(root))
        opened = {e.account for e in led.entries if isinstance(e, data.Open)}
        where: dict[str, tuple[str, int]] = {}
        for e in led.entries:
            ref = (e.meta or {}).get("ref") if isinstance(e, data.Transaction) else None
            if ref in edits:
                where[ref] = (e.meta["filename"], e.meta["lineno"])
        for ref, changes in edits.items():
            _validate(changes, opened)
            if ref not in where:
                raise KeyError(ref)
        by_file: dict[str, list[tuple[int, str]]] = {}
        for ref, (path, lineno) in where.items():
            by_file.setdefault(path, []).append((lineno - 1, ref))
        before = {p: Path(p).read_text() for p in by_file}
        try:
            for path, items in by_file.items():
                lines = before[path].split("\n")
                for start, ref in sorted(items, reverse=True):
                    end = _block_end(lines, start)
                    lines[start:end] = _rewrite(lines[start:end], edits[ref], ref)
                Path(path).write_text("\n".join(lines))
            errors = ledger.load(config.main_ledger(root)).errors
            if errors:
                raise ValueError(
                    "ledger would have errors: " + "; ".join(ledger.format_errors(errors)[:3])
                )
        except BaseException:
            for path, text in before.items():
                Path(path).write_text(text)
            raise
        n = len(edits)
        gitops.commit_all(root, message or f"labels: {n} transaction{'s' * (n != 1)}")
        return n


def add_meta(root: Path, additions: dict[str, dict[str, str]], message: str) -> int:
    """Add text meta lines to transactions by ref, keeping what is there (a key already present is
    left alone), in one commit. All of it or none. Returns the number of transactions changed."""
    from fin import config, ledger

    with gitops.write_lock(root):
        led = ledger.load(config.main_ledger(root))
        where: dict[str, tuple[str, int]] = {}
        for e in led.entries:
            ref = (e.meta or {}).get("ref") if isinstance(e, data.Transaction) else None
            if additions.get(ref):
                where[ref] = (e.meta["filename"], e.meta["lineno"])
        missing = [r for r, add in additions.items() if add and r not in where]
        if missing:
            raise KeyError(f"no transaction for {', '.join(sorted(missing)[:3])}")
        by_file: dict[str, list[tuple[int, str]]] = {}
        for ref, (path, lineno) in where.items():
            by_file.setdefault(path, []).append((lineno - 1, ref))
        before = {p: Path(p).read_text() for p in by_file}
        changed = 0
        try:
            for path, items in by_file.items():
                lines = before[path].split("\n")
                for start, ref in sorted(items, reverse=True):
                    end = _block_end(lines, start)
                    block = lines[start:end]
                    have = {m.group(1) for ln in block if (m := re.match(r"^  ([a-z_]+):", ln))}
                    spot = next(i for i, ln in enumerate(block) if _POSTING.match(ln))
                    new = [f'  {k}: "{v}"' for k, v in additions[ref].items() if k not in have]
                    if new:
                        lines[start:end] = [*block[:spot], *new, *block[spot:]]
                        changed += 1
                Path(path).write_text("\n".join(lines))
            errors = ledger.load(config.main_ledger(root)).errors
            if errors:
                raise ValueError(
                    "ledger would have errors: " + "; ".join(ledger.format_errors(errors)[:3])
                )
        except BaseException:
            for path, text in before.items():
                Path(path).write_text(text)
            raise
        if changed:
            gitops.commit_all(root, message)
        return changed


# ---- rules: remember a payee's labels so later imports get them -------------------------------

RULES_FILE = "ledger/manual/rules.beancount"
UNCATEGORIZED = "Expenses:Uncategorized"
INCOME_UNCATEGORIZED = "Income:Uncategorized"


def rule_key(payee_norm: str, merchant_type: str | None = None) -> str:
    return f"{payee_norm}|{merchant_type}" if merchant_type else payee_norm


def rule_for(rules: dict[str, dict], payee_norm: str, merchant_type: str | None = None):
    """The most specific rule: payee + merchant type first, then the payee alone."""
    return (merchant_type and rules.get(rule_key(payee_norm, merchant_type))) or rules.get(
        payee_norm
    )


def read_rules(led) -> dict[str, dict]:
    """`{rule key: labels}` from `custom "fin-rule"` directives; the latest per key wins and a rule
    with no labels forgets it. The key is the normalized payee, or `payee|merchant type` for a rule
    that only applies to that merchant type (a shop like Digitec sells everything). Rules look like:

        2026-10-18 custom "fin-rule" "twint p p"
          category: "Expenses:Food:Dining"
          cadence: "regular"

        2026-10-18 custom "fin-rule" "digitec" "Book Stores"
          category: "Expenses:Shopping:Media"
    """
    rules: dict[str, dict] = {}
    customs = [e for e in led.entries if isinstance(e, data.Custom) and e.type == "fin-rule"]
    for e in sorted(customs, key=lambda e: e.date):
        key = rule_key(*(str(v.value) for v in e.values[:2]))
        found = {k: e.meta[k] for k in ("category", *META_KEYS) if k in (e.meta or {})}
        if found:
            rules[key] = found
        else:
            rules.pop(key, None)
    return rules


def rule_examples(led, limit: int = 80) -> list[tuple[str, str]]:
    """Recent `(rule key, category)` choices a person made (not the model's own rules), newest
    first: what the model is shown so its suggestions follow your habits."""
    customs = [e for e in led.entries if isinstance(e, data.Custom) and e.type == "fin-rule"]
    latest: dict[str, tuple] = {}
    for e in sorted(customs, key=lambda e: e.date):
        key = rule_key(*(str(v.value) for v in e.values[:2]))
        meta = e.meta or {}
        if "category" in meta and meta.get("source") != "model":
            latest[key] = (e.date, meta["category"])
        else:
            latest.pop(key, None)
    ranked = sorted(latest.items(), key=lambda kv: kv[1][0], reverse=True)
    return [(k, v[1]) for k, v in ranked[:limit]]


def record_rule(
    root: Path,
    payee_norm: str,
    found: dict,
    day=None,
    merchant_type: str | None = None,
    source: str | None = None,
) -> None:
    """Append a rule (or, with no labels, forget it) and commit it. `payee_norm` may be a rule key
    (`payee|merchant type`). `source` ("model") marks rules a person has not confirmed."""
    from datetime import date

    from fin import config, ledger
    from fin.ledger_settings import _append_directive

    if "|" in payee_norm:
        payee_norm, _, merchant_type = payee_norm.partition("|")
    if not re.fullmatch(r"[a-z ]+", payee_norm):
        raise ValueError(f"not a normalized payee: {payee_norm!r}")
    if merchant_type and re.search(r'["\\\n]', merchant_type):
        raise ValueError("merchant type cannot contain quotes or line breaks")
    found = {k: v for k, v in found.items() if v is not None}
    opened = {
        e.account for e in ledger.load(config.main_ledger(root)).entries if isinstance(e, data.Open)
    }
    _validate(found, opened)
    head = f'{day or date.today()} custom "fin-rule" "{payee_norm}"'
    if merchant_type:
        head += f' "{merchant_type}"'
    lines = [head]
    for k, v in found.items():
        lines.append(f"  {k}: " + (("TRUE" if v else "FALSE") if k == "major" else f'"{v}"'))
    if source and found:
        lines.append(f'  source: "{source}"')
    note = "forget" if not found else "remember"
    _append_directive(
        root,
        RULES_FILE,
        "\n".join(lines) + "\n",
        f"rule: {note} {rule_key(payee_norm, merchant_type)}",
    )


def apply_rules(root: Path) -> int:
    """Label transactions the rules cover and nothing has labelled yet; returns how many changed.
    A rule with a category only fills in `Expenses:Uncategorized`; the other labels go on any
    payment of that payee that does not already carry them. Importers call this after writing."""
    from fin import config, ledger
    from fin.analytics.recurring import normalize_payee

    led = ledger.load(config.main_ledger(root))
    rules = read_rules(led)
    edits: dict[str, dict] = {}
    for e in led.entries:
        if not isinstance(e, data.Transaction) or not (e.meta or {}).get("ref"):
            continue
        rule = rule_for(
            rules, normalize_payee(e.payee or ""), (e.meta or {}).get("merchant_category")
        )
        # a rule for an income category fills `Income:Uncategorized` (salary), any other the expense
        income = str(rule.get("category", "")).startswith("Income:") if rule else False
        spent = [
            p for p in e.postings if p.account.startswith("Income:" if income else "Expenses:")
        ]
        if not rule or len(spent) != 1:
            continue
        todo = {k: v for k, v in rule.items() if k in META_KEYS and k not in (e.meta or {})}
        if "category" in rule and spent[0].account == (
            INCOME_UNCATEGORIZED if income else UNCATEGORIZED
        ):
            todo["category"] = rule["category"]
            todo["categorized"] = "rule"
        elif "category" in rule:
            todo.pop("category", None)
        if todo:
            edits[e.meta["ref"]] = todo
    return apply(root, edits, f"rules: label {len(edits)} transaction(s)") if edits else 0
