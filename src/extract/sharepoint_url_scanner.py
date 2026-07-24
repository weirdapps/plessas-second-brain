"""SharePoint URL scanner — extract and deduplicate SharePoint links from email HTML."""

import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


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

    # Find all SharePoint URLs (case-insensitive)
    pattern = r'https://[^\s"\'<>]+\.sharepoint\.com/[^\s"\'<>]*'
    matches = re.findall(pattern, html, re.IGNORECASE)

    if not matches:
        return []

    # Clean and deduplicate
    cleaned = set()
    for url in matches:
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
