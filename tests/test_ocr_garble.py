"""A text is Greek read as Latin look-alike letters when few of its letters are Greek and Greek
function words appear in their Latin spellings ("kal" for «και», "tnv" for «την»).

Every string here is composed for the test (tests/greek_text.py).
"""

import pytest

from src.extract.ocr_garble import (
    GREEK_SHARE_MAX,
    OCR_GARBLED,
    is_garbled,
    looks_like_garbled_greek,
    mark_garbled,
    script_profile,
)
from tests.greek_text import ENGLISH, GREEK, garble


def test_the_substitution_leaves_no_greek_letter():
    assert script_profile(garble(GREEK)).greek == 0


def test_greek_read_as_latin_look_alikes_is_garbled():
    assert looks_like_garbled_greek(garble(GREEK))


def test_greek_in_capitals_read_as_latin_look_alikes_is_garbled():
    caps = "ΑΙΤΗΣΗ ΚΑΙ ΔΗΛΩΣΗ ΓΙΑ ΤΗΝ ΕΓΚΡΙΣΗ ΤΟΥ ΠΡΟΓΡΑΜΜΑΤΟΣ ΚΑΙ ΤΩΝ ΟΡΩΝ ΤΟΥ ΔΑΝΕΙΟΥ"
    assert looks_like_garbled_greek(garble(caps))


@pytest.mark.parametrize(
    "text",
    [
        GREEK,
        ENGLISH,
        # Greek with the English terms the owner's mail keeps in English.
        "Η ομάδα του project θα παρουσιάσει το budget στο Steering Committee και το go-live "
        "είναι για τον Μάρτιο, με UAT από την Τρίτη.",
        "",
        None,
        "1.204 | 33,1% | 2,9% | 12/05/2026",
    ],
)
def test_clean_text_is_not_garbled(text):
    assert not looks_like_garbled_greek(text)


def test_half_read_greek_is_not_garbled_while_its_greek_share_is_high():
    half = GREEK + " " + garble(GREEK[:80])
    assert script_profile(half).greek_share > GREEK_SHARE_MAX
    assert not looks_like_garbled_greek(half)


def test_one_look_alike_word_repeated_is_not_enough():
    """A name such as Kai, three times in English text, is one word, not Greek."""
    assert not looks_like_garbled_greek("Kai met the team. Kai read the plan, and Kai agreed. " * 2)


def test_a_few_chance_matches_in_a_long_latin_text_are_not_enough():
    long_text = (ENGLISH + " ") * 40 + "tou oti kal"
    assert not looks_like_garbled_greek(long_text)


def test_accented_latin_look_alikes_still_count():
    """An OCR that reads «είναι» as "eívai" keeps the accent on a Latin letter."""
    accented = "eívai kal tou tnv twv yia " + "plan " * 10
    assert looks_like_garbled_greek(accented)


def test_the_profile_counts_greek_and_latin_letters_only():
    profile = script_profile("Άλφα ἀρχή µε 12 Latin é!")
    # Ά λ φ α, ἀ ρ χ ή, µ ε (the micro sign tesseract gives for «μ» counts as Greek)
    assert (profile.greek, profile.latin) == (10, 6)
    assert profile.letters == 16
    assert profile.greek_share == pytest.approx(10 / 16)
    assert profile.latin_share == pytest.approx(6 / 16)


def test_a_text_with_no_letters_has_no_share():
    profile = script_profile("12/05 - 3,5% (+2,4)")
    assert (profile.letters, profile.greek_share, profile.latin_share) == (0, 0.0, 0.0)


def test_the_mark_is_added_once_and_keeps_what_the_error_said():
    once = mark_garbled("time budget spent, 2 pages left unread")
    assert once.startswith("time budget spent, 2 pages left unread; ")
    assert is_garbled(once) and OCR_GARBLED in once
    assert mark_garbled(once) == once
    assert is_garbled(mark_garbled(None))
    assert not is_garbled(None) and not is_garbled("OCR returned insufficient text")
