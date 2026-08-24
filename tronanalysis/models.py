from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any


@dataclass(frozen=True, slots=True)
class Transfer:
    src: str
    dst: str
    amount: float
    asset: str
    timestamp_ms: int | None
    txid: str
    token_contract: str = ""
    source: str = "trongrid"

    @property
    def uid(self) -> str:
        raw = "|".join(
            [
                self.txid or "",
                self.src or "",
                self.dst or "",
                self.asset or "",
                self.token_contract or "",
                f"{self.amount:.18g}",
                str(self.timestamp_ms or ""),
            ]
        )
        return sha256(raw.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class NodeAttribution:
    entity_type: str = "address"
    name: str = ""
    public_tag: str = ""
    grey_tag: str = ""
    red_tag: str = ""
    risk: bool = False
    is_contract: bool = False
    source: str = ""
    confidence: str = "unknown"
    evidence: str = ""

    def as_graph_attrs(self) -> dict[str, Any]:
        return {
            "type": self.entity_type,
            "name": self.name,
            "public_tag": self.public_tag,
            "grey_tag": self.grey_tag,
            "red_tag": self.red_tag,
            "risk": bool(self.risk),
            "is_contract": bool(self.is_contract),
            "attribution_source": self.source,
            "attribution_confidence": self.confidence,
            "attribution_evidence": self.evidence,
        }
