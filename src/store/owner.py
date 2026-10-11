"""Whether an address is the owner's, by BRAIN_USER_EMAIL_PATTERN.

Shared by the calendar loader (whose meetings are the owner's) and the source classes (which
mail the owner's jobs sent him). Both used a case-blind substring test, which took
'owner@example.com.example.net' and 'notowner@example.com' for 'owner@example.com': anyone
holding such an address could pass mail off as the owner's own.
"""

# What may stand before the pattern's local part in an address: 'first.owner@example.com'.
_SEPARATORS = ".-_+"


def is_owner_address(address: str | None, pattern: str | None) -> bool:
    """Whether `address` is the owner's, case-blind.

    The pattern's domain, when it names one, is the address's whole domain; its local part ends
    the address's local part, starting it or following a separator. So 'owner@example.com'
    matches 'owner@example.com' and 'first.owner@example.com', 'owner' and 'owner@' match an
    'owner' local part at any domain, and '@example.com' matches the whole domain, as the
    substring test did. An empty pattern matches no one: '' is inside every string.
    """
    pattern = (pattern or "").strip().lower()
    address = (address or "").strip().lower()
    if not pattern or not address:
        return False
    local, at, domain = pattern.partition("@")
    address_local, _, address_domain = address.partition("@")
    if domain and address_domain != domain:
        return False
    if not local:
        return bool(at)
    if not address_local.endswith(local):
        return False
    before = address_local[: -len(local)]
    return not before or before[-1] in _SEPARATORS
