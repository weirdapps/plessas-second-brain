"""Local text extraction from attachment files.

Extracts text from PDF, Word, PowerPoint, Excel, images (OCR),
.eml, .rpmsg, and plain text files. No API calls — all local.
"""

import html
import os
import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

# A ceiling against runaway input, not a cap real documents reach: text is stored in full and
# Phase 2 summarises a long one in parts (src/extract/attachment_pipeline.py).
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

SKIP_EXTENSIONS = {".mp4", ".mp3", ".wav", ".rar", ".7z", ".gz", ".mso", ".wmz"}

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
ZIP_MIME_TYPES = frozenset({"application/zip", "application/x-zip-compressed"})


def extract_text_from_file(
    file_path: str, mime_type: str, _depth: int = 0, *, zip_seconds: float | None = None
) -> dict:
    """Extract text from a file based on its MIME type.

    Returns dict with keys: text, method, status, error.
    status is one of: 'extracted', 'partial', 'failed', 'skipped'.
    _depth is how deep inside archives this file sits; only _extract_zip passes it.
    zip_seconds is how long an archive may spend reading members, ZIP_MAX_SECONDS when None;
    reextract passes math.inf, since a one-time recovery run has the time.
    """
    ext = Path(file_path).suffix.lower()

    # Skip unsupported types
    if ext in SKIP_EXTENSIONS:
        return {"text": None, "method": None, "status": "skipped", "error": None}

    # A declared archive or media type is a claim, and senders make it wrongly:
    # 60 .docx and 15 .pptx on the replica arrived labelled application/zip and
    # were skipped here unread, although every one of them opens. So the bytes
    # get the last word. A genuine archive still sniffs as application/zip (or
    # as nothing) and is skipped exactly as before.
    if mime_type in SKIP_MIME_TYPES:
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

    # IRM/RMS-protected content arrives with mime application/encrypted but an
    # ordinary .xlsx/.docx/.pptx name, so the extension branches below claimed
    # it first and the zip-based readers logged "BadZipFile: File is not a zip
    # file" as a hard FAILURE — 750 rows on the live DB. The bytes are encrypted
    # at rest: no parser reads them without IRM rights, so this is a permanent
    # skip, not a fault worth re-counting. .rpmsg still falls through to
    # _extract_rpmsg, which recovers best-effort metadata from the OLE wrapper.
    if mime_type == "application/encrypted" and ext != ".rpmsg":
        return {
            "text": None,
            "method": None,
            "status": "skipped",
            "error": f"IRM-protected {ext or 'file'}: no extractable text without rights",
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
        # Rights protection is also stored as an OLE2 container holding \x06DataSpaces, whatever
        # the declared type says; the check above sees only application/encrypted. Sent to the
        # legacy readers, such a workbook was recorded as failed ("Can't find workbook in OLE2
        # compound document"). \x06DataSpaces sits in the first directory sector, where
        # _ole_stream_names reads; EncryptedPackage often does not.
        if (
            ext != ".rpmsg"
            and _magic(file_path) == b"\xd0\xcf\x11\xe0"
            and "\x06DataSpaces" in _ole_stream_names(file_path)
        ):
            return {
                "text": None,
                "method": None,
                "status": "skipped",
                "error": f"IRM-protected {ext or 'file'}: no extractable text without rights",
            }
        if ext == ".zip" or mime_type in ZIP_MIME_TYPES:
            sniffed = sniff_mime_type(file_path)
            if sniffed == "application/zip":
                seconds = ZIP_MAX_SECONDS if zip_seconds is None else zip_seconds
                return _extract_zip(file_path, _depth, seconds)
            if sniffed is None:
                return {"text": None, "method": None, "status": "skipped", "error": None}
            mime_type = sniffed  # an Office document sent as a zip
        if mime_type == "application/pdf" or ext == ".pdf":
            return _extract_pdf(file_path)
        elif (
            mime_type
            in ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",)
            or ext == ".docx"
        ):
            return _extract_docx(file_path)
        elif mime_type == "application/msword" or ext == ".doc":
            return _extract_doc(file_path)
        elif (
            mime_type
            in ("application/vnd.openxmlformats-officedocument.presentationml.presentation",)
            or ext == ".pptx"
        ):
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
        ):
            # Sniff, do not trust the extension. 993 attachments on the live
            # corpus are named .xlsx and are legacy OLE2 .xls: Excel keeps the
            # name when a user saves an old workbook, and mail systems relabel
            # freely. openpyxl raises BadZipFile on those, the row is marked
            # failed, and nothing retries it, so their content was simply absent
            # from the brain. The same two-magic-number check already guards
            # _extract_xlsb below; this is the branch it was missing.
            if _magic(file_path) == b"\xd0\xcf\x11\xe0":
                return _extract_xls(file_path)
            return _extract_excel(file_path)
        elif (
            mime_type
            and mime_type.startswith("image/")
            or ext in (".png", ".jpg", ".jpeg", ".gif", ".tiff", ".tif", ".bmp", ".jfif")
        ):
            return _extract_image_ocr(file_path)
        elif mime_type == "message/rfc822" or ext == ".eml":
            return _extract_eml(file_path)
        elif mime_type == "application/encrypted" or ext == ".rpmsg":
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
                return extract_text_from_file(file_path, sniffed, _depth, zip_seconds=zip_seconds)
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


