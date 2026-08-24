"""Test offline: nessuna chiamata di rete, il client e' sostituito da un fake."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tron_analysis.addresses import (
    InvalidAddress, USDT_CONTRACT, base58_to_hex, hex_to_base58,
    is_valid_address, load_addresses,
)
from tron_analysis.graph import GraphBuilder, ScanConfig
from tron_analysis.parsing import parse_native, parse_trc20

# --------------------------------------------------------------------------- #
# Indirizzi
# --------------------------------------------------------------------------- #


def test_usdt_contract_ha_checksum_valido():
    assert is_valid_address(USDT_CONTRACT)


def test_roundtrip_base58_hex():
    hex_form = base58_to_hex(USDT_CONTRACT)
    assert hex_form.startswith("41") and len(hex_form) == 42
    assert hex_to_base58(hex_form) == USDT_CONTRACT


def test_checksum_errato_viene_rifiutato():
    corrotto = USDT_CONTRACT[:-1] + ("a" if USDT_CONTRACT[-1] != "a" else "b")
    assert not is_valid_address(corrotto)


def test_hex_non_valido_alza_eccezione():
    # La v1 restituiva la stringa hex, creando un nodo duplicato nel grafo.
    with pytest.raises(InvalidAddress):
        hex_to_base58("deadbeef")


def test_load_addresses_dedup_e_ordine(tmp_path):
    a, b = _addr(1), _addr(2)
    f = tmp_path / "seed.csv"
    f.write_text(f"nota,{a}\n{b},{a}\n", encoding="utf-8")
    assert load_addresses(str(f)) == [a, b]


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _addr(n: int) -> str:
    return hex_to_base58("41" + f"{n:040x}")


def _trc20(src, dst, value="1000000", decimals=6, ts=1_700_000_000_000,
           txid="tx1", symbol="USDT", type_="Transfer"):
    return {
        "transaction_id": txid,
        "type": type_,
        "from": src,
        "to": dst,
        "value": value,
        "block_timestamp": ts,
        "token_info": {"symbol": symbol, "decimals": decimals,
                       "address": USDT_CONTRACT},
    }


def _native(src, dst, amount=5_000_000, ts=1_700_000_000_000, txid="ntx1",
            ret="SUCCESS", ctype="TransferContract"):
    return {
        "txID": txid,
        "block_timestamp": ts,
        "ret": [{"contractRet": ret}],
        "raw_data": {"contract": [{
            "type": ctype,
            "parameter": {"value": {
                "owner_address": base58_to_hex(src),
                "to_address": base58_to_hex(dst),
                "amount": amount,
            }},
        }]},
    }


def test_trc20_decimals_zero_non_diventa_sei():
    # Bug v1: `int(info.get("decimals", 6) or 6)` -> 0 diventava 6.
    t = parse_trc20(_trc20(_addr(1), _addr(2), value="7", decimals=0))
    assert t.amount == Decimal(7)


def test_trc20_ignora_gli_approval():
    assert parse_trc20(_trc20(_addr(1), _addr(2), type_="Approval")) is None


def test_trc20_precisione_decimale():
    t = parse_trc20(_trc20(_addr(1), _addr(2), value="123456789012345678", decimals=6))
    assert t.amount == Decimal("123456789012.345678")


def test_native_converte_in_base58():
    t = parse_native(_native(_addr(1), _addr(2)))
    assert (t.src, t.dst, t.asset) == (_addr(1), _addr(2), "TRX")
    assert t.amount == Decimal(5)


def test_native_scarta_le_transazioni_fallite():
    assert parse_native(_native(_addr(1), _addr(2), ret="OUT_OF_ENERGY")) is None


def test_native_ignora_i_non_transfer():
    assert parse_native(_native(_addr(1), _addr(2), ctype="TriggerSmartContract")) is None


# --------------------------------------------------------------------------- #
# BFS
# --------------------------------------------------------------------------- #


class FakeClient:
    """Restituisce record grezzi per indirizzo, come farebbe TronGrid."""

    def __init__(self, trc20=None, native=None):
        self.trc20 = trc20 or {}
        self.native = native or {}
        self.calls = []

    def fetch_trc20(self, address, direction, start_ms, end_ms, window_days,
                    contract=None, max_records=None):
        self.calls.append(("trc20", address))
        for record in self.trc20.get(address, []):
            if direction == "in" and record["to"] != address:
                continue
            if direction == "out" and record["from"] != address:
                continue
            yield record

    def fetch_native(self, address, direction, start_ms, end_ms, window_days,
                     max_records=None):
        self.calls.append(("trx", address))
        yield from self.native.get(address, [])


def _config(**kwargs) -> ScanConfig:
    base = dict(depth=1, assets=("trc20",), end_ms=2_000_000_000_000,
                enrich=False, stop_on_exchange=True)
    base.update(kwargs)
    return ScanConfig(**base)


def test_transazione_condivisa_non_viene_contata_due_volte():
    """Il bug piu' serio della v1: A->B scaricata sia da A sia da B."""
    a, b = _addr(1), _addr(2)
    record = _trc20(a, b, value="10000000")  # 10 USDT
    client = FakeClient(trc20={a: [record], b: [record]})

    builder = GraphBuilder(client, _config(direction="both", depth=1), labels={})
    graph = builder.build([a])

    assert graph.number_of_edges() == 1
    data = graph[a][b]["USDT"]
    assert data["amount"] == pytest.approx(10.0)
    assert data["count"] == 1
    assert builder.stats["transfers_duplicated"] == 1


