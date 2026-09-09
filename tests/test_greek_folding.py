"""Greek accent folding: the index and the query must agree.

Most of this corpus is Greek, and FTS5's unicode61 case-folds but does not strip
the tonos. `remove_diacritics 2` does not either, measured on SQLite 3.53.4. So
the index held "παρουσίαση" and a search for "παρουσιαση" found almost nothing.
Measured on the live corpus before schema v20, against a LIKE baseline:

    παρουσίαση  4577 accented   137 unaccented    (3% of true)
    ψηφιακή      827 accented     9 unaccented    (1% of true)

Greeks routinely type without accents. This was a large silent loss.
"""

import sqlite3

from src.config import CURRENT_SCHEMA_VERSION
from src.store.greek import FOLD_PAIRS, fold, fold_sql_expr
from src.store.query import _sanitize_fts5_query
from src.store.schema import create_database, get_connection


class TestFoldAgreement:
    def test_python_and_sql_agree_on_every_pair(self):
        """Two implementations of one rule is how an index and its queries drift
        apart, so this asserts they cannot.
        """
        conn = sqlite3.connect(":memory:")
        for accented, _bare in FOLD_PAIRS:
            sample = f"προ{accented}μετα"
            sql = conn.execute(f"SELECT {fold_sql_expr('?')}", (sample,)).fetchone()[0]
            assert sql == fold(sample), f"disagreement on {accented!r}"
        conn.close()

    def test_agrees_on_realistic_mixed_text(self):
        conn = sqlite3.connect(":memory:")
        for s in [
            "Η έγκριση της παρουσίασης για τις κάρτες",
            # Single word on purpose: two consecutive long ALL-CAPS Greek words
            # are name-shaped, and the gauntlet rightly flags them.
            "ΨΗΦΙΑΚΗ",
            "mixed English and ελληνικά with ΆΈΉΊΌΎΏ",
            "no greek at all",
            "",
        ]:
            sql = conn.execute(f"SELECT {fold_sql_expr('?')}", (s,)).fetchone()[0]
            assert sql == fold(s), f"disagreement on {s!r}"
        conn.close()

    def test_leaves_non_greek_alone(self):
        assert fold("café naïve") == "café naïve"
        assert fold("plain ascii") == "plain ascii"

    def test_is_length_preserving(self):
        """1:1 per character is what keeps snippet() offsets aligned."""
        for accented, _ in FOLD_PAIRS:
            assert len(fold(f"x{accented}y")) == 3


class TestQuerySideFolding:
    def test_the_sanitizer_folds(self):
        assert _sanitize_fts5_query("παρουσίαση") == '"παρουσιαση"'

    def test_an_unaccented_query_is_unchanged(self):
        assert _sanitize_fts5_query("παρουσιαση") == '"παρουσιαση"'

    def test_accented_and_unaccented_produce_the_same_match_expression(self):
        assert _sanitize_fts5_query("κάρτες") == _sanitize_fts5_query("καρτες")


