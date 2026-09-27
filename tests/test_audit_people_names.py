"""A person's name is never replaced by a garbled one, nor by whatever is longer.

Seven people rows on the replica, the owner's and three direct reports' among
them, carried names such as 'Ķ°ĶŦĶĨ...'. That is Greek written out as GBK and
read back as ISO-8859-10, and it is twice as long as the real name, so the old
'the longer name wins' rule in find_or_create_person kept it for good and every
name lookup missed the real record (schema-load-1, mcp-2). All names below are
synthetic.
"""

from src.store.dedup_people import pick_best_name
from src.store.normalizer import find_or_create_person, looks_garbled, recover_garbled_greek
from src.store.schema import create_database

GREEK = "ΠΑΠΑΔΟΠΟΥΛΟΥ ΜΑΡΙΝΑ"  # the gauntlet's synthetic placeholder
# The corruption itself, reproduced: the replica's pattern, one 'Ķ' (byte A6,
# GBK's Greek row) before every letter.
GARBLED = GREEK.encode("gbk").decode("iso8859_10")


def _name(conn, person_id):
    return conn.execute("SELECT name FROM people WHERE id = ?", (person_id,)).fetchone()[0]


class TestGarbledNames:
    def test_the_corruption_matches_the_replicas_pattern(self):
        assert GARBLED.count("Ķ") == len(GREEK.replace(" ", ""))
        letters = [ch for ch in GARBLED if ch.isalpha()]
        assert all("\u0100" <= ch <= "\u017f" for ch in letters)
        assert len(GARBLED) > len(GREEK)

    def test_a_garbled_name_is_recovered_to_its_greek(self):
        assert recover_garbled_greek(GARBLED) == GREEK
        assert looks_garbled(GARBLED)

    def test_lower_case_greek_is_recovered_too(self):
        lower = "παπαδοπουλου μαρινα"
        assert recover_garbled_greek(lower.encode("gbk").decode("iso8859_10")) == lower

    def test_a_truncated_garbled_name_still_looks_garbled(self):
        # Cut inside a letter's pair it no longer decodes, but it is still no name.
        cut = GARBLED[:-1]
        assert recover_garbled_greek(cut) is None
        assert looks_garbled(cut)

    def test_real_names_are_not_garbled(self):
        for name in (
            GREEK,
            "Παπαδόπουλος Νίκος",
            "John Doe",
            "Jiří Novák",
            "Łukasz Żółć",
            "Ķīlis Jānis",
            "john.doe@example.com",
            "",
        ):
            assert recover_garbled_greek(name) is None, name
            assert not looks_garbled(name), name


class TestFindOrCreatePersonNames:
    def test_a_garbled_display_name_never_renames(self):
        conn = create_database(":memory:")
        pid = find_or_create_person(conn, "np@example.com", "np@example.com")

        # Truncated, so there is no Greek to recover from it.
        find_or_create_person(conn, GARBLED[:-1], "np@example.com", display_name=True)

        assert _name(conn, pid) == "np@example.com"

    def test_a_garbled_name_is_stored_as_its_greek(self):
        conn = create_database(":memory:")
        pid = find_or_create_person(conn, GARBLED, "np@example.com")
        assert _name(conn, pid) == GREEK

    def test_a_longer_display_name_does_not_win(self):
        conn = create_database(":memory:")
        pid = find_or_create_person(conn, "Nikos Papadopoulos", "np@example.com")

        find_or_create_person(
            conn, "PAPADOPOULOS NIKOS (Cards Division)", "np@example.com", display_name=True
        )

        assert _name(conn, pid) == "Nikos Papadopoulos"

    def test_a_canonical_name_is_never_overwritten(self):
        conn = create_database(":memory:")
        # What import-people leaves behind: the canonical name, role and department.
        conn.execute(
            "INSERT INTO people (name, email, role, department) "
            "VALUES ('Effie Example', 'effie@example.com', 'Director', 'Cards')"
        )
        pid = conn.execute("SELECT id FROM people").fetchone()[0]

        find_or_create_person(conn, "EXAMPLE EFFIE MARIA", "effie@example.com", display_name=True)

        assert _name(conn, pid) == "Effie Example"

    def test_a_display_name_replaces_an_address_standing_in_for_one(self):
        conn = create_database(":memory:")
        pid = find_or_create_person(conn, "np@example.com", "np@example.com")

        find_or_create_person(conn, "Nikos Papadopoulos", "NP@example.com", display_name=True)

        assert _name(conn, pid) == "Nikos Papadopoulos"

    def test_a_display_name_replaces_a_garbled_name(self):
        conn = create_database(":memory:")
        conn.execute("INSERT INTO people (name, email) VALUES (?, 'np@example.com')", (GARBLED,))
        pid = conn.execute("SELECT id FROM people").fetchone()[0]

        find_or_create_person(conn, GREEK, "np@example.com", display_name=True)

        assert _name(conn, pid) == GREEK

    def test_an_extracted_name_never_renames(self):
        # people_roles names come from the model, not the headers: not even an
        # address standing in for a name is theirs to replace.
        conn = create_database(":memory:")
        pid = find_or_create_person(conn, "np@example.com", "np@example.com")

        find_or_create_person(conn, "Nikos Papadopoulos", "np@example.com")

        assert _name(conn, pid) == "np@example.com"


class TestPickBestName:
    def test_never_picks_a_garbled_name(self):
        assert pick_best_name([GREEK, GARBLED]) == GREEK
        assert pick_best_name([GARBLED, "Nikos Papadopoulos"]) == "Nikos Papadopoulos"

    def test_a_garbled_name_alone_comes_back_as_its_greek(self):
        assert pick_best_name([GARBLED]) == GREEK

    def test_still_prefers_title_case(self):
        assert pick_best_name(["NIKOS PAPADOPOULOS", "Nikos Papadopoulos"]) == (
            "Nikos Papadopoulos"
        )
