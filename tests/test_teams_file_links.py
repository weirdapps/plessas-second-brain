"""The SharePoint links a Teams message carries: the files sent with it, and the links in its text.

A file sent in Teams is not in the message's HTML. It is an entry of properties.files, a JSON
string, naming the file by objectUrl, sometimes with the spaces of its path unencoded. A channel
post nests the message in an envelope, and holds its replies there too. properties.atp and
properties.links are Safe Links' and the link previews' copies of what the files and the text
already name, so they are not read. The payloads below are synthetic.
"""

import json

from src.extract.sharepoint_url_scanner import normalise_sharepoint_url
from src.extract.teams_file_links import holds_file_link, message_urls

SITE = "https://contoso.sharepoint.com/sites/Team/Shared Documents/General"
ONEDRIVE = "https://contoso-my.sharepoint.com/personal/ann_example_com/Documents"


def _file(object_url=None, file_url=None, name="Plan.pdf"):
    entry = {"@type": "http://schema.skype.com/File", "fileName": name, "itemid": "item-1"}
    if object_url is not None:
        entry["objectUrl"] = object_url
        entry["baseUrl"] = object_url.rsplit("/", 2)[0]
    entry["fileInfo"] = {"fileUrl": file_url, "shareUrl": None}
    return entry


def _chat_message(files=(), content="", **props):
    """A chat message as chatsvc returns it: flat, properties.files a JSON string."""
    properties = dict(props)
    if files:
        properties["files"] = json.dumps(list(files))
    return {"id": "1760000000000", "content": content, "properties": properties}


def _post(files=(), replies=()):
    """A channel post envelope: the message nested, its replies beside it."""
    message = _chat_message(files)
    return {
        "id": message["id"],
        "message": message,
        "replies": {"messages": list(replies), "totalCount": len(replies)},
    }


def test_a_sent_file_is_found_by_its_object_url_with_its_path_encoded():
    raw = json.dumps(_chat_message([_file(f"{SITE}/Plan 2027.pdf")]))

    assert message_urls(raw, "") == [
        "https://contoso.sharepoint.com/sites/Team/Shared%20Documents/General/Plan%202027.pdf"
    ]


def test_an_entry_without_an_object_url_is_found_by_its_file_url():
    raw = json.dumps(_chat_message([_file(file_url=f"{ONEDRIVE}/Budget.xlsx")]))

    assert message_urls(raw, "") == [f"{ONEDRIVE}/Budget.xlsx"]


def test_files_are_read_from_a_list_as_well_as_from_a_json_string():
    message = _chat_message()
    message["properties"]["files"] = [_file(f"{ONEDRIVE}/Budget.xlsx")]

    assert message_urls(json.dumps(message), "") == [f"{ONEDRIVE}/Budget.xlsx"]


def test_a_channel_post_is_read_inside_its_envelope():
    raw = json.dumps(_post([_file(f"{ONEDRIVE}/Budget.xlsx")]))

    assert message_urls(raw, None) == [f"{ONEDRIVE}/Budget.xlsx"]


def test_links_in_the_text_are_found_with_the_files_once_each():
    link = f"{ONEDRIVE}/Budget.xlsx"
    raw = json.dumps(_chat_message([_file(link)]))
    notes = SITE.replace(" ", "%20") + "/Notes.docx"
    html = f'<p>see <a href="{link}">the budget</a> and <a href="{notes}">notes</a></p>'

    assert message_urls(raw, html) == [
        f"{ONEDRIVE}/Budget.xlsx",
        "https://contoso.sharepoint.com/sites/Team/Shared%20Documents/General/Notes.docx",
    ]


def test_safe_links_and_link_previews_are_not_read():
    """atp and links copy what the files and the text name; read, they add the share-link
    spelling of every file sent, a second fetch of the same bytes."""
    share = "https://contoso-my.sharepoint.com/:x:/g/personal/ann_example_com/EQbudget"
    raw = json.dumps(
        _chat_message(
            atp=json.dumps([{"URL": share, "Xsdata": "opaque"}]),
            links=json.dumps([{"url": f"{ONEDRIVE}/Other.docx"}]),
        )
    )

    assert message_urls(raw, "") == []


def test_a_reply_is_read_only_when_it_is_not_a_message_of_its_own():
    stored = {
        "id": "1760000060000",
        "properties": {"files": json.dumps([_file(f"{ONEDRIVE}/A.pdf")])},
    }
    unstored = {
        "id": "1760000120000",
        "content": f'<a href="{ONEDRIVE}/C.docx">c</a>',
        "properties": {"files": json.dumps([_file(f"{ONEDRIVE}/B.pdf")])},
    }
    raw = json.dumps(_post(replies=[stored, unstored]))

    urls = message_urls(raw, "", reply_stored=lambda reply_id: reply_id == stored["id"])

    assert urls == [f"{ONEDRIVE}/B.pdf", f"{ONEDRIVE}/C.docx"]
    assert message_urls(raw, "") == [], "replies are rows of their own unless told otherwise"


def test_no_payload_or_a_broken_one_still_reads_the_html():
    html = f'<a href="{ONEDRIVE}/Budget.xlsx">budget</a>'

    assert message_urls(None, html) == [f"{ONEDRIVE}/Budget.xlsx"]
    assert message_urls("{not json", html) == [f"{ONEDRIVE}/Budget.xlsx"]
    raw = json.dumps(_chat_message(files=[]) | {"properties": {"files": "[not json"}})
    assert message_urls(raw, None) == []


def test_a_file_entry_off_sharepoint_is_ignored():
    raw = json.dumps(_chat_message([_file("https://files.example.com/a.pdf"), {"objectUrl": 7}]))

    assert message_urls(raw, "") == []


def test_holds_file_link_looks_at_the_files_of_the_message_and_its_replies():
    reply = {
        "id": "1760000060000",
        "properties": {"files": json.dumps([_file(f"{ONEDRIVE}/A.pdf")])},
    }

    assert holds_file_link(json.dumps(_chat_message([_file(f"{ONEDRIVE}/A.pdf")])))
    assert holds_file_link(json.dumps(_post(replies=[reply])))
    assert not holds_file_link(json.dumps(_chat_message(content=f"{ONEDRIVE}/A.pdf")))
    assert not holds_file_link(None)
    assert not holds_file_link("{not json")


def test_a_url_is_normalised_as_the_mail_scan_would_record_it():
    assert normalise_sharepoint_url(f"{ONEDRIVE}/Budget.xlsx?web=1") == f"{ONEDRIVE}/Budget.xlsx"
    assert normalise_sharepoint_url(f"{SITE}/Σχέδιο.pdf") == (
        "https://contoso.sharepoint.com/sites/Team/Shared%20Documents/General/"
        "%CE%A3%CF%87%CE%AD%CE%B4%CE%B9%CE%BF.pdf"
    )
    assert normalise_sharepoint_url("https://files.example.com/a.pdf") is None
    assert normalise_sharepoint_url("") is None
