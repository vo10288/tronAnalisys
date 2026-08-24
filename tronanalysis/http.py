from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from pathlib import Path
from typing import Any

import requests

from .utils import json_dumps, utc_now_iso

LOG = logging.getLogger(__name__)


class JsonDiskCache:
    def __init__(self, directory: str | Path | None, ttl_seconds: int = 86400):
        self.directory = Path(directory).expanduser() if directory else None
        self.ttl_seconds = max(0, int(ttl_seconds))
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, url: str, params: dict[str, Any]) -> Path | None:
        if not self.directory:
            return None
        material = json.dumps([url, sorted(params.items())], sort_keys=True, default=str)
        digest = hashlib.sha256(material.encode()).hexdigest()
        return self.directory / f"{digest}.json"

    def get(self, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
        path = self._path(url, params)
        if not path or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            created = float(payload.get("_cache_created", 0))
            if self.ttl_seconds and time.time() - created > self.ttl_seconds:
                return None
            return payload.get("data")
        except (OSError, ValueError, TypeError):
            return None

    def put(self, url: str, params: dict[str, Any], data: dict[str, Any]) -> None:
        path = self._path(url, params)
        if not path:
            return
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json_dumps({"_cache_created": time.time(), "data": data}),
            encoding="utf-8",
        )
        tmp.replace(path)


class ResilientJsonClient:
    """GET-only JSON client with retry/backoff, Retry-After, pacing and optional cache."""

    def __init__(
        self,
        api_key: str | None = None,
        sleep_seconds: float = 0.20,
        timeout: float = 30.0,
        max_retries: int = 5,
        cache: JsonDiskCache | None = None,
    ):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "tronAnalisys/2.0"})
        if api_key:
            self.session.headers["TRON-PRO-API-KEY"] = api_key
        self.sleep_seconds = max(0.0, sleep_seconds)
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.cache = cache
        self.provenance: list[dict[str, Any]] = []

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if self.cache:
            hit = self.cache.get(url, params)
            if hit is not None:
                self._record(url, params, cached=True)
                return hit

        backoff = 1.0
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                if response.status_code == 429 or response.status_code >= 500:
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else backoff
                    delay += random.uniform(0, min(delay * 0.15, 1.0))
                    LOG.warning("HTTP %s da %s; retry %s/%s", response.status_code, url, attempt, self.max_retries)
                    time.sleep(delay)
                    backoff = min(backoff * 2, 30.0)
                    continue
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError(f"Risposta JSON inattesa da {url}")
                if self.sleep_seconds:
                    time.sleep(self.sleep_seconds)
                if self.cache:
                    self.cache.put(url, params, data)
                self._record(url, params, cached=False)
                return data
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt == self.max_retries:
                    break
                time.sleep(backoff + random.uniform(0, 0.25))
                backoff = min(backoff * 2, 30.0)
        if last_error:
            raise last_error
        raise RuntimeError(f"Richiesta fallita: {url}")

    def _record(self, url: str, params: dict[str, Any], cached: bool) -> None:
        self.provenance.append(
            {
                "url": url,
                "parameter_names": sorted(params),
                "accessed_utc": utc_now_iso(),
                "cached": cached,
            }
        )
