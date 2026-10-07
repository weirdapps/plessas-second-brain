"""Local text extraction from attachment files.

Extracts text from PDF, Word, PowerPoint, Excel, images (OCR),
.eml, .rpmsg, and plain text files. No API calls — all local.
"""

import html
import io
import os
import re
import time
import zlib
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

# A ceiling against runaway input: text is stored in full up to it, and Phase 2 summarises a
# long one in parts (src/extract/attachment_pipeline.py). Kept at 2,000,000 after a review
# measured 50,000,000: 3.3 GB of memory for every write to such a row, and search snippets that
# never finished. A text cut here says its file is kept, and the sweep keeps it
# (src/store/file_sweep.py UNREAD_SQL): a 39.8M-character call log stays on disk.
MAX_TEXT_CHARS = 2_000_000
# Minimum characters to consider a successful extraction
MIN_TEXT_CHARS = 50

# MIME types we skip entirely (video, audio, archives, Outlook artifacts)
SKIP_MIME_TYPES = {
    "video/mp4",
    "audio/mpeg",
    "audio/x-wav",
    "audio/wav",
    "application/x-rar-compressed",
    "application/x-7z-compressed",
    "application/gzip",
}

SKIP_EXTENSIONS = {".mp4", ".mp3", ".wav", ".rar", ".7z", ".gz", ".wmz"}

# Formats with a reader of their own, chosen by the name: their bytes have no magic a sniff
# knows, and senders declare them as anything (image/g3fax, application/gzip, x-coff).
OWN_READER_EXTENSIONS = frozenset({".mso"})
# What one file may inflate to, all of its compressed layers together (an .mso container and
# every object in it; a .wmz or .emz drawing). The largest .mso on the producer inflates to
# 1.6 MB and the largest .wmz to 1.9 MB, and the VPS that reads them has 7 GB shared with the
# MCP server. Past this a file is taken for a decompression bomb: what was read is kept, and
# the error says the rest is unread for good.
INFLATE_MAX_BYTES = 64 << 20
# An Office part the XML fallbacks read: refused past this size or past ZIP_MAX_RATIO, and the
# read itself stops here whatever the zip header claims.
XML_PART_MAX_BYTES = 64 << 20

# A zip is unpacked into a temporary directory and every member extracted; nothing is kept.
# The guards stop a hostile archive: too many members, too many bytes, a member compressed
# like a zip bomb, or archives nested inside archives.
ZIP_MAX_MEMBERS = 500
ZIP_MAX_TOTAL_BYTES = 1 << 30
ZIP_MAX_RATIO = 100
ZIP_MAX_DEPTH = 1
# A member can cost minutes (OCR), and an archive without a content row is offered again on
# every run, so an archive stops reading members once this is spent and keeps what it read.
# Its error then says "members left unread", which the sweep treats as not stored
# (src/store/file_sweep.py UNREAD_SQL) and reextract --zip reads again with no budget.
ZIP_MAX_SECONDS = 120
# Every page of a scan is OCR'd, a few seconds each. A scan stops once this is spent and keeps
# the pages it read; its error then says "pages left unread", like an archive's, and reextract
# reads it again with no budget.
OCR_MAX_SECONDS = 120
# Tesseract's languages, and what a scan with too little text says.
OCR_LANGS = "eng+ell"
OCR_INSUFFICIENT = "OCR returned insufficient text"
ZIP_MIME_TYPES = frozenset({"application/zip", "application/x-zip-compressed"})


def extract_text_from_file(
    file_path: str,
    mime_type: str,
    _depth: int = 0,
    *,
    zip_seconds: float | None = None,
    ocr_seconds: float | None = None,
) -> dict:
    """Extract text from a file based on its MIME type.

    Returns dict with keys: text, method, status, error.
    status is one of: 'extracted', 'partial', 'failed', 'skipped', 'encrypted' (the bytes are
    ciphertext; method 'rms', 'rms-message' or 'password', see encrypted_result).
    _depth is how deep inside archives this file sits; only _extract_zip passes it.
    zip_seconds is how long an archive may spend reading members, ZIP_MAX_SECONDS when None;
    ocr_seconds how long a scan may spend on its pages, OCR_MAX_SECONDS when None. reextract
    passes math.inf for both, since a one-time recovery run has the time. A text cut at
    MAX_TEXT_CHARS says so in the error.
    """
    result = _extract_by_type(
        file_path, mime_type, _depth, zip_seconds=zip_seconds, ocr_seconds=ocr_seconds
    )
    text = result.get("text")
    error = result.get("error") or ""
    if text and len(text) >= MAX_TEXT_CHARS and "file kept" not in error:
        note = f"text cut at {MAX_TEXT_CHARS:,} characters; the rest is unread, file kept"
        result = {**result, "error": f"{error}; {note}" if error else note}
    return result


