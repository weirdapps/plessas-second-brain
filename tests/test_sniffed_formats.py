"""Small text formats are read when their bytes are text, and only then.

Calendar invites (.ics), contact cards (.vcf), XML, EPS, subtitles (.vtt), shortcuts (.url) and
clear-signed mail (.p7m) were skipped as unsupported types, though each is text a reader here
handles. They are routed by what their bytes are, never by their name alone: the plain-text
reader falls back to latin-1, which decodes any byte, so a binary file under a text name would be
"read" as noise. An SVG or a script saved under an image name is read as the text it is, rather
than failing in the image reader. RTF and Outlook .msg items stay unread, with a reason.
"""

import pytest

from src.extract import attachment_extractors as ax
from src.extract.attachment_extractors import extract_text_from_file
from tests.ole_fixtures import STREAM, ole_file

WORDS = "Quarterly review of the regional branch network, with the plan for next year"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _write(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return str(path)


SIGNED = (
    'Content-Type: multipart/signed; protocol="application/pkcs7-signature";'
    ' micalg=sha-256; boundary="sig"\r\nMIME-Version: 1.0\r\n\r\n'
    "--sig\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
    f"{WORDS}\r\n"
    "--sig\r\nContent-Type: application/pkcs7-signature; name=smime.p7s\r\n"
    "Content-Transfer-Encoding: base64\r\n\r\nMIIBszCCAVmgAwIBAgIUQ0FGRQ==\r\n--sig--\r\n"
)


def test_a_clear_signed_message_is_read_as_mail(tmp_path):
    out = extract_text_from_file(_write(tmp_path, "smime.p7m", SIGNED), "application/pkcs7-mime")

    assert (out["status"], out["method"]) == ("extracted", "email_parser")
    assert WORDS in out["text"]
    assert "MIIBszCC" not in out["text"]


@pytest.mark.parametrize(
    ("name", "mime", "body"),
    [
        ("invite.ics", "text/calendar", f"BEGIN:VCALENDAR\r\nSUMMARY:{WORDS}\r\nEND:VCALENDAR\r\n"),
        ("invite.ics", "application/octet-stream", f"BEGIN:VCALENDAR\nDESCRIPTION:{WORDS}\n"),
        ("card.vcf", "text/vcard", f"BEGIN:VCARD\nVERSION:3.0\nNOTE:{WORDS}\nEND:VCARD\n"),
        ("talk.vtt", "text/vtt", f"WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n{WORDS}\n"),
        ("logo.eps", "application/postscript", f"%!PS-Adobe-3.0 EPSF-3.0\n%%Title: ({WORDS})\n"),
        (
            "link.url",
            "application/octet-stream",
            f"[InternetShortcut]\nURL=https://example.org/{WORDS.replace(' ', '-')}\n",
        ),
    ],
)
def test_a_text_format_is_read_as_plain_text(tmp_path, name, mime, body):
    out = extract_text_from_file(_write(tmp_path, name, body), mime)

    assert (out["status"], out["method"]) == ("extracted", "direct_read")


def test_xml_is_read_without_its_markup(tmp_path):
    xml = f'<?xml version="1.0"?><report><title>{WORDS}</title><n>12</n></report>'
    out = extract_text_from_file(_write(tmp_path, "report.xml", xml), "application/xml")

    assert out["status"] == "extracted"
    assert WORDS in out["text"] and "<title>" not in out["text"]


def test_binary_bytes_under_a_text_name_are_not_read_as_text(tmp_path):
    data = bytes(range(256)) * 8
    out = extract_text_from_file(_write(tmp_path, "invite.ics", data), "text/calendar")

    assert out["status"] == "skipped"
    assert out["text"] is None


def test_an_svg_under_an_image_name_is_read_from_its_text_nodes(tmp_path):
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><style>.a{fill:red}</style>'
        f'<rect width="10" height="10"/><text x="1" y="2">{WORDS}</text></svg>'
    )
    out = extract_text_from_file(_write(tmp_path, "diagram.png", svg), "image/png")

    assert (out["status"], out["method"]) == ("extracted", "svg")
    assert WORDS in out["text"] and "fill:red" not in out["text"]


def test_a_script_under_an_image_name_is_read_as_text(tmp_path):
    script = f"@echo off\r\nrem {WORDS}\r\ncopy a.txt b.txt\r\n"
    out = extract_text_from_file(_write(tmp_path, "capture.png", script), "image/png")

    assert (out["status"], out["method"]) == ("extracted", "direct_read")
    assert WORDS in out["text"]


def test_a_real_image_still_goes_to_ocr(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        ax,
        "_extract_image_ocr",
        lambda p, *_a: (
            seen.append(p) or {"text": None, "method": "ocr", "status": "skipped", "error": "x"}
        ),
    )
    path = _write(tmp_path, "photo.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 64)

    extract_text_from_file(path, "image/png")

    assert seen == [path]


def test_an_rtf_document_is_skipped_with_the_reason(tmp_path):
    rtf = r"{\rtf1\ansi\deff0 {\fonttbl {\f0 Arial;}} \f0\fs24 " + WORDS + r"\par }"
    for name, mime in (
        ("memo.rtf", "application/rtf"),
        ("Outlook-memo", "application/octet-stream"),
    ):
        out = extract_text_from_file(_write(tmp_path, name, rtf), mime)
        assert out["status"] == "skipped"
        assert out["error"].startswith("RTF document: no reader for this format")


def test_an_outlook_item_is_skipped_with_the_reason(tmp_path):
    data = ole_file([("__substg1.0_0037001F", STREAM), ("__properties_version1.0", STREAM)])
    for name in ("Re meeting.msg", "Outlook-item"):
        out = extract_text_from_file(_write(tmp_path, name, data), "application/octet-stream")
        assert out["status"] == "skipped"
        assert out["error"] == "Outlook item (.msg): no reader for this format"


def test_an_html_page_named_pptx_is_read_as_html(tmp_path):
    page = f"<html><head><title>Review</title></head><body><p>{WORDS}</p></body></html>"
    out = extract_text_from_file(_write(tmp_path, "Review.pptx", page), PPTX)

    assert out["status"] == "extracted"
    assert WORDS in out["text"] and "<p>" not in out["text"]


def test_a_mail_saved_without_a_name_is_read_as_mail(tmp_path):
    mail = (
        "X-Receiver: someone@example.org\nReceived: from mx.example.org\n"
        f"Subject: Plan\nContent-Type: text/plain\n\n{WORDS}\n"
    )
    out = extract_text_from_file(_write(tmp_path, "Outlook-a1b2", mail), "application/octet-stream")

    assert (out["status"], out["method"]) == ("extracted", "email_parser")


def test_plain_text_that_starts_like_a_header_stays_plain_text(tmp_path):
    note = f"Date: tomorrow, after the board\nTo: whoever reads this\n{WORDS}\n"
    out = extract_text_from_file(_write(tmp_path, "Outlook-note", note), "application/octet-stream")

    assert (out["status"], out["method"]) == ("extracted", "direct_read")
    assert WORDS in out["text"]
