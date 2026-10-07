"""Synthetic OLE2 compound files for the attachment tests: directory names only, no payload.

A real rights-protected or password-protected Office file is an OLE2 container whose directory
names say how it is encrypted. These builders write a valid version 3 container (512-byte
sectors, the FAT first, then the directory chain) holding nothing but those names, so a test can
put a name in a later directory sector, past the first one, where real files keep it.
"""

import struct

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


def _entry(name: str, kind: int, right: int, child: int) -> bytes:
    raw = bytearray(128)
    encoded = (name + "\0").encode("utf-16-le")
    raw[: len(encoded)] = encoded
    struct.pack_into("<HBB", raw, 64, len(encoded), kind, 1)
    struct.pack_into("<III", raw, 68, NOSTREAM, right, child)
    struct.pack_into("<I", raw, 116, ENDOFCHAIN)
    return bytes(raw)


def ole_file(entries, *, pad_sectors: int = 0, cycle: bool = False) -> bytes:
    """A version 3 OLE2 file whose directory holds Root Entry and then `entries`.

    entries are (name, STORAGE or STREAM). Every entry hangs off the root as a chain of right
    siblings, which a name walk does not care about. `pad_sectors` unused sectors sit between
    the FAT and the directory, to push the directory chain past the first FAT sector. With
    `cycle` the last directory sector points back at the first, as a damaged file can.
    """
    names = [("Root Entry", ROOT), *entries]
    dir_sectors = (len(names) + 3) // 4
    fat_sectors = 1
    while fat_sectors * PER_FAT < fat_sectors + pad_sectors + dir_sectors:
        fat_sectors += 1
    first_dir = fat_sectors + pad_sectors
    chain = list(range(first_dir, first_dir + dir_sectors))

    fat = [FREESECT] * (fat_sectors * PER_FAT)
    for i in range(fat_sectors):
        fat[i] = FATSECT
    for here, there in zip(chain, chain[1:], strict=False):
        fat[here] = there
    fat[chain[-1]] = chain[0] if cycle else ENDOFCHAIN

    header = bytearray(SECTOR)
    header[:8] = OLE_MAGIC
    struct.pack_into("<HHHHH", header, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<II", header, 44, fat_sectors, first_dir)
    struct.pack_into("<IIIIII", header, 52, 0, 4096, ENDOFCHAIN, 0, ENDOFCHAIN, 0)
    difat = [FREESECT] * 109
    difat[:fat_sectors] = range(fat_sectors)
    struct.pack_into("<109I", header, 76, *difat)

    directory = bytearray()
    for i, (name, kind) in enumerate(names):
        right = i + 1 if 0 < i < len(names) - 1 else NOSTREAM
        child = 1 if i == 0 and len(names) > 1 else NOSTREAM
        directory += _entry(name, kind, right, child)
    directory += bytes(dir_sectors * SECTOR - len(directory))

    body = struct.pack(f"<{len(fat)}I", *fat) + bytes(pad_sectors * SECTOR) + bytes(directory)
    return bytes(header) + body
