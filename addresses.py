"""Gestione degli indirizzi TRON: validazione, conversione hex/base58, input.

La conversione base58check e' implementata qui per evitare la dipendenza
opzionale `base58` (che nella versione precedente era importata dentro la
funzione e falliva in silenzio, inquinando il grafo con nodi in formato hex).
"""

from __future__ import annotations

import csv
import hashlib
import os
import re
from typing import Iterable

__all__ = [
    "ADDR_RE",
    "USDT_CONTRACT",
    "DEFAULT_LABELS",
    "is_valid_address",
    "hex_to_base58",
    "base58_to_hex",
    "load_addresses",
    "load_labels",
    "load_keys",
]

ADDR_RE = re.compile(r"T[1-9A-HJ-NP-Za-km-z]{33}")
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

DEFAULT_LABELS: dict[str, dict] = {
    USDT_CONTRACT: {"type": "token_contract", "name": "USDT (TRC20)"},
}

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}

# Prefisso mainnet degli indirizzi TRON (0x41) e lunghezza del payload.
_TRON_PREFIX = 0x41
_TRON_RAW_LEN = 21


class InvalidAddress(ValueError):
    """Indirizzo TRON non valido o non convertibile."""


def _sha256d(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def _b58encode(raw: bytes) -> str:
    num = int.from_bytes(raw, "big")
    chars: list[str] = []
    while num > 0:
        num, rem = divmod(num, 58)
        chars.append(_B58_ALPHABET[rem])
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * pad + "".join(reversed(chars))


def _b58decode(text: str) -> bytes:
    num = 0
    for ch in text:
        try:
            num = num * 58 + _B58_INDEX[ch]
        except KeyError as exc:
            raise InvalidAddress(f"Carattere base58 non valido: {ch!r}") from exc
    body = num.to_bytes((num.bit_length() + 7) // 8, "big")
    pad = len(text) - len(text.lstrip("1"))
    return b"\x00" * pad + body


def is_valid_address(value: str | None) -> bool:
    """True se `value` e' un indirizzo base58check TRON con checksum corretto."""
    if not value or not ADDR_RE.fullmatch(value):
        return False
    try:
        raw = _b58decode(value)
    except InvalidAddress:
        return False
    if len(raw) != _TRON_RAW_LEN + 4 or raw[0] != _TRON_PREFIX:
        return False
    return _sha256d(raw[:-4])[:4] == raw[-4:]


def hex_to_base58(hex_addr: str | None) -> str:
    """Converte un indirizzo TRON hex (41...) in base58check.

    Alza InvalidAddress invece di restituire silenziosamente il formato hex:
    un fallback silenzioso genera due nodi distinti per lo stesso indirizzo.
    """
    if not hex_addr:
        raise InvalidAddress("Indirizzo vuoto")
    if hex_addr.startswith("T"):
        if not is_valid_address(hex_addr):
            raise InvalidAddress(f"Base58 non valido: {hex_addr}")
        return hex_addr
    clean = hex_addr[2:] if hex_addr.lower().startswith("0x") else hex_addr
    try:
        raw = bytes.fromhex(clean)
    except ValueError as exc:
        raise InvalidAddress(f"Hex non valido: {hex_addr}") from exc
    if len(raw) != _TRON_RAW_LEN or raw[0] != _TRON_PREFIX:
        raise InvalidAddress(f"Payload TRON non valido: {hex_addr}")
    return _b58encode(raw + _sha256d(raw)[:4])


def base58_to_hex(addr: str) -> str:
    """Converte un indirizzo base58check TRON nel formato hex `41...`."""
    if not is_valid_address(addr):
        raise InvalidAddress(f"Base58 non valido: {addr}")
    return _b58decode(addr)[:-4].hex()


def _iter_text_chunks(path: str) -> Iterable[str]:
    if path.lower().endswith((".csv", ".tsv")):
        delimiter = "\t" if path.lower().endswith(".tsv") else ","
        with open(path, newline="", encoding="utf-8", errors="ignore") as fh:
            for row in csv.reader(fh, delimiter=delimiter):
                yield from row
    else:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            yield fh.read()


def load_addresses(path: str, *, strict: bool = False) -> list[str]:
    """Estrae indirizzi TRON da .csv/.txt/testo, deduplicati e in ordine.

    Con `strict=True` scarta gli indirizzi il cui checksum non e' valido
    (utile per non inseguire refusi di trascrizione).
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"File non trovato: {path}")

    seen: set[str] = set()
    out: list[str] = []
    discarded = 0
    for chunk in _iter_text_chunks(path):
        for match in ADDR_RE.findall(chunk):
            if match in seen:
                continue
            if strict and not is_valid_address(match):
                discarded += 1
                continue
            seen.add(match)
            out.append(match)

    if not out:
        raise ValueError("Nessun indirizzo TRON valido trovato nel file di input.")
    if discarded:
        import logging

        logging.getLogger(__name__).warning(
            "%d indirizzi scartati per checksum non valido", discarded
        )
    return out


def load_labels(path: str | None) -> dict[str, dict]:
    """Carica un CSV `address,type,name` e lo fonde con DEFAULT_LABELS."""
    labels = {k: dict(v) for k, v in DEFAULT_LABELS.items()}
    if not path:
        return labels
    with open(path, newline="", encoding="utf-8", errors="ignore") as fh:
        for row in csv.DictReader(fh):
            addr = (row.get("address") or "").strip()
            if ADDR_RE.fullmatch(addr):
                labels[addr] = {
                    "type": (row.get("type") or "labeled").strip(),
                    "name": (row.get("name") or "").strip(),
                    "source": "manual",
                }
    return labels


def load_keys(path: str | None) -> dict[str, str]:
    """Carica le API key da CSV (`servizio,chiave` oppure header `*_key`)."""
    keys: dict[str, str] = {}
    if not path:
        return keys
    with open(path, newline="", encoding="utf-8", errors="ignore") as fh:
        rows = [
            r
            for r in csv.reader(fh)
            if any(c.strip() for c in r) and not r[0].strip().startswith("#")
        ]
    if not rows:
        return keys

    header = [c.strip().lower() for c in rows[0]]
    if any("key" in h for h in header) and len(rows) >= 2:
        for name, value in zip(header, rows[1]):
            if "trongrid" in name:
                keys["trongrid"] = value.strip()
            elif "tronscan" in name:
                keys["tronscan"] = value.strip()
        return keys

    for row in rows:
        cells = [c.strip() for c in row if c.strip()]
        if len(cells) >= 2:
            name, value = cells[0].lower(), cells[1]
            if "trongrid" in name:
                keys["trongrid"] = value
            elif "tronscan" in name:
                keys["tronscan"] = value
    return keys