class TestIndexSideFolding:
    def _db(self, tmp_path):
        path = tmp_path / "b.db"
        create_database(str(path)).close()
        conn = get_connection(str(path))
        conn.execute(
            "INSERT INTO emails (message_id, date_received, subject, summary, content) "
            "VALUES (1, '2026-09-01T00:00:00Z', 'θέμα', 'η παρουσίαση των πελατών', "
            "'κάρτες και έγκριση')"
        )
        conn.commit()
        return conn

    def test_a_fresh_database_is_at_v20(self, tmp_path):
        conn = create_database(str(tmp_path / "b.db"))
        from src.store.schema import get_schema_version

        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION >= 20
        conn.close()

    def test_unaccented_accented_and_uppercase_all_match(self, tmp_path):
        conn = self._db(tmp_path)
        for q in ["παρουσίαση", "παρουσιαση", "ΠΑΡΟΥΣΙΑΣΗ", "Παρουσίαση"]:
            n = conn.execute(
                "SELECT count(*) FROM emails_fts WHERE emails_fts MATCH ?",
                (_sanitize_fts5_query(q),),
            ).fetchone()[0]
            assert n == 1, f"{q!r} matched {n}"
        conn.close()

    def test_the_stored_text_keeps_its_accents(self, tmp_path):
        """Only the INDEX is folded. Display text must be untouched."""
        conn = self._db(tmp_path)
        assert (
            conn.execute("SELECT summary FROM emails").fetchone()[0] == "η παρουσίαση των πελατών"
        )
        conn.close()

    def test_the_folded_columns_cost_no_storage(self, tmp_path):
        """VIRTUAL, not STORED: emails.content is 1.1 GB on the live corpus, so a
        materialised duplicate would have grown the database by about a third on
        every replica and in every offsite snapshot.
        """
        conn = self._db(tmp_path)
        rows = {r[1]: r[6] for r in conn.execute("SELECT * FROM pragma_table_xinfo('emails')")}
        assert rows["summary_f"] == 2, "summary_f should be VIRTUAL (hidden=2)"
        assert rows["content_f"] == 2, "content_f should be VIRTUAL (hidden=2)"
        conn.close()

    def test_a_rebuild_does_not_undo_the_folding(self, tmp_path):
        """The reason this uses generated columns rather than folding in the
        triggers: an external-content 'rebuild' reads the content table directly
        and bypasses triggers entirely.
        """
        conn = self._db(tmp_path)
        conn.execute("INSERT INTO emails_fts(emails_fts) VALUES('rebuild')")
        n = conn.execute(
            "SELECT count(*) FROM emails_fts WHERE emails_fts MATCH ?",
            (_sanitize_fts5_query("παρουσιαση"),),
        ).fetchone()[0]
        assert n == 1
        conn.close()

    def test_inserts_and_deletes_keep_the_index_in_step(self, tmp_path):
        conn = self._db(tmp_path)
        conn.execute(
            "INSERT INTO emails (message_id, date_received, summary) "
            "VALUES (2, '2026-09-02T00:00:00Z', 'δοκιμή εγγραφής')"
        )
        conn.commit()
        q = _sanitize_fts5_query("δοκιμη")
        assert (
            conn.execute(
                "SELECT count(*) FROM emails_fts WHERE emails_fts MATCH ?", (q,)
            ).fetchone()[0]
            == 1
        )
        conn.execute("DELETE FROM emails WHERE message_id = 2")
        conn.commit()
        assert (
            conn.execute(
                "SELECT count(*) FROM emails_fts WHERE emails_fts MATCH ?", (q,)
            ).fetchone()[0]
            == 0
        )
        conn.close()

    def test_every_fts_table_got_folded_columns(self, tmp_path):
        from src.store.schema import _FOLDED_FTS

        conn = create_database(str(tmp_path / "b.db"))
        for _fts, table, columns in _FOLDED_FTS:
            names = {r[1] for r in conn.execute(f"PRAGMA table_xinfo({table})")}
            for col in columns:
                assert f"{col}_f" in names, f"{table}.{col}_f missing"
        conn.close()


class TestMigrationIsRerunnable:
    """run_migrations is called on connection, so it runs constantly. It must be
    a no-op the second time.

    The first version of this guard read PRAGMA table_info, which omits hidden
    columns, and a VIRTUAL generated column is hidden. So it could not see the
    columns it had just added and the second run raised "duplicate column name".
    """

    def test_running_it_twice_is_a_no_op(self, tmp_path):
        from src.store.schema import get_schema_version, run_migrations

        conn = create_database(str(tmp_path / "b.db"))
        conn.execute(
            "INSERT INTO emails (message_id, date_received, summary) "
            "VALUES (1, '2026-09-01T00:00:00Z', 'η παρουσίαση')"
        )
        conn.commit()
        run_migrations(conn)
        run_migrations(conn)
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        assert (
            conn.execute(
                "SELECT count(*) FROM emails_fts WHERE emails_fts MATCH ?",
                (_sanitize_fts5_query("παρουσιαση"),),
            ).fetchone()[0]
            == 1
        )
        conn.close()
