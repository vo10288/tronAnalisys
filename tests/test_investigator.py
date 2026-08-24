from tronanalysis.investigator import InvestigationOptions, build_graph
from tronanalysis.models import Transfer


class FakeProvider:
    def transfers(self, address, **kwargs):
        data = {
            "S": [Transfer("S", "A", 10, "USDT", 1, "tx1"), Transfer("S", "B", 5, "USDT", 2, "tx2")],
            "A": [Transfer("A", "C", 8, "USDT", 3, "tx3")],
            "B": [],
            "C": [],
        }
        yield from data.get(address, [])


def test_bfs_depth_and_dedup():
    options = InvestigationOptions(depth=2, end_ms=10, contract=None, enrich=False)
    result = build_graph(FakeProvider(), ["S"], options)
    assert set(result.graph) == {"S", "A", "B", "C"}
    assert len(result.transfers) == 3
    assert result.graph.nodes["C"]["discovery_level"] == 2


def test_max_nodes_is_strict():
    options = InvestigationOptions(depth=1, end_ms=10, contract=None, enrich=False, max_nodes=2)
    result = build_graph(FakeProvider(), ["S"], options)
    assert result.graph.number_of_nodes() <= 2
    assert result.truncated is True
    assert "max_nodes" in result.truncation_reasons
