from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


NODE_REF_RE = re.compile(r"<node-(\d+)>")


def _simple_tool_key(name: Any) -> str:
    return " ".join(str(name or "").strip().lower().split())


def _canonical_tool_key(name: Any) -> str:
    text = str(name or "").strip().lower()
    text = re.sub(r"[-_\s]+", " ", text)
    return " ".join(text.split())


def _tool_lookup_keys(name: Any) -> List[str]:
    text = str(name or "").strip()
    keys = [text, _simple_tool_key(text), _canonical_tool_key(text)]
    out: List[str] = []
    seen = set()
    for key in keys:
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _read_json_or_jsonl(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read file: {path} ({exc})") from exc

    stripped = text.strip()
    if not stripped:
        return []

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        records: List[Any] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid JSON/JSONL in {path} at line {line_no}: {exc.msg}"
                ) from exc
        return records


def _coerce_record_list(payload: Any, *, label: str) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for key in ("data", "records", "rows", "samples", "tools", "nodes"):
            value = payload.get(key)
            if isinstance(value, list):
                items = value
                break
        else:
            items = [payload]
    else:
        raise ValueError(f"{label} must be a JSON object, JSON array, or JSONL records")

    records: List[Dict[str, Any]] = []
    for idx, item in enumerate(items):
        if isinstance(item, dict):
            records.append(item)
        else:
            records.append({"_invalid_record": item, "_index": idx})
    return records


def normalize_tool_name(tool_name: Any, alias_map: Dict[str, str]) -> str:
    text = str(tool_name or "").strip()
    if not text:
        return ""
    for key in _tool_lookup_keys(text):
        if key in alias_map:
            return alias_map[key]
    return text


def _register_alias(alias_map: Dict[str, str], alias: Any, target: Any) -> None:
    target_text = str(target or "").strip()
    if not target_text:
        return
    for key in _tool_lookup_keys(alias):
        alias_map[key] = target_text


def _coerce_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _parse_maybe_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return default
    return default


def load_ontology(path: Path) -> Dict[str, Dict[str, Any]]:
    payload = _read_json_or_jsonl(path)
    records = _coerce_record_list(payload, label="ontology")
    ontology: Dict[str, Dict[str, Any]] = {}

    for record in records:
        tool = str(record.get("tool") or record.get("id") or "").strip()
        if not tool:
            continue
        entry = {
            "tool": tool,
            "desc": str(record.get("desc") or ""),
            "input_artifacts": _coerce_list(record.get("input_artifacts")),
            "output_artifacts": _coerce_list(record.get("output_artifacts")),
            "operation": record.get("operation"),
            "action": record.get("action"),
            "needs_review": bool(record.get("needs_review", False)),
        }
        for key in _tool_lookup_keys(tool):
            ontology[key] = entry

    return ontology


def load_aliases(path: Optional[Path], ontology: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    alias_map: Dict[str, str] = {}
    seen_tools = set()
    for entry in ontology.values():
        tool = str(entry.get("tool") or "").strip()
        if not tool or tool in seen_tools:
            continue
        seen_tools.add(tool)
        _register_alias(alias_map, tool, tool)

    if path is None:
        return alias_map
    if not path.exists():
        return alias_map

    payload = _read_json_or_jsonl(path)
    if not isinstance(payload, dict):
        raise ValueError(f"tool aliases must be a JSON object: {path}")
    for alias, target in payload.items():
        _register_alias(alias_map, alias, target)
    return alias_map


def parse_node_refs_from_arguments(arguments: Any) -> List[int]:
    refs: List[int] = []

    def visit(value: Any) -> None:
        if isinstance(value, str):
            for match in NODE_REF_RE.finditer(value):
                refs.append(int(match.group(1)))
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)

    visit(arguments)
    return refs


