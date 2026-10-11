"""Card numbers, IBANs and password values are masked before they reach the store.

redact.py knew credential shapes only, so an October 2026 audit found card
numbers and IBANs in plaintext in attachment text, mail bodies and the key facts
the extractor copies out of them, all full-text indexed and replicated, and
passwords lifted into key facts. Findings security-privacy-01 and -07,
code-extract-02.

Every number here is generated at run time (tests/payment_data.py).
"""

import pytest

from src.redact import redact_payload, redact_secrets
from tests.payment_data import card, grouped, iban, luhn_ok, masked, not_luhn, printed
from tests.payment_data import digits as _digits

CARD = "[REDACTED:card]"
IBAN = "[REDACTED:iban]"
PASSWORD = "[REDACTED:password]"

VISA = card("4", 16)
MASTERCARD = card("53", 16)
MASTERCARD_2 = card("2300", 16)
AMEX = card("37", 15)
DINERS = card("36", 14)
DISCOVER = card("6011", 16)


class TestCardNumbers:
    @pytest.mark.parametrize(
        "number",
        [
            VISA,
            card("4", 13),
            card("4", 19),
            MASTERCARD,
            MASTERCARD_2,
            AMEX,
            card("34", 15),
            DINERS,
            card("38", 14),
            card("302", 14),
            DISCOVER,
            card("65", 16),
            card("645", 19),
        ],
    )
    def test_each_issuer_keeps_its_first_six_and_last_four(self, number):
        assert luhn_ok(number)
        out = redact_secrets(f"card {number} on file")
        assert out == f"card {masked(number)} on file"
        assert number not in out

    @pytest.mark.parametrize("sep", [" ", "-", " ", " "])
    def test_a_number_grouped_in_fours_is_masked(self, sep):
        text = f"Κάρτα: {grouped(VISA, sep=sep)}."
        assert redact_secrets(text) == f"Κάρτα: {masked(VISA)}."

    def test_greek_prose_with_a_space_grouped_number(self):
        text = (
            f"Ο αριθμός της κάρτας είναι {grouped(MASTERCARD)}, λήξη 12/27, "
            "ποσό 1.234,56 €, τηλ. 210 123 4567."
        )
        out = redact_secrets(text)
        assert out == text.replace(grouped(MASTERCARD), masked(MASTERCARD))

    @pytest.mark.parametrize(
        ("number", "sizes"),
        [(AMEX, (4, 6, 5)), (DINERS, (4, 6, 4)), (card("4", 19), (4,) * 4 + (3,))],
    )
    def test_issuer_layouts_other_than_fours(self, number, sizes):
        assert redact_secrets(grouped(number, sizes)) == masked(number)

    def test_a_number_after_another_number_is_still_found(self):
        """A row number, a year or a branch code printed before the card."""
        for before in ("1", "07", "2025", "12345678"):
            text = f"{before} {grouped(VISA)} 12/27"
            assert redact_secrets(text) == f"{before} {masked(VISA)} 12/27", before

    def test_a_three_digit_group_after_the_card_stays_outside_the_mask(self):
        cvv = next(c for c in ("123", "456", "789") if not luhn_ok(MASTERCARD + c))
        text = f"{grouped(MASTERCARD)} {cvv}"
        assert redact_secrets(text) == f"{masked(MASTERCARD)} {cvv}"

    def test_a_list_of_numbers_is_masked_entry_by_entry(self):
        rows = [card("4", 16, seed) for seed in range(5)] + [MASTERCARD, AMEX]
        text = "\n".join(f"{i} | {n} | 12/27" for i, n in enumerate(rows, 1))
        text += f"\n{grouped(VISA)} {grouped(MASTERCARD)}"
        out = redact_secrets(text)
        for number in rows + [VISA, MASTERCARD]:
            assert number not in out.replace(" ", "")
        assert out.count(CARD) == len(rows) + 2

    def test_inside_a_payload(self):
        out = redact_payload({"emails": [{"content": f"pan {VISA}", "n": 3}]})
        assert out == {"emails": [{"content": f"pan {masked(VISA)}", "n": 3}]}


class TestNumbersThatAreNotCards:
    """A random 16-digit number passes Luhn one time in ten, so the other
    checks carry the precision: the issuer range, the issuer's length, more
    than two distinct digits, and a number standing on its own."""

    @pytest.mark.parametrize(
        "text",
        [
            not_luhn(VISA),
            grouped(not_luhn(MASTERCARD)),
            card("1", 16),  # no issuer starts with 1 (epoch timestamps do)
            card("9", 16),
            card("7", 16),
            card("2100", 16),  # below the 2-series Mastercard range
            card("53", 15),  # Mastercard is 16 long
            card("37", 16),  # Amex is 15 long
            card("4", 15),
            card("4", 20),
            "4" + _digits(25),  # inside a longer run
            "ref " + card("4", 16) + "7",
            "id" + VISA,  # part of a word or an id
            VISA + "ab",
            "0." + VISA,  # the fraction of a decimal
            "+" + card("4", 13),  # a phone number
            "ποσό 1 234 567 890 123 456 €",
            "τηλ. 210 123 4567, κινητό 69 1234 5678",
            "αριθμός πρωτοκόλλου 1234 5678 9012 3456",
            "sha256 " + "0123456789abcdef" * 4,
            "2026-10-11T08:15:00.1234567+03:00",
        ],
    )
    def test_is_left_as_it_was(self, text):
        assert redact_secrets(text) == text

    def test_two_distinct_digits_are_not_a_card(self):
        """Fill text such as 4444 4444 4444 4448 is excluded even when it checks."""
        filler = next(f"4{b}" * 8 for b in "98765" if luhn_ok(f"4{b}" * 8))
        assert redact_secrets(filler) == filler


