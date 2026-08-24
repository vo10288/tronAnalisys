import networkx as nx

from tronanalysis.analytics import add_graph_analytics, detect_patterns, shortest_paths


def make_graph():
    g = nx.MultiDiGraph()
    for idx in range(6):
        g.add_edge(f"A{idx}", "HUB", key="USDT", amount=10.0, weight=10.0, count=1)
    g.add_edge("HUB", "OUT", key="USDT", amount=50.0, weight=50.0, count=1)
    for n in g:
        g.nodes[n]["total_in"] = 10
        g.nodes[n]["total_out"] = 10
    return g


def test_analytics_and_patterns():
    g = make_graph()
    summary = add_graph_analytics(g)
    assert summary["nodes"] == 8
    assert "pagerank" in g.nodes["HUB"]
    assert any(x.pattern.startswith("fan_in") and x.node == "HUB" for x in detect_patterns(g))


def test_shortest_paths():
    g = make_graph()
    paths = shortest_paths(g, "A0", "OUT")
    assert paths[0] == ["A0", "HUB", "OUT"]


def test_peel_like_does_not_mix_assets():
    g = nx.MultiDiGraph()
    g.add_edge("X", "M", key="USDT", asset="USDT", amount=100.0, weight=100.0, count=1)
    g.add_edge("M", "Y", key="TRX", asset="TRX", amount=100.0, weight=100.0, count=1)
    for n in g:
        g.nodes[n]["total_in"] = 100
        g.nodes[n]["total_out"] = 100
    findings = detect_patterns(g, min_degree=99)
    assert not any(x.pattern == "peel_like" and x.node == "M" for x in findings)
