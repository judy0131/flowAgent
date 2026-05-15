from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWHUB_DIR = ROOT / "data" / "workflowhub"
OUT_DIR = WORKFLOWHUB_DIR


def safe_node_id(name: str) -> str:
    chars = []
    for ch in name:
        if ch.isalnum():
            chars.append(ch.lower())
        else:
            chars.append("_")
    out = "".join(chars).strip("_")
    while "__" in out:
        out = out.replace("__", "_")
    return f"n_{out}" if out else "n_tool"


def load_steps(ga_path: Path) -> list[dict]:
    data = json.loads(ga_path.read_text(encoding="utf-8"))
    steps = data.get("steps", {})
    if isinstance(steps, dict):
        return [v for _, v in sorted(steps.items(), key=lambda kv: int(kv[0]))]
    return steps


def merge_attr_sets(target: dict[str, set[str]], source: dict[str, set[str]]) -> None:
    for key, values in source.items():
        target[key].update(values)


def step_tool_attrs(step: dict[str, Any]) -> dict[str, set[str]]:
    attrs: dict[str, set[str]] = defaultdict(set)

    tool_id = str(step.get("tool_id", "") or "").strip()
    content_id = str(step.get("content_id", "") or "").strip()
    tool_version = str(step.get("tool_version", "") or "").strip()
    if tool_id:
        attrs["tool_id"].add(tool_id)
    if content_id:
        attrs["content_id"].add(content_id)
    if tool_version:
        attrs["tool_version"].add(tool_version)

    repo = step.get("tool_shed_repository") or {}
    if isinstance(repo, dict):
        repo_name = str(repo.get("name", "") or "").strip()
        repo_owner = str(repo.get("owner", "") or "").strip()
        repo_shed = str(repo.get("tool_shed", "") or "").strip()
        repo_revision = str(repo.get("changeset_revision", "") or "").strip()
        if repo_name:
            attrs["repo_name"].add(repo_name)
        if repo_owner:
            attrs["repo_owner"].add(repo_owner)
        if repo_shed:
            attrs["repo_tool_shed"].add(repo_shed)
        if repo_revision:
            attrs["repo_changeset_revision"].add(repo_revision)

    return attrs


def attrs_to_text(attrs: dict[str, set[str]], key: str) -> str:
    values = attrs.get(key, set())
    if not values:
        return ""
    return "|".join(sorted(values))


def collect_edges() -> tuple[
    dict[tuple[str, str], int],
    dict[tuple[str, str], set[str]],
    dict[str, dict[str, set[str]]],
]:
    edge_count: dict[tuple[str, str], int] = defaultdict(int)
    edge_workflows: dict[tuple[str, str], set[str]] = defaultdict(set)
    tool_attrs: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    ga_files = sorted(WORKFLOWHUB_DIR.glob("*.crate/*.ga"))
    for ga_file in ga_files:
        steps = load_steps(ga_file)
        step_by_id = {str(step.get("id")): step for step in steps}
        workflow_name = ga_file.stem

        for step in steps:
            if step.get("type") != "tool":
                continue

            target_tool = str(step.get("name", "")).strip()
            if not target_tool:
                continue
            merge_attr_sets(tool_attrs[target_tool], step_tool_attrs(step))

            input_connections = step.get("input_connections") or {}
            if not isinstance(input_connections, dict):
                continue

            upstream_ids = set()
            for conn in input_connections.values():
                if isinstance(conn, dict) and "id" in conn:
                    upstream_ids.add(str(conn["id"]))

            for upstream_id in upstream_ids:
                upstream = step_by_id.get(upstream_id)
                if not upstream or upstream.get("type") != "tool":
                    continue
                source_tool = str(upstream.get("name", "")).strip()
                if not source_tool:
                    continue
                merge_attr_sets(tool_attrs[source_tool], step_tool_attrs(upstream))
                key = (source_tool, target_tool)
                edge_count[key] += 1
                edge_workflows[key].add(workflow_name)

    return edge_count, edge_workflows, tool_attrs


