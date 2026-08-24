from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from .http import ResilientJsonClient
from .models import NodeAttribution, Transfer
from .utils import base58check_from_hex

LOG = logging.getLogger(__name__)
TRONGRID_BASE = "https://api.trongrid.io"
TRONSCAN_BASE = "https://apilist.tronscanapi.com/api"
PAGE_LIMIT = 200
SUN = 1_000_000
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


class TronGridProvider:
    chain = "tron"

    def __init__(self, http: ResilientJsonClient):
        self.http = http

    @staticmethod
    def _windows(start_ms: int, end_ms: int, window_days: int) -> Iterator[tuple[int, int]]:
        if window_days <= 0:
            yield start_ms, end_ms
            return
        step = window_days * 86_400_000
        cursor = start_ms
        while cursor <= end_ms:
            window_end = min(cursor + step - 1, end_ms)
            yield cursor, window_end
            if window_end >= end_ms:
                break
            cursor = window_end + 1

    def _paginate(self, endpoint: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        url = f"{TRONGRID_BASE}{endpoint}"
        current = dict(params)
        current["limit"] = PAGE_LIMIT
        seen_fingerprints: set[str] = set()
        while True:
            payload = self.http.get_json(url, current)
            data = payload.get("data") or []
            if not isinstance(data, list):
                break
            for item in data:
                if isinstance(item, dict):
                    yield item
            fingerprint = (payload.get("meta") or {}).get("fingerprint")
            if not fingerprint or not data:
                break
            if fingerprint in seen_fingerprints:
                LOG.warning("Fingerprint ripetuto per %s: interruzione anti-loop", endpoint)
                break
            seen_fingerprints.add(fingerprint)
            current["fingerprint"] = fingerprint

    def transfers(
        self,
        address: str,
        direction: str,
        start_ms: int,
        end_ms: int,
        window_days: int,
        asset: str,
        contract: str | None,
    ) -> Iterator[Transfer]:
        seen: set[str] = set()
        if asset in {"trc20", "both", "auto"}:
            for transfer in self._trc20(address, direction, start_ms, end_ms, window_days, contract):
                if transfer.uid not in seen:
                    seen.add(transfer.uid)
                    yield transfer
        if asset in {"trx", "both", "auto"}:
            for transfer in self._native(address, direction, start_ms, end_ms, window_days):
                if transfer.uid not in seen:
                    seen.add(transfer.uid)
                    yield transfer

    def _base_params(self, direction: str, start_ms: int, end_ms: int) -> dict[str, Any]:
        params: dict[str, Any] = {
            "only_confirmed": "true",
            "min_block_timestamp": start_ms,
            "max_block_timestamp": end_ms,
            "order_by": "block_timestamp,asc",
        }
        if direction == "in":
            params["only_to"] = "true"
        elif direction == "out":
            params["only_from"] = "true"
        return params

    def _trc20(
        self,
        address: str,
        direction: str,
        start_ms: int,
        end_ms: int,
        window_days: int,
        contract: str | None,
    ) -> Iterator[Transfer]:
        endpoint = f"/v1/accounts/{address}/transactions/trc20"
        for left, right in self._windows(start_ms, end_ms, window_days):
            params = self._base_params(direction, left, right)
            if contract:
                params["contract_address"] = contract
            for record in self._paginate(endpoint, params):
                parsed = parse_trc20(record)
                if parsed:
                    yield parsed

    def _native(
        self,
        address: str,
        direction: str,
        start_ms: int,
        end_ms: int,
        window_days: int,
    ) -> Iterator[Transfer]:
        endpoint = f"/v1/accounts/{address}/transactions"
        for left, right in self._windows(start_ms, end_ms, window_days):
            params = self._base_params(direction, left, right)
            for record in self._paginate(endpoint, params):
                parsed = parse_native(record)
                if parsed:
                    yield parsed


class TronScanEnricher:
    CEX_KW = (
        "binance", "okx", "okex", "bybit", "huobi", "htx", "kucoin", "gate",
        "bitfinex", "kraken", "mexc", "bitget", "coinbase", "poloniex",
        "exchange", "hot wallet", "hotwallet", "deposit",
    )
    BRIDGE_KW = ("bridge", "router", "stargate", "allbridge", "wormhole", "celer", "cbridge", "multichain")
    DEX_KW = ("sunswap", "justswap", "dex", "liquidity pool", "amm")

    def __init__(self, http: ResilientJsonClient):
        self.http = http
        self._cache: dict[str, NodeAttribution] = {}

    def enrich(self, address: str) -> NodeAttribution:
        if address in self._cache:
            return self._cache[address]
        try:
            acc = self.http.get_json(f"{TRONSCAN_BASE}/accountv2", {"address": address})
        except Exception as exc:  # enrichment must never abort the investigation
            LOG.warning("TronScan enrichment fallito per %s: %s", address, exc)
            result = NodeAttribution(source="tronscan", evidence="enrichment_failed")
            self._cache[address] = result
            return result

        public = str(acc.get("publicTag") or acc.get("addressTag") or "").strip()
        grey = str(acc.get("greyTag") or "").strip()
        red = str(acc.get("redTag") or "").strip()
        name = public or grey or str(acc.get("name") or "").strip() or red
        contract_map = acc.get("contractMap") or {}
        is_contract = bool(contract_map.get(address)) if isinstance(contract_map, dict) else False
        risk = bool(red or acc.get("feedbackRisk"))
        entity_type = self._infer_type(public, grey, red, is_contract)
        confidence = "high" if (public or red or is_contract) else "low"
        evidence_parts = [part for part in [public, grey, red] if part]
        if is_contract:
            evidence_parts.append("contractMap")
        if acc.get("feedbackRisk"):
            evidence_parts.append("feedbackRisk")
        result = NodeAttribution(
            entity_type=entity_type,
            name=name,
            public_tag=public,
            grey_tag=grey,
            red_tag=red,
            risk=risk,
            is_contract=is_contract,
            source="tronscan:accountv2",
            confidence=confidence,
            evidence="; ".join(evidence_parts),
        )
        self._cache[address] = result
        return result

    @classmethod
    def _infer_type(cls, public: str, grey: str, red: str, is_contract: bool) -> str:
        tag = f"{public} {grey}".lower()
        if red:
            return "risk"
        if any(k in tag for k in cls.CEX_KW):
            return "cex"
        if any(k in tag for k in cls.BRIDGE_KW):
            return "bridge"
        if any(k in tag for k in cls.DEX_KW):
            return "dex"
        if is_contract:
            return "smart_contract"
        if public or grey:
            return "service"
        return "address"


def parse_trc20(record: dict[str, Any]) -> Transfer | None:
    if record.get("type") not in (None, "Transfer"):
        return None
    token = record.get("token_info") or {}
    try:
        decimals = int(token.get("decimals", 6) or 0)
        amount = int(record.get("value", "0")) / (10 ** decimals)
    except (TypeError, ValueError, OverflowError):
        return None
    src = record.get("from")
    dst = record.get("to")
    txid = str(record.get("transaction_id") or record.get("transactionId") or "")
    if not src or not dst or not txid:
        return None
    contract = str(token.get("address") or token.get("tokenId") or record.get("contract_address") or "")
    return Transfer(
        src=str(src),
        dst=str(dst),
        amount=float(amount),
        asset=str(token.get("symbol") or "TRC20"),
        timestamp_ms=_int_or_none(record.get("block_timestamp")),
        txid=txid,
        token_contract=contract,
    )


def parse_native(tx: dict[str, Any]) -> Transfer | None:
    try:
        contract = tx["raw_data"]["contract"][0]
    except (KeyError, IndexError, TypeError):
        return None
    if contract.get("type") != "TransferContract":
        return None
    value = ((contract.get("parameter") or {}).get("value") or {})
    src = base58check_from_hex(value.get("owner_address"))
    dst = base58check_from_hex(value.get("to_address"))
    txid = str(tx.get("txID") or "")
    if not src or not dst or not txid:
        return None
    try:
        amount = float(value.get("amount", 0) or 0) / SUN
    except (TypeError, ValueError):
        return None
    return Transfer(
        src=src,
        dst=dst,
        amount=amount,
        asset="TRX",
        timestamp_ms=_int_or_none(tx.get("block_timestamp")),
        txid=txid,
    )


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
