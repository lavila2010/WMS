"""Extract readable strings from ReportLab PDFs (compressed or uncompressed)."""

from __future__ import annotations

import re
import zlib
from base64 import a85decode


def extract_pdf_text(data: bytes) -> str:
    chunks = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        payload = match.group(1)
        if payload.startswith(b"\r\n"):
            payload = payload[2:]
        elif payload.startswith(b"\n") or payload.startswith(b"\r"):
            payload = payload[1:]
        decoded = None
        for candidate in (payload, payload.strip()):
            try:
                decoded = zlib.decompress(candidate)
                break
            except Exception:
                try:
                    decoded = zlib.decompress(a85decode(candidate, adobe=True))
                    break
                except Exception:
                    continue
        chunks.append(decoded if decoded is not None else payload)
    blob = b"\n".join(chunks) if chunks else data
    literals = []
    for match in re.finditer(rb"\((?:\\.|[^\\)])*\)", blob):
        raw = match.group(0)[1:-1]
        raw = (
            raw.replace(b"\\\\", b"\\")
            .replace(b"\\(", b"(")
            .replace(b"\\)", b")")
            .replace(b"\\n", b"\n")
        )
        literals.append(raw.decode("latin-1", "replace"))
    if literals:
        return "\n".join(literals)
    try:
        return blob.decode("latin-1", "replace")
    except Exception:
        return data.decode("latin-1", "replace")


def pdf_page_count(data: bytes) -> int:
    match = re.search(rb"/Type\s*/Pages.*?/Count\s+(\d+)", data, re.S)
    if match:
        return int(match.group(1))
    return len(re.findall(rb"/Type\s*/Page[^s]", data))
