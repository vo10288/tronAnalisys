from __future__ import annotations

import csv
from pathlib import Path

from .models import NodeAttribution
from .utils import ADDR_RE


def load_labels(path: str | Path | None) -> dict[str, NodeAttribution]:
    labels: dict[str, NodeAttribution] = {}
    if not path:
        return labels
    p = Path(path)
    with p.open(newline="", encoding="utf-8", errors="ignore") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            address = (row.get("address") or "").strip()
            if not ADDR_RE.fullmatch(address):
                continue
            labels[address] = NodeAttribution(
                entity_type=(row.get("type") or "labeled").strip(),
                name=(row.get("name") or "").strip(),
                risk=(row.get("risk") or "").strip().lower() in {"1", "true", "yes", "y"},
                is_contract=(row.get("is_contract") or "").strip().lower() in {"1", "true", "yes", "y"},
                source=(row.get("source") or "manual").strip(),
                confidence=(row.get("confidence") or "high").strip(),
                evidence=(row.get("evidence") or "manual label").strip(),
            )
    return labels
