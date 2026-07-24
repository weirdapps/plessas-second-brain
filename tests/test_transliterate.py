"""Tests for Greek->Latin transliteration + name canonicalization."""

from src.store.transliterate import canonical_name, transliterate_greek


class TestTransliterateGreek:
    def test_common_names(self):
        assert transliterate_greek("Παπαδόπουλος") == "papadopoulos"
        assert transliterate_greek("Νίκος") == "nikos"

    def test_digraphs(self):
        assert transliterate_greek("Ιωάννου") == "ioannou"  # ου -> ou
        assert transliterate_greek("Μπάμπης").startswith("b")  # μπ -> b

    def test_latin_passthrough(self):
        assert transliterate_greek("Papadopoulos") == "papadopoulos"
        assert transliterate_greek("O'Brien") == "o'brien"


class TestCanonicalName:
    def test_order_independent(self):
        assert canonical_name("Παπαδόπουλος Νίκος") == canonical_name("Νίκος Παπαδόπουλος")

    def test_cross_script_equivalence(self):
        assert (
            canonical_name("Νίκος Παπαδόπουλος")
            == canonical_name("Nikos Papadopoulos")
            == "nikos papadopoulos"
        )

    def test_punctuation_and_case(self):
        assert canonical_name("Novak, Maria") == canonical_name("maria novak") == "maria novak"

    def test_drops_single_char_tokens(self):
        assert canonical_name("Maria A Novak") == "maria novak"

    def test_empty_and_symbols(self):
        assert canonical_name("") == ""
        assert canonical_name("   ") == ""
