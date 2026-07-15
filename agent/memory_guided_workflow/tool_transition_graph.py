from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


@dataclass
class ToolNode:
    tool_id: str
    name: str
    description: str = ""
    input_types: List[str] = field(default_factory=list)
    output_types: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolTransitionEdge:
    source_tool_id: str
    target_tool_id: str
    edge_type: str = ""
    count: int = 0
    transition_probability: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


class ToolTransitionGraph:
    """First-order tool transition graph for incremental planning priors."""

    def __init__(
        self,
        tool_desc_path: str,
        graph_desc_path: str,
        data_path: Optional[str] = None,
        exclude_data_path: Optional[str] = None,
    ):
        self.tool_desc_path = tool_desc_path
        self.graph_desc_path = graph_desc_path
        self.data_path = data_path
        self.exclude_data_path = exclude_data_path
        self.nodes: Dict[str, ToolNode] = {}
        self.edges: Dict[Tuple[str, str], ToolTransitionEdge] = {}
        self.adjacency: Dict[str, List[ToolTransitionEdge]] = {}
        self.reverse_adjacency: Dict[str, List[ToolTransitionEdge]] = {}
        self.transition_counts: Dict[str, Dict[str, int]] = {}
        self.metadata: Dict[str, Any] = {
            "tool_desc_path": tool_desc_path,
            "graph_desc_path": graph_desc_path,
            "data_path": data_path,
            "exclude_data_path": exclude_data_path,
            "excluded_record_id_count": 0,
            "transition_records_total": 0,
            "transition_records_used": 0,
            "transition_records_excluded": 0,
            "unknown_graph_tools": [],
            "out_of_graph_transitions": [],
        }

    def build(self) -> "ToolTransitionGraph":
        """Build nodes, legal graph edges, observed counts, and probabilities."""
        self.load_tool_nodes()
        self.load_graph_edges()
        self.load_transition_counts()
        self.compute_transition_probabilities()
        self._refresh_metadata_counts()
        return self

    def load_tool_nodes(self) -> Dict[str, ToolNode]:
        """Load ToolNode objects from tool_desc.json nodes."""
        payload = _read_json(self.tool_desc_path)
        raw_nodes = payload.get("nodes", []) if isinstance(payload, Mapping) else []

        self.nodes = {}
        for raw_node in raw_nodes:
            if not isinstance(raw_node, Mapping):
                continue
            tool_id = normalize_tool_id(raw_node.get("id", ""))
            if not tool_id:
                continue
            self.nodes[tool_id] = ToolNode(
                tool_id=tool_id,
                name=tool_id,
                description=str(raw_node.get("desc", "") or ""),
                input_types=_coerce_str_list(raw_node.get("input-type")),
                output_types=_coerce_str_list(raw_node.get("output-type")),
                metadata={"raw": dict(raw_node)},
            )

        self._refresh_metadata_counts()
        return dict(self.nodes)

    def load_graph_edges(self) -> None:
        """Load legal graph edges from graph_desc.json links."""
        payload = _read_json(self.graph_desc_path)
        raw_links = payload.get("links", []) if isinstance(payload, Mapping) else []

        self.edges = {}
        self.adjacency = {}
        self.reverse_adjacency = {}
        self.transition_counts = {}
        unknown_tools: List[str] = []

        for raw_link in raw_links:
            if not isinstance(raw_link, Mapping):
                continue
            source = normalize_tool_id(raw_link.get("source", ""))
            target = normalize_tool_id(raw_link.get("target", ""))
            if not source or not target:
                continue

            if source not in self.nodes or target not in self.nodes:
                if source and source not in self.nodes:
                    unknown_tools.append(source)
                if target and target not in self.nodes:
                    unknown_tools.append(target)
                continue

            edge = ToolTransitionEdge(
                source_tool_id=source,
                target_tool_id=target,
                edge_type=str(raw_link.get("type", "") or ""),
                metadata={"raw": dict(raw_link)},
            )
            self.edges[(source, target)] = edge
            self.transition_counts.setdefault(source, {}).setdefault(target, 0)

        self._rebuild_adjacency()
        self.metadata["unknown_graph_tools"] = _unique_list(unknown_tools)
        self._refresh_metadata_counts()

    def load_transition_counts(self) -> None:
        """Count observed first-order transitions from data.json gold workflows."""
        if not self.data_path:
            return

        excluded_ids = load_record_ids(self.exclude_data_path) if self.exclude_data_path else set()
        out_of_graph_counts: Dict[Tuple[str, str], int] = {}
        records_total = 0
        records_used = 0
        records_excluded = 0
        for record in _read_json_records(self.data_path):
            records_total += 1
            record_id = get_record_id(record)
            if record_id and record_id in excluded_ids:
                records_excluded += 1
                continue
            records_used += 1
            links = _safe_parse_links(record.get("tool_links"))
            if not links:
                links = _safe_parse_links(record.get("sampled_links"))

            for link in links:
                if not isinstance(link, Mapping):
                    continue
                source = normalize_tool_id(link.get("source", ""))
                target = normalize_tool_id(link.get("target", ""))
                if not source or not target:
                    continue

                edge = self.edges.get((source, target))
                if edge is None:
                    out_of_graph_counts[(source, target)] = out_of_graph_counts.get((source, target), 0) + 1
                    continue

                self.transition_counts.setdefault(source, {}).setdefault(target, 0)
                self.transition_counts[source][target] += 1
                edge.count += 1

        self.metadata["out_of_graph_transitions"] = [
            {"source_tool_id": source, "target_tool_id": target, "count": count}
            for (source, target), count in sorted(out_of_graph_counts.items())
        ]
        self.metadata["excluded_record_id_count"] = len(excluded_ids)
        self.metadata["transition_records_total"] = records_total
        self.metadata["transition_records_used"] = records_used
        self.metadata["transition_records_excluded"] = records_excluded
        self._rebuild_adjacency()

    def compute_transition_probabilities(self) -> None:
        """Compute P(target | source) for legal graph_desc edges."""
        for source, outgoing_edges in self.adjacency.items():
            total = sum(edge.count for edge in outgoing_edges)
            for edge in outgoing_edges:
                edge.transition_probability = (edge.count / total) if total > 0 else 0.0
        self._rebuild_adjacency()

    def get_transition_probability(self, source_tool_id: str, target_tool_id: str) -> float:
        """Return P(target | source), or 0.0 when the legal edge is absent."""
        edge = self.edges.get((normalize_tool_id(source_tool_id), normalize_tool_id(target_tool_id)))
        return edge.transition_probability if edge is not None else 0.0

    def has_edge(self, source_tool_id: str, target_tool_id: str) -> bool:
        """Return True when graph_desc defines the legal edge."""
        return (normalize_tool_id(source_tool_id), normalize_tool_id(target_tool_id)) in self.edges

    def get_successors(self, tool_id: str, top_k: int = 10) -> List[ToolTransitionEdge]:
        """Return outgoing edges sorted by count and transition probability."""
        edges = list(self.adjacency.get(normalize_tool_id(tool_id), []))
        edges.sort(key=lambda edge: (edge.count, edge.transition_probability, edge.target_tool_id), reverse=True)
        return edges[: max(top_k, 0)]

    def get_predecessors(self, tool_id: str, top_k: int = 10) -> List[ToolTransitionEdge]:
        """Return incoming edges sorted by count and transition probability."""
        edges = list(self.reverse_adjacency.get(normalize_tool_id(tool_id), []))
        edges.sort(key=lambda edge: (edge.count, edge.transition_probability, edge.source_tool_id), reverse=True)
        return edges[: max(top_k, 0)]

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the graph to plain Python data."""
        self._refresh_metadata_counts()
        return {
            "nodes": {tool_id: _node_to_dict(node) for tool_id, node in self.nodes.items()},
            "edges": [_edge_to_dict(edge) for edge in self._sorted_edges()],
            "adjacency": {
                source: [
                    {
                        "target_tool_id": edge.target_tool_id,
                        "edge_type": edge.edge_type,
                        "count": edge.count,
                        "transition_probability": edge.transition_probability,
                    }
                    for edge in edges
                ]
                for source, edges in sorted(self.adjacency.items())
            },
            "metadata": dict(self.metadata),
        }

    def save(self, path: str) -> None:
        """Save graph JSON to path."""
        with open(path, "w", encoding="utf-8") as file:
            json.dump(self.to_dict(), file, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "ToolTransitionGraph":
        """Load a saved ToolTransitionGraph JSON file."""
        payload = _read_json(path)
        metadata = dict(payload.get("metadata", {}) or {})
        graph = cls(
            tool_desc_path=str(metadata.get("tool_desc_path", "")),
            graph_desc_path=str(metadata.get("graph_desc_path", "")),
            data_path=metadata.get("data_path"),
            exclude_data_path=metadata.get("exclude_data_path"),
        )
        graph.nodes = {
            normalize_tool_id(tool_id): _node_from_dict(node_payload)
            for tool_id, node_payload in dict(payload.get("nodes", {}) or {}).items()
            if isinstance(node_payload, Mapping)
        }
        graph.edges = {}
        graph.transition_counts = {}
        for edge_payload in payload.get("edges", []) or []:
            if not isinstance(edge_payload, Mapping):
                continue
            edge = _edge_from_dict(edge_payload)
            graph.edges[(edge.source_tool_id, edge.target_tool_id)] = edge
            graph.transition_counts.setdefault(edge.source_tool_id, {})[edge.target_tool_id] = edge.count
        graph.metadata = metadata
        graph.metadata.setdefault("unknown_graph_tools", [])
        graph.metadata.setdefault("out_of_graph_transitions", [])
        graph._rebuild_adjacency()
        graph._refresh_metadata_counts()
        return graph

    def _rebuild_adjacency(self) -> None:
        self.adjacency = {}
        self.reverse_adjacency = {}
        for edge in self.edges.values():
            self.adjacency.setdefault(edge.source_tool_id, []).append(edge)
            self.reverse_adjacency.setdefault(edge.target_tool_id, []).append(edge)

        for edges in self.adjacency.values():
            edges.sort(key=lambda edge: (edge.count, edge.transition_probability, edge.target_tool_id), reverse=True)
        for edges in self.reverse_adjacency.values():
            edges.sort(key=lambda edge: (edge.count, edge.transition_probability, edge.source_tool_id), reverse=True)

    def _sorted_edges(self) -> List[ToolTransitionEdge]:
        edges = list(self.edges.values())
        edges.sort(key=lambda edge: (edge.source_tool_id, edge.target_tool_id))
        return edges

    def _refresh_metadata_counts(self) -> None:
        self.metadata["num_nodes"] = len(self.nodes)
        self.metadata["num_edges"] = len(self.edges)
        self.metadata["tool_desc_path"] = self.tool_desc_path
        self.metadata["graph_desc_path"] = self.graph_desc_path
        self.metadata["data_path"] = self.data_path
        self.metadata["exclude_data_path"] = self.exclude_data_path
        self.metadata.setdefault("excluded_record_id_count", 0)
        self.metadata.setdefault("transition_records_total", 0)
        self.metadata.setdefault("transition_records_used", 0)
        self.metadata.setdefault("transition_records_excluded", 0)
        self.metadata.setdefault("unknown_graph_tools", [])
        self.metadata.setdefault("out_of_graph_transitions", [])


def normalize_tool_id(value: Any) -> str:
    """Normalize tool ids while preserving case and spaces."""
    return str(value).strip()


def get_record_id(record: Mapping[str, Any]) -> str:
    return str(record.get("id") or record.get("ID") or "").strip()


def load_record_ids(path: str) -> set[str]:
    return {record_id for record_id in (get_record_id(record) for record in _read_json_records(path)) if record_id}


def _read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def _read_json_records(path: str) -> Iterable[Mapping[str, Any]]:
    with open(path, "r", encoding="utf-8") as file:
        text = file.read().strip()
    if not text:
        return []

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        records: List[Mapping[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, Mapping):
                records.append(item)
        return records

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        return [payload]
    return []


def _safe_parse_links(value: Any) -> List[Mapping[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, Mapping)]
        return []
    return []


def _coerce_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _unique_list(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for value in values:
        item = str(value)
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _node_to_dict(node: ToolNode) -> Dict[str, Any]:
    return {
        "tool_id": node.tool_id,
        "name": node.name,
        "description": node.description,
        "input_types": list(node.input_types),
        "output_types": list(node.output_types),
        "metadata": dict(node.metadata),
    }


def _node_from_dict(payload: Mapping[str, Any]) -> ToolNode:
    tool_id = normalize_tool_id(payload.get("tool_id") or payload.get("name") or "")
    return ToolNode(
        tool_id=tool_id,
        name=str(payload.get("name") or tool_id),
        description=str(payload.get("description", "") or ""),
        input_types=_coerce_str_list(payload.get("input_types")),
        output_types=_coerce_str_list(payload.get("output_types")),
        metadata=dict(payload.get("metadata", {}) or {}),
    )


def _edge_to_dict(edge: ToolTransitionEdge) -> Dict[str, Any]:
    return {
        "source_tool_id": edge.source_tool_id,
        "target_tool_id": edge.target_tool_id,
        "edge_type": edge.edge_type,
        "count": edge.count,
        "transition_probability": edge.transition_probability,
        "metadata": dict(edge.metadata),
    }


def _edge_from_dict(payload: Mapping[str, Any]) -> ToolTransitionEdge:
    return ToolTransitionEdge(
        source_tool_id=normalize_tool_id(payload.get("source_tool_id") or payload.get("source") or ""),
        target_tool_id=normalize_tool_id(payload.get("target_tool_id") or payload.get("target") or ""),
        edge_type=str(payload.get("edge_type") or payload.get("type") or ""),
        count=int(payload.get("count", 0) or 0),
        transition_probability=float(payload.get("transition_probability", 0.0) or 0.0),
        metadata=dict(payload.get("metadata", {}) or {}),
    )


def _print_top_edges(title: str, edges: List[ToolTransitionEdge]) -> None:
    print(title)
    for index, edge in enumerate(edges, start=1):
        print(
            f"{index:2d}. {edge.source_tool_id} -> {edge.target_tool_id} "
            f"type={edge.edge_type} count={edge.count} "
            f"prob={edge.transition_probability:.4f}"
        )


def _main() -> None:
    parser = argparse.ArgumentParser(description="Build a first-order Tool Transition Graph.")
    parser.add_argument("--tool_desc", required=True, help="Path to tool_desc.json")
    parser.add_argument("--graph_desc", required=True, help="Path to graph_desc.json")
    parser.add_argument("--data", default=None, help="Optional path to data.json / JSONL")
    parser.add_argument(
        "--exclude-data",
        default=None,
        help="Optional data.json / JSONL whose record ids should be excluded from --data counts",
    )
    parser.add_argument("--output", default=None, help="Optional output path for graph JSON")
    args = parser.parse_args()

    graph = ToolTransitionGraph(
        tool_desc_path=args.tool_desc,
        graph_desc_path=args.graph_desc,
        data_path=args.data,
        exclude_data_path=args.exclude_data,
    ).build()

    if args.output:
        graph.save(args.output)

    counted_edges = [edge for edge in graph.edges.values() if edge.count > 0]
    print(f"number of tool nodes: {len(graph.nodes)}")
    print(f"number of graph edges: {len(graph.edges)}")
    print(f"number of edges with count > 0: {len(counted_edges)}")
    if args.exclude_data:
        print(f"excluded record ids: {graph.metadata.get('excluded_record_id_count', 0)}")
        print(f"transition records total: {graph.metadata.get('transition_records_total', 0)}")
        print(f"transition records used: {graph.metadata.get('transition_records_used', 0)}")
        print(f"transition records excluded: {graph.metadata.get('transition_records_excluded', 0)}")

    top_by_count = sorted(
        graph.edges.values(),
        key=lambda edge: (edge.count, edge.transition_probability, edge.source_tool_id, edge.target_tool_id),
        reverse=True,
    )[:20]
    top_by_probability = sorted(
        graph.edges.values(),
        key=lambda edge: (edge.transition_probability, edge.count, edge.source_tool_id, edge.target_tool_id),
        reverse=True,
    )[:20]

    _print_top_edges("\ntop-20 edges by count:", top_by_count)
    _print_top_edges("\ntop-20 edges by transition_probability:", top_by_probability)

    for tool_id in [
        "Image Downloader",
        "Image-to-Text",
        "Text-to-Image",
        "Video-to-Audio",
        "Audio Splicer",
    ]:
        _print_top_edges(f"\ntop successors for {tool_id}:", graph.get_successors(tool_id, top_k=10))


if __name__ == "__main__":
    _main()
