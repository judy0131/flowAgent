from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from .export_three_tables import (
        _materialize_semantic_graph,
        _normalize_task_name,
        _read_jsonl,
        _resolve_data_dir_arg,
    )
except ImportError:
    from export_three_tables import (  # type: ignore
        _materialize_semantic_graph,
        _normalize_task_name,
        _read_jsonl,
        _resolve_data_dir_arg,
    )


QUALITY_SCORE_WEIGHTS = {
    "exact": 4.0,
    "node_f1": 2.0,
    "edge_f1": 2.0,
    "arg_value_f1": 1.0,
}


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_maybe_json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _multiset_f1(pred_items: Iterable[str], gold_items: Iterable[str]) -> Tuple[float, float, float]:
    pred_counter = Counter(pred_items)
    gold_counter = Counter(gold_items)
    matches = sum(min(pred_counter[key], gold_counter[key]) for key in set(pred_counter) | set(gold_counter))
    pred_total = sum(pred_counter.values())
    gold_total = sum(gold_counter.values())

    precision = matches / pred_total if pred_total else (1.0 if gold_total == 0 else 0.0)
    recall = matches / gold_total if gold_total else (1.0 if pred_total == 0 else 0.0)
    if precision + recall == 0.0:
        return precision, recall, 0.0
    return precision, recall, 2.0 * precision * recall / (precision + recall)


def _normalize_case_graph(record: Dict[str, Any], *, step_ref_base: str) -> Dict[str, Any]:
    raw_task_nodes = record.get("task_nodes")
    raw_task_links = record.get("task_links")
    if raw_task_nodes is None:
        raw_task_nodes = record.get("tool_nodes", [])
    if raw_task_links is None:
        raw_task_links = record.get("tool_links", [])

    task_nodes = _parse_maybe_json_list(raw_task_nodes)
    task_links = _parse_maybe_json_list(raw_task_links)
    node_names, normalized_links, normalized_arguments = _materialize_semantic_graph(
        task_nodes,
        task_links,
        step_ref_base=step_ref_base,
    )
    flattened_arguments: List[str] = []
    for idx, tokens in enumerate(normalized_arguments):
        node_name = node_names[idx] if idx < len(node_names) else ""
        for token in tokens:
            flattened_arguments.append(f"{node_name}|{token}")
    link_pairs = sorted((str(link.get("source", "")), str(link.get("target", ""))) for link in normalized_links)
    return {
        "node_names": node_names,
        "node_counter": Counter(node_names),
        "link_pairs": link_pairs,
        "link_pair_set": set(link_pairs),
        "flattened_arguments": sorted(flattened_arguments),
        "has_edges": bool(link_pairs),
    }


def _evaluate_candidate_result(
    candidate_result: Dict[str, Any],
    gold_result: Dict[str, Any],
    *,
    step_ref_base: str,
) -> Dict[str, Any]:
    pred_graph = _normalize_case_graph(candidate_result, step_ref_base=step_ref_base)
    gold_graph = _normalize_case_graph(gold_result, step_ref_base=step_ref_base)

    _, _, node_f1 = _multiset_f1(pred_graph["node_names"], gold_graph["node_names"])
    _, _, arg_value_f1 = _multiset_f1(pred_graph["flattened_arguments"], gold_graph["flattened_arguments"])

    if gold_graph["has_edges"]:
        _, _, edge_f1 = _multiset_f1(pred_graph["link_pair_set"], gold_graph["link_pair_set"])
    else:
        edge_f1 = None

    exact_match = (
        pred_graph["node_names"] == gold_graph["node_names"]
        and pred_graph["link_pairs"] == gold_graph["link_pairs"]
        and pred_graph["flattened_arguments"] == gold_graph["flattened_arguments"]
    )

    edge_component = 1.0 if edge_f1 is None else edge_f1
    weighted_sum = (
        QUALITY_SCORE_WEIGHTS["exact"] * float(exact_match)
        + QUALITY_SCORE_WEIGHTS["node_f1"] * node_f1
        + QUALITY_SCORE_WEIGHTS["edge_f1"] * edge_component
        + QUALITY_SCORE_WEIGHTS["arg_value_f1"] * arg_value_f1
    )
    max_weight = sum(QUALITY_SCORE_WEIGHTS.values())
    quality_score = weighted_sum / max_weight if max_weight else 0.0

    return {
        "exact_match": exact_match,
        "node_f1": node_f1,
        "edge_f1": edge_f1,
        "arg_value_f1": arg_value_f1,
        "quality_score": quality_score,
        "has_edge_gold": gold_graph["has_edges"],
    }


