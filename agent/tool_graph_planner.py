from __future__ import annotations

import csv
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple


@dataclass(frozen=True)
class ToolEdge:
    source_tool: str
    target_tool: str
    count: int
    workflows: Tuple[str, ...]


@dataclass(frozen=True)
class ToolNode:
    tool_name: str
    tool_id: str = ""
    content_id: str = ""
    tool_version: str = ""
    repo_name: str = ""
    repo_owner: str = ""
    repo_tool_shed: str = ""
    repo_changeset_revision: str = ""


class ToolGraphPlanner:
    """Graph-aware helper for tool recommendation and path lookup."""

    def __init__(self, edges: Iterable[ToolEdge], nodes: Optional[Dict[str, ToolNode]] = None):
        self._edges: List[ToolEdge] = list(edges)
        self._nodes: Dict[str, ToolNode] = nodes or {}
        self._edge_by_pair: Dict[Tuple[str, str], ToolEdge] = {}
        self._outgoing: Dict[str, List[ToolEdge]] = defaultdict(list)
        self._incoming: Dict[str, List[ToolEdge]] = defaultdict(list)
        self._tools: Set[str] = set()

        for edge in self._edges:
            key = (edge.source_tool, edge.target_tool)
            self._edge_by_pair[key] = edge
            self._outgoing[edge.source_tool].append(edge)
            self._incoming[edge.target_tool].append(edge)
            self._tools.add(edge.source_tool)
            self._tools.add(edge.target_tool)

        for tool, outgoing in self._outgoing.items():
            outgoing.sort(key=lambda e: (-e.count, e.target_tool))
        self._start_tools: List[str] = sorted(
            [tool for tool in self._tools if len(self._incoming[tool]) == 0],
            key=lambda name: (-sum(e.count for e in self._outgoing[name]), name),
        )

    @classmethod
    def from_csv(cls, edges_csv_path: Path) -> "ToolGraphPlanner":
        edges: List[ToolEdge] = []
        nodes: Dict[str, ToolNode] = {}
        with edges_csv_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                src = (row.get("source_tool") or "").strip()
                dst = (row.get("target_tool") or "").strip()
                if not src or not dst:
                    continue
                count = int((row.get("count") or "0").strip() or "0")
                workflows = tuple(
                    w.strip() for w in (row.get("workflows") or "").split(",") if w.strip()
                )
                edges.append(
                    ToolEdge(
                        source_tool=src,
                        target_tool=dst,
                        count=count,
                        workflows=workflows,
                    )
                )

                src_node = ToolNode(
                    tool_name=src,
                    tool_id=(row.get("source_tool_id") or "").strip(),
                    content_id=(row.get("source_content_id") or "").strip(),
                    tool_version=(row.get("source_tool_version") or "").strip(),
                    repo_name=(row.get("source_repo_name") or "").strip(),
                    repo_owner=(row.get("source_repo_owner") or "").strip(),
                    repo_tool_shed=(row.get("source_repo_tool_shed") or "").strip(),
                    repo_changeset_revision=(row.get("source_repo_changeset_revision") or "").strip(),
                )
                dst_node = ToolNode(
                    tool_name=dst,
                    tool_id=(row.get("target_tool_id") or "").strip(),
                    content_id=(row.get("target_content_id") or "").strip(),
                    tool_version=(row.get("target_tool_version") or "").strip(),
                    repo_name=(row.get("target_repo_name") or "").strip(),
                    repo_owner=(row.get("target_repo_owner") or "").strip(),
                    repo_tool_shed=(row.get("target_repo_tool_shed") or "").strip(),
                    repo_changeset_revision=(row.get("target_repo_changeset_revision") or "").strip(),
                )
                nodes[src] = src_node
                nodes[dst] = dst_node
        return cls(edges=edges, nodes=nodes)

    @property
    def tools(self) -> Set[str]:
        return set(self._tools)

    def get_node(self, tool_name: str) -> Optional[ToolNode]:
        return self._nodes.get(tool_name)

    def is_valid_transition(self, source_tool: str, target_tool: str) -> bool:
        return (source_tool, target_tool) in self._edge_by_pair

    def recommend_start_tools(self, top_k: int = 5) -> List[Dict[str, object]]:
        out: List[Dict[str, object]] = []
        for tool in self._start_tools[: max(top_k, 0)]:
            support = sum(edge.count for edge in self._outgoing.get(tool, []))
            out.append(
                {
                    "tool": tool,
                    "score": float(support),
                    "reason": f"start node with total downstream support={support}",
                }
            )
        return out

    def recommend_next_tools(
        self,
        current_tool: Optional[str],
        *,
        visited_tools: Optional[Set[str]] = None,
        top_k: int = 5,
        min_count: int = 1,
        goal_keywords: Optional[Iterable[str]] = None,
    ) -> List[Dict[str, object]]:
        visited = visited_tools or set()
        keywords = [k.lower().strip() for k in (goal_keywords or []) if k and k.strip()]

        if not current_tool:
            return self.recommend_start_tools(top_k=top_k)

        outgoing = self._outgoing.get(current_tool, [])
        candidates: List[Dict[str, object]] = []
        for edge in outgoing:
            if edge.count < min_count:
                continue
            if edge.target_tool in visited:
                continue

            score = float(edge.count)
            matched = [kw for kw in keywords if kw in edge.target_tool.lower()]
            if matched:
                score += 2.0 * len(matched)

            candidates.append(
                {
                    "source_tool": edge.source_tool,
                    "target_tool": edge.target_tool,
                    "edge_count": edge.count,
                    "score": score,
                    "workflows": list(edge.workflows),
                    "reason": (
                        f"observed transition {edge.source_tool} -> {edge.target_tool} "
                        f"{edge.count} times"
                    ),
                }
            )

        candidates.sort(key=lambda x: (-float(x["score"]), str(x["target_tool"])))
        return candidates[: max(top_k, 0)]

    def shortest_path(self, source_tool: str, target_tool: str) -> List[str]:
        if source_tool == target_tool:
            return [source_tool]
        if source_tool not in self._tools or target_tool not in self._tools:
            return []

        q: deque[str] = deque([source_tool])
        parent: Dict[str, str] = {}
        visited = {source_tool}
        while q:
            cur = q.popleft()
            for edge in self._outgoing.get(cur, []):
                nxt = edge.target_tool
                if nxt in visited:
                    continue
                visited.add(nxt)
                parent[nxt] = cur
                if nxt == target_tool:
                    path = [target_tool]
                    while path[-1] != source_tool:
                        path.append(parent[path[-1]])
                    path.reverse()
                    return path
                q.append(nxt)
        return []

    def explain_transition(self, source_tool: str, target_tool: str) -> Dict[str, object]:
        edge = self._edge_by_pair.get((source_tool, target_tool))
        if edge is None:
            return {"valid": False, "message": "no observed transition"}
        return {
            "valid": True,
            "count": edge.count,
            "workflows": list(edge.workflows),
            "message": (
                f"observed transition {source_tool} -> {target_tool} {edge.count} times "
                f"in {len(edge.workflows)} workflow(s)"
            ),
        }
