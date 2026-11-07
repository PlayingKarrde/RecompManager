"""Recognising game dumps: hashing, N64 byte order, PS4/Switch/Xbox metadata.

Everything here reads plain, unencrypted data only. Encrypted packages (PS4
.pkg contents, Switch .nsp/.xci) are recognised by their headers or names but
never decrypted.
"""

from __future__ import annotations

import hashlib
import re
import struct
import zipfile
import zlib
from pathlib import Path
from typing import Any

CHUNK = 1024 * 1024

# ---------------------------------------------------------------- hashing

# N64 ROMs come in three byte orders; hash lists (e.g. SoH's) use big-endian .z64.
_N64_Z64 = b"\x80\x37\x12\x40"
_N64_V64 = b"\x37\x80\x40\x12"  # byte-swapped pairs
_N64_N64 = b"\x40\x12\x37\x80"  # little-endian words


def n64_order(head: bytes) -> str | None:
    if head[:4] == _N64_Z64:
        return "z64"
    if head[:4] == _N64_V64:
        return "v64"
    if head[:4] == _N64_N64:
        return "n64"
    return None


def _to_z64(chunk: bytes, order: str) -> bytes:
    if order == "v64":
        b = bytearray(chunk)
        b[0::2], b[1::2] = chunk[1::2], chunk[0::2]
        return bytes(b)
    if order == "n64":
        b = bytearray(len(chunk))
        for i in range(4):
            b[i::4] = chunk[3 - i :: 4]
        return bytes(b)
    return chunk


def hash_stream(read, size_hint: int | None = None) -> dict[str, Any]:
    """sha1, md5, crc32 and sha256 of a stream, plus the .z64 sha1 for N64 ROMs."""
    sha1, md5, sha256 = hashlib.sha1(), hashlib.md5(), hashlib.sha256()
    crc = 0
    size = 0
    order: str | None = None
    z64: Any = None
    first = True
    while chunk := read(CHUNK):
        if first:
            order = n64_order(chunk)
            if order and order != "z64":
                z64 = hashlib.sha1()
            first = False
        sha1.update(chunk)
        md5.update(chunk)
        sha256.update(chunk)
        crc = zlib.crc32(chunk, crc)
        size += len(chunk)
        if z64 is not None and order:
            z64.update(_to_z64(chunk, order))
    out: dict[str, Any] = {
        "size": size,
        "sha1": sha1.hexdigest(),
        "md5": md5.hexdigest(),
        "crc32": f"{crc & 0xFFFFFFFF:08x}",
        "sha256": sha256.hexdigest(),
    }
    if order:
        out["n64_order"] = order
        out["sha1_z64"] = z64.hexdigest() if z64 is not None else out["sha1"]
    return out


def hash_file(path: str | Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return hash_stream(f.read)


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- archives


ARCHIVE_EXTS = (".zip", ".7z")


def zip_entries(path: str | Path) -> list[dict[str, Any]]:
    """Name, size and CRC32 of each file in a zip, read from its index only."""
    with zipfile.ZipFile(path) as z:
        return [
            {"name": i.filename, "size": i.file_size, "crc32": f"{i.CRC:08x}"}
            for i in z.infolist()
            if not i.is_dir()
        ]


def hash_zip_member(path: str | Path, member: str) -> dict[str, Any]:
    with zipfile.ZipFile(path) as z, z.open(member) as f:
        return hash_stream(f.read)


# ---------------------------------------------------------------- PS4


def parse_param_sfo(data: bytes) -> dict[str, Any]:
    """Keys of a PS4/PS Vita param.sfo (TITLE_ID, APP_VER, VERSION, TITLE, ...)."""
    if data[:4] != b"\x00PSF":
        raise ValueError("not a param.sfo")
    key_start, data_start, count = struct.unpack_from("<III", data, 8)
    out: dict[str, Any] = {}
    for i in range(count):
        key_off, fmt, length, _max, data_off = struct.unpack_from("<HHIII", data, 0x14 + i * 16)
        k_end = data.index(b"\x00", key_start + key_off)
        key = data[key_start + key_off : k_end].decode("utf-8", "replace")
        raw = data[data_start + data_off : data_start + data_off + length]
        if fmt == 0x0404:
            out[key] = struct.unpack("<I", raw[:4])[0]
        else:
            out[key] = raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
    return out


def ps4_folder_facts(folder: Path) -> dict[str, Any] | None:
    sfo = folder / "sce_sys" / "param.sfo"
    if not (folder / "eboot.bin").is_file() or not sfo.is_file():
        return None
    try:
        p = parse_param_sfo(sfo.read_bytes())
    except (ValueError, struct.error):
        return {"param_sfo_error": True}
    keep = ("TITLE_ID", "TITLE", "APP_VER", "VERSION", "CONTENT_ID", "CATEGORY")
    return {k: p[k] for k in keep if k in p}


_PKG_MAGIC = b"\x7fCNT"


def ps4_pkg_content_id(path: str | Path) -> str | None:
    """Content ID (e.g. UP9000-CUSA03173_00-…) from a PS4 .pkg's plain header."""
    with open(path, "rb") as f:
        head = f.read(0x80)
    if head[:4] != _PKG_MAGIC:
        return None
    cid = head[0x40:0x64].split(b"\x00", 1)[0].decode("ascii", "replace")
    return cid or None


def content_id_title(cid: str) -> str | None:
    m = re.match(r"^[A-Z]{2}\d{4}-([A-Z]{4}\d{5})_", cid)
    return m.group(1) if m else None


# ---------------------------------------------------------------- Switch


def parse_npdm_program_id(data: bytes) -> str | None:
    """Program ID from an exefs main.npdm (META header → ACI0 section)."""
    if data[:4] != b"META" or len(data) < 0x80:
        return None
    aci_off, aci_size = struct.unpack_from("<II", data, 0x70)
    if aci_size < 0x18 or data[aci_off : aci_off + 4] != b"ACI0":
        return None
    (pid,) = struct.unpack_from("<Q", data, aci_off + 0x10)
    return f"{pid:016X}"


def switch_folder_facts(folder: Path) -> dict[str, Any] | None:
    npdm = folder / "exefs" / "main.npdm"
    if not npdm.is_file() or not (folder / "romfs").is_dir():
        return None
    return {"program_id": parse_npdm_program_id(npdm.read_bytes()[:0x10000])}


_TITLE_ID_IN_NAME = re.compile(r"\[?(0100[0-9A-Fa-f]{12})\]?")


def switch_title_id_from_name(name: str) -> str | None:
    m = _TITLE_ID_IN_NAME.search(name)
    return m.group(1).upper() if m else None


# ---------------------------------------------------------------- Xbox


_XISO_MAGIC = b"MICROSOFT*XBOX*MEDIA"
# Where the XDVDFS header sits: plain XISO, and a full Redump image's game partition.
_XISO_OFFSETS = (0x10000, 0x18300000 + 0x10000)


def is_xbox_iso(path: str | Path) -> bool:
    try:
        with open(path, "rb") as f:
            for off in _XISO_OFFSETS:
                f.seek(off)
                if f.read(len(_XISO_MAGIC)) == _XISO_MAGIC:
                    return True
    except OSError:
        return False
    return False
