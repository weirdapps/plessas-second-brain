"""Tests for Greek->Latin transliteration + name canonicalization."""

import pytest

from src.store.transliterate import canonical_name, name_keys, spelling_key, transliterate_greek


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


class TestSpellingKey:
    """One key for a name however it is spelled: the Greek and the romanisations
    people type for it. Every surname below is invented."""

    @pytest.mark.parametrize(
        ("greek", "latin"),
        [
            ("Μπροστοπούλου", "Brostopoulou"),  # μπ: B
            ("Μπροστοπούλου", "Mprostopoulou"),  # μπ: Mp
            ("Μπροστοπούλου", "Brostopulu"),  # ου: U
            ("Κυριαζίδη", "Kiriazidi"),  # υ: I
            ("Κυριαζίδη", "Kyriazidi"),  # υ: Y
            ("Ηλιοπούλου", "Iliopoulou"),  # η: I
            ("Θεοχαρίδη", "Theocharidi"),  # θ: Th, χ: Ch
            ("Θεοχαρίδη", "Theoharidi"),  # χ: H
            ("Χαρβαλίδης", "Kharvalidis"),  # χ: Kh
            ("Φωτοπούλου", "Fotopoulou"),  # φ: F
            ("Φωτοπούλου", "Photopoulou"),  # φ: Ph
            ("Ψαρολίδη", "Psarolidi"),  # ψ: Ps
            ("Ξυπνητίδης", "Xypnitidis"),  # ξ: X
            ("Ξυπνητίδης", "Ksipnitidis"),  # ξ: Ks
            ("ΞΥΠΝΗΤΙΔΗΣ", "xypnitidis"),  # capitals and the final sigma
            ("Ξυπνητιδης", "Ξυπνητίδης"),  # accents
        ],
    )
    def test_a_greek_name_and_its_romanisations_share_one_key(self, greek, latin):
        assert spelling_key(greek) == spelling_key(latin)

    def test_different_names_keep_different_keys(self):
        assert spelling_key("Μπροστοπούλου") != spelling_key("Μπροστοπούλος")
        assert spelling_key("Δημήτρα") != spelling_key("Δημήτριος")

    def test_a_word_of_two_letters_is_left_whole(self):
        # 'AI' must stay 'ai': a collapsed 'e' would equal an initial.
        assert spelling_key("AI") == "ai"
        assert spelling_key("Ou") == "ou"

    def test_name_keys_drop_titles_and_punctuation(self):
        assert name_keys("κα Μπροστοπούλου") == [spelling_key("Brostopoulou")]
        assert name_keys("Mrs. E. Brostopoulou (Ευανθία)") == ["e", "brostopulu", "evanthia"]
        assert name_keys("") == []