def _extract_task_nodes(sample: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates = [
        sample.get("tool_nodes"),
        sample.get("task_nodes"),
        sample.get("nodes"),
    ]
    result = sample.get("result")
    if isinstance(result, dict):
        candidates.extend([result.get("task_nodes"), result.get("tool_nodes")])

    for candidate in candidates:
        parsed = _parse_maybe_json(candidate, [])
        if isinstance(parsed, list):
            return [item if isinstance(item, dict) else {} for item in parsed]
        if isinstance(parsed, dict):
            return [parsed]
    return []


def _extract_task_links(sample: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates = [
        sample.get("tool_links"),
        sample.get("task_links"),
        sample.get("links"),
    ]
    result = sample.get("result")
    if isinstance(result, dict):
        candidates.extend([result.get("task_links"), result.get("tool_links")])

    for candidate in candidates:
        parsed = _parse_maybe_json(candidate, [])
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        if isinstance(parsed, dict):
            return [parsed]
    return []


def _tool_name_from_node(node: Dict[str, Any], fallback_idx: int) -> str:
    for key in ("task", "tool", "id", "name", "test"):
        value = node.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return f"unknown_tool_{fallback_idx}"


def _arguments_from_node(node: Dict[str, Any]) -> List[Any]:
    for key in ("arguments", "args", "original_arguments"):
        if key in node:
            value = node.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                return [value]
            if value is None:
                return []
            return [value]
    return []


def _ontology_lookup(ontology: Dict[str, Dict[str, Any]], tool: str) -> Optional[Dict[str, Any]]:
    for key in _tool_lookup_keys(tool):
        entry = ontology.get(key)
        if entry is not None:
            return entry
    return None


def _ontology_lookup_before_normalization(
    ontology: Dict[str, Dict[str, Any]],
    tool: str,
) -> Optional[Dict[str, Any]]:
    text = str(tool or "").strip()
    simple = _simple_tool_key(text)
    seen_tools = set()
    for entry in ontology.values():
        ontology_tool = str(entry.get("tool") or "").strip()
        if not ontology_tool or ontology_tool in seen_tools:
            continue
        seen_tools.add(ontology_tool)
        if text == ontology_tool or simple == _simple_tool_key(ontology_tool):
            return entry
    return None


def _artifact_transitions_for_node(node: Dict[str, Any]) -> List[str]:
    inputs = [
        str(item).strip()
        for item in _coerce_list(node.get("input_artifacts"))
        if str(item).strip()
    ]
    outputs = [
        str(item).strip()
        for item in _coerce_list(node.get("output_artifacts"))
        if str(item).strip()
    ]
    if not inputs or not outputs:
        return []

    transitions: List[str] = []
    seen = set()
    for input_artifact in inputs:
        for output_artifact in outputs:
            transition = f"{input_artifact}->{output_artifact}"
            if transition in seen:
                continue
            seen.add(transition)
            transitions.append(transition)
    return transitions


def _resolve_link_endpoint(
    endpoint: Any,
    tool_to_indices: Dict[str, List[int]],
    *,
    prefer_before: Optional[int] = None,
    alias_map: Optional[Dict[str, str]] = None,
) -> Optional[int]:
    if isinstance(endpoint, int):
        return endpoint if endpoint >= 0 else None
    text = str(endpoint or "").strip()
    if not text:
        return None
    if text.isdigit():
        value = int(text)
        return value if value >= 0 else None

    candidates = [text, _simple_tool_key(text), _canonical_tool_key(text)]
    if alias_map is not None:
        normalized = normalize_tool_name(text, alias_map)
        candidates.extend([normalized, _simple_tool_key(normalized), _canonical_tool_key(normalized)])
    indices: List[int] = []
    for candidate in candidates:
        indices = tool_to_indices.get(candidate) or []
        if indices:
            break
    if not indices:
        return None
    if prefer_before is not None:
        before = [idx for idx in indices if idx < prefer_before]
        if before:
            return before[-1]
    return indices[0]


def _dedupe_edges(edges: Iterable[Dict[str, int]], node_count: int) -> List[Dict[str, int]]:
    seen = set()
    out: List[Dict[str, int]] = []
    for edge in edges:
        try:
            source = int(edge.get("source"))
            target = int(edge.get("target"))
        except (TypeError, ValueError):
            continue
        if source == target:
            continue
        if not (0 <= source < node_count and 0 <= target < node_count):
            continue
        key = (source, target)
        if key in seen:
            continue
        seen.add(key)
        out.append({"source": source, "target": target})
    return out


def _edges_from_task_links(
    task_links: List[Dict[str, Any]],
    tool_to_indices: Dict[str, List[int]],
    node_count: int,
    alias_map: Dict[str, str],
) -> List[Dict[str, int]]:
    edges: List[Dict[str, int]] = []
    for link in task_links:
        target = _resolve_link_endpoint(link.get("target"), tool_to_indices, alias_map=alias_map)
        source = _resolve_link_endpoint(
            link.get("source"),
            tool_to_indices,
            prefer_before=target,
            alias_map=alias_map,
        )
        if source is None or target is None:
            continue
        edges.append({"source": source, "target": target})
    return _dedupe_edges(edges, node_count)


def _resolve_node_ref(raw_ref: int, target_idx: int, node_count: int) -> Optional[int]:
    if 0 <= raw_ref < target_idx:
        return raw_ref
    one_based = raw_ref - 1
    if 0 <= one_based < target_idx:
        return one_based
    if 0 <= raw_ref < node_count and raw_ref != target_idx:
        return raw_ref
    return None


def _edges_from_argument_refs(task_nodes: List[Dict[str, Any]]) -> List[Dict[str, int]]:
    node_count = len(task_nodes)
    edges: List[Dict[str, int]] = []
    for target_idx, node in enumerate(task_nodes):
        arguments = _arguments_from_node(node)
        for raw_ref in parse_node_refs_from_arguments(arguments):
            source_idx = _resolve_node_ref(raw_ref, target_idx, node_count)
            if source_idx is not None:
                edges.append({"source": source_idx, "target": target_idx})
    return _dedupe_edges(edges, node_count)


def build_typed_action_graph(
    sample: Dict[str, Any],
    ontology: Dict[str, Dict[str, Any]],
    alias_map: Dict[str, str],
) -> Tuple[Dict[str, Any], List[str], Counter[str]]:
    task_nodes = _extract_task_nodes(sample)
    task_links = _extract_task_links(sample)
    typed_nodes: List[Dict[str, Any]] = []
    missing_tools: List[str] = []
    alias_replacements: Counter[str] = Counter()
    tool_to_indices: Dict[str, List[int]] = defaultdict(list)
    node_original_tools: List[str] = []
    node_normalized_tools: List[str] = []

    for idx, node in enumerate(task_nodes):
        original_tool = _tool_name_from_node(node, idx)
        normalized_tool = normalize_tool_name(original_tool, alias_map)
        ontology_entry = _ontology_lookup(ontology, normalized_tool)
        final_tool = (
            str(ontology_entry.get("tool") or normalized_tool).strip()
            if ontology_entry is not None
            else normalized_tool
        )
        node_original_tools.append(original_tool)
        node_normalized_tools.append(final_tool)
        if final_tool != original_tool.strip():
            alias_replacements[f"{original_tool} -> {final_tool}"] += 1
        for key in _tool_lookup_keys(original_tool) + _tool_lookup_keys(final_tool):
            tool_to_indices[key].append(idx)

    for idx, node in enumerate(task_nodes):
        original_tool = node_original_tools[idx]
        normalized_tool = node_normalized_tools[idx]
        ontology_entry = _ontology_lookup(ontology, normalized_tool)
        original_arguments = _arguments_from_node(node)
        if ontology_entry is None:
            missing_tools.append(original_tool)
            typed_node = {
                "id": idx,
                "tool": normalized_tool,
                "original_tool": original_tool,
                "action": None,
                "operation": None,
                "input_artifacts": [],
                "output_artifacts": [],
                "original_arguments": original_arguments,
                "needs_review": True,
            }
        else:
            final_tool = str(ontology_entry.get("tool") or normalized_tool).strip()
            typed_node = {
                "id": idx,
                "tool": final_tool,
                "original_tool": original_tool,
                "action": ontology_entry.get("action"),
                "operation": ontology_entry.get("operation"),
                "input_artifacts": _coerce_list(ontology_entry.get("input_artifacts")),
                "output_artifacts": _coerce_list(ontology_entry.get("output_artifacts")),
                "original_arguments": original_arguments,
            }
            if bool(ontology_entry.get("needs_review", False)):
                typed_node["needs_review"] = True
        typed_nodes.append(typed_node)

    explicit_edges = _edges_from_task_links(task_links, tool_to_indices, len(task_nodes), alias_map)
    typed_edges = explicit_edges if explicit_edges else _edges_from_argument_refs(task_nodes)

    artifact_transitions: List[str] = []
    for typed_node in typed_nodes:
        artifact_transitions.extend(_artifact_transitions_for_node(typed_node))

    record = {
        "case_id": str(sample.get("id") or sample.get("case_id") or "").strip(),
        "instruction": str(sample.get("instruction") or sample.get("user_requirement") or "").strip(),
        "typed_action_nodes": typed_nodes,
        "typed_action_edges": typed_edges,
        "stats": {
            "node_count": len(typed_nodes),
            "edge_count": len(typed_edges),
            "artifact_transitions": artifact_transitions,
        },
    }
    if missing_tools:
        record["missing_ontology_tools"] = missing_tools
    if alias_replacements:
        record["alias_replacements"] = dict(sorted(alias_replacements.items()))
    return record, missing_tools, alias_replacements


def write_jsonl(path: Path, records: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _counter_to_sorted_dict(counter: Counter[str]) -> Dict[str, int]:
    return {
        key: int(value)
        for key, value in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _count_missing_before_normalization(
    gold_records: List[Dict[str, Any]],
    ontology: Dict[str, Dict[str, Any]],
) -> Counter[str]:
    missing: Counter[str] = Counter()
    for sample in gold_records:
        for idx, node in enumerate(_extract_task_nodes(sample)):
            tool = _tool_name_from_node(node, idx)
            if _ontology_lookup_before_normalization(ontology, tool) is None:
                missing[tool] += 1
    return missing


def _print_stats(
    records: List[Dict[str, Any]],
    before_missing: Counter[str],
    after_missing: Counter[str],
    alias_distribution: Counter[str],
    *,
    top_k_unresolved: int,
) -> None:
    total_cases = len(records)
    total_nodes = sum(int(record["stats"]["node_count"]) for record in records)
    total_edges = sum(int(record["stats"]["edge_count"]) for record in records)
    operation_distribution: Counter[str] = Counter()
    artifact_transition_distribution: Counter[str] = Counter()

    for record in records:
        for node in record.get("typed_action_nodes", []):
            operation = node.get("operation")
            if operation:
                operation_distribution[str(operation)] += 1
        for transition in record.get("stats", {}).get("artifact_transitions", []):
            artifact_transition_distribution[str(transition)] += 1

    avg_nodes = float(total_nodes) / float(total_cases) if total_cases else 0.0
    avg_edges = float(total_edges) / float(total_cases) if total_cases else 0.0

    print(f"[STATS] total_cases={total_cases}")
    print(f"[STATS] total_nodes={total_nodes}")
    print(f"[STATS] total_edges={total_edges}")
    print("[STATS] Before normalization:")
    print(f"[STATS]   missing_ontology_nodes={sum(before_missing.values())}")
    print(f"[STATS]   missing_unique_tools={len(before_missing)}")
    print("[STATS] After normalization:")
    print(f"[STATS]   missing_ontology_nodes={sum(after_missing.values())}")
    print(f"[STATS]   missing_unique_tools={len(after_missing)}")
    print(f"[STATS] alias_replacement_count={sum(alias_distribution.values())}")
    print(
        "[STATS] alias_distribution="
        f"{json.dumps(_counter_to_sorted_dict(alias_distribution), ensure_ascii=False)}"
    )
    print(
        "[STATS] unresolved_tools_after_alias_normalization="
        f"{json.dumps(_counter_to_sorted_dict(after_missing), ensure_ascii=False)}"
    )
    print(
        "[STATS] operation_distribution="
        f"{json.dumps(dict(sorted(operation_distribution.items())), ensure_ascii=False)}"
    )
    print(
        "[STATS] artifact_transition_distribution="
        f"{json.dumps(dict(sorted(artifact_transition_distribution.items())), ensure_ascii=False)}"
    )
    print(f"[STATS] average_nodes_per_workflow={avg_nodes:.4f}")
    print(f"[STATS] average_edges_per_workflow={avg_edges:.4f}")
    top_k = max(int(top_k_unresolved), 0)
    if top_k:
        top_unresolved = {
            key: int(value)
            for key, value in after_missing.most_common(top_k)
        }
        print(
            f"[STATS] top_{top_k}_unresolved_tools="
            f"{json.dumps(top_unresolved, ensure_ascii=False)}"
        )


def _load_gold_records(path: Path) -> List[Dict[str, Any]]:
    payload = _read_json_or_jsonl(path)
    return _coerce_record_list(payload, label="gold workflow")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert TaskBench gold workflows into typed action graph JSONL."
    )
    parser.add_argument("--gold", required=True, type=Path, help="Path to TaskBench gold data.json/jsonl.")
    parser.add_argument("--ontology", required=True, type=Path, help="Path to action_ontology.json/jsonl.")
    parser.add_argument(
        "--aliases",
        type=Path,
        default=None,
        help="Optional tool_aliases.json. Defaults to action ontology directory/tool_aliases.json.",
    )
    parser.add_argument("--output", required=True, type=Path, help="Path to gold_action_graph.jsonl.")
    parser.add_argument(
        "--remaining_missing_output",
        type=Path,
        default=None,
        help="Optional path for remaining_missing_tools.json. Defaults to output directory.",
    )
    parser.add_argument("--top_k_unresolved", type=int, default=30)
    args = parser.parse_args()

    try:
        ontology = load_ontology(args.ontology)
        if not ontology:
            raise ValueError(f"ontology has no usable tool entries: {args.ontology}")
        alias_path = args.aliases if args.aliases is not None else args.ontology.parent / "tool_aliases.json"
        alias_map = load_aliases(alias_path, ontology)

        gold_records = _load_gold_records(args.gold)
        before_missing = _count_missing_before_normalization(gold_records, ontology)
        output_records: List[Dict[str, Any]] = []
        after_missing: Counter[str] = Counter()
        alias_distribution: Counter[str] = Counter()
        for sample in gold_records:
            record, missing_tools, alias_replacements = build_typed_action_graph(sample, ontology, alias_map)
            output_records.append(record)
            after_missing.update(missing_tools)
            alias_distribution.update(alias_replacements)

        write_jsonl(args.output, output_records)
        remaining_missing_output = (
            args.remaining_missing_output
            if args.remaining_missing_output is not None
            else args.output.with_name("remaining_missing_tools.json")
        )
        _write_json(remaining_missing_output, _counter_to_sorted_dict(after_missing))
        print(f"[DONE] wrote {args.output}")
        print(f"[DONE] wrote {remaining_missing_output}")
        if alias_path.exists():
            print(f"[INFO] aliases={alias_path}")
        else:
            print(f"[INFO] aliases not found; used ontology identity normalization only: {alias_path}")
        _print_stats(
            output_records,
            before_missing,
            after_missing,
            alias_distribution,
            top_k_unresolved=args.top_k_unresolved,
        )
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
