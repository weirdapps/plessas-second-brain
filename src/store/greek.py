"""Greek accent folding, defined once for both Python and SQL.

Most of this corpus is Greek, and FTS5's unicode61 tokenizer case-folds but does
NOT strip the Greek tonos. `remove_diacritics 2` does not help either: measured on
SQLite 3.53.4, it leaves the precomposed Greek vowels alone. So the index held
"παρουσίαση" and a search for "παρουσιαση" found almost nothing. Measured against
a LIKE baseline on the live corpus before this landed:

    παρουσίαση  4577 accented   137 unaccented    (3%)
    πελατών     6675 accented   106 unaccented    (2%)
    κάρτες      3550 accented   277 unaccented    (8%)
    ψηφιακή      827 accented     9 unaccented    (1%)

Greeks routinely type without accents, and an agent reformulating a query drops
them freely, so this was a large and silent loss.

The fix folds BOTH sides: the index stores folded text and the query is folded
before it is matched. Accented queries keep working, because folding an accented
query yields the same folded form.

The pairs below are the single source of truth. `fold()` is the Python side, used
on the query string; `fold_sql_expr()` generates the equivalent nested replace()
chain used by the generated columns the FTS indexes. A test asserts the two agree
on real corpus text, because two implementations of one rule is exactly how an
index and its queries drift apart.

Folding is 1:1 per character, which is what makes it safe here: snippet() offsets
computed against the folded text still line up with the original.
"""

# Precomposed Greek vowels carrying tonos or dialytika, mapped to their bare form.
# Uppercase entries are included because Greek all-caps legitimately keeps the
# dialytika even though it drops the tonos, and because text arrives in both.
FOLD_PAIRS: tuple[tuple[str, str], ...] = (
    ("ά", "α"),
    ("έ", "ε"),
    ("ή", "η"),
    ("ί", "ι"),
    ("ό", "ο"),
    ("ύ", "υ"),
    ("ώ", "ω"),
    ("ϊ", "ι"),
    ("ϋ", "υ"),
    ("ΐ", "ι"),
    ("ΰ", "υ"),
    ("Ά", "Α"),
    ("Έ", "Ε"),
    ("Ή", "Η"),
    ("Ί", "Ι"),
    ("Ό", "Ο"),
    ("Ύ", "Υ"),
    ("Ώ", "Ω"),
    ("Ϊ", "Ι"),
    ("Ϋ", "Υ"),
)


def fold(text: str) -> str:
    """Strip Greek tonos and dialytika. Non-Greek text passes through unchanged."""
    if not text:
        return text
    for accented, bare in FOLD_PAIRS:
        text = text.replace(accented, bare)
    return text


def fold_sql_expr(column: str) -> str:
    """A nested replace() chain equivalent to fold(), for use in SQL.

    Pure SQL on purpose. These expressions live in GENERATED columns that SQLite
    evaluates on its own, including during an FTS5 'rebuild', so they cannot
    depend on a Python function being registered by whichever process happens to
    open the database.
    """
    expr = column
    for accented, bare in FOLD_PAIRS:
        expr = f"replace({expr},'{accented}','{bare}')"
    return expr
