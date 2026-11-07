"""Synthetic stand-ins for game dumps. No real game data, ever.

These build just enough structure (headers, metadata files) for the server to
recognise a dump's kind and identity.
"""

from __future__ import annotations

import hashlib
import struct
import zipfile
from pathlib import Path

N64_Z64_MAGIC = b"\x80\x37\x12\x40"


def fake_n64_rom(seed: str, size: int = 64 * 1024, order: str = "z64") -> bytes:
    body = (hashlib.sha256(seed.encode()).digest() * (size // 32 + 1))[: size - 4]
    z64 = N64_Z64_MAGIC + body
    if order == "z64":
        return z64
    b = bytearray(z64)
    if order == "v64":
        b[0::2], b[1::2] = z64[1::2], z64[0::2]
    else:  # n64: little-endian words
        for i in range(4):
            b[i::4] = z64[3 - i :: 4]
    return bytes(b)


def make_param_sfo(values: dict[str, str | int]) -> bytes:
    """A minimal PS4 param.sfo with the given keys."""
    keys = b""
    data = b""
    entries = []
    for k, v in values.items():
        key_off = len(keys)
        keys += k.encode() + b"\x00"
        if isinstance(v, int):
            raw, fmt = struct.pack("<I", v), 0x0404
        else:
            raw, fmt = v.encode() + b"\x00", 0x0204
        entries.append((key_off, fmt, len(raw), len(raw), len(data)))
        data += raw
    header_len = 0x14 + 16 * len(entries)
    key_start = header_len
    data_start = key_start + len(keys)
    out = b"\x00PSF" + struct.pack("<IIII", 0x101, key_start, data_start, len(entries))
    for e in entries:
        out += struct.pack("<HHIII", *e)
    return out + keys + data


def make_npdm(program_id: str) -> bytes:
    """A minimal main.npdm whose ACI0 section carries the program ID."""
    meta = bytearray(0x80)
    meta[0:4] = b"META"
    aci_off = 0x80
    struct.pack_into("<II", meta, 0x70, aci_off, 0x40)
    aci = bytearray(0x40)
    aci[0:4] = b"ACI0"
    struct.pack_into("<Q", aci, 0x10, int(program_id, 16))
    return bytes(meta) + bytes(aci)


def ps4_folder(root: Path, title_id: str = "CUSA03173", app_ver: str = "01.09") -> Path:
    (root / "sce_sys").mkdir(parents=True, exist_ok=True)
    (root / "eboot.bin").write_bytes(b"\x7fELF fake eboot " + title_id.encode())
    (root / "sce_sys/param.sfo").write_bytes(
        make_param_sfo({"TITLE_ID": title_id, "APP_VER": app_ver, "TITLE": "Fake Title"})
    )
    (root / "dvdroot_ps4").mkdir(exist_ok=True)
    (root / "dvdroot_ps4/data.bin").write_bytes(b"x" * 1000)
    return root


def switch_folder(root: Path, program_id: str = "0100F2C0115B6000", files: int = 5) -> Path:
    (root / "exefs").mkdir(parents=True, exist_ok=True)
    (root / "exefs/main").write_bytes(b"fake main")
    (root / "exefs/main.npdm").write_bytes(make_npdm(program_id))
    for i in range(files):
        d = root / "romfs" / f"dir{i % 2}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"file{i}.bin").write_bytes(bytes([i]) * (100 + i))
    return root


def zip_with(path: Path, members: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def make_ps4_pkg(content_id: str) -> bytes:
    head = bytearray(0x100)
    head[0:4] = b"\x7fCNT"
    head[0x40 : 0x40 + len(content_id)] = content_id.encode()
    return bytes(head)


def make_xiso(files: dict[str, bytes]) -> bytes:
    """A minimal Xbox disc image (XDVDFS) holding files in its root, as a plain XISO."""
    sector = 2048
    names = list(files)
    table = bytearray()
    offsets = []
    for name in names:  # right-leaning chain: each entry's right child is the next one
        offsets.append(len(table))
        table += b"\0" * (14 + len(name))
        table += b"\0" * (-len(table) % 4)
    data_sector = 34 + (len(table) + sector - 1) // sector
    next_sector = data_sector
    for i, name in enumerate(names):
        right = offsets[i + 1] // 4 if i + 1 < len(names) else 0
        struct.pack_into(
            "<HHIIBB", table, offsets[i], 0, right, next_sector, len(files[name]), 0x20, len(name)
        )
        table[offsets[i] + 14 : offsets[i] + 14 + len(name)] = name.encode()
        next_sector += (len(files[name]) + sector - 1) // sector
    table += b"\xff" * (-len(table) % sector)
    img = bytearray(sector * 32)
    vd = bytearray(sector)
    vd[:20] = b"MICROSOFT*XBOX*MEDIA"
    struct.pack_into("<II", vd, 20, 33, len(table))
    vd[0x7EC:0x800] = b"MICROSOFT*XBOX*MEDIA"
    img += vd
    img += b"\0" * (sector * 33 - len(img))
    img += table
    img += b"\0" * (sector * data_sector - len(img))
    for name in names:
        img += files[name] + b"\0" * (-len(files[name]) % sector)
    return bytes(img)
