#!/usr/bin/env python3
"""HamRadioTrainer .etq extractor/packer.

Usage:
  python etq_tool.py extract input.etq output_dir
  python etq_tool.py pack   input_dir output.etq
"""
import sys, struct, zlib, hashlib
from pathlib import Path

KEY = b"Decrypt error in file '%s'. Please contact the programmer.\n"

def transform(data: bytes, encrypt: bool) -> bytes:
    L, K = len(data), len(KEY)
    start = L % K
    step = (L >> 3) & 7 or 3
    out = bytearray()
    for i, b in enumerate(data):
        k = KEY[(start + i * step) % K]
        out.append((b + k) & 0xFF if encrypt else (b - k) & 0xFF)
    return bytes(out)

def qt_uncompress(data: bytes) -> bytes:
    return zlib.decompress(data[4:])

def qt_compress(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + zlib.compress(data, 9)

def read_etq(data: bytes) -> dict[str, bytes]:
    if data[:8] != b"ETQ1\x00\x00\x00\x00":
        raise ValueError("Kein ETQ1-Container")
    off, files = 8, {}
    while off < len(data):
        if data[off:off+4] != b"FI2\x00":
            raise ValueError(f"Ungültiger Eintrag bei Offset {off}")
        size = int.from_bytes(data[off+4:off+8], "big")
        md5 = data[off+12:off+28]
        namelen = int.from_bytes(data[off+28:off+30], "big")
        name = data[off+30:off+30+namelen].decode()
        payload = data[off+30+namelen:off+8+size]
        if hashlib.md5(payload).digest() != md5:
            raise ValueError(f"MD5-Fehler bei {name}")
        files[name] = transform(payload, False)
        off += 8 + size
    return files

def write_etq(files: dict[str, bytes]) -> bytes:
    out = bytearray(b"ETQ1\x00\x00\x00\x00")
    for name, data in files.items():
        payload = transform(data, True)
        nb = name.encode()
        size = 22 + len(nb) + len(payload)
        flags = b"\x00\x01\x00\x01" if name.endswith(".xml") else b"\x00\x00\x00\x01"
        out += b"FI2\x00" + size.to_bytes(4, "big") + flags
        out += hashlib.md5(payload).digest()
        out += len(nb).to_bytes(2, "big") + nb + payload
    return bytes(out)

def extract(etq: Path, outdir: Path):
    files = read_etq(etq.read_bytes())
    for name, data in files.items():
        target = outdir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(qt_uncompress(data) if name.endswith(".xml") else data)

def pack(indir: Path, etq: Path):
    files = {}
    for p in sorted(indir.rglob("*")):
        if p.is_file():
            rel = p.relative_to(indir).as_posix()
            data = p.read_bytes()
            files[rel] = qt_compress(data) if rel.endswith(".xml") else data
    etq.write_bytes(write_etq(files))

if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in {"extract", "pack"}:
        raise SystemExit(__doc__)
    if sys.argv[1] == "extract":
        extract(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        pack(Path(sys.argv[2]), Path(sys.argv[3]))