def _extract_by_type(
    file_path: str,
    mime_type: str,
    _depth: int,
    *,
    zip_seconds: float | None,
    ocr_seconds: float | None,
) -> dict:
    ext = Path(file_path).suffix.lower()

    # Skip unsupported types
    if ext in SKIP_EXTENSIONS:
        return {"text": None, "method": None, "status": "skipped", "error": None}

    # A declared archive or media type is a claim, and senders make it wrongly:
    # 60 .docx and 15 .pptx on the replica arrived labelled application/zip and
    # were skipped here unread, although every one of them opens. So the bytes
    # get the last word. A genuine archive still sniffs as application/zip (or
    # as nothing) and is skipped exactly as before.
    if mime_type in SKIP_MIME_TYPES and ext not in OWN_READER_EXTENSIONS:
        sniffed = sniff_mime_type(file_path)
        if sniffed is None or sniffed in SKIP_MIME_TYPES:
            return {"text": None, "method": None, "status": "skipped", "error": None}
        mime_type = sniffed

    # Check file exists
    if not os.path.isfile(file_path):
        return {
            "text": None,
            "method": None,
            "status": "failed",
            "error": f"File not found: {file_path}",
        }

    try:
        # A zero-byte file holds nothing to read. Parsers raise on it (PyMuPDF: EmptyFileError),
        # which used to be recorded as a failure and counted as a parser fault.
        if os.path.getsize(file_path) == 0:
            return {
                "text": None,
                "method": None,
                "status": "skipped",
                "error": "empty file (0 bytes)",
            }
        # Encrypted at rest, whatever the name or the declared type says: rights-protected
        # Office files and messages, password-protected ones. Sent to the readers they were
        # recorded as failures (BadZipFile, "Can't find workbook in OLE2 compound document",
        # CompoundFileInvalidMagicError), or as skips blaming a custom export format.
        protected = encryption_of(file_path)
        if protected:
            return protected
        if ext == ".mso":
            return _extract_mso(file_path, _depth, zip_seconds, ocr_seconds)
        if ext == ".zip" or mime_type in ZIP_MIME_TYPES:
            sniffed = sniff_mime_type(file_path)
            if sniffed == "application/zip":
                seconds = ZIP_MAX_SECONDS if zip_seconds is None else zip_seconds
                return _extract_zip(file_path, _depth, seconds, ocr_seconds)
            if sniffed is None:
                return {"text": None, "method": None, "status": "skipped", "error": None}
            mime_type = sniffed  # an Office document sent as a zip
        # The Office 2007+ readers open a zip, so a file named or labelled as one is sent to them
        # only when it is one. Anything else goes by its bytes (the else branch below): a legacy
        # .doc named .docx, an HTML page named .pptx, a CSV labelled application/vnd.ms-excel.
        magic = _magic(file_path)
        is_zip = magic == b"PK\x03\x04"
        if mime_type == "application/pdf" or ext == ".pdf":
            return _extract_pdf(file_path, ocr_seconds)
        elif (
            mime_type
            in ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",)
            or ext == ".docx"
        ) and is_zip:
            return _extract_docx(file_path)
        elif mime_type == "application/msword" or ext == ".doc":
            return _extract_doc(file_path)
        elif (
            mime_type
            in ("application/vnd.openxmlformats-officedocument.presentationml.presentation",)
            or ext == ".pptx"
        ) and is_zip:
            return _extract_pptx(file_path)
        elif ext == ".xlsb":
            return _extract_xlsb(file_path)
        elif ext == ".xls":
            return _extract_xls(file_path)
        elif (
            mime_type
            in (
                "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            or ext == ".xlsx"
        ) and (is_zip or magic == b"\xd0\xcf\x11\xe0"):
            # Sniff, do not trust the extension. 993 attachments on the live
            # corpus are named .xlsx and are legacy OLE2 .xls: Excel keeps the
            # name when a user saves an old workbook, and mail systems relabel
            # freely. openpyxl raises BadZipFile on those, the row is marked
            # failed, and nothing retries it, so their content was simply absent
            # from the brain. The same two-magic-number check already guards
            # _extract_xlsb below; this is the branch it was missing.
            if magic == b"\xd0\xcf\x11\xe0":
                return _extract_xls(file_path)
            return _extract_excel(file_path)
        elif (
            mime_type
            and mime_type.startswith("image/")
            or ext in (".png", ".jpg", ".jpeg", ".gif", ".tiff", ".tif", ".bmp", ".jfif")
        ):
            return _extract_image_ocr(file_path, ocr_seconds)
        elif mime_type == "message/rfc822" or ext == ".eml":
            return _extract_eml(file_path)
        elif ext == ".rpmsg":
            # An MSIPC one was recorded as encrypted above; this is a container that is not.
            return _extract_rpmsg(file_path)
        elif mime_type in ("text/plain", "text/csv", "text/markdown") or ext in (
            ".txt",
            ".csv",
            ".md",
        ):
            return _extract_plain_text(file_path)
        elif mime_type == "text/html" or ext in (".html", ".htm"):
            return _extract_html(file_path)
        else:
            # Neither the declared type nor the name said what this is, which
            # is what an extensionless Outlook part or a name that lost its dot
            # ("Status Updatepptx") looks like. Ask the bytes once. The
            # recursion ends because a second pass sniffs the same type.
            sniffed = sniff_mime_type(file_path)
            if sniffed and sniffed != mime_type and sniffed not in SKIP_MIME_TYPES:
                return extract_text_from_file(
                    file_path, sniffed, _depth, zip_seconds=zip_seconds, ocr_seconds=ocr_seconds
                )
            return {
                "text": None,
                "method": None,
                "status": "skipped",
                "error": f"Unsupported type: {mime_type} ({ext})",
            }
    except Exception as e:
        return {
            "text": None,
            "method": None,
            "status": "failed",
            "error": f"{type(e).__name__}: {str(e)[:500]}",
        }


def _extract_pdf(path: str, ocr_seconds: float | None = None) -> dict:
    """Extract text from PDF using PyMuPDF, with OCR fallback."""
    import fitz

    doc = fitz.open(path)
    # Reading pages of a PDF that needs a password raises "document closed or encrypted", which
    # was recorded as a failure (14 rows). The dispatcher records it first (encryption_of).
    if doc.needs_pass:
        doc.close()
        return encrypted_result("password", ".pdf")
    pages = []
    for page in doc:
        pages.append(page.get_text())
    doc.close()

    text = "\n\n".join(pages)
    text = _truncate(text)

    # If very little text extracted, fall back to per-page OCR (scanned PDF).
    # _extract_image_ocr cannot read PDFs (Image.open fails); _ocr_pdf_pages
    # renders each page via fitz.get_pixmap before OCR-ing. See B3 spec.
    if len(text.strip()) < MIN_TEXT_CHARS:
        budget = OCR_MAX_SECONDS if ocr_seconds is None else ocr_seconds
        ocr_result = _ocr_pdf_pages(path, seconds=budget)
        if ocr_result["status"] == "extracted" and len(ocr_result["text"] or "") > len(
            text.strip()
        ):
            return ocr_result
        if _apply_noise_filter(text):
            # The scan's own verdict when it has one: an OCR that failed, or one its budget cut
            # short. Reported as a plain skip, the sweep would take the file for finished.
            ocr_error = ocr_result.get("error") or ""
            if ocr_result["status"] == "failed" or "left unread" in ocr_error:
                return ocr_result
            return {
                "text": None,
                "method": "pymupdf",
                "status": "skipped",
                "error": "Insufficient text extracted",
            }

    return {"text": text, "method": "pymupdf", "status": "extracted", "error": None}


def _extract_docx(path: str) -> dict:
    """Extract text from .docx using python-docx, or from its XML when python-docx refuses it."""
    from docx import Document

    try:
        doc = Document(path)
    except Exception:
        # python-docx loads every relationship target, and Word writes some footer
        # relationships with Target="NULL": Document() raised KeyError ("There is no item
        # named 'word/NULL' in the archive") on 9 files whose body was intact.
        fallback = _read_xml_fallback(path, _docx_xml_text, "docx-xml")
        if fallback is None:
            raise
        return fallback
    parts = []

    for para in doc.paragraphs:
        if para.text.strip():
            parts.append(para.text)

    # Extract table content
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))

    text = _truncate("\n".join(parts))
    if _apply_noise_filter(text):
        return {
            "text": None,
            "method": "python-docx",
            "status": "skipped",
            "error": "Insufficient text extracted",
        }
    return {"text": text, "method": "python-docx", "status": "extracted", "error": None}


