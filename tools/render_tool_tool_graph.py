from __future__ import annotations

import csv
from pathlib import Path

import matplotlib.pyplot as plt
import networkx as nx


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "workflowhub"
EDGES_CSV = DATA_DIR / "tool_tool_edges_enriched.csv"


def read_edges() -> list[tuple[str, str, int]]:
    rows: list[tuple[str, str, int]] = []
    with EDGES_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            src = (row.get("source_tool") or "").strip()
            dst = (row.get("target_tool") or "").strip()
            count_text = (row.get("count") or "0").strip()
            if not src or not dst:
                continue
            try:
                count = int(count_text)
            except ValueError:
                count = 1
            rows.append((src, dst, count))
    return rows


def build_graph(edges: list[tuple[str, str, int]], min_weight: int = 1) -> nx.DiGraph:
    g = nx.DiGraph()
    for src, dst, w in edges:
        if w < min_weight:
            continue
        g.add_edge(src, dst, weight=w)
    return g


def draw_graph(g: nx.DiGraph, out_png: Path, title: str) -> None:
    if g.number_of_nodes() == 0:
        raise ValueError("Graph has no nodes after filtering.")

    plt.figure(figsize=(20, 14), dpi=150)
    pos = nx.spring_layout(g, seed=42, k=1.4 / max(1, g.number_of_nodes() ** 0.5), iterations=300)

    degree_map = dict(g.degree())
    node_sizes = [500 + 180 * degree_map[n] for n in g.nodes()]

    edge_weights = [g[u][v].get("weight", 1) for u, v in g.edges()]
    edge_widths = [0.8 + 0.8 * w for w in edge_weights]

    nx.draw_networkx_nodes(
        g,
        pos,
        node_size=node_sizes,
        node_color="#E8F1FA",
        edgecolors="#2C5282",
        linewidths=0.9,
    )
    nx.draw_networkx_edges(
        g,
        pos,
        width=edge_widths,
        edge_color="#4A5568",
        arrows=True,
        arrowsize=12,
        alpha=0.85,
        connectionstyle="arc3,rad=0.06",
    )
    nx.draw_networkx_labels(g, pos, font_size=7)

    edge_labels = {(u, v): str(g[u][v].get("weight", 1)) for u, v in g.edges()}
    nx.draw_networkx_edge_labels(g, pos, edge_labels=edge_labels, font_size=6, label_pos=0.55)

    plt.title(title, fontsize=16)
    plt.axis("off")
    plt.tight_layout()
    plt.savefig(out_png, bbox_inches="tight")
    plt.close()


def main() -> None:
    edges = read_edges()
    graph_all = build_graph(edges, min_weight=1)
    graph_core = build_graph(edges, min_weight=2)

    out_all = DATA_DIR / "tool_tool_graph_full.png"
    out_core = DATA_DIR / "tool_tool_graph_core_w2.png"

    draw_graph(
        graph_all,
        out_all,
        f"Tool -> Tool Graph (all edges, nodes={graph_all.number_of_nodes()}, edges={graph_all.number_of_edges()})",
    )
    draw_graph(
        graph_core,
        out_core,
        f"Tool -> Tool Graph (weight>=2, nodes={graph_core.number_of_nodes()}, edges={graph_core.number_of_edges()})",
    )

    print(f"Wrote: {out_all}")
    print(f"Wrote: {out_core}")


if __name__ == "__main__":
    main()