def write_csv(
    edge_count: dict[tuple[str, str], int],
    edge_workflows: dict[tuple[str, str], set[str]],
    tool_attrs: dict[str, dict[str, set[str]]],
) -> Path:
    out_csv = OUT_DIR / "tool_tool_edges_enriched.csv"
    rows = []
    for (src, dst), count in edge_count.items():
        src_attrs = tool_attrs.get(src, {})
        dst_attrs = tool_attrs.get(dst, {})
        rows.append(
            (
                src,
                dst,
                count,
                ",".join(sorted(edge_workflows[(src, dst)])),
                attrs_to_text(src_attrs, "tool_id"),
                attrs_to_text(src_attrs, "content_id"),
                attrs_to_text(src_attrs, "tool_version"),
                attrs_to_text(src_attrs, "repo_name"),
                attrs_to_text(src_attrs, "repo_owner"),
                attrs_to_text(src_attrs, "repo_tool_shed"),
                attrs_to_text(src_attrs, "repo_changeset_revision"),
                attrs_to_text(dst_attrs, "tool_id"),
                attrs_to_text(dst_attrs, "content_id"),
                attrs_to_text(dst_attrs, "tool_version"),
                attrs_to_text(dst_attrs, "repo_name"),
                attrs_to_text(dst_attrs, "repo_owner"),
                attrs_to_text(dst_attrs, "repo_tool_shed"),
                attrs_to_text(dst_attrs, "repo_changeset_revision"),
            )
        )
    rows.sort(key=lambda r: (-r[2], r[0], r[1]))

    with out_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "source_tool",
                "target_tool",
                "count",
                "workflows",
                "source_tool_id",
                "source_content_id",
                "source_tool_version",
                "source_repo_name",
                "source_repo_owner",
                "source_repo_tool_shed",
                "source_repo_changeset_revision",
                "target_tool_id",
                "target_content_id",
                "target_tool_version",
                "target_repo_name",
                "target_repo_owner",
                "target_repo_tool_shed",
                "target_repo_changeset_revision",
            ]
        )
        writer.writerows(rows)
    return out_csv


def write_nodes_csv(tool_attrs: dict[str, dict[str, set[str]]]) -> Path:
    out_csv = OUT_DIR / "tool_nodes_enriched.csv"
    rows = []
    for tool in sorted(tool_attrs):
        attrs = tool_attrs[tool]
        rows.append(
            (
                safe_node_id(tool),
                tool,
                attrs_to_text(attrs, "tool_id"),
                attrs_to_text(attrs, "content_id"),
                attrs_to_text(attrs, "tool_version"),
                attrs_to_text(attrs, "repo_name"),
                attrs_to_text(attrs, "repo_owner"),
                attrs_to_text(attrs, "repo_tool_shed"),
                attrs_to_text(attrs, "repo_changeset_revision"),
            )
        )
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "node_id",
                "tool_name",
                "tool_id",
                "content_id",
                "tool_version",
                "repo_name",
                "repo_owner",
                "repo_tool_shed",
                "repo_changeset_revision",
            ]
        )
        writer.writerows(rows)
    return out_csv


def write_dot(edge_count: dict[tuple[str, str], int]) -> Path:
    out_dot = OUT_DIR / "tool_tool_graph.dot"
    tools = set()
    for src, dst in edge_count:
        tools.add(src)
        tools.add(dst)

    lines = [
        "digraph tool_tool_graph {",
        "  rankdir=LR;",
        '  graph [fontsize=12, fontname="Arial"];',
        '  node [shape=box, style="rounded,filled", fillcolor="#F7FAFC", color="#4A5568", fontname="Arial"];',
        '  edge [color="#4A5568", fontname="Arial"];',
    ]

    id_map = {tool: safe_node_id(tool) for tool in sorted(tools)}
    for tool in sorted(tools):
        lines.append(f'  {id_map[tool]} [label="{tool.replace(chr(34), chr(39))}"];')

    for (src, dst), count in sorted(edge_count.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1])):
        lines.append(f'  {id_map[src]} -> {id_map[dst]} [label="{count}"];')

    lines.append("}")
    out_dot.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_dot


def write_mermaid(edge_count: dict[tuple[str, str], int]) -> Path:
    out_mmd = OUT_DIR / "tool_tool_graph.mmd"
    tools = set()
    for src, dst in edge_count:
        tools.add(src)
        tools.add(dst)

    id_map = {tool: safe_node_id(tool) for tool in sorted(tools)}
    lines = ["flowchart LR"]

    for tool in sorted(tools):
        label = tool.replace('"', "'")
        lines.append(f'  {id_map[tool]}["{label}"]')

    for (src, dst), count in sorted(edge_count.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1])):
        lines.append(f"  {id_map[src]} -->|{count}| {id_map[dst]}")

    out_mmd.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_mmd


def main() -> None:
    edge_count, edge_workflows, tool_attrs = collect_edges()
    out_csv = write_csv(edge_count, edge_workflows, tool_attrs)
    out_nodes_csv = write_nodes_csv(tool_attrs)
    out_dot = write_dot(edge_count)
    out_mmd = write_mermaid(edge_count)
    print(f"Edges: {len(edge_count)}")
    print(f"Tools: {len(tool_attrs)}")
    print(f"Wrote: {out_csv}")
    print(f"Wrote: {out_nodes_csv}")
    print(f"Wrote: {out_dot}")
    print(f"Wrote: {out_mmd}")


if __name__ == "__main__":
    main()