def _candidate_sort_key(item: Dict[str, Any]) -> Tuple[float, float, float, float, float]:
    edge_component = 1.0 if item.get("edge_f1") is None else _safe_float(item.get("edge_f1"))
    return (
        _safe_float(item.get("quality_score")),
        float(bool(item.get("exact_match"))),
        _safe_float(item.get("node_f1")),
        edge_component,
        _safe_float(item.get("arg_value_f1")),
    )


def _candidate_family_name(item: Dict[str, Any]) -> str:
    family = str(item.get("family_name") or item.get("strategy_name") or "").strip()
    return family or "unknown"


def _candidate_variant_name(item: Dict[str, Any]) -> str:
    variant = str(item.get("variant_name") or item.get("strategy_name") or "").strip()
    return variant or _candidate_family_name(item)


def _workflow_signature_from_result(result: Dict[str, Any]) -> str:
    task_nodes = result.get("task_nodes", [])
    if not isinstance(task_nodes, list):
        task_nodes = []
    task_links = result.get("task_links", [])
    if not isinstance(task_links, list):
        task_links = []

    normalized_nodes: List[Dict[str, Any]] = []
    for node in task_nodes:
        if not isinstance(node, dict):
            continue
        normalized_nodes.append(
            {
                "task": node.get("task"),
                "arguments": list(node.get("arguments") or []),
            }
        )

    normalized_links: List[Dict[str, Any]] = []
    for link in task_links:
        if not isinstance(link, dict):
            continue
        normalized_links.append(
            {
                "source": link.get("source"),
                "target": link.get("target"),
            }
        )
    normalized_links.sort(key=lambda item: (str(item.get("source", "")), str(item.get("target", ""))))
    return json.dumps(
        {
            "task_nodes": normalized_nodes,
            "task_links": normalized_links,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _structure_signature_from_result(result: Dict[str, Any]) -> str:
    task_nodes = result.get("task_nodes", [])
    if not isinstance(task_nodes, list):
        task_nodes = []

    normalized_nodes: List[Dict[str, Any]] = []
    for node in task_nodes:
        if not isinstance(node, dict):
            continue
        upstream_inputs: Dict[str, int] = {}
        raw_arguments = node.get("arguments", [])
        if not isinstance(raw_arguments, list):
            raw_arguments = []
        for arg_idx, arg in enumerate(raw_arguments, start=1):
            match = re.fullmatch(r"<node-(\d+)>", str(arg).strip())
            if match:
                upstream_inputs[f"arg{arg_idx}"] = int(match.group(1))
        normalized_nodes.append(
            {
                "task": node.get("task"),
                "upstream_inputs": upstream_inputs,
            }
        )
    return json.dumps(normalized_nodes, ensure_ascii=False, sort_keys=True)


def _candidate_workflow_signature(item: Dict[str, Any], candidate_result: Dict[str, Any]) -> str:
    signature = str(item.get("workflow_signature") or item.get("signature") or "").strip()
    return signature or _workflow_signature_from_result(candidate_result)


def _candidate_structure_signature(item: Dict[str, Any], candidate_result: Dict[str, Any]) -> str:
    signature = str(item.get("structure_signature") or "").strip()
    return signature or _structure_signature_from_result(candidate_result)


def _parse_structure_signature(signature: str) -> List[Tuple[str, Tuple[Tuple[str, int], ...]]]:
    if not signature:
        return []
    try:
        payload = json.loads(signature)
    except Exception:
        return []
    if not isinstance(payload, list):
        return []

    parsed: List[Tuple[str, Tuple[Tuple[str, int], ...]]] = []
    for node in payload:
        if not isinstance(node, dict):
            continue
        task = str(node.get("task", "")).strip()
        upstream_inputs = node.get("upstream_inputs", {})
        normalized_inputs: List[Tuple[str, int]] = []
        if isinstance(upstream_inputs, dict):
            for key, value in upstream_inputs.items():
                try:
                    normalized_inputs.append((str(key).strip(), int(value)))
                except (TypeError, ValueError):
                    continue
        normalized_inputs.sort(key=lambda item: item[0])
        parsed.append((task, tuple(normalized_inputs)))
    return parsed


def _levenshtein_distance(a: List[Tuple[str, Tuple[Tuple[str, int], ...]]], b: List[Tuple[str, Tuple[Tuple[str, int], ...]]]) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)

    prev = list(range(len(b) + 1))
    for i, item_a in enumerate(a, start=1):
        curr = [i]
        for j, item_b in enumerate(b, start=1):
            cost = 0 if item_a == item_b else 1
            curr.append(
                min(
                    prev[j] + 1,
                    curr[j - 1] + 1,
                    prev[j - 1] + cost,
                )
            )
        prev = curr
    return prev[-1]


def _structure_signature_distance(signature_a: str, signature_b: str) -> float:
    parsed_a = _parse_structure_signature(signature_a)
    parsed_b = _parse_structure_signature(signature_b)
    max_len = max(len(parsed_a), len(parsed_b), 1)
    return float(_levenshtein_distance(parsed_a, parsed_b)) / float(max_len)


def _pairwise_structure_distances(candidates: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Optional[float]]:
    distances: List[Dict[str, Any]] = []
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            left = candidates[i]
            right = candidates[j]
            distance = _structure_signature_distance(
                str(left.get("structure_signature", "")),
                str(right.get("structure_signature", "")),
            )
            distances.append(
                {
                    "candidate_a_id": left.get("id"),
                    "candidate_b_id": right.get("id"),
                    "family_a": _candidate_family_name(left),
                    "family_b": _candidate_family_name(right),
                    "variant_a": _candidate_variant_name(left),
                    "variant_b": _candidate_variant_name(right),
                    "distance": distance,
                }
            )
    if not distances:
        return distances, None
    return distances, mean(float(item["distance"]) for item in distances)


def _selected_candidate_from_dump(row: Dict[str, Any], evaluated_candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    selected_plan_id = row.get("selected_plan_id")
    for candidate in evaluated_candidates:
        if candidate.get("id") == selected_plan_id:
            return candidate
    return None


def _analyze_case(
    row: Dict[str, Any],
    gold_row: Dict[str, Any],
    *,
    step_ref_base: str,
) -> Dict[str, Any]:
    raw_candidates = row.get("candidates", [])
    if not isinstance(raw_candidates, list):
        raw_candidates = []

    evaluated_candidates: List[Dict[str, Any]] = []
    for candidate in raw_candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_result = candidate.get("result", {})
        if not isinstance(candidate_result, dict):
            continue
        metrics = _evaluate_candidate_result(candidate_result, gold_row, step_ref_base=step_ref_base)
        evaluated_candidates.append(
            {
                "id": candidate.get("id"),
                "generation_index": candidate.get("generation_index"),
                "strategy_name": candidate.get("strategy_name"),
                "family_name": _candidate_family_name(candidate),
                "variant_name": _candidate_variant_name(candidate),
                "sampling_temperature": candidate.get("sampling_temperature"),
                "planner_score": candidate.get("score"),
                "planner_score_details": candidate.get("score_details"),
                "dependency_check": candidate.get("dependency_check"),
                "selection_meta": candidate.get("selection_meta"),
                "workflow_signature": _candidate_workflow_signature(candidate, candidate_result),
                "structure_signature": _candidate_structure_signature(candidate, candidate_result),
                **metrics,
            }
        )

    selected = _selected_candidate_from_dump(row, evaluated_candidates)
    selected_from_candidate_pool = selected is not None
    if selected is None:
        selected_metrics = _evaluate_candidate_result(
            row.get("selected_result", {}),
            gold_row,
            step_ref_base=step_ref_base,
        )
        selected = {
            "id": row.get("selected_plan_id"),
            "generation_index": None,
            "strategy_name": "selected_result_fallback",
            "family_name": "selected_result_fallback",
            "variant_name": "selected_result_fallback",
            "sampling_temperature": None,
            "planner_score": row.get("selected_candidate_score"),
            "planner_score_details": None,
            "dependency_check": None,
            "selection_meta": None,
            "workflow_signature": _workflow_signature_from_result(row.get("selected_result", {})),
            "structure_signature": _structure_signature_from_result(row.get("selected_result", {})),
            **selected_metrics,
        }

    oracle_candidates = evaluated_candidates if evaluated_candidates else [selected]
    structural_unique_candidate_count = len(
        {
            str(item.get("structure_signature", "")).strip()
            for item in oracle_candidates
            if str(item.get("structure_signature", "")).strip()
        }
    )
    exact_unique_candidate_count = len(
        {
            str(item.get("workflow_signature", "")).strip()
            for item in oracle_candidates
            if str(item.get("workflow_signature", "")).strip()
        }
    )
    pairwise_structure_distance, mean_pairwise_structure_distance = _pairwise_structure_distances(
        oracle_candidates
    )
    family_upper_bounds: Dict[str, float] = {}
    for item in oracle_candidates:
        family_name = _candidate_family_name(item)
        family_upper_bounds[family_name] = max(
            family_upper_bounds.get(family_name, float("-inf")),
            _safe_float(item.get("quality_score")),
        )
    best_quality = max(oracle_candidates, key=_candidate_sort_key)
    best_node = max(oracle_candidates, key=lambda item: _safe_float(item.get("node_f1")))
    best_edge = None
    edge_candidates = [item for item in oracle_candidates if item.get("edge_f1") is not None]
    if edge_candidates:
        best_edge = max(edge_candidates, key=lambda item: _safe_float(item.get("edge_f1")))

    exact_oracle = any(bool(item.get("exact_match")) for item in oracle_candidates)
    node_oracle = any(abs(_safe_float(item.get("node_f1")) - 1.0) <= 1e-9 for item in oracle_candidates)
    has_edge_gold = bool(best_quality.get("has_edge_gold", False))
    edge_oracle = None
    if has_edge_gold:
        edge_oracle = any(abs(_safe_float(item.get("edge_f1")) - 1.0) <= 1e-9 for item in edge_candidates)

    selected_edge_f1 = selected.get("edge_f1")
    selected_edge_component = 1.0 if selected_edge_f1 is None else _safe_float(selected_edge_f1)
    best_edge_component = selected_edge_component if best_edge is None else _safe_float(best_edge.get("edge_f1"))
    selected_quality = _safe_float(selected.get("quality_score"))
    best_quality_score = _safe_float(best_quality.get("quality_score"))

    return {
        "id": str(row.get("id", "")),
        "type": str(gold_row.get("type", gold_row.get("method", "overall"))),
        "candidate_count": len(evaluated_candidates),
        "structural_unique_candidate_count": structural_unique_candidate_count,
        "exact_unique_candidate_count": exact_unique_candidate_count,
        "structure_diversity": (
            float(structural_unique_candidate_count) / float(len(oracle_candidates))
            if oracle_candidates
            else 0.0
        ),
        "pairwise_structure_distance": pairwise_structure_distance,
        "mean_pairwise_structure_distance": mean_pairwise_structure_distance,
        "oracle_upper_bound_by_family": family_upper_bounds,
        "selected_in_candidate_pool": selected_from_candidate_pool,
        "selection_route": row.get("selection_route"),
        "selected_plan_id": row.get("selected_plan_id"),
        "selected_candidate_id": selected.get("id"),
        "selected_strategy_name": selected.get("strategy_name"),
        "selected_family_name": _candidate_family_name(selected),
        "selected_variant_name": _candidate_variant_name(selected),
        "best_quality_candidate_id": best_quality.get("id"),
        "best_quality_strategy_name": best_quality.get("strategy_name"),
        "best_quality_family_name": _candidate_family_name(best_quality),
        "best_quality_variant_name": _candidate_variant_name(best_quality),
        "best_quality_score": best_quality_score,
        "selected_quality_score": selected_quality,
        "rerank_regret": max(best_quality_score - selected_quality, 0.0),
        "oracle_better": best_quality_score > selected_quality + 1e-9,
        "exact_oracle": exact_oracle,
        "node_oracle": node_oracle,
        "edge_oracle": edge_oracle,
        "selected_exact": bool(selected.get("exact_match")),
        "selected_node_f1": _safe_float(selected.get("node_f1")),
        "selected_edge_f1": selected_edge_f1,
        "selected_arg_value_f1": _safe_float(selected.get("arg_value_f1")),
        "best_node_f1": _safe_float(best_node.get("node_f1")),
        "best_edge_f1": None if best_edge is None else _safe_float(best_edge.get("edge_f1")),
        "best_arg_value_f1": _safe_float(best_quality.get("arg_value_f1")),
        "node_oracle_gain": max(_safe_float(best_node.get("node_f1")) - _safe_float(selected.get("node_f1")), 0.0),
        "edge_oracle_gain": None if best_edge is None else max(best_edge_component - selected_edge_component, 0.0),
        "quality_oracle_gain": max(best_quality_score - selected_quality, 0.0),
        "selected_planner_score": selected.get("planner_score"),
        "best_quality_planner_score": best_quality.get("planner_score"),
        "candidates": evaluated_candidates,
    }


def _aggregate_case_rows(rows: List[Dict[str, Any]], *, label: str) -> Dict[str, Any]:
    edge_rows = [row for row in rows if row.get("edge_oracle") is not None]

    def _rate(items: List[Any]) -> float:
        return mean(1.0 if bool(item) else 0.0 for item in items) if items else 0.0

    def _mean_numeric(values: List[Optional[float]]) -> float:
        usable = [float(v) for v in values if v is not None]
        return mean(usable) if usable else 0.0

    return {
        "split": label,
        "case_count": len(rows),
        "edge_oracle_support": len(edge_rows),
        "mean_structural_unique_candidate_count": _mean_numeric(
            [row.get("structural_unique_candidate_count") for row in rows]
        ),
        "mean_exact_unique_candidate_count": _mean_numeric(
            [row.get("exact_unique_candidate_count") for row in rows]
        ),
        "mean_structure_diversity": _mean_numeric([row.get("structure_diversity") for row in rows]),
        "mean_pairwise_structure_distance": _mean_numeric(
            [row.get("mean_pairwise_structure_distance") for row in rows]
        ),
        "exact_oracle_rate": _rate([row.get("exact_oracle") for row in rows]),
        "node_oracle_rate": _rate([row.get("node_oracle") for row in rows]),
        "edge_oracle_rate": _rate([row.get("edge_oracle") for row in edge_rows]),
        "oracle_better_rate": _rate([row.get("oracle_better") for row in rows]),
        "selected_exact_rate": _rate([row.get("selected_exact") for row in rows]),
        "selected_mean_node_f1": _mean_numeric([row.get("selected_node_f1") for row in rows]),
        "best_mean_node_f1": _mean_numeric([row.get("best_node_f1") for row in rows]),
        "selected_mean_edge_f1": _mean_numeric([row.get("selected_edge_f1") for row in edge_rows]),
        "best_mean_edge_f1": _mean_numeric([row.get("best_edge_f1") for row in edge_rows]),
        "mean_node_oracle_gain": _mean_numeric([row.get("node_oracle_gain") for row in rows]),
        "mean_edge_oracle_gain": _mean_numeric([row.get("edge_oracle_gain") for row in edge_rows]),
        "mean_quality_oracle_gain": _mean_numeric([row.get("quality_oracle_gain") for row in rows]),
        "mean_rerank_regret": _mean_numeric([row.get("rerank_regret") for row in rows]),
        "mean_selected_quality_score": _mean_numeric([row.get("selected_quality_score") for row in rows]),
        "mean_best_quality_score": _mean_numeric([row.get("best_quality_score") for row in rows]),
    }


def _write_summary_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    fieldnames = [
        "split",
        "case_count",
        "edge_oracle_support",
        "mean_structural_unique_candidate_count",
        "mean_exact_unique_candidate_count",
        "mean_structure_diversity",
        "mean_pairwise_structure_distance",
        "exact_oracle_rate",
        "node_oracle_rate",
        "edge_oracle_rate",
        "oracle_better_rate",
        "selected_exact_rate",
        "selected_mean_node_f1",
        "best_mean_node_f1",
        "selected_mean_edge_f1",
        "best_mean_edge_f1",
        "mean_node_oracle_gain",
        "mean_edge_oracle_gain",
        "mean_quality_oracle_gain",
        "mean_rerank_regret",
        "mean_selected_quality_score",
        "mean_best_quality_score",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_case_details_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _counter_to_sorted_dict(counter: Counter) -> Dict[str, int]:
    return {str(key): int(counter[key]) for key in sorted(counter.keys(), key=lambda item: (int(item) if str(item).isdigit() else str(item)))}


def _family_upper_bound_summary(rows: List[Dict[str, Any]]) -> Tuple[Dict[str, float], Dict[str, int]]:
    family_values: Dict[str, List[float]] = {}
    family_support: Dict[str, int] = {}
    for row in rows:
        payload = row.get("oracle_upper_bound_by_family", {})
        if not isinstance(payload, dict):
            continue
        for family, score in payload.items():
            family_name = str(family).strip()
            if not family_name:
                continue
            family_values.setdefault(family_name, []).append(_safe_float(score))
            family_support[family_name] = family_support.get(family_name, 0) + 1

    summarized = {
        family: mean(scores)
        for family, scores in sorted(family_values.items(), key=lambda item: item[0])
        if scores
    }
    return summarized, family_support


def _write_case_family_regret_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    fieldnames = [
        "case_id",
        "type",
        "candidate_count",
        "structural_unique_candidate_count",
        "exact_unique_candidate_count",
        "structure_diversity",
        "mean_pairwise_structure_distance",
        "selected_family",
        "selected_variant",
        "oracle_best_family",
        "oracle_best_variant",
        "selected_score",
        "oracle_score",
        "regret",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "case_id": row.get("id"),
                    "type": row.get("type"),
                    "candidate_count": row.get("candidate_count"),
                    "structural_unique_candidate_count": row.get("structural_unique_candidate_count"),
                    "exact_unique_candidate_count": row.get("exact_unique_candidate_count"),
                    "structure_diversity": row.get("structure_diversity"),
                    "mean_pairwise_structure_distance": row.get("mean_pairwise_structure_distance"),
                    "selected_family": row.get("selected_family_name"),
                    "selected_variant": row.get("selected_variant_name"),
                    "oracle_best_family": row.get("best_quality_family_name"),
                    "oracle_best_variant": row.get("best_quality_variant_name"),
                    "selected_score": row.get("selected_quality_score"),
                    "oracle_score": row.get("best_quality_score"),
                    "regret": row.get("rerank_regret"),
                }
            )


def analyze_candidate_dump(
    *,
    data_dir: Path,
    candidate_dump_path: Path,
    save_dir: Path,
    step_ref_base: str,
) -> Dict[str, Any]:
    gold_rows = {
        str(row.get("id", "")): row
        for row in _read_jsonl(data_dir / "data.json")
    }
    candidate_rows = _read_jsonl(candidate_dump_path)

    analyzed_rows: List[Dict[str, Any]] = []
    skipped_ids: List[str] = []
    for row in candidate_rows:
        sid = str(row.get("id", ""))
        gold_row = gold_rows.get(sid)
        if gold_row is None:
            skipped_ids.append(sid)
            continue
        analyzed_rows.append(_analyze_case(row, gold_row, step_ref_base=step_ref_base))

    summary_rows = [_aggregate_case_rows(analyzed_rows, label="overall")]
    for split in ("single", "chain", "dag"):
        split_rows = [row for row in analyzed_rows if str(row.get("type", "")).lower() == split]
        if split_rows:
            summary_rows.append(_aggregate_case_rows(split_rows, label=split))

    overall_summary = next((row for row in summary_rows if row.get("split") == "overall"), {})
    structural_unique_candidate_count_distribution = _counter_to_sorted_dict(
        Counter(int(row.get("structural_unique_candidate_count", 0)) for row in analyzed_rows)
    )
    exact_unique_candidate_count_distribution = _counter_to_sorted_dict(
        Counter(int(row.get("exact_unique_candidate_count", 0)) for row in analyzed_rows)
    )
    oracle_best_family_distribution = {
        key: int(value)
        for key, value in sorted(
            Counter(str(row.get("best_quality_family_name", "")).strip() for row in analyzed_rows if str(row.get("best_quality_family_name", "")).strip()).items(),
            key=lambda item: item[0],
        )
    }
    family_win_count = dict(oracle_best_family_distribution)
    oracle_upper_bound_by_family, oracle_upper_bound_by_family_support = _family_upper_bound_summary(analyzed_rows)
    mean_structure_diversity = mean(float(row.get("structure_diversity", 0.0)) for row in analyzed_rows) if analyzed_rows else 0.0
    pairwise_distance_rows = [
        float(row.get("mean_pairwise_structure_distance"))
        for row in analyzed_rows
        if row.get("mean_pairwise_structure_distance") is not None
    ]
    mean_pairwise_structure_distance = mean(pairwise_distance_rows) if pairwise_distance_rows else 0.0

    _ensure_dir(save_dir)
    summary_csv_path = save_dir / "oracle_summary.csv"
    summary_json_path = save_dir / "oracle_summary.json"
    case_details_path = save_dir / "oracle_case_details.jsonl"
    case_family_regret_report_path = save_dir / "case_family_regret_report.csv"
    case_family_regret_summary_path = save_dir / "case_family_regret_summary.json"

    _write_summary_csv(summary_csv_path, summary_rows)
    _write_case_details_jsonl(case_details_path, analyzed_rows)
    _write_case_family_regret_csv(case_family_regret_report_path, analyzed_rows)
    summary_payload = {
        "data_dir": str(data_dir),
        "candidate_dump_path": str(candidate_dump_path),
        "save_dir": str(save_dir),
        "step_ref_base": step_ref_base,
        "quality_score_weights": QUALITY_SCORE_WEIGHTS,
        "case_count": len(analyzed_rows),
        "skipped_ids": skipped_ids,
        "summary_rows": summary_rows,
        "structural_unique_candidate_count_distribution": structural_unique_candidate_count_distribution,
        "exact_unique_candidate_count_distribution": exact_unique_candidate_count_distribution,
        "oracle_best_family_distribution": oracle_best_family_distribution,
        "family_win_count": family_win_count,
        "structure_diversity": {
            "mean": mean_structure_diversity,
            "max": max((float(row.get("structure_diversity", 0.0)) for row in analyzed_rows), default=0.0),
            "min": min((float(row.get("structure_diversity", 0.0)) for row in analyzed_rows), default=0.0),
        },
        "oracle_upper_bound_by_family": oracle_upper_bound_by_family,
        "oracle_upper_bound_by_family_support": oracle_upper_bound_by_family_support,
        "pairwise_structure_distance": {
            "mean": mean_pairwise_structure_distance,
            "case_support": len(pairwise_distance_rows),
        },
        "mean_edge_oracle_gain": overall_summary.get("mean_edge_oracle_gain", 0.0),
        "mean_node_oracle_gain": overall_summary.get("mean_node_oracle_gain", 0.0),
        "oracle_better_rate": overall_summary.get("oracle_better_rate", 0.0),
        "summary_csv_path": str(summary_csv_path),
        "case_details_path": str(case_details_path),
        "case_family_regret_report_path": str(case_family_regret_report_path),
        "case_family_regret_summary_path": str(case_family_regret_summary_path),
    }
    summary_json_path.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    case_family_regret_summary_path.write_text(
        json.dumps(summary_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary_payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline oracle analysis for saved candidate pools.")
    parser.add_argument("--data_dir", type=str, default="taskbench/data_multimedia")
    parser.add_argument("--candidate_dump_path", type=str, required=True)
    parser.add_argument("--save_dir", type=str, default=None)
    parser.add_argument("--step_ref_base", choices=["one", "zero"], default="one")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    data_dir = _resolve_data_dir_arg(args.data_dir)
    candidate_dump_path = Path(args.candidate_dump_path).resolve()
    if not candidate_dump_path.exists():
        raise FileNotFoundError(f"candidate_dump_path not found: {candidate_dump_path}")

    if args.save_dir:
        save_dir = Path(args.save_dir).resolve()
    else:
        save_dir = (candidate_dump_path.parent.parent / "oracle_analysis").resolve()

    result = analyze_candidate_dump(
        data_dir=data_dir,
        candidate_dump_path=candidate_dump_path,
        save_dir=save_dir,
        step_ref_base=args.step_ref_base,
    )
    print(f"[DONE] oracle analysis written to {result['save_dir']}")
    print(f"[DONE] summary_csv={result['summary_csv_path']}")
    print(f"[DONE] case_details={result['case_details_path']}")


if __name__ == "__main__":
    main()