def test_archi_conservano_la_finestra_temporale():
    a, b = _addr(1), _addr(2)
    client = FakeClient(trc20={a: [
        _trc20(a, b, txid="t1", ts=1_600_000_000_000),
        _trc20(a, b, txid="t2", ts=1_700_000_000_000),
    ]})
    graph = GraphBuilder(client, _config(depth=0), labels={}).build([a])
    data = graph[a][b]["USDT"]
    assert data["count"] == 2
    assert data["first_ts"] == 1_600_000_000_000
    assert data["last_ts"] == 1_700_000_000_000
    assert data["first_seen"].startswith("2020-")


def test_soglie_separate_per_asset():
    a, b = _addr(1), _addr(2)
    client = FakeClient(
        trc20={a: [_trc20(a, b, value="2000000")]},          # 2 USDT
        native={a: [_native(a, b, amount=2_000_000)]},        # 2 TRX
    )
    config = _config(
        assets=("trx", "trc20"), depth=0,
        min_amounts={"USDT": Decimal("5"), "TRX": Decimal("1")},
    )
    graph = GraphBuilder(client, config, labels={}).build([a])
    assets = {key for _, _, key in graph.edges(keys=True)}
    assert assets == {"TRX"}  # l'USDT sotto soglia e' escluso, il TRX no


def test_hub_non_viene_espanso():
    hub = _addr(1)
    records = [_trc20(hub, _addr(i), txid=f"t{i}") for i in range(2, 8)]
    client = FakeClient(trc20={hub: records})

    builder = GraphBuilder(client, _config(depth=2, max_degree=3), labels={})
    graph = builder.build([hub])

    assert graph.nodes[hub]["hub"] is True
    assert builder.truncated is True
    # nessuna chiamata sulle controparti dell'hub
    assert [c for c in client.calls if c[1] != hub] == []


def test_cex_non_viene_espanso():
    a, cex, oltre = _addr(1), _addr(2), _addr(3)
    client = FakeClient(trc20={
        a: [_trc20(a, cex, txid="t1")],
        cex: [_trc20(cex, oltre, txid="t2")],
    })
    labels = {cex: {"type": "cex", "name": "Exchange X"}}

    builder = GraphBuilder(client, _config(depth=2), labels=labels)
    graph = builder.build([a])

    assert oltre not in graph
    assert graph.nodes[cex]["boundary"] is True
    assert builder.stats["boundaries_not_expanded"] == 1


def test_profondita_rispettata():
    a, b, c = _addr(1), _addr(2), _addr(3)
    client = FakeClient(trc20={
        a: [_trc20(a, b, txid="t1")],
        b: [_trc20(b, c, txid="t2")],
    })
    graph = GraphBuilder(client, _config(depth=1, direction="out"), labels={}).build([a])
    assert set(graph.nodes) == {a, b, c}      # l'arco b->c e' osservato
    assert graph.nodes[c]["expanded"] is False  # ma c non viene espanso


def test_export_produce_i_file(tmp_path):
    from tron_analysis.export import export_graph

    a, b = _addr(1), _addr(2)
    client = FakeClient(trc20={a: [_trc20(a, b)]})
    builder = GraphBuilder(client, _config(depth=0, keep_transfers=True), labels={})
    graph = builder.build([a])

    paths = export_graph(graph, str(tmp_path / "out"),
                         transfers=builder.transfers, manifest={"tool": "test"})
    names = {p.rsplit("/", 1)[-1] for p in paths}
    assert names == {"out.gexf", "out_nodes.csv", "out_edges.csv",
                     "out_transfers.csv", "out_manifest.json"}
