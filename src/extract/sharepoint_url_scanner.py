"""SharePoint URL scanner — extract and deduplicate SharePoint links from email HTML."""

import re
from html import unescape  # NOT `import html`: the parameter below shadows it
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

# An href tells us exactly where the URL ends: at the attribute's own closing
# quote. That is the only way to keep an apostrophe INSIDE a URL, and page
# titles containing one are common ("Sales-Rally-Q2-'2025.aspx"). One pattern
# per quote style rather than a backreference, so each excludes only its own
# delimiter and can never run past it into the anchor text.
_HREF_PATTERNS = (
    re.compile(r'href\s*=\s*"(https://[^\s"<>]+\.sharepoint\.com/[^\s"<>]*)"', re.IGNORECASE),
    re.compile(r"href\s*=\s*'(https://[^\s'<>]+\.sharepoint\.com/[^\s'<>]*)'", re.IGNORECASE),
)

# ...and the fallback for a URL that is not in an href at all: plain-text links
# pasted into message bodies, and hrefs whose markup arrived HTML-escaped (the
# delimiter there is "&quot;", not a quote character). Without the delimiter to
# stop on, a quote has to end the match, so an apostrophe still truncates here.
_BARE_PATTERN = re.compile(r'https://[^\s"\'<>]+\.sharepoint\.com/[^\s"\'<>]*', re.IGNORECASE)


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

    # href-anchored first, then the bare scan over what is LEFT: an href match
    # is removed before the fallback runs, so a URL the fallback would have
    # truncated at an apostrophe cannot survive as a second, broken entry.
    matches: list[str] = []
    remainder = html
    for pattern in _HREF_PATTERNS:
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
        # Unescape BEFORE the rstrip, which then removes the resulting quote.
        url = unescape(url)

        # Strip trailing punctuation that regex might capture
        url = url.rstrip(".,;)\"'>")

        # Parse URL
        parsed = urlparse(url)

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