# Legacy .doc converters, tried in order. textutil is macOS-only, so on the
# Linux VPS every .doc recorded "[Errno 2] No such file or directory:
# 'textutil'" as status=failed — 21 rows, indistinguishable from genuinely
# corrupt files. antiword/catdoc are the portable equivalents; when none is
# installed the content is simply unreachable on this host, which is a skip.
_DOC_CONVERTERS = (
    ("textutil", ["textutil", "-convert", "txt", "-stdout"]),
    ("antiword", ["antiword"]),
    ("catdoc", ["catdoc"]),
)


def _extract_doc(path: str) -> dict:
    """Extract text from legacy .doc files via the first available converter."""
    import subprocess

    for method, argv in _DOC_CONVERTERS:
        try:
            result = subprocess.run(
                [*argv, path],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except FileNotFoundError:
            continue  # converter absent on this platform — try the next
        except subprocess.TimeoutExpired as e:
            return {"text": None, "method": method, "status": "failed", "error": str(e)}

        text = _truncate(result.stdout)
        if _apply_noise_filter(text):
            return {
                "text": None,
                "method": method,
                "status": "skipped",
                "error": "Insufficient text extracted",
            }
        return {"text": text, "method": method, "status": "extracted", "error": None}

    return {
        "text": None,
        "method": None,
        "status": "skipped",
        "error": "No legacy .doc converter available (textutil/antiword/catdoc)",
    }


def _collect_shape_text(shapes, out: list[str]) -> None:
    """Append the text of every shape, descending into groups.

    A GroupShape has neither a text frame nor a table, so a flat walk dropped
    the labels, callouts and diagram boxes a consulting-style deck builds as
    groups, while the row still said 'extracted'. Groups nest, and a group can
    hold a table, so the walk recurses rather than unwrapping one level. The
    test is isinstance, not shape_type, because shape_type raises
    NotImplementedError on an autoshape python-pptx does not recognise.
    """
    from pptx.shapes.group import GroupShape

    for shape in shapes:
        if isinstance(shape, GroupShape):
            _collect_shape_text(shape.shapes, out)
            continue
        if shape.has_text_frame:
            for para in shape.text_frame.paragraphs:
                if para.text.strip():
                    out.append(para.text)
        if getattr(shape, "has_table", False):
            for row in shape.table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    out.append(" | ".join(cells))


def _extract_pptx(path: str) -> dict:
    """Extract text from PowerPoint using python-pptx, or from its XML when python-pptx refuses."""
    from pptx import Presentation

    try:
        prs = Presentation(path)
    except Exception:
        # python-pptx reads every part, so one media member with a bad CRC
        # ("BadZipFile: Bad CRC-32 for file 'ppt/media/image5.svg'") failed 4 decks whose
        # slides were intact. A truncated deck has no readable zip and stays a failure.
        fallback = _read_xml_fallback(path, _pptx_xml_text, "pptx-xml")
        if fallback is None:
            raise
        return fallback
    parts = []

    for i, slide in enumerate(prs.slides, 1):
        slide_text: list[str] = []
        _collect_shape_text(slide.shapes, slide_text)
        if slide_text:
            parts.append(f"--- Slide {i} ---\n" + "\n".join(slide_text))

        # Extract speaker notes
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                parts.append(f"[Notes] {notes}")

    text = _truncate("\n\n".join(parts))
    if _apply_noise_filter(text):
        return {
            "text": None,
            "method": "python-pptx",
            "status": "skipped",
            "error": "Insufficient text extracted",
        }
    return {"text": text, "method": "python-pptx", "status": "extracted", "error": None}


# Text runs, tabs and line breaks of WordprocessingML (w:) and DrawingML (a:) parts. Read with a
# pattern rather than a parser: these fallbacks exist for files a strict reader rejects, and a
# pattern expands no entities.
_XML_RUNS = {
    ns: re.compile(rf"<{ns}:t(?:\s[^>]*)?>([^<]*)</{ns}:t>|<{ns}:(tab|br|cr)\b[^>]*/>")
    for ns in ("w", "a")
}


def _xml_paragraphs(xml: str, ns: str) -> list[str]:
    """The text of each paragraph of one Office XML part, empty paragraphs dropped."""
    out = []
    for chunk in xml.split(f"</{ns}:p>"):
        runs = [
            text if not mark else ("\t" if mark == "tab" else "\n")
            for text, mark in _XML_RUNS[ns].findall(chunk)
        ]
        line = html.unescape("".join(runs)).strip()
        if line:
            out.append(line)
    return out


def _read_member(zf, name: str) -> bytes | None:
    """A zip member's bytes, or None when it is missing, unreadable or out of bounds.

    The declared size and compression ratio are checked before reading (XML_PART_MAX_BYTES,
    ZIP_MAX_RATIO), and the read stops at the size bound whatever the header said.
    """
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > XML_PART_MAX_BYTES or info.file_size > ZIP_MAX_RATIO * max(
        info.compress_size, 1
    ):
        return None
    try:
        with zf.open(info) as member:
            data = member.read(XML_PART_MAX_BYTES + 1)
    except Exception:
        return None  # a bad CRC, a bad deflate, a header that understated the size
    return data if len(data) <= XML_PART_MAX_BYTES else None


def _member_paragraphs(zf, name: str, ns: str) -> list[str]:
    """A member's paragraphs; nothing when it cannot be read or is out of bounds."""
    data = _read_member(zf, name)
    return [] if data is None else _xml_paragraphs(data.decode("utf-8", "replace"), ns)


def _docx_xml_text(path: str) -> str:
    """The body, then the headers and footers, of a .docx read from its XML parts."""
    import zipfile

    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        ordered = ["word/document.xml"] + sorted(
            n for n in names if re.fullmatch(r"word/(?:header|footer)\d*\.xml", n)
        )
        lines = [line for n in ordered if n in names for line in _member_paragraphs(zf, n, "w")]
    return "\n".join(lines)


def _pptx_xml_text(path: str) -> str:
    """Each slide's text and its speaker notes, read from a .pptx's XML parts.

    Same shape as _extract_pptx. Slides go by the number in their part name. A slide's notes
    are the notes part its relationships name, and of that part only the body placeholder,
    which is what python-pptx calls the notes text.
    """
    import posixpath
    import zipfile

    parts = []
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        slides = sorted(
            (int(m.group(1)), n)
            for n in names
            if (m := re.fullmatch(r"ppt/slides/slide(\d+)\.xml", n))
        )
        for number, name in slides:
            lines = _member_paragraphs(zf, name, "a")
            if lines:
                parts.append(f"--- Slide {number} ---\n" + "\n".join(lines))
            rels = _read_member(zf, f"ppt/slides/_rels/slide{number}.xml.rels") or b""
            targets = re.findall(
                r'Target="([^"]*notesSlide[^"]*)"', rels.decode("utf-8", "replace")
            )
            for target in targets[:1]:
                notes = _read_member(zf, posixpath.normpath(posixpath.join("ppt/slides", target)))
                if notes is None:
                    continue
                xml = notes.decode("utf-8", "replace")
                body = [
                    line
                    for shape in re.findall(r"<p:sp\b.*?</p:sp>", xml, flags=re.DOTALL)
                    if 'type="body"' in shape
                    for line in _xml_paragraphs(shape, "a")
                ]
                if body:
                    parts.append("[Notes] " + "\n".join(body))
    return "\n\n".join(parts)


def _read_xml_fallback(path: str, read, method: str) -> dict | None:
    """The text `read` finds in an Office zip's XML, or None when it finds too little.

    For a file the library reader refused: None hands the reader's own error back to the
    caller, which records it, so a file with no readable text stays a failure that says why.
    """
    try:
        text = _truncate(read(path))
    except Exception:
        return None
    if _apply_noise_filter(text):
        return None
    return {"text": text, "method": method, "status": "extracted", "error": None}


def _magic(path: str, n: int = 4) -> bytes:
    """First n bytes of a file, or b"" if it cannot be read.

    b"\\xd0\\xcf\\x11\\xe0" is the OLE2 compound-document header (legacy .xls,
    .doc, .msg); b"PK\\x03\\x04" is a zip, which is what every modern Office
    format is.
    """
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except OSError:
        return b""


# Office zips announce their kind by the top-level folder their parts sit in.
_OOXML_BY_FOLDER = (
    ("word/", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("ppt/", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ("xl/", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
)

# OLE2 stream names that identify the application that wrote the file.
_OLE_BY_STREAM = (
    ("WordDocument", "application/msword"),
    ("Workbook", "application/vnd.ms-excel"),
    ("Book", "application/vnd.ms-excel"),
    ("PowerPoint Document", "application/vnd.ms-powerpoint"),
    ("__properties_version1.0", "application/vnd.ms-outlook"),
)


def sniff_mime_type(path: str) -> str | None:
    """The MIME type a file's own bytes declare, or None when they do not say.

    For names that carry no usable extension. Outlook saves some parts as
    "Outlook-xxxx" and users lose the dot in "...Updatepptx", so
    mimetypes.guess_type returns None for both and the row used to be recorded
    as application/octet-stream: 57 attachments, among them PDFs, decks and
    screenshots, that neither text extraction nor the image pipeline ever read.
    """
    magic = _magic(path, 8)
    if magic.startswith(b"%PDF"):
        return "application/pdf"
    if magic.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if magic.startswith(b"\x89PNG"):
        return "image/png"
    if magic.startswith(b"PK\x03\x04"):
        import zipfile

        # Any failure to read the directory means the bytes do not say, so the
        # sniff answers None. zipfile raises more than BadZipFile on a crafted
        # zip: a name flagged UTF-8 that holds invalid bytes raises
        # UnicodeDecodeError, and letting that out aborts the whole sync from
        # the registrar and makes the file a poison row for the extractor.
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
        except Exception:
            return None
        for folder, mime in _OOXML_BY_FOLDER:
            if any(n.startswith(folder) for n in names):
                return mime
        return "application/zip"
    if magic.startswith(b"\xd0\xcf\x11\xe0"):
        streams = _ole_stream_names(path)
        if any(n.startswith("__substg1.0_") for n in streams):
            return "application/vnd.ms-outlook"
        for stream, mime in _OLE_BY_STREAM:
            if stream in streams:
                return mime
    return None


# Sector numbers above this are markers (end of chain, free, FAT, DIFAT), not sectors.
_OLE_MAXREGSECT = 0xFFFFFFFA
_OLE_ENDOFCHAIN = 0xFFFFFFFE
# A real directory holds tens of entries, an Outlook item a few thousand; past this the walk
# stops with the names it has. A DIFAT sector lists 127 FAT sectors (8 MB of a 512-byte-sector
# file), so this many describe 32 GB.
_OLE_MAX_DIRECTORY_ENTRIES = 65_536
_OLE_MAX_DIFAT_SECTORS = 4096


def _ole_stream_names(path: str) -> set[str]:
    """Every name in an OLE2 file's directory, or an empty set when it cannot be read.

    The header fixes the sector size (a power of two at offset 30), the number of FAT sectors
    (offset 44), the first directory sector (offset 48) and where the FAT sectors are: the first
    109 at offset 76, any more in the DIFAT chain that starts at offset 68. The directory is a
    chain of sectors linked through the FAT, and each 128-byte entry holds a UTF-16LE name whose
    byte length sits at offset 64. The whole chain is read. Until 2026-10 only its first sector
    was, which holds four entries in a 512-byte file: the names that say a file is encrypted
    and how (DRMEncryptedDataSpace, EncryptionInfo, EncryptedPackage) sit deeper, so 1,436
    encrypted files reached the readers and were recorded as failures. A broken chain ends the
    walk with the names read so far; a chain that loops (directory or DIFAT) ends where it
    repeats; and the walk stops at _OLE_MAX_DIRECTORY_ENTRIES entries and
    _OLE_MAX_DIFAT_SECTORS DIFAT sectors, so a crafted header cannot make it read without end.
    """
    import struct

    names: set[str] = set()
    try:
        with open(path, "rb") as f:
            header = f.read(512)
            if len(header) < 512:
                return names
            (shift,) = struct.unpack_from("<H", header, 30)
            if shift not in (9, 12):
                return names
            size, per = 1 << shift, (1 << shift) // 4
            fat_count, first_dir = struct.unpack_from("<II", header, 44)
            difat_sector, difat_count = struct.unpack_from("<II", header, 68)
            fat_sectors = list(struct.unpack_from("<109I", header, 76))
            seen: set[int] = set()
            while (
                len(fat_sectors) < fat_count
                and len(seen) < min(difat_count, _OLE_MAX_DIFAT_SECTORS)
                and difat_sector <= _OLE_MAXREGSECT
                and difat_sector not in seen
            ):
                seen.add(difat_sector)
                f.seek((difat_sector + 1) << shift)
                block = f.read(size)
                if len(block) < size:
                    break
                entries = struct.unpack(f"<{per}I", block)
                fat_sectors.extend(entries[:-1])
                difat_sector = entries[-1]
            fat_sectors = fat_sectors[:fat_count]
            fat_cache: dict[int, tuple[int, ...]] = {}

            def next_sector(sector: int) -> int:
                index, slot = divmod(sector, per)
                if index >= len(fat_sectors) or fat_sectors[index] > _OLE_MAXREGSECT:
                    return _OLE_ENDOFCHAIN
                if index not in fat_cache:
                    f.seek((fat_sectors[index] + 1) << shift)
                    block = f.read(size)
                    if len(block) < size:
                        return _OLE_ENDOFCHAIN
                    fat_cache[index] = struct.unpack(f"<{per}I", block)
                return fat_cache[index][slot]

            sector, visited = first_dir, set[int]()
            while (
                sector <= _OLE_MAXREGSECT
                and sector not in visited
                and len(visited) * (size // 128) < _OLE_MAX_DIRECTORY_ENTRIES
            ):
                visited.add(sector)
                f.seek((sector + 1) << shift)
                block = f.read(size)
                for offset in range(0, len(block) - 127, 128):
                    (length,) = struct.unpack_from("<H", block, offset + 64)
                    if 2 <= length <= 64:
                        names.add(
                            block[offset : offset + length - 2].decode("utf-16-le", "replace")
                        )
                if len(block) < size:
                    break
                sector = next_sector(sector)
    except OSError:
        return names
    return names


# Directory names that say an Office file is encrypted, and how ([MS-OFFCRYPTO] 2.2.11, 2.3.4).
# Rights Management keeps an Office 2007+ file as EncryptedPackage under a DRMEncrypted data
# space, and a 97-2003 one as \tDRMContent under a \tDRMDataSpace. Password encryption keeps
# EncryptionInfo beside EncryptedPackage, which rights management never writes.
_RMS_NAMES = frozenset(
    {
        "DRMEncryptedDataSpace",
        "DRMEncryptedTransform",
        "\tDRMDataSpace",
        "\tDRMTransform",
        "\tDRMContent",
    }
)
_ENCRYPTED_PAYLOAD_NAMES = frozenset({"EncryptedPackage", "\x06DataSpaces"})


def encrypted_result(method: str, ext: str) -> dict:
    """The verdict for a file encrypted at rest. method: 'rms', 'rms-message' or 'password'.

    status 'encrypted' is a contract: the sweep keeps such a file, the health check counts it
    apart from failures, and Phase 2 never sees it, since it selects 'extracted' rows only. The
    error names the kind and carries none of the markers (file_sweep.UNREAD_SQL) that mean the
    bytes were never read: they were, and they are ciphertext.
    """
    what = ext or "file"
    reasons = {
        "rms": f"Rights-protected (RMS) {what}: encrypted at rest, "
        "cannot be read without the issuer's rights",
        "rms-message": "Rights-protected message (.rpmsg, MSIPC): encrypted at rest, "
        "cannot be read without the sender's rights",
        "password": f"Password-protected {what}: encrypted, cannot be read without its password",
    }
    return {"text": None, "method": method, "status": "encrypted", "error": reasons[method]}


def encryption_of(path: str) -> dict | None:
    """The 'encrypted' verdict for a file whose bytes are encrypted at rest, or None.

    Decided by the bytes alone, never by the name or the declared type: senders label these
    files application/encrypted, octet-stream or an ordinary Office type, and name them .xlsx
    whatever they hold. An MSIPC container is a protected message; an OLE2 container is
    encrypted when its directory says so; a PDF when it cannot be opened without a password (an
    owner password alone only restricts printing and copying, and the text reads). A legacy
    workbook's password sits inside its BIFF stream, so _extract_xls reports that one.
    """
    ext = Path(path).suffix.lower()
    magic = _magic(path, 8)
    if magic.startswith(_RPMSG_MAGIC):
        return encrypted_result("rms-message", ext)
    if magic.startswith(b"\xd0\xcf\x11\xe0"):
        names = _ole_stream_names(path)
        if names & _RMS_NAMES:
            return encrypted_result("rms", ext)
        if "EncryptionInfo" in names and names & _ENCRYPTED_PAYLOAD_NAMES:
            return encrypted_result("password", ext)
        if names & _ENCRYPTED_PAYLOAD_NAMES:
            return encrypted_result("rms", ext)
        return None
    if magic.startswith(b"%PDF"):
        import fitz

        try:
            doc = fitz.open(path)
            locked = bool(doc.needs_pass)
            doc.close()
        except Exception:
            return None  # a damaged PDF is the reader's to report
        return encrypted_result("password", ext) if locked else None
    return None


def _extract_excel(path: str) -> dict:
    """Extract every row of every sheet from .xlsx files via openpyxl.

    .xlsb and .xls are dispatched to dedicated parsers (_extract_xlsb,
    _extract_xls) before reaching this function — see extract_text_from_file.
    """
    import openpyxl

    parts = []
    # A handle, not the path: openpyxl refuses a path whose extension it does
    # not know, and a workbook identified by its bytes may have none.
    with open(path, "rb") as fh:
        wb = openpyxl.load_workbook(fh, read_only=True, data_only=True)

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            # Read-only mode stops at the size the sheet declares, and real files understate it.
            if hasattr(ws, "reset_dimensions"):
                ws.reset_dimensions()
            rows_text = []
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) if c is not None else "" for c in row]
                if any(c.strip() for c in cells):
                    rows_text.append(" | ".join(c for c in cells if c.strip()))

            if rows_text:
                parts.append(f"--- Sheet: {sheet_name} ---\n" + "\n".join(rows_text))

        wb.close()

    text = _truncate("\n\n".join(parts))
    if _apply_noise_filter(text):
        return {
            "text": None,
            "method": "openpyxl",
            "status": "skipped",
            "error": "Insufficient text extracted",
        }
    return {"text": text, "method": "openpyxl", "status": "extracted", "error": None}


def _extract_xls(path: str) -> dict:
    """Extract every row of every sheet from legacy .xls files.

    xlrd 2.0+ dropped .xlsx support and now handles only the legacy BIFF
    .xls format. Mirrors _extract_excel's shape (a header per sheet) so
    downstream LLM extraction sees the same structure regardless of source format.
    """
    import xlrd

    try:
        wb = xlrd.open_workbook(path)
    except Exception as e:
        # A FILEPASS record after the workbook's BOF: the sheets are RC4-encrypted under a
        # password (Excel's default one included), which xlrd does not decrypt. 6 rows.
        if str(e) == "Workbook is encrypted":
            return encrypted_result("password", Path(path).suffix.lower())
        return {
            "text": None,
            "method": "xlrd",
            "status": "failed",
            "error": f"xlrd open failed: {e}",
        }

    parts = []
    for sheet in wb.sheets():
        rows_text = []
        for row_idx in range(sheet.nrows):
            cells = [str(c) if c is not None else "" for c in sheet.row_values(row_idx)]
            if any(c.strip() for c in cells):
                rows_text.append(" | ".join(c for c in cells if c.strip()))
        if rows_text:
            parts.append(f"--- Sheet: {sheet.name} ---\n" + "\n".join(rows_text))

    text = _truncate("\n\n".join(parts))
    if _apply_noise_filter(text):
        return {
            "text": None,
            "method": "xlrd",
            "status": "skipped",
            "error": "Insufficient text extracted",
        }
    return {"text": text, "method": "xlrd", "status": "extracted", "error": None}


def _extract_xlsb(path: str) -> dict:
    """Extract every row of every sheet from .xlsb (Excel binary) files.

    pyxlsb is the only mature Python reader for the .xlsb format; it reads rows as a stream.
    """
    import pyxlsb

    parts = []

    try:
        with pyxlsb.open_workbook(path) as wb:
            for sheet_name in wb.sheets:
                rows_text = []
                with wb.get_sheet(sheet_name) as sheet:
                    for row in sheet.rows():
                        cells = [
                            str(cell.v) if cell is not None and cell.v is not None else ""
                            for cell in row
                        ]
                        if any(c.strip() for c in cells):
                            rows_text.append(" | ".join(c for c in cells if c.strip()))
                if rows_text:
                    parts.append(f"--- Sheet: {sheet_name} ---\n" + "\n".join(rows_text))
    except Exception as e:
        # Many real-world files have a .xlsb extension but are actually OLE
        # compound documents (legacy .xls magic d0cf11e0...). pyxlsb errors
        # with "File is not a zip file" on those. Detect and fall through to
        # xlrd, which handles the OLE BIFF format. If xlrd also fails, return
        # the original pyxlsb error since that's the user-facing classification.
        if "not a zip file" in str(e).lower():
            xls_result = _extract_xls(path)
            if xls_result["status"] == "extracted":
                # Tag method so downstream debugging can see the extension/format mismatch.
                xls_result["method"] = "xlrd (fallback from .xlsb)"
                return xls_result
            if xls_result["status"] == "encrypted":
                return xls_result
            # OLE compound document with no Excel workbook stream — likely a
            # custom export format (ACME financial tools, etc.) that wraps
            # binary content in OLE but has no spreadsheet payload. No library
            # will rescue this as Excel data; classify as skipped (not failed)
            # so future retries don't waste cycles on it.
            if "find workbook" in (xls_result.get("error") or "").lower():
                return {
                    "text": None,
                    "method": "pyxlsb→xlrd",
                    "status": "skipped",
                    "error": "OLE compound document, no Excel workbook stream — likely custom export format, not standard Excel",
                }
        return {
            "text": None,
            "method": "pyxlsb",
            "status": "failed",
            "error": f"pyxlsb open failed: {e}",
        }

    text = _truncate("\n\n".join(parts))
    if _apply_noise_filter(text):
        return {
            "text": None,
            "method": "pyxlsb",
            "status": "skipped",
            "error": "Insufficient text extracted",
        }
    return {"text": text, "method": "pyxlsb", "status": "extracted", "error": None}


def _ocr_frames(img, budget: float) -> dict:
    """OCR every page of a multi-page TIFF, inside the scan budget, like _ocr_pdf_pages."""
    import pytesseract
    from PIL import ImageSequence

    pages, note = [], None
    started = time.monotonic()
    for i, frame in enumerate(ImageSequence.Iterator(img)):
        if i and time.monotonic() - started > budget:
            note = f"time budget spent, {img.n_frames - i} pages left unread"
            break
        page = pytesseract.image_to_string(frame.convert("RGB"), lang=OCR_LANGS)
        if page.strip():
            pages.append(page.strip())
    text = _truncate("\n\n".join(pages))
    if _apply_noise_filter(text):
        return {
            "text": None,
            "method": "ocr",
            "status": "skipped",
            "error": f"{OCR_INSUFFICIENT}; {note}" if note else OCR_INSUFFICIENT,
        }
    return {"text": text, "method": "ocr", "status": "extracted", "error": note}


def _extract_image_ocr(path: str, ocr_seconds: float | None = None) -> dict:
    """Extract text from images using Tesseract OCR; every page of a multi-page TIFF."""
    try:
        import io

        import pytesseract
        from PIL import Image
    except ImportError:
        return {
            "text": None,
            "method": "ocr",
            "status": "failed",
            "error": "pytesseract or Pillow not installed",
        }

    # pytesseract rejects any image whose PIL .format isn't in its allowlist
    # (JPEG, PNG, GIF, BMP, TIFF, WEBP, PPM). Phone-camera JPEGs are often
    # encoded as MPO (Multi-Picture Object) — a JPEG container with multiple
    # frames — and tesseract raises TypeError: Unsupported image format/type.
    # Re-encode through PNG to drop the container and yield a clean format.
    _TESSERACT_SAFE_FORMATS = {"JPEG", "PNG", "GIF", "BMP", "TIFF", "WEBP", "PPM"}

    try:
        img = Image.open(path)
        if img.format == "TIFF" and getattr(img, "n_frames", 1) > 1:
            return _ocr_frames(img, OCR_MAX_SECONDS if ocr_seconds is None else ocr_seconds)
        if img.format not in _TESSERACT_SAFE_FORMATS:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            buf.seek(0)
            img = Image.open(buf)
        text = pytesseract.image_to_string(img, lang=OCR_LANGS)
        text = _truncate(text)

        if _apply_noise_filter(text):
            return {
                "text": None,
                "method": "ocr",
                "status": "skipped",
                "error": OCR_INSUFFICIENT,
            }
        return {"text": text, "method": "ocr", "status": "extracted", "error": None}
    except Exception as e:
        return {
            "text": None,
            "method": "ocr",
            "status": "failed",
            "error": f"{type(e).__name__}: {str(e)[:200]}",
        }


def _ocr_pdf_pages(path: str, seconds: float | None = None) -> dict:
    """Render each PDF page to a PIL image and OCR it. Used as fallback for
    scanned PDFs when PyMuPDF text extraction yields too little content.

    200 DPI is the sweet spot for printed text: enough resolution for
    Tesseract to recognize Greek + English glyphs reliably, low enough that
    a typical 2-page scan completes in 3-10 seconds. Every page is read until
    `seconds` (OCR_MAX_SECONDS when None) is spent; then the pages read are kept
    and the error says how many were left unread.
    """
    import io

    import fitz
    import pytesseract
    from PIL import Image

    budget = OCR_MAX_SECONDS if seconds is None else seconds
    note = None
    try:
        doc = fitz.open(path)
        pages_text = []
        started = time.monotonic()
        for page_num, page in enumerate(doc):
            if page_num and time.monotonic() - started > budget:
                note = f"time budget spent, {doc.page_count - page_num} pages left unread"
                break
            pix = page.get_pixmap(dpi=200)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            page_text = pytesseract.image_to_string(img, lang=OCR_LANGS)
            if page_text.strip():
                pages_text.append(page_text.strip())
        doc.close()
    except Exception as e:
        return {
            "text": None,
            "method": "pymupdf+tesseract",
            "status": "failed",
            "error": f"{type(e).__name__}: {str(e)[:200]}",
        }

    text = _truncate("\n\n".join(pages_text))
    if _apply_noise_filter(text):
        return {
            "text": None,
            "method": "pymupdf+tesseract",
            "status": "skipped",
            "error": f"{OCR_INSUFFICIENT}; {note}" if note else OCR_INSUFFICIENT,
        }
    return {
        "text": text,
        "method": "pymupdf+tesseract",
        "status": "extracted",
        "error": note,
    }


def _extract_eml(path: str) -> dict:
    """Parse .eml files to extract headers and body text."""
    with open(path, "rb") as f:
        msg: EmailMessage = BytesParser(policy=policy.default).parse(f)  # type: ignore[assignment]

    parts = []
    parts.append(f"Subject: {msg.get('subject', 'N/A')}")
    parts.append(f"From: {msg.get('from', 'N/A')}")
    parts.append(f"Date: {msg.get('date', 'N/A')}")
    parts.append("")

    body = msg.get_body(preferencelist=("plain", "html"))
    if body:
        content = body.get_content()
        if body.get_content_type() == "text/html":
            content = _strip_html_tags(content)
        parts.append(content)

    text = _truncate("\n".join(parts))
    if _apply_noise_filter(text):
        return {
            "text": None,
            "method": "email_parser",
            "status": "skipped",
            "error": "Insufficient text extracted",
        }
    return {
        "text": text,
        "method": "email_parser",
        "status": "extracted",
        "error": None,
    }


# The first bytes of a Rights-Protected Message. This is the MSIPC container
# format, NOT an OLE2 compound document (which begins d0cf11e0).
_RPMSG_MAGIC = b"\x76\xe8\x04\x60"


def _extract_rpmsg(path: str) -> dict:
    """Report an .rpmsg as permanently unreadable, because it is.

    This used to open the file with `compoundfiles` and salvage printable
    strings. It never once worked. Measured across the live corpus: 685 rows
    carry this method and ZERO of them hold a single character of extracted
    text. Every real file fails with CompoundFileInvalidMagicError, for the
    plain reason that .rpmsg is not an OLE2 compound document: the 687 files on
    the producer all begin 76e8 0460, the MSIPC magic, where OLE2 begins d0cf11e0.

    So the previous code could not have succeeded on any input, and the
    dependency it needed was the only source-only package in the tree, built
    from setup.py on every install on every host. Removing it is what lets the
    dependency install refuse to build source distributions at all.

    The honest verdict is 'encrypted' (method 'rms-message'), the one every
    rights-protected file gets: the payload is encrypted, and no parser reads it
    without rights. It is not a fault, so it does not count against the
    extraction failure rate. The dispatcher records it from the magic before it
    gets here (encryption_of); this answers the same when called directly.
    """
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
    except OSError as e:
        return {
            "text": None,
            "method": "rpmsg",
            "status": "failed",
            "error": f"{type(e).__name__}: {str(e)[:200]}",
        }

    if magic == _RPMSG_MAGIC:
        return encrypted_result("rms-message", ".rpmsg")
    return {
        "text": None,
        "method": "rpmsg",
        "status": "skipped",
        "error": f"unrecognised .rpmsg container (magic {magic.hex()}); not MSIPC",
    }


def _copy_at_most(src, dst, limit: int) -> int | None:
    """Copy at most `limit` bytes; None when the source holds more than its header said."""
    written = 0
    while chunk := src.read(1 << 16):
        written += len(chunk)
        if written > limit:
            return None
        dst.write(chunk)
    return written


def _member_outcome(name: str, result: dict) -> tuple[list[str], list[str]]:
    """A member's text for its archive, and the note it leaves: its error when it gave no text,
    or its mark when it was read in part, which the archive keeps so the sweep does not take it
    for read in full."""
    error = result.get("error") or ""
    if result.get("text"):
        in_part = "left unread" in error or "file kept" in error
        return [f"=== {name} ===\n{result['text']}"], [f"{name}: {error}"] if in_part else []
    return [], [f"{name}: {error}"] if error else []


def _extract_zip(path: str, depth: int, seconds: float, ocr_seconds: float | None = None) -> dict:
    """Unpack an archive into a temporary directory and extract every member.

    Nothing outlives the call. A member is written under a name made up here, never under the
    name the archive carries, so a member called "../x" cannot land outside the directory.
    """
    import mimetypes
    import tempfile
    import zipfile

    if depth > ZIP_MAX_DEPTH:
        return {
            "text": None,
            "method": "zip",
            "status": "skipped",
            "error": f"nested archive deeper than {ZIP_MAX_DEPTH} level; unread, file kept",
        }
    parts: list[str] = []
    notes: list[str] = []
    with zipfile.ZipFile(path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > ZIP_MAX_MEMBERS:
            return {
                "text": None,
                "method": "zip",
                "status": "skipped",
                "error": f"{len(infos)} members, more than {ZIP_MAX_MEMBERS}; unread, file kept",
            }
        total = sum(i.file_size for i in infos)
        if total > ZIP_MAX_TOTAL_BYTES:
            return {
                "text": None,
                "method": "zip",
                "status": "skipped",
                "error": (
                    f"{total} bytes uncompressed, more than {ZIP_MAX_TOTAL_BYTES}; unread, file kept"
                ),
            }
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="sb-zip-") as tmp:
            for n, info in enumerate(infos):
                if n and time.monotonic() - started > seconds:
                    notes.append(f"time budget spent, {len(infos) - n} members left unread")
                    break
                name = info.filename
                if info.flag_bits & 0x1:
                    notes.append(f"{name}: encrypted, skipped")
                    continue
                if info.file_size > ZIP_MAX_RATIO * max(info.compress_size, 1):
                    notes.append(
                        f"{name}: compression ratio over {ZIP_MAX_RATIO}; unread, file kept"
                    )
                    continue
                member = Path(tmp) / f"{n}{Path(name).suffix.lower()}"
                # One member's fault (a bad CRC, an unsupported method such as Deflate64) is
                # named and skipped; the readable members are not thrown away with it.
                try:
                    with zf.open(info) as src, open(member, "wb") as dst:
                        copied = _copy_at_most(src, dst, info.file_size)
                    if copied is None:
                        notes.append(f"{name}: larger than its header says, skipped")
                        continue
                    mime = mimetypes.guess_type(name.lower())[0] or ""
                    result = extract_text_from_file(
                        str(member), mime, depth + 1, zip_seconds=seconds, ocr_seconds=ocr_seconds
                    )
                except Exception as e:
                    notes.append(f"{name}: {type(e).__name__}: {str(e)[:200]}")
                    continue
                text, said = _member_outcome(name, result)
                parts.extend(text)
                notes.extend(said)
    error = "; ".join(notes) or None
    if not parts:
        return {
            "text": None,
            "method": "zip",
            "status": "skipped",
            "error": error or "no readable members",
        }
    return {
        "text": _truncate("\n\n".join(parts)),
        "method": "zip",
        "status": "extracted",
        "error": error,
    }


class _InflatesTooFar(ValueError):
    pass


class _InflateBudget:
    """The bytes one file may still inflate to, across all of its compressed layers.

    zlib is asked for at most what is left (decompressobj with max_length), never for the
    whole stream, so a bomb costs no more memory than the budget.
    """

    def __init__(self, limit: int | None = None):
        self.limit = INFLATE_MAX_BYTES if limit is None else limit
        self.left = self.limit

    def inflate(self, data: bytes, wbits: int = zlib.MAX_WBITS) -> bytes:
        out = zlib.decompressobj(wbits).decompress(data, self.left + 1)
        if len(out) > self.left:
            raise _InflatesTooFar(f"inflates past {self.limit:,} bytes")
        self.left -= len(out)
        return out


def _unpack(data: bytes, budget: _InflateBudget) -> bytes | None:
    """The OLE2 file in a part packed as a 4-byte length and a zlib stream, or None."""
    if len(data) < 6 or data[4] != 0x78:
        return None
    try:
        inflated = budget.inflate(data[4:])
    except zlib.error:
        return None
    return inflated if inflated.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") else None


def _root_streams(ole: bytes):
    """(xlrd compound document, names of the streams directly under its root)."""
    from xlrd import compdoc

    doc = compdoc.CompDoc(ole, logfile=io.StringIO())
    return doc, [doc.dirlist[i].name for i in doc.dirlist[0].children if doc.dirlist[i].etype == 2]


def _read_embedded_object(
    ole: bytes, stem: Path, depth: int, zip_seconds: float | None, ocr_seconds: float | None
) -> dict:
    """One embedded object, read by the reader its payload calls for.

    A Package stream is an Office 2007+ file, written out whole and read by its bytes. A
    Workbook (or Book) stream is a BIFF workbook, which xlrd finds inside the object's own
    OLE2 file, so the object is written out as it is.
    """
    doc, names = _root_streams(ole)
    if "Package" in names:
        stem.write_bytes(doc.get_named_stream("Package"))
        mime = ""
    elif "Workbook" in names or "Book" in names:
        stem = stem.with_suffix(".xls")
        stem.write_bytes(ole)
        mime = "application/vnd.ms-excel"
    else:
        return {
            "text": None,
            "method": None,
            "status": "skipped",
            "error": "no workbook or package inside",
        }
    return extract_text_from_file(
        str(stem), mime, depth + 1, zip_seconds=zip_seconds, ocr_seconds=ocr_seconds
    )


def _extract_mso(
    path: str, depth: int, zip_seconds: float | None = None, ocr_seconds: float | None = None
) -> dict:
    """Read the objects in an Office object container (.mso), each with its own reader.

    Outlook keeps the objects pasted into an HTML mail, most often Excel charts, in an
    oledata.mso part: a 4-byte length and a zlib stream holding an OLE2 file, whose root
    streams each hold one object packed the same way. On the producer all 255 had that shape,
    and their 349 objects were BIFF chart workbooks or Office 2007+ packages. Each object is
    read like an archive member, in a temporary directory nothing outlives; one that cannot be
    read is named in the error and the rest are kept.
    """
    import tempfile

    budget = _InflateBudget()
    with open(path, "rb") as f:
        data = f.read(budget.limit + 1)
    try:
        container = _unpack(data, budget)
    except _InflatesTooFar as e:
        return {
            "text": None,
            "method": "mso",
            "status": "skipped",
            "error": f"container {e}; unread, file kept",
        }
    if container is None:
        return {
            "text": None,
            "method": "mso",
            "status": "skipped",
            "error": "not an Office object container: no packed OLE2 file inside",
        }
    doc, streams = _root_streams(container)
    parts: list[str] = []
    notes: list[str] = []
    with tempfile.TemporaryDirectory(prefix="sb-mso-") as tmp:
        for n, name in enumerate(streams, 1):
            label = f"embedded object {n}"
            try:
                obj = _unpack(doc.get_named_stream(name), budget)
                if obj is None:
                    notes.append(f"{label}: not a packed OLE2 object")
                    continue
                result = _read_embedded_object(
                    obj, Path(tmp) / str(n), depth, zip_seconds, ocr_seconds
                )
            except _InflatesTooFar as e:
                notes.append(f"{label}: {e}; it and {len(streams) - n} more unread, file kept")
                break
            except Exception as e:
                notes.append(f"{label}: {type(e).__name__}: {str(e)[:200]}")
                continue
            text, said = _member_outcome(label, result)
            parts.extend(text)
            notes.extend(said)
    error = "; ".join(notes) or None
    if not parts:
        return {
            "text": None,
            "method": "mso",
            "status": "skipped",
            "error": error or "no embedded objects",
        }
    return {
        "text": _truncate("\n\n".join(parts)),
        "method": "mso",
        "status": "extracted",
        "error": error,
    }


def _extract_plain_text(path: str) -> dict:
    """Read plain text, CSV, or markdown files directly."""
    for encoding in ("utf-8", "latin-1", "cp1253"):
        try:
            with open(path, encoding=encoding) as f:
                text = f.read()
            text = _truncate(text)
            if _apply_noise_filter(text):
                return {
                    "text": None,
                    "method": "direct_read",
                    "status": "skipped",
                    "error": "Insufficient text",
                }
            return {
                "text": text,
                "method": "direct_read",
                "status": "extracted",
                "error": None,
            }
        except UnicodeDecodeError:
            continue
    return {
        "text": None,
        "method": "direct_read",
        "status": "failed",
        "error": "Could not decode file with any supported encoding",
    }


def _extract_html(path: str) -> dict:
    """Read HTML files and strip tags."""
    for encoding in ("utf-8", "latin-1", "cp1253"):
        try:
            with open(path, encoding=encoding) as f:
                raw = f.read()
            text = _strip_html_tags(raw)
            text = _truncate(text)
            if _apply_noise_filter(text):
                return {
                    "text": None,
                    "method": "direct_read",
                    "status": "skipped",
                    "error": "Insufficient text",
                }
            return {
                "text": text,
                "method": "direct_read",
                "status": "extracted",
                "error": None,
            }
        except UnicodeDecodeError:
            continue
    return {
        "text": None,
        "method": "direct_read",
        "status": "failed",
        "error": "Could not decode file",
    }


def _strip_html_tags(raw_html: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r"<style[^>]*>.*?</style>", "", raw_html, flags=re.DOTALL)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _truncate(text: str, max_chars: int | None = None) -> str:
    """Truncate text to max_chars, MAX_TEXT_CHARS when None (read at call time)."""
    limit = MAX_TEXT_CHARS if max_chars is None else max_chars
    if len(text) > limit:
        return text[:limit]
    return text


def _apply_noise_filter(text: str) -> bool:
    """Return True if text should be filtered out (noise).

    Filters: too short (<50 chars) or >90% non-alphanumeric.
    """
    stripped = (text or "").strip()
    if len(stripped) < MIN_TEXT_CHARS:
        return True
    alnum = sum(1 for c in stripped if c.isalnum())
    if alnum / len(stripped) < 0.1:
        return True
    return False
