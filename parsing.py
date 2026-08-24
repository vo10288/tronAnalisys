"""Normalizzazione dei record TronGrid in trasferimenti tipizzati.

Gli importi usano Decimal: su valori USDT grandi il float perde precisione
proprio dove serve (ricostruzione di importi esatti in un report).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from .addresses import InvalidAddress, hex_to_base58

log = logging.getLogger(__name__)

SUN_DECIMALS = 6  # 1 TRX = 1e6 SUN


@dataclass(frozen=True, slots=True)
class Transfer:
    txid: str
    src: str
    dst: str
    amount: Decimal
    asset: str
    timestamp: int  # millisecondi
    contract: str | None = None
    event_index: int | None = None

    @property
    def dedup_key(self) -> tuple:
        """Identifica univocamente un trasferimento.

        Serve perche' con --direction both la stessa tx viene scaricata due
        volte (una dal mittente, una dal destinatario): senza dedup gli
        importi aggregati risultano raddoppiati.
        """
        return (self.txid, self.event_index, self.src, self.dst,
                self.asset, str(self.amount))

    def counterparty(self, address: str) -> str | None:
        if self.src == address:
            return self.dst
        if self.dst == address:
            return self.src
        return None


def _decimals(token_info: dict) -> int:
    """Legge i decimali senza trasformare uno 0 legittimo in 6.

    Il bug originale era `int(info.get("decimals", 6) or 6)`: in Python
    `0 or 6` vale 6, quindi ogni token con 0 decimali veniva diviso per 1e6.
    """
    raw = token_info.get("decimals")
    if raw is None:
        return 6
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 6
    return value if 0 <= value <= 32 else 6


def parse_trc20(record: dict) -> Transfer | None:
    """Converte un record TRC20 in Transfer, o None se non e' un trasferimento."""
    if record.get("type") != "Transfer":
        return None  # Approval e altri eventi non sono flussi di valore

    src, dst = record.get("from"), record.get("to")
    if not (src and dst):
        return None

    info = record.get("token_info") or {}
    decimals = _decimals(info)
    try:
        amount = Decimal(str(record.get("value", "0"))).scaleb(-decimals)
    except (TypeError, ValueError, ArithmeticError):
        log.debug("Valore TRC20 non parsabile: %r", record.get("value"))
        return None

    timestamp = record.get("block_timestamp")
    txid = record.get("transaction_id") or record.get("txID")
    if not txid or timestamp is None:
        return None

    return Transfer(
        txid=txid,
        src=src,
        dst=dst,
        amount=amount,
        asset=info.get("symbol") or "TRC20",
        timestamp=int(timestamp),
        contract=info.get("address"),
        event_index=record.get("event_index"),
    )


def parse_native(tx: dict) -> Transfer | None:
    """Converte una transazione nativa TRX (TransferContract) in Transfer."""
    try:
        contract = tx["raw_data"]["contract"][0]
    except (KeyError, IndexError, TypeError):
        return None
    if contract.get("type") != "TransferContract":
        return None  # TRC10, chiamate a smart contract, staking, ecc.

    # Scarta le transazioni fallite: senza questo controllo il grafo mostra
    # flussi di valore che sulla chain non sono mai avvenuti.
    receipts = tx.get("ret") or []
    if receipts and receipts[0].get("contractRet") not in (None, "SUCCESS"):
        return None

    value = (contract.get("parameter") or {}).get("value") or {}
    try:
        src = hex_to_base58(value.get("owner_address"))
        dst = hex_to_base58(value.get("to_address"))
    except InvalidAddress as exc:
        log.warning("Indirizzo non convertibile in %s: %s", tx.get("txID"), exc)
        return None

    timestamp = tx.get("block_timestamp")
    txid = tx.get("txID")
    if not txid or timestamp is None:
        return None

    amount = Decimal(int(value.get("amount") or 0)).scaleb(-SUN_DECIMALS)
    return Transfer(
        txid=txid,
        src=src,
        dst=dst,
        amount=amount,
        asset="TRX",
        timestamp=int(timestamp),
    )
