"""Git merge driver for ledger files: union of transactions by `ref`.

Imports on two machines append different transactions to the same month file; a line-based
merge would conflict. This merges at block level (blocks are separated by blank lines):
a block is identified by its `ref` meta, or by its text when it has none. Standard three-way
rules per block; a block both sides changed differently is a real conflict.

Usage (git calls it): python -m fin.mergedriver %O %A %B   (result is written to %A)
"""

import hashlib
import re
import sys
from pathlib import Path

REF = re.compile(r'^\s+ref:\s*"([^"]+)"', re.MULTILINE)
DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})\b")
SPLIT = re.compile(r"\n[ \t]*\n+")


def blocks(text: str) -> dict[str, str]:
    """Ordered mapping key -> block text."""
    out: dict[str, str] = {}
    for raw in SPLIT.split(text.strip("\n")):
        block = raw.strip("\n")
        if not block.strip():
            continue
        m = REF.search(block)
        key = f"ref:{m.group(1)}" if m else "text:" + hashlib.sha1(block.encode()).hexdigest()
        out[key] = block
    return out


def _sort_key(blocks_in_order: list[str]) -> list[str]:
    """Chronological order; undated blocks (comments) stay after the block they followed."""
    keyed, last = [], "0000-00-00"
    for i, b in enumerate(blocks_in_order):
        m = DATE.match(b)
        last = m.group(1) if m else last
        keyed.append((last, i, b))
    return [b for _, _, b in sorted(keyed)]


def merge(base: str, ours: str, theirs: str) -> tuple[str, bool]:
    """Returns (merged text, has_conflict)."""
    b, o, t = blocks(base), blocks(ours), blocks(theirs)
    order = list(o) + [k for k in t if k not in o] + [k for k in b if k not in o and k not in t]
    result, conflict = [], False
    for key in order:
        bb, ob, tb = b.get(key), o.get(key), t.get(key)
        if ob == tb:
            pick = ob
        elif ob == bb:
            pick = tb
        elif tb == bb:
            pick = ob
        else:
            conflict = True
            pick = f"<<<<<<< ours\n{ob or ''}\n=======\n{tb or ''}\n>>>>>>> theirs"
        if pick:
            result.append(pick)
    ordered = result if conflict else _sort_key(result)
    return ("\n\n".join(ordered) + "\n" if ordered else ""), conflict


def main(argv: list[str]) -> int:
    base_p, ours_p, theirs_p = (Path(a) for a in argv[:3])
    merged, conflict = merge(base_p.read_text(), ours_p.read_text(), theirs_p.read_text())
    ours_p.write_text(merged)
    return 1 if conflict else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
