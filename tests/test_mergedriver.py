from fin.mergedriver import merge


def txn(ref, day, amount="1.00"):
    return f'2026-01-{day:02d} * "P" "n"\n  ref: "{ref}"\n  Expenses:Food  {amount} CHF\n  Assets:Bank\n'


def j(*blocks):
    return "\n".join(blocks)


def test_union_of_independent_additions_in_date_order():
    base = txn("x:1", 1)
    merged, conflict = merge(base, j(base, txn("x:3", 3)), j(base, txn("x:2", 2)))
    assert not conflict
    assert [line for line in merged.splitlines() if "ref:" in line] == [
        '  ref: "x:1"',
        '  ref: "x:2"',
        '  ref: "x:3"',
    ]


def test_same_change_on_both_sides_is_not_duplicated():
    base = txn("x:1", 1)
    both = j(base, txn("x:2", 2))
    merged, conflict = merge(base, both, both)
    assert not conflict and merged.count('ref: "x:2"') == 1


def test_one_side_edit_wins_when_other_untouched():
    base = j(txn("x:1", 1), txn("x:2", 2))
    edited = base.replace("1.00 CHF", "9.00 CHF", 1)
    merged, conflict = merge(base, edited, base)
    assert not conflict and "9.00 CHF" in merged


def test_deletion_on_one_side_sticks():
    base = j(txn("x:1", 1), txn("x:2", 2))
    merged, conflict = merge(base, txn("x:1", 1), base)
    assert not conflict and 'ref: "x:2"' not in merged


def test_both_sides_edit_same_ref_differently_conflicts():
    base = txn("x:1", 1)
    merged, conflict = merge(base, base.replace("1.00", "8.00"), base.replace("1.00", "9.00"))
    assert conflict and "<<<<<<<" in merged and "8.00" in merged and "9.00" in merged


def test_blocks_without_ref_are_kept_once():
    base = "; header comment\n\n" + txn("x:1", 1)
    merged, conflict = merge(base, base + "\n2026-01-05 balance Assets:Bank 1 CHF\n", base)
    assert not conflict and merged.count("header comment") == 1 and "balance Assets:Bank" in merged


def test_add_add_with_empty_base():
    merged, conflict = merge("", txn("a:1", 1), txn("b:1", 2))
    assert not conflict and 'ref: "a:1"' in merged and 'ref: "b:1"' in merged
