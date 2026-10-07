"""SharePoint URL scanner — extract and deduplicate SharePoint links from email HTML."""

import re
from html import unescape  # NOT `import html`: the parameter below shadows it
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

# A quoted attribute value tells us exactly where the URL ends: at the
# attribute's own closing quote. That is the only way to keep an apostrophe
# INSIDE a URL, and page titles containing one are common
# ("Sales-Rally-Q2-'2025.aspx"). Any attribute, not only href: Outlook's Safe
# Links keeps the address it rewrote in originalsrc="...", and read by the bare
# scan below that copy was cut at the apostrophe and recorded as a second,
# broken link. One pattern per quote style rather than a backreference, so each
# excludes only its own delimiter and can never run past it into the text.
_ATTRIBUTE_PATTERNS = (
    re.compile(r'=\s*"(https://[^\s"<>]+\.sharepoint\.com/[^\s"<>]*)"', re.IGNORECASE),
    re.compile(r"=\s*'(https://[^\s'<>]+\.sharepoint\.com/[^\s'<>]*)'", re.IGNORECASE),
)

# ...and the fallback for a URL that is not an attribute value: plain-text
# links pasted into message bodies, the visible text of a link, and markup that
# arrived HTML-escaped (the delimiter there is "&quot;", not a quote). With no
# delimiter to stop on, an apostrophe ends the URL only where nothing URL-like
# follows it: "Q2-'2025.aspx" keeps it, "'https://.../a.aspx', then" does not.
_BARE_PATTERN = re.compile(
    r"https://[^\s\"'<>]+\.sharepoint\.com/(?:[^\s\"'<>]|'(?=[^\s\"'<>]))*", re.IGNORECASE
)

# A URL holds none of these unencoded, so once its entities are decoded the
# first one is where the markup around it begins: an escaped href's closing
# "&quot;&gt;" decodes to '">', and the anchor text after it is not the URL's.
_MARKUP_DELIMITER = re.compile(r'[\s"<>]')

# Markup quoted inside markup is escaped once per level: "&amp;amp;" is one "&"
# two levels down. Unescaping stops at the first pass that changes nothing.
_MAX_UNESCAPE_PASSES = 3


def _unescape_fully(url: str) -> str:
    for _ in range(_MAX_UNESCAPE_PASSES):
        plain = unescape(url)
        if plain == url:
            break
        url = plain
    return url


def extract_sharepoint_urls(html: str | None) -> list[str]:
    """
    Extract SharePoint URLs from email HTML content.

    Args:
        html: Email HTML content (may be None or empty)

    Returns:
        List of deduplicated SharePoint URLs with tracking params removed
    """
    if not html:
        return []

    # Attribute-anchored first, then the bare scan over what is LEFT: a quoted
    # value is removed before the fallback runs, so a URL the fallback would
    # have truncated cannot survive as a second, broken entry.
    matches: list[str] = []
    remainder = html
    for pattern in _ATTRIBUTE_PATTERNS:
        matches.extend(pattern.findall(remainder))
        remainder = pattern.sub(" ", remainder)
    matches.extend(_BARE_PATTERN.findall(remainder))

    if not matches:
        return []

    # Clean and deduplicate
    cleaned = set()
    for url in matches:
        # This is markup, so entities are the HTML's, not the file name's:
        # "Economy-&amp;-Markets.aspx" is a page whose title contains an "&",
        # and an escaped href ends in "&quot;" rather than a quote. 23 links on
        # prod were stored with those literals baked in and 404ed forever.
        # Unescape BEFORE cutting at the markup and the rstrip, which then
        # remove what the entities decoded to.
        url = _MARKUP_DELIMITER.split(_unescape_fully(url), maxsplit=1)[0]

        # Strip trailing punctuation that regex might capture
        url = url.rstrip(".,;)\"'>")

        # Parse URL. urlparse raises on some shapes the regexes admit (an
        # unclosed "[" reads as an IPv6 literal); skip that URL rather than let
        # one email kill the nightly scan and starve every email behind it.
        try:
            parsed = urlparse(url)
        except ValueError:
            continue

        # Filter out tracking query params
        tracking_params = {"web", "source", "csf", "e", "cid", "nav"}
        if parsed.query:
            params = parse_qs(parsed.query)
            # Keep only non-tracking params
            clean_params = {k: v for k, v in params.items() if k.lower() not in tracking_params}

            # Rebuild query string
            if clean_params:
                # parse_qs returns lists, flatten single values
                query = urlencode({k: v[0] if len(v) == 1 else v for k, v in clean_params.items()})
            else:
                query = ""
        else:
            query = parsed.query

        # Rebuild URL
        clean_url = urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                query,
                parsed.fragment,
            )
        )

        cleaned.add(clean_url)

    return sorted(cleaned)  # Sort for deterministic output