def _extract_pdf(path: str) -> dict:
    """Extract text from PDF using PyMuPDF, with OCR fallback."""
    import fitz

    doc = fitz.open(path)
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
        ocr_result = _ocr_pdf_pages(path)
        if ocr_result["status"] == "extracted" and len(ocr_result["text"] or "") > len(
            text.strip()
        ):
            return ocr_result
        if _apply_noise_filter(text):
            return {
                "text": None,
                "method": "pymupdf",
                "status": "skipped",
                "error": "Insufficient text extracted",
            }

    return {"text": text, "method": "pymupdf", "status": "extracted", "error": None}


def _extract_docx(path: str) -> dict:
    """Extract text from .docx using python-docx."""
    from docx import Document

    doc = Document(path)
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
    """Extract text from PowerPoint using python-pptx."""
    from pptx import Presentation

    prs = Presentation(path)
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


def _ole_stream_names(path: str) -> set[str]:
    """Names in the first directory sector of an OLE2 file, or an empty set.

    The header fixes the sector size (a power of two at offset 30) and the
    first directory sector (offset 48), and each 128-byte directory entry holds
    a UTF-16LE name whose byte length sits at offset 64. The streams that say
    which application wrote the file are children of the root entry, so they
    sit in that first sector; reading one sector avoids a parser dependency.
    """
    import struct

    try:
        with open(path, "rb") as f:
            header = f.read(512)
            if len(header) < 512:
                return set()
            (shift,) = struct.unpack_from("<H", header, 30)
            (first_dir,) = struct.unpack_from("<I", header, 48)
            if shift not in (9, 12):
                return set()
            f.seek((first_dir + 1) << shift)
            sector = f.read(1 << shift)
    except OSError:
        return set()
    names = set()
    for offset in range(0, len(sector) - 127, 128):
        (length,) = struct.unpack_from("<H", sector, offset + 64)
        if 2 <= length <= 64:
            names.add(sector[offset : offset + length - 2].decode("utf-16-le", "replace"))
    return names


def _extract_excel(path: str) -> dict:
    """Extract headers + first 50 rows per sheet from .xlsx files via openpyxl.

    .xlsb and .xls are dispatched to dedicated parsers (_extract_xlsb,
    _extract_xls) before reaching this function — see extract_text_from_file.
    """
    import openpyxl

    parts = []
    # A handle, not the path: openpyxl refuses a path whose extension it does
    # not know, and a workbook identified by its bytes may have none.
    with open(path, "rb") as fh:
        wb = openpyxl.load_workbook(fh, read_only=True, data_only=True)

        max_sheets = 20
        for _sheet_idx, sheet_name in enumerate(wb.sheetnames[:max_sheets]):
            ws = wb[sheet_name]
            rows_text = []
            row_count = 0
            for row in ws.iter_rows(max_row=51, values_only=True):
                cells = [str(c) if c is not None else "" for c in row]
                if any(c.strip() for c in cells):
                    rows_text.append(" | ".join(c for c in cells if c.strip()))
                row_count += 1
                if row_count >= 51:
                    break

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
    """Extract headers + first 50 rows per sheet from legacy .xls files.

    xlrd 2.0+ dropped .xlsx support and now handles only the legacy BIFF
    .xls format — perfect fit for our case. Mirrors _extract_excel's shape
    (sheet headers, max 20 sheets, max 51 rows per sheet) so downstream
    LLM extraction sees the same structure regardless of source format.
    """
    import xlrd

    try:
        wb = xlrd.open_workbook(path)
    except Exception as e:
        return {
            "text": None,
            "method": "xlrd",
            "status": "failed",
            "error": f"xlrd open failed: {e}",
        }

    parts = []
    max_sheets = 20
    for sheet in list(wb.sheets())[:max_sheets]:
        rows_text = []
        for row_idx in range(min(sheet.nrows, 51)):
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
    """Extract headers + first 50 rows per sheet from .xlsb (Excel binary) files.

    pyxlsb is the only mature Python reader for the .xlsb format. It loads
    the whole workbook (no read_only mode like openpyxl), but our per-sheet
    + per-row caps below bound the per-file work.
    """
    import pyxlsb

    parts = []
    max_sheets = 20

    try:
        with pyxlsb.open_workbook(path) as wb:
            for sheet_name in list(wb.sheets)[:max_sheets]:
                rows_text = []
                with wb.get_sheet(sheet_name) as sheet:
                    for row_idx, row in enumerate(sheet.rows()):
                        if row_idx >= 51:
                            break
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


