"""Client TronGrid: paginazione a cursore con guardie anti-loop e cap."""

from __future__ import annotations

import logging
from typing import Iterator

from .http import HttpClient, RateLimiter, HttpStats

log = logging.getLogger(__name__)

TRONGRID_BASE = "https://api.trongrid.io"
PAGE_LIMIT = 200  # massimo consentito da TronGrid
MAX_PAGES = 2000  # guardia: 400k record per finestra


class TronGrid:
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
            TRONGRID_BASE,
            api_key,
            limiter=limiter,
            min_interval=min_interval,
            timeout=timeout,
            max_retries=max_retries,
            stats=stats,
        )

    # ------------------------------------------------------------------ #

    def _paginate(self, endpoint: str, base_params: dict, max_records: int | None = None
                  ) -> Iterator[dict]:
        """Itera le pagine di una finestra tramite fingerprint.

        Protetta contro i fingerprint ripetuti (che altrimenti generano un
        loop infinito) e limitata da MAX_PAGES / max_records.
        """
        params = dict(base_params, limit=PAGE_LIMIT)
        seen_fingerprints: set[str] = set()
        emitted = 0

        for page in range(MAX_PAGES):
            payload = self.http.get_json(endpoint, params)
            data = payload.get("data") or []
            for item in data:
                yield item
                emitted += 1
                if max_records is not None and emitted >= max_records:
                    log.debug("Cap di %d record raggiunto su %s", max_records, endpoint)
                    return

            fingerprint = (payload.get("meta") or {}).get("fingerprint")
            if not fingerprint or not data:
                return
            if fingerprint in seen_fingerprints:
                log.warning("Fingerprint ripetuto su %s: interrompo la paginazione", endpoint)
                return
            seen_fingerprints.add(fingerprint)
            params["fingerprint"] = fingerprint
        else:
            log.warning("Raggiunto MAX_PAGES (%d) su %s", MAX_PAGES, endpoint)

    @staticmethod
    def windows(start_ms: int, end_ms: int, window_days: int) -> Iterator[tuple[int, int]]:
        if window_days <= 0:
            yield start_ms, end_ms
            return
        step = window_days * 86_400_000
        cur = start_ms
        while cur < end_ms:
            yield cur, min(cur + step, end_ms)
            cur += step

    @staticmethod
    def _direction_params(direction: str) -> dict:
        if direction == "in":
            return {"only_to": "true"}
        if direction == "out":
            return {"only_from": "true"}
        return {}

    # ------------------------------------------------------------------ #

    def fetch_trc20(
        self,
        address: str,
        direction: str,
        start_ms: int,
        end_ms: int,
        window_days: int = 0,
        contract: str | None = None,
        max_records: int | None = None,
    ) -> Iterator[dict]:
        endpoint = f"/v1/accounts/{address}/transactions/trc20"
        remaining = max_records
        for w_start, w_end in self.windows(start_ms, end_ms, window_days):
            params = {
                "only_confirmed": "true",
                "min_block_timestamp": w_start,
                "max_block_timestamp": w_end,
                "order_by": "block_timestamp,asc",
                **self._direction_params(direction),
            }
            if contract:
                params["contract_address"] = contract
            for record in self._paginate(endpoint, params, remaining):
                yield record
                if remaining is not None:
                    remaining -= 1
                    if remaining <= 0:
                        return

    def fetch_native(
        self,
        address: str,
        direction: str,
        start_ms: int,
        end_ms: int,
        window_days: int = 0,
        max_records: int | None = None,
    ) -> Iterator[dict]:
        endpoint = f"/v1/accounts/{address}/transactions"
        remaining = max_records
        for w_start, w_end in self.windows(start_ms, end_ms, window_days):
            params = {
                "only_confirmed": "true",
                "min_block_timestamp": w_start,
                "max_block_timestamp": w_end,
                "order_by": "block_timestamp,asc",
                **self._direction_params(direction),
            }
            for record in self._paginate(endpoint, params, remaining):
                yield record
                if remaining is not None:
                    remaining -= 1
                    if remaining <= 0:
                        return
