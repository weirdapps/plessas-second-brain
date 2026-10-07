"""Synthetic OLE2 compound files for the attachment tests: directory names only, no payload.

A real rights-protected or password-protected Office file is an OLE2 container whose directory
names say how it is encrypted. These builders write a valid version 3 container (512-byte
sectors, the FAT first, then the directory chain) holding nothing but those names, so a test can
put a name in a later directory sector, past the first one, where real files keep it.
"""

import struct
import zlib

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD
NOSTREAM = 0xFFFFFFFF
STORAGE = 1
STREAM = 2
ROOT = 5
SECTOR = 512
PER_FAT = SECTOR // 4

# Names [MS-OFFCRYPTO] gives the two kinds of encrypted Office 2007+ file. Root Entry and three
# of these fill the first directory sector, so the names that tell the kinds apart sit in the
# second, where the old one-sector reader never looked.
RMS_NAMES = [
    ("\x06DataSpaces", STORAGE),
    ("DataSpaceMap", STREAM),
    ("DataSpaceInfo", STORAGE),
    ("DRMEncryptedDataSpace", STREAM),
    ("TransformInfo", STORAGE),
    ("DRMEncryptedTransform", STORAGE),
    ("\x06Primary", STREAM),
    ("EncryptedPackage", STREAM),
]
PASSWORD_NAMES = [
    ("\x06DataSpaces", STORAGE),
    ("DataSpaceMap", STREAM),
    ("DataSpaceInfo", STORAGE),
    ("StrongEncryptionDataSpace", STREAM),
    ("TransformInfo", STORAGE),
    ("StrongEncryptionTransform", STORAGE),
    ("\x06Primary", STREAM),
    ("EncryptionInfo", STREAM),
    ("EncryptedPackage", STREAM),
]
# A 97-2003 document under rights management: its payload is \tDRMContent.
LEGACY_RMS_NAMES = [
    ("\x05SummaryInformation", STREAM),
    ("\x05DocumentSummaryInformation", STREAM),
    ("\x06DataSpaces", STORAGE),
    ("DataSpaceInfo", STORAGE),
    ("\tDRMDataSpace", STREAM),
    ("\tDRMContent", STREAM),
]


def _entry(name: str, kind: int, right: int, child: int, start: int, size: int) -> bytes:
    raw = bytearray(128)
    encoded = (name + "\0").encode("utf-16-le")
    raw[: len(encoded)] = encoded
    struct.pack_into("<HBB", raw, 64, len(encoded), kind, 1)
    struct.pack_into("<III", raw, 68, NOSTREAM, right, child)
    struct.pack_into("<II", raw, 116, start, size)
    return bytes(raw)


