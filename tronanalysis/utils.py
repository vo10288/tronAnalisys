from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Iterable

ADDR_RE = re.compile(r"T[1-9A-HJ-NP-Za-km-z]{33}")
_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader; existing environment variables are never overwritten."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def load_addresses(path: str | Path) -> list[str]:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"File non trovato: {p}")
    chunks: list[str] = []
    if p.suffix.lower() == ".csv":
        with p.open(newline="", encoding="utf-8", errors="ignore") as fh:
            for row in csv.reader(fh):
                chunks.extend(row)
    else:
        chunks.append(p.read_text(encoding="utf-8", errors="ignore"))
    seen: set[str] = set()
    out: list[str] = []
    for chunk in chunks:
        for address in ADDR_RE.findall(chunk):
            if address not in seen:
                seen.add(address)
                out.append(address)
    if not out:
        raise ValueError("Nessun indirizzo TRON valido trovato nel file di input.")
    return out


def load_keys_csv(path: str | Path | None) -> dict[str, str]:
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"File API key non trovato: {p}")
    with p.open(newline="", encoding="utf-8", errors="ignore") as fh:
        rows = [row for row in csv.reader(fh) if any(c.strip() for c in row)]
    rows = [r for r in rows if not r[0].strip().startswith("#")]
    if not rows:
        return {}
    keys: dict[str, str] = {}
    header = [c.strip().lower() for c in rows[0]]
    if any("key" in h for h in header) and len(rows) >= 2:
        for h, value in zip(header, rows[1]):
            value = value.strip()
            if "trongrid" in h and value:
                keys["trongrid"] = value
            elif "tronscan" in h and value:
                keys["tronscan"] = value
        return keys
    for row in rows:
        cells = [c.strip() for c in row if c.strip()]
        if len(cells) < 2:
            continue
        name, value = cells[0].lower(), cells[1]
        if "trongrid" in name:
            keys["trongrid"] = value
        elif "tronscan" in name:
            keys["tronscan"] = value
    return keys


def parse_time_ms(value: str | None, default: int) -> int:
    if not value:
        return default
    if value.isdigit():
        return int(value)
    parsed = dt.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    return int(parsed.timestamp() * 1000)


def iso_utc(timestamp_ms: int | None) -> str:
    if not timestamp_ms:
        return ""
    return dt.datetime.fromtimestamp(timestamp_ms / 1000, tz=dt.timezone.utc).isoformat()


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def json_dumps(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)


def base58check_from_hex(hex_address: str | None) -> str | None:
    if not hex_address:
        return None
    if hex_address.startswith("T"):
        return hex_address
    try:
        raw = bytes.fromhex(hex_address)
    except ValueError:
        return None
    checksum = hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
    payload = raw + checksum
    n = int.from_bytes(payload, "big")
    encoded = bytearray()
    while n:
        n, rem = divmod(n, 58)
        encoded.append(_B58_ALPHABET[rem])
    pad = len(payload) - len(payload.lstrip(b"\0"))
    encoded.extend(_B58_ALPHABET[0] for _ in range(pad))
    return bytes(reversed(encoded)).decode("ascii")


def chunked(items: Iterable[str], size: int) -> Iterable[list[str]]:
    batch: list[str] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