class TestIbans:
    GR = iban("GR", _digits(23, 1))
    DE = iban("DE", _digits(18, 2))
    ES = iban("ES", _digits(20, 4))  # 24 characters: every printed group is four long

    def test_compact_in_greek_prose(self):
        text = f"IBAN {self.GR} για την πληρωμή"
        assert redact_secrets(text) == f"IBAN GR{IBAN}{self.GR[-4:]} για την πληρωμή"

    @pytest.mark.parametrize("value", [GR, DE, ES])
    def test_print_form_keeps_country_and_last_four(self, value):
        out = redact_secrets(f"ΙΒΑΝ: {printed(value)}.")
        assert out == f"ΙΒΑΝ: {value[:2]}{IBAN}{value[-4:]}."

    def test_a_short_word_after_the_last_group_is_not_read_as_part_of_it(self):
        out = redact_secrets(f"{printed(self.ES)} EUR")
        assert out == f"ES{IBAN}{self.ES[-4:]} EUR"

    def test_an_amount_after_the_last_group_is_not_read_as_part_of_it(self):
        out = redact_secrets(f"{printed(self.ES)} 1000 EUR")
        assert out == f"ES{IBAN}{self.ES[-4:]} 1000 EUR"

    def test_two_printed_one_after_the_other(self):
        other = iban("CY", _digits(24, 6))
        out = redact_secrets(f"{printed(self.ES)} {printed(other)}")
        assert out == f"ES{IBAN}{self.ES[-4:]} CY{IBAN}{other[-4:]}"

    def test_a_card_printed_after_a_compact_iban(self):
        out = redact_secrets(f"{self.GR} {grouped(VISA, sep=chr(0xA0))}")
        assert out == f"GR{IBAN}{self.GR[-4:]} {masked(VISA)}"

    def test_a_bank_code_in_letters(self):
        value = iban("GB", "ABCD" + _digits(14, 5))
        assert redact_secrets(value) == f"GB{IBAN}{value[-4:]}"

    def test_its_digits_are_not_read_as_a_card_as_well(self):
        value = iban("GR", "0" + VISA + _digits(6))
        out = redact_secrets(printed(value))
        assert out == f"GR{IBAN}{value[-4:]}" and CARD not in out

    @pytest.mark.parametrize(
        "text",
        [
            "GR" + str((int(GR[2:4]) + 1) % 100).zfill(2) + GR[4:],  # wrong check digits
            printed("GR00" + "0" + _digits(3) + "0" + _digits(3) + "0" + _digits(3)),
            GR.lower(),
            "GR12 3456",  # too short
            "ABCD" + GR,  # part of a longer token
        ],
    )
    def test_is_left_as_it_was(self, text):
        assert redact_secrets(text) == text


class TestPasswordValues:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Password: abcd1234", f"Password: {PASSWORD}"),
            ("password=hunter2 next", f"password={PASSWORD} next"),
            ("PWD: letmein", f"PWD: {PASSWORD}"),
            ("passwd = swordfish", f"passwd = {PASSWORD}"),
            (
                "Meeting ID: 123 456 789 Passcode: abc123",
                f"Meeting ID: 123 456 789 Passcode: {PASSWORD}",
            ),
            ("The password is: blue42.", f"The password is: {PASSWORD}"),
            ('{"password": "two words"}', f'{{"password": "{PASSWORD}"}}'),
            ("db_password='opensesame'", f"db_password='{PASSWORD}'"),
            ("Password: <b>hidden1</b>", f"Password: <b>{PASSWORD}</b>"),
            ("<td>Password:</td><td>hidden1</td>", f"<td>Password:</td><td>{PASSWORD}</td>"),
            ("Κωδικός πρόσβασης: abc12!", f"Κωδικός πρόσβασης: {PASSWORD}"),
            ("ο κωδικό πρόσβασης: x1", f"ο κωδικό πρόσβασης: {PASSWORD}"),
            ("κωδικος προσβασης=zz9", f"κωδικος προσβασης={PASSWORD}"),
        ],
    )
    def test_the_key_stays_and_the_value_goes(self, text, expected):
        assert redact_secrets(text) == expected

    def test_greek_capitals_without_accents(self):
        key = "κωδικος προσβασης".upper()
        assert redact_secrets(f"{key}: Ab12") == f"{key}: {PASSWORD}"

    @pytest.mark.parametrize(
        "text",
        [
            "Κωδικός: 12345",  # a product or customer code
            "Κωδικός πελάτη: 987654",
            "Please reset your password before Friday.",
            "Password reset requested",
            "passwords: see the vault",
            "password_hash = compute(x)",
            "if password == other:",
            "Password:",
            "Password: [REDACTED:password]",
            'password: "[REDACTED:password]"',
            "password: [REDACTED:google-key]",
        ],
    )
    def test_is_left_as_it_was(self, text):
        assert redact_secrets(text) == text


@pytest.mark.parametrize(
    "text",
    [
        f"card {grouped(VISA)} iban {printed(TestIbans.GR)} Password: x1 key AKIAIOSFODNN7EXAMPLE",
        f"password: {VISA}",
        f"{TestIbans.ES} {MASTERCARD} {AMEX}",
    ],
)
def test_masking_twice_changes_nothing(text):
    once = redact_secrets(text)
    assert once != text
    assert redact_secrets(once) == once


def test_a_long_text_full_of_numbers_costs_little():
    """Every number is a candidate, so the cost must stay linear in the text."""
    import time

    text = ("1 2 3 4 5 6 7 8 9 0 " * 20_000) + (_digits(12) + " ") * 5_000
    started = time.perf_counter()
    redact_secrets(text)
    assert time.perf_counter() - started < 1.0