def _extract_image_ocr(path: str) -> dict:
    """Extract text from images using Tesseract OCR."""
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
        if img.format not in _TESSERACT_SAFE_FORMATS:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            buf.seek(0)
            img = Image.open(buf)
        text = pytesseract.image_to_string(img, lang="eng+ell")
        text = _truncate(text)

        if _apply_noise_filter(text):
            return {
                "text": None,
                "method": "ocr",
                "status": "skipped",
                "error": "OCR returned insufficient text",
            }
        return {"text": text, "method": "ocr", "status": "extracted", "error": None}
    except Exception as e:
        return {
            "text": None,
            "method": "ocr",
            "status": "failed",
            "error": f"{type(e).__name__}: {str(e)[:200]}",
        }


def _ocr_pdf_pages(path: str, max_pages: int = 30) -> dict:
    """Render each PDF page to a PIL image and OCR it. Used as fallback for
    scanned PDFs when PyMuPDF text extraction yields too little content.

    200 DPI is the sweet spot for printed text — enough resolution for
    Tesseract to recognize Greek + English glyphs reliably, low enough that
    a typical 2-page scan completes in 3-10 seconds. Hard-cap at 30 pages
    to bound worst-case time on big documents (most ACME scans are <10).
    """
    import io

    import fitz
    import pytesseract
    from PIL import Image

    try:
        doc = fitz.open(path)
        pages_text = []
        for page_num, page in enumerate(doc):
            if page_num >= max_pages:
                break
            pix = page.get_pixmap(dpi=200)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            page_text = pytesseract.image_to_string(img, lang="eng+ell")
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
            "error": "OCR returned insufficient text",
        }
    return {
        "text": text,
        "method": "pymupdf+tesseract",
        "status": "extracted",
        "error": None,
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

    The honest verdict is a skip, matching how the dispatcher already treats
    other IRM-protected content: the payload is encrypted, and no parser reads
    it without rights. Marking it 'skipped' rather than 'failed' also stops it
    counting against the extraction failure rate, which is what a fault should
    mean.
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
        return {
            "text": None,
            "method": "rpmsg",
            "status": "skipped",
            "error": "IRM-protected message (MSIPC): encrypted, no extractable text without rights",
        }
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


def _extract_zip(path: str, depth: int, seconds: float) -> dict:
    """Unpack an archive into a temporary directory and extract every member.

    Nothing outlives the call. A member is written under a name made up here, never under the
    name the archive carries, so a member called "../x" cannot land outside the directory.
    """
    import mimetypes
    import tempfile
    import time
    import zipfile

    if depth > ZIP_MAX_DEPTH:
        return {
            "text": None,
            "method": "zip",
            "status": "skipped",
            "error": f"nested archive deeper than {ZIP_MAX_DEPTH} level",
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
                "error": f"{len(infos)} members, more than {ZIP_MAX_MEMBERS}",
            }
        total = sum(i.file_size for i in infos)
        if total > ZIP_MAX_TOTAL_BYTES:
            return {
                "text": None,
                "method": "zip",
                "status": "skipped",
                "error": f"{total} bytes uncompressed, more than {ZIP_MAX_TOTAL_BYTES}",
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
                    notes.append(f"{name}: compression ratio over {ZIP_MAX_RATIO}, skipped")
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
                        str(member), mime, depth + 1, zip_seconds=seconds
                    )
                except Exception as e:
                    notes.append(f"{name}: {type(e).__name__}: {str(e)[:200]}")
                    continue
                if result.get("text"):
                    parts.append(f"=== {name} ===\n{result['text']}")
                elif result.get("error"):
                    notes.append(f"{name}: {result['error']}")
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


def _truncate(text: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    """Truncate text to max_chars."""
    if len(text) > max_chars:
        return text[:max_chars]
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
