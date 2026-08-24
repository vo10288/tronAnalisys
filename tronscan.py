"""Arricchimento dei nodi tramite TronScan (publicTag/greyTag/redTag, rischio)."""

from __future__ import annotations

import logging
import threading

from .http import ApiError, HttpClient, HttpStats, RateLimiter

log = logging.getLogger(__name__)

TRONSCAN_BASE = "https://apilist.tronscanapi.com/api"

CEX_KEYWORDS = (
    "binance", "okx", "okex", "bybit", "huobi", "htx", "kucoin", "gate",
    "bitfinex", "kraken", "mexc", "bitget", "coinbase", "poloniex",
    "exchange", "hot wallet", "hotwallet", "deposit",
)
BRIDGE_KEYWORDS = (
    "bridge", "swap", "router", "stargate", "allbridge", "wormhole",
    "celer", "cbridge", "multichain",
)


class TronScan:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        min_interval: float = 0.25,
        timeout: float = 30.0,
        max_retries: int = 4,
        limiter: RateLimiter | None = None,
        stats: HttpStats | None = None,
    ) -> None:
        self.http = HttpClient(
            TRONSCAN_BASE,
            api_key,
            limiter=limiter,
            min_interval=min_interval,
            timeout=timeout,
            max_retries=max_retries,
            stats=stats,
        )
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()

    def enrich(self, address: str) -> dict:
        """Ritorna i metadati pubblici dell'indirizzo (cache-ati)."""
        with self._lock:
            cached = self._cache.get(address)
        if cached is not None:
            return cached

        info = self._fetch(address)
        with self._lock:
            self._cache[address] = info
        return info

    def _fetch(self, address: str) -> dict:
        try:
            account = self.http.get_json("/account", {"address": address}) or {}
        except ApiError as exc:
            log.warning("TronScan /account fallita per %s: %s", address, exc)
            return {"enrich_error": True}

        public = (account.get("publicTag") or "").strip()
        grey = (account.get("greyTag") or "").strip()
        red = (account.get("redTag") or "").strip()

        try:
            security = self.http.get_json(
                "/security/account/data", {"address": address}
            ) or {}
        except ApiError as exc:
            log.warning("TronScan /security fallita per %s: %s", address, exc)
            security = {}

        return {
            "public_tag": public,
            "grey_tag": grey,
            "red_tag": red,
            "blacklist": bool(security.get("is_black_list")),
            "fraud": bool(security.get("has_fraud_transaction")),
            "type": infer_type(public, grey, red),
            "name": public or grey or red,
            "source": "tronscan",
        }


def infer_type(public: str, grey: str, red: str) -> str:
    tag = f"{public} {grey}".lower()
    if red:
        return "risk"
    if any(k in tag for k in CEX_KEYWORDS):
        return "cex"
    if any(k in tag for k in BRIDGE_KEYWORDS):
        return "bridge"
    if public or grey:
        return "service"
    return "address"
