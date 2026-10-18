"""The SharePoint links a Teams message carries, for `process-sharepoint` to fetch.

A file sent in Teams is not in the message's HTML: it is an entry of properties.files, a JSON
string, that names the file by objectUrl (by fileInfo.fileUrl in the few entries without one),
sometimes with the spaces of its path unencoded. A channel post nests the message in an
envelope, message.properties.files, and holds its replies beside it, replies.messages[]. A
link written in the text is in the HTML, which the store keeps in content_html.

Two more properties mention SharePoint, and neither is read. properties.atp is Safe Links'
record of every link the message holds, files and hyperlinks alike: it names a sent file by its
sharing link (/:x:/g/...), a second spelling of the same file whose fetch would bring the same
bytes again, and a hyperlink by the URL the HTML already gives. properties.links holds the
previews of the hyperlinks in the text. Over the 732 stored payloads that mention SharePoint in
one store, on 2026-10-18, neither named a file or page the files and the HTML did not.
"""

import json
from collections.abc import Callable

from src.export.teams_export import _replies_of
from src.extract.sharepoint_url_scanner import extract_sharepoint_urls, normalise_sharepoint_url


def _payload(raw_json: str | None) -> dict | None:
    if not raw_json:
        return None
    try:
        item = json.loads(raw_json)
    except ValueError:
        return None
    return item if isinstance(item, dict) else None


def _message(item: dict) -> dict:
    """The message itself: a channel post's envelope holds it under "message"."""
    inner = item.get("message")
    return inner if isinstance(inner, dict) else item


def _file_urls(message: dict) -> set[str]:
    """The SharePoint files sent with one message, from its properties.files."""
    props = message.get("properties")
    files = props.get("files") if isinstance(props, dict) else None
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except ValueError:
            return set()
    if not isinstance(files, list):
        return set()
    urls: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            continue
        url = entry.get("objectUrl")
        info = entry.get("fileInfo")
        if not url and isinstance(info, dict):
            url = info.get("fileUrl")
        found = normalise_sharepoint_url(url) if isinstance(url, str) else None
        if found:
            urls.add(found)
    return urls


def message_urls(
    raw_json: str | None,
    content_html: str | None,
    *,
    reply_stored: Callable[[str], bool] = lambda reply_id: True,
) -> list[str]:
    """Every SharePoint link of one stored Teams message: the files sent with it, the links in
    its HTML, and the same of each reply its payload holds that `reply_stored` says is not a
    message of its own. A reply stored as a row is read there, under its own date, so by
    default no reply is read here. A payload that is missing or does not parse leaves the HTML.
    """
    urls = set(extract_sharepoint_urls(content_html))
    item = _payload(raw_json)
    if item is not None:
        urls |= _file_urls(_message(item))
        for reply in _replies_of(item):
            if reply_stored(str(reply["id"])):
                continue
            urls |= _file_urls(reply)
            content = reply.get("content")
            urls.update(extract_sharepoint_urls(content if isinstance(content, str) else None))
    return sorted(urls)


def holds_file_link(raw_json: str | None) -> bool:
    """Whether a payload names a SharePoint file sent with the message or with a reply in it:
    the links the payload alone holds, which the HTML kept in content_html does not."""
    item = _payload(raw_json)
    if item is None:
        return False
    return bool(_file_urls(_message(item))) or any(_file_urls(r) for r in _replies_of(item))
