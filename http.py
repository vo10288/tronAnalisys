"""Client HTTP condiviso: rate limiting thread-safe, retry mirati, statistiche.

Differenze rispetto alla versione precedente:
- i 4xx diversi da 429 non vengono piu' ritentati (erano 4 tentativi a vuoto);
- viene rispettato l'header Retry-After;
- il backoff ha jitter;
- gli errori NON vengono nascosti dietro una risposta vuota: chi chiama riceve
  un'eccezione, cosi' un buco nei dati non viene scambiato per "nessuna
  transazione" (in un contesto investigativo e' la differenza fra un errore e
  una conclusione sbagliata);
- la sessione e' thread-local, per poter parallelizzare l'arricchimento.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)


class ApiError(RuntimeError):
    """Errore non recuperabile durante una chiamata all'API."""


class RateLimiter:
    """Limitatore a intervallo minimo, condivisibile fra thread."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        if not self.min_interval:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            self._next_at = max(now, self._next_at) + self.min_interval
        if wait > 0:
            time.sleep(wait)


@dataclass
class HttpStats:
    requests: int = 0
    retries: int = 0
    failures: int = 0
    rate_limited: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def bump(self, field_name: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self, field_name, getattr(self, field_name) + amount)

    def as_dict(self) -> dict[str, int]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "failures": self.failures,
            "rate_limited": self.rate_limited,
        }


class HttpClient:
    """GET JSON con retry/backoff su 429, 5xx ed errori di rete."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        limiter: RateLimiter | None = None,
        min_interval: float = 0.25,
        timeout: float = 30.0,
        max_retries: int = 4,
        stats: HttpStats | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.limiter = limiter or RateLimiter(min_interval)
        self.timeout = timeout
        self.max_retries = max_retries
        self.stats = stats or HttpStats()
        self._local = threading.local()

    @property
    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers["User-Agent"] = "tronAnalisys/2.0"
            if self.api_key:
                session.headers["TRON-PRO-API-KEY"] = self.api_key
            self._local.session = session
        return session

    @staticmethod
    def _retry_after(response: requests.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    def get_json(self, path: str, params: dict | None = None) -> dict:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        backoff = 1.0
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            self.limiter.acquire()
            try:
                self.stats.bump("requests")
                response = self.session.get(url, params=params, timeout=self.timeout)

                if response.status_code == 429:
                    self.stats.bump("rate_limited")
                    delay = self._retry_after(response) or backoff
                elif response.status_code >= 500:
                    delay = backoff
                elif response.status_code >= 400:
                    # 400/401/403/404: ritentare non serve.
                    self.stats.bump("failures")
                    raise ApiError(f"HTTP {response.status_code} su {url}")
                else:
                    return response.json()

                last_error = ApiError(f"HTTP {response.status_code} su {url}")
                log.debug("Retry %s (HTTP %s) fra %.1fs", url, response.status_code, delay)
            except ApiError:
                raise
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                delay = backoff
                log.debug("Retry %s (%s) fra %.1fs", url, exc.__class__.__name__, delay)

            if attempt == self.max_retries - 1:
                break
            self.stats.bump("retries")
            time.sleep(delay + random.uniform(0, 0.3 * delay))
            backoff = min(backoff * 2, 30.0)

        self.stats.bump("failures")
        raise ApiError(f"Chiamata fallita dopo {self.max_retries} tentativi: {url}") from last_error
