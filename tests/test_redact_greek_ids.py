"""Greek tax ids (AFM, 9 digits) and social security numbers (AMKA, 11 digits) are
masked when their label, in Greek or Latin letters, stands next to them.

Security audit of 2026-11-01: redact.py masked cards, IBANs and passwords but no
tax number, and an AFM or AMKA copied into a key fact is indexed and replicated like
any other text. A bare run of 9 or 11 digits is an amount, a phone number or an id
as often as not, so only a number with its label is taken. Every number here is
invented: the 11-digit one is the 9-digit placeholder with two more digits.
"""

import pytest

from src.redact import redact_payload, redact_secrets

AFM = "[REDACTED:afm]"
AMKA = "[REDACTED:amka]"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ΑΦΜ: 123456789", f"ΑΦΜ: {AFM}"),
        ("ΑΦΜ 123456789", f"ΑΦΜ {AFM}"),
        ("Α.Φ.Μ. 123456789", f"Α.Φ.Μ. {AFM}"),
        ("Α.Φ.Μ.: 123456789", f"Α.Φ.Μ.: {AFM}"),
        ("Α. Φ. Μ. 123456789", f"Α. Φ. Μ. {AFM}"),
        ("αφμ:123456789", f"αφμ:{AFM}"),
        ("Αφμ = 123456789", f"Αφμ = {AFM}"),
        ("AFM: 123456789", f"AFM: {AFM}"),
        ("afm 123456789", f"afm {AFM}"),
        ("A.F.M. 123456789", f"A.F.M. {AFM}"),
        ("ΑΦΜ123456789", f"ΑΦΜ{AFM}"),
        ("Ο ΑΦΜ: 123456789, ΔΟΥ Α' Αθηνών.", f"Ο ΑΦΜ: {AFM}, ΔΟΥ Α' Αθηνών."),
    ],
)
def test_a_tax_id_beside_its_label_is_masked(text, expected):
    assert redact_secrets(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ΑΜΚΑ: 12345678901", f"ΑΜΚΑ: {AMKA}"),
        ("ΑΜΚΑ 12345678901", f"ΑΜΚΑ {AMKA}"),
        ("Α.Μ.Κ.Α. 12345678901", f"Α.Μ.Κ.Α. {AMKA}"),
        ("άμκα: 12345678901", f"άμκα: {AMKA}"),
        ("AMKA 12345678901", f"AMKA {AMKA}"),
        ("amka: 12345678901", f"amka: {AMKA}"),
        ("A.M.K.A.: 12345678901", f"A.M.K.A.: {AMKA}"),
    ],
)
def test_a_social_security_number_beside_its_label_is_masked(text, expected):
    assert redact_secrets(text) == expected


def test_both_in_one_line_and_in_a_payload():
    text = "ΑΦΜ: 987654321, ΑΜΚΑ: 12345678901"
    masked = text.replace("987654321", AFM).replace("12345678901", AMKA)
    assert redact_secrets(text) == masked
    assert redact_payload({"key_facts": [text]}) == {"key_facts": [masked]}


@pytest.mark.parametrize(
    "text",
    [
        # Bare numbers: amounts, phone numbers, references.
        "Το ποσό ήταν 123456789 ευρώ.",
        "Reference 12345678901 for the order.",
        "Call 2101234567 or 6912345678.",
        "invoice 123456789 paid",
        # A label with the wrong number of digits is not one of these ids.
        "ΑΦΜ 1234567890",
        "AFM 12345678",
        "ΑΜΚΑ 123456789",
        "AMKA 123456789012",
        # The letters inside a longer word are not the label.
        "SAFM 123456789",
        "ΚΑΦΜΑ 123456789",
        "AMKAS 12345678901",
        # A label with words between it and a number, and one with no number at all.
        "Ο ΑΦΜ λείπει από το τιμολόγιο 987654321.",
        "Please send your AMKA by Friday.",
    ],
)
def test_bare_numbers_and_labels_alone_are_left_alone(text):
    assert redact_secrets(text) == text