def ole_file(entries, *, pad_sectors: int = 0, cycle: bool = False) -> bytes:
    """A version 3 OLE2 file whose directory holds Root Entry and then `entries`.

    entries are (name, STORAGE or STREAM), or (name, STREAM, data) for a stream with content.
    Every entry hangs off the root as a chain of right siblings. A stream's data sits in whole
    sectors after the directory, chained through the FAT; the mini stream cutoff is 0, so even
    a small stream lives in ordinary sectors and no mini stream is needed. `pad_sectors` unused
    sectors sit between the FAT and the directory, to push the directory chain past the first
    FAT sector. With `cycle` the last directory sector points back at the first, as a damaged
    file can.
    """
    names = [
        ("Root Entry", ROOT, b""),
        *[(e[0], e[1], e[2] if len(e) > 2 else b"") for e in entries],
    ]
    dir_sectors = (len(names) + 3) // 4
    data_sectors = [-(-len(data) // SECTOR) for _name, _kind, data in names]
    fat_sectors = 1
    while fat_sectors * PER_FAT < fat_sectors + pad_sectors + dir_sectors + sum(data_sectors):
        fat_sectors += 1
    first_dir = fat_sectors + pad_sectors
    chain = list(range(first_dir, first_dir + dir_sectors))

    fat = [FREESECT] * (fat_sectors * PER_FAT)
    for i in range(fat_sectors):
        fat[i] = FATSECT
    for here, there in zip(chain, chain[1:], strict=False):
        fat[here] = there
    fat[chain[-1]] = chain[0] if cycle else ENDOFCHAIN

    starts, blob, sector = [], bytearray(), first_dir + dir_sectors
    for (_name, _kind, data), count in zip(names, data_sectors, strict=True):
        if not count:
            starts.append(ENDOFCHAIN)
            continue
        starts.append(sector)
        for k in range(count):
            fat[sector + k] = sector + k + 1 if k < count - 1 else ENDOFCHAIN
        sector += count
        blob += data + bytes(count * SECTOR - len(data))

    header = bytearray(SECTOR)
    header[:8] = OLE_MAGIC
    struct.pack_into("<HHHHH", header, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<II", header, 44, fat_sectors, first_dir)
    struct.pack_into("<IIIIII", header, 52, 0, 0, ENDOFCHAIN, 0, ENDOFCHAIN, 0)
    difat = [FREESECT] * 109
    difat[:fat_sectors] = range(fat_sectors)
    struct.pack_into("<109I", header, 76, *difat)

    directory = bytearray()
    for i, (name, kind, data) in enumerate(names):
        right = i + 1 if 0 < i < len(names) - 1 else NOSTREAM
        child = 1 if i == 0 and len(names) > 1 else NOSTREAM
        directory += _entry(name, kind, right, child, starts[i], len(data))
    directory += bytes(dir_sectors * SECTOR - len(directory))

    body = struct.pack(f"<{len(fat)}I", *fat) + bytes(pad_sectors * SECTOR) + bytes(directory)
    return bytes(header) + body + bytes(blob)


def _record(kind: int, data: bytes = b"") -> bytes:
    return struct.pack("<HH", kind, len(data)) + data


def biff8_workbook(sheet: str, rows: list[list[str]]) -> bytes:
    """A minimal BIFF8 Workbook stream with one worksheet of text cells, which xlrd reads.

    Globals (BOF, CODEPAGE, BOUNDSHEET, EOF), then the sheet (BOF, DIMENSIONS, a LABEL per
    cell, EOF). BOUNDSHEET holds the sheet BOF's offset in the stream.
    """
    bof = struct.pack("<HHHHII", 0x0600, 0x0005, 0x0DBB, 0x07CC, 0, 6)
    name = sheet.encode("latin-1")

    def globals_for(offset: int) -> bytes:
        boundsheet = struct.pack("<IBB", offset, 0, 0) + bytes([len(name), 0]) + name
        return (
            _record(0x0809, bof)
            + _record(0x0042, struct.pack("<H", 1252))
            + _record(0x0085, boundsheet)
            + _record(0x000A)
        )

    cells = b"".join(
        _record(0x0204, struct.pack("<HHHHB", r, c, 0, len(text), 0) + text.encode("latin-1"))
        for r, row in enumerate(rows)
        for c, text in enumerate(row)
    )
    width = max((len(row) for row in rows), default=0)
    sheet_stream = (
        _record(0x0809, struct.pack("<HHHHII", 0x0600, 0x0010, 0x0DBB, 0x07CC, 0, 6))
        + _record(0x0200, struct.pack("<IIHHH", 0, len(rows), 0, width, 0))
        + cells
        + _record(0x000A)
    )
    head = globals_for(0)
    return globals_for(len(head)) + sheet_stream


def packed(data: bytes) -> bytes:
    """How an Office object container stores a part: a 4-byte length, then a zlib stream."""
    return struct.pack("<I", len(data)) + zlib.compress(data)


def workbook_object(workbook: bytes) -> bytes:
    """An embedded Excel object as Office keeps it: an OLE2 file with a Workbook stream."""
    return ole_file(
        [
            ("\x01CompObj", STREAM, b"\x01\x00\xfe\xff"),
            ("\x01Ole", STREAM, b"\x01\x00\x00\x02"),
            ("\x03ObjInfo", STREAM, b"\x00\x00"),
            ("Workbook", STREAM, workbook),
        ]
    )


def package_object(package: bytes) -> bytes:
    """An embedded Office 2007+ object: an OLE2 file whose Package stream is the file itself."""
    return ole_file(
        [
            ("\x01CompObj", STREAM, b"\x01\x00\xfe\xff"),
            ("\x01Ole", STREAM, b"\x01\x00\x00\x02"),
            ("\x03ObjInfo", STREAM, b"\x00\x00"),
            ("Package", STREAM, package),
        ]
    )


def mso_file(objects: list[bytes]) -> bytes:
    """An oledata.mso: a packed OLE2 file whose root streams each hold one packed object."""
    streams = [(f"_{1836661189 + i}", STREAM, packed(obj)) for i, obj in enumerate(objects)]
    return packed(ole_file(streams))
