"""Card numbers and IBANs built at run time for the redaction tests.

A card is a body plus its computed Luhn digit, an IBAN carries its computed mod-97
check digits. None is a literal in the source, none comes from a mailbox, and none
is a published test number.
"""


def digits(n: int, seed: int = 3) -> str:
    """n digits with no pattern a check could reject, and no literal in the source."""
    return "".join(str((seed + 7 * i) % 10) for i in range(n))


def luhn_ok(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def card(prefix: str, length: int, seed: int = 3) -> str:
    """A Luhn-valid number of `length` digits starting with `prefix`."""
    body = prefix + digits(length - len(prefix) - 1, seed)
    return next(body + c for c in "0123456789" if luhn_ok(body + c))


def not_luhn(number: str) -> str:
    """The same number with a check digit that fails."""
    return number[:-1] + str((int(number[-1]) + 1) % 10)


def masked(number: str) -> str:
    """A card number as the masker leaves it."""
    only = "".join(c for c in number if c.isdigit())
    return f"{only[:6]}[REDACTED:card]{only[-4:]}"


def grouped(number: str, sizes=(4, 4, 4, 4), sep=" ") -> str:
    out, i = [], 0
    for size in sizes:
        out.append(number[i : i + size])
        i += size
    return sep.join(out)


def iban(country: str, bban: str) -> str:
    """An IBAN with its computed mod-97 check digits."""
    number = int("".join(str(int(c, 36)) for c in bban + country + "00"))
    return f"{country}{98 - number % 97:02d}{bban}"


def printed(value: str) -> str:
    """The ISO 13616 print form: groups of four, the last one shorter."""
    return " ".join(value[i : i + 4] for i in range(0, len(value), 4))
