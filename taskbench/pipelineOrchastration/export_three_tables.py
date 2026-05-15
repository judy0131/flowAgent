import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple


def _resolve_data_dir_arg(data_dir_arg: str) -> Path:
    raw = Path(data_dir_arg)
    if raw.is_absolute():
        return raw.resolve()

    cwd_candidate = raw.resolve()
    if cwd_candidate.exists():
        return cwd_candidate

    repo_root = Path(__file__).resolve().parents[2]
    repo_candidate = (repo_root / raw).resolve()
    if repo_candidate.exists():
        return repo_candidate
    return cwd_candidate


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _ensure_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [value]
    return [value]


def _parse_maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _normalize_task_name(name: Any) -> str:
    return str(name).replace("_", " ").strip()


def _pct(v: Any) -> str:
    if v is None:
        return ""
    return f"{float(v) * 100:.2f}"


def _normalize_arg_text(value: Any) -> str:
    text = str(value).strip()
    if not text:
        return text
    if len(text) >= 2 and text[0] == "{" and text[-1] == "}":
        text = text[1:-1].strip()
        if not text:
            return text
    return text


def _looks_like_node_reference(text: str) -> bool:
    if re.fullmatch(r"<node-\d+(?:[^>]*)>", text, flags=re.IGNORECASE):
        return True
    return re.fullmatch(
        r"(?i)(?:\<?\s*)?"
        r"(?:(?:output|result|audio|video|text|image|file|data)(?:[\s_-]+\w+)*)?"
        r"(?:[\s_-]+(?:of|from))?"
        r"[\s_-]*step[\s_-]*\d+"
        r"(?:\s*\([^)]*\))?"
        r"(?:\s*\>?)?",
        text,
    ) is not None


def _build_incoming_source_map(task_links: Any) -> Dict[str, set[str]]:
    incoming_sources: Dict[str, set[str]] = {}
    if not isinstance(task_links, list):
        return incoming_sources
    for link in task_links:
        if not isinstance(link, dict):
            continue
        source = _normalize_task_name(link.get("source", ""))
        target = _normalize_task_name(link.get("target", ""))
        if not source or not target:
            continue
        incoming_sources.setdefault(target, set()).add(source)
    return incoming_sources


def _resolve_node_reference(
    text: str,
    current_index: int,
    node_names: List[str],
    current_task: str,
    incoming_source_map: Dict[str, set[str]],
    step_ref_base: str = "one",
) -> int | None:
    node_match = re.fullmatch(r"<node-(\d+)(?:[^>]*)>", text, flags=re.IGNORECASE)
    if node_match:
        raw_index = int(node_match.group(1))
        candidates: List[int] = []
        for candidate in (raw_index, raw_index - 1):
            if 0 <= candidate < len(node_names) and candidate < current_index and candidate not in candidates:
                candidates.append(candidate)
        if not candidates:
            return None

        expected_sources = incoming_source_map.get(current_task, set())
        if expected_sources:
            matched_candidates = [
                candidate for candidate in candidates if _normalize_task_name(node_names[candidate]) in expected_sources
            ]
            if len(matched_candidates) == 1:
                return matched_candidates[0]
            if matched_candidates:
                candidates = matched_candidates

        if raw_index == current_index and (raw_index - 1) in candidates:
            return raw_index - 1
        if raw_index in candidates:
            return raw_index
        return max(candidates)

    match = re.fullmatch(
        r"(?i)(?:\<?\s*)?"
        r"(?:(?:output|result|audio|video|text|image|file|data)(?:[\s_-]+\w+)*)?"
        r"(?:[\s_-]+(?:of|from))?"
        r"[\s_-]*step[\s_-]*(\d+)"
        r"(?:\s*\([^)]*\))?"
        r"(?:\s*\>?)?",
        text,
    )
    if not match:
        return None

    step_no = int(match.group(1))
    if step_ref_base == "zero":
        idx = max(step_no - 1, 0)
    else:
        idx = step_no - 1
    if 0 <= idx < len(node_names) and idx < current_index:
        return idx
    return None


def _normalize_link_pairs(links: Any) -> List[Dict[str, str]]:
    pairs: List[Dict[str, str]] = []
    seen = set()
    if not isinstance(links, list):
        return pairs
    for link in links:
        if not isinstance(link, dict):
            continue
        source = _normalize_task_name(link.get("source", ""))
        target = _normalize_task_name(link.get("target", ""))
        if not source or not target:
            continue
        pair = (source.lower(), target.lower())
        if pair in seen:
            continue
        seen.add(pair)
        pairs.append({"source": pair[0], "target": pair[1]})
    return pairs


def _normalize_argument_token(
    value: Any,
    current_index: int,
    node_names: List[str],
    current_task: str,
    incoming_source_map: Dict[str, set[str]],
    step_ref_base: str = "one",
) -> str | None:
    raw_value = value
    if isinstance(raw_value, dict):
        raw_value = raw_value.get("value")
    if isinstance(raw_value, list):
        raw_value = " ".join(str(item) for item in raw_value)
    if raw_value is None:
        return None

    text = _normalize_arg_text(raw_value)
    if not text:
        return None

    ref_index = _resolve_node_reference(
        text,
        current_index,
        node_names,
        current_task,
        incoming_source_map,
        step_ref_base=step_ref_base,
    )
    if ref_index is not None:
        return f"ref:{_normalize_task_name(node_names[ref_index]).lower()}"
    if _looks_like_node_reference(text):
        return None
    return f"lit:{text.lower()}"


def _materialize_semantic_graph(
    nodes: Any,
    declared_links: Any,
    step_ref_base: str = "one",
) -> Tuple[List[str], List[Dict[str, str]], List[List[str]]]:
    normalized_nodes = list(nodes) if isinstance(nodes, list) else []
    node_names = [
        _normalize_task_name(node.get("task", "")) if isinstance(node, dict) else ""
        for node in normalized_nodes
    ]
    incoming_source_map = _build_incoming_source_map(declared_links)
    normalized_arguments: List[List[str]] = []
    inferred_links: List[Dict[str, str]] = []
    seen_pairs = set()

    for current_index, node in enumerate(normalized_nodes):
        current_task = node_names[current_index]
        tokens: List[str] = []
        arguments = node.get("arguments", []) if isinstance(node, dict) else []
        for argument in _ensure_list(arguments):
            token = _normalize_argument_token(
                argument,
                current_index,
                node_names,
                current_task,
                incoming_source_map,
                step_ref_base=step_ref_base,
            )
            if token is None:
                continue
            tokens.append(token)
            if token.startswith("ref:"):
                source = token[4:]
                pair = (source, current_task.lower())
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    inferred_links.append({"source": source, "target": current_task.lower()})
        normalized_arguments.append(sorted(tokens))

    normalized_node_names = [_normalize_task_name(name).lower() for name in node_names]
    normalized_links = _normalize_link_pairs(inferred_links)
    return normalized_node_names, normalized_links, normalized_arguments


def _write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _extract_date_suffix(text: str) -> str:
    match = re.search(r"(20\d{6})(?!\d)", text)
    return match.group(1) if match else ""


def _format_date_suffix(date_suffix: str) -> str:
    if re.fullmatch(r"\d{8}", date_suffix):
        return f"{date_suffix[:4]}-{date_suffix[4:6]}-{date_suffix[6:]}"
    return date_suffix


def _detect_model_label(text: str) -> str:
    if not text:
        return ""

    haystack = text.strip().lower().replace("_", "-")
    if "gemini-2.5-flash" in haystack:
        return "gemini-2.5-flash"
    if re.search(r"gpt-?5(?:\.)?4-?xhigh", haystack):
        return "gpt-5.4-xhigh"
    if re.search(r"gpt-?5(?:\.)?4", haystack):
        return "gpt-5.4"
    if "qwen-max" in haystack or "qianwen" in haystack:
        return "qwen-max"
    if re.search(r"\bqwen\b", haystack):
        return "qwen"
    if "gemini" in haystack:
        return "gemini"
    return ""


def _model_specificity(model: str) -> int:
    if not model:
        return 0
    if model in {"gemini-2.5-flash", "gpt-5.4-xhigh"}:
        return 3
    if model in {"gpt-5.4", "qwen-max"}:
        return 2
    return 1


def _model_family(model: str) -> str:
    if model.startswith("gpt-"):
        return "gpt"
    if model.startswith("gemini"):
        return "gemini"
    if model.startswith("qwen") or model == "qianwen":
        return "qwen"
    return ""


def _resolve_local_artifact_path(raw_path: Any, fallback_dir: Path) -> Path | None:
    text = str(raw_path or "").strip()
    if not text:
        return None

    candidate = Path(text)
    if candidate.exists():
        return candidate

    local_candidate = (fallback_dir / candidate.name).resolve()
    if local_candidate.exists():
        return local_candidate
    return local_candidate


def _infer_run_artifact_path(
    artifact_dir: Path,
    llm_stem: str,
    summary_suffix: str,
    date_suffix: str,
) -> Path | None:
    if not artifact_dir.exists():
        return None

    patterns: List[str] = []
    if summary_suffix:
        patterns.extend(
            [
                f"{llm_stem}*{summary_suffix}.json",
                f"*{summary_suffix}.json",
            ]
        )
    if date_suffix:
        patterns.extend(
            [
                f"{llm_stem}*{date_suffix}*.json",
                f"*{date_suffix}*.json",
            ]
        )

    preferred_model = _detect_model_label(summary_suffix)
    preferred_family = _model_family(preferred_model)
    summary_tokens = [token.lower() for token in re.findall(r"[A-Za-z]+|\d+", summary_suffix)]
    best_candidate: Path | None = None
    best_score = -1
    seen = set()
    for pattern in patterns:
        for candidate in sorted(artifact_dir.glob(pattern)):
            resolved = candidate.resolve()
            key = str(resolved)
            if key in seen or not resolved.is_file():
                continue
            seen.add(key)
            candidate_family = _model_family(_detect_model_label(resolved.stem))
            if preferred_family and candidate_family and candidate_family != preferred_family:
                continue

            candidate_tokens = {token.lower() for token in re.findall(r"[A-Za-z]+|\d+", resolved.stem)}
            score = sum(1 for token in summary_tokens if token in candidate_tokens)
            if preferred_family and candidate_family == preferred_family:
                score += 2
            if date_suffix and date_suffix in resolved.stem:
                score += 1

            if score > best_score:
                best_score = score
                best_candidate = resolved

    return best_candidate


def _infer_model_label(
    llm_stem: str,
    date_suffix: str,
    llm_label: str,
    *,
    pred_file: Path | None = None,
    metrics_file: Path | None = None,
    extra_hints: List[str] | None = None,
) -> str:
    def _pick_best_model(hints: List[str], family: str = "") -> str:
        best = ""
        for hint in hints:
            model = _detect_model_label(hint)
            if not model:
                continue
            if family and _model_family(model) != family:
                continue
            if _model_specificity(model) > _model_specificity(best):
                best = model
        return best

    strong_hints: List[str] = []
    if extra_hints:
        strong_hints.extend(extra_hints)
    if pred_file is not None:
        strong_hints.extend([pred_file.name, pred_file.stem])
    if metrics_file is not None:
        strong_hints.extend([metrics_file.name, metrics_file.stem])

    weak_hints = [llm_stem, llm_label]

    strong_model = _pick_best_model(strong_hints)
    preferred_family = _model_family(strong_model)
    weak_model = _pick_best_model(weak_hints, family=preferred_family) if preferred_family else _pick_best_model(weak_hints)

    search_dirs: List[Path] = []
    if pred_file is not None:
        search_dirs.append(pred_file.parent)
    if metrics_file is not None:
        search_dirs.append(metrics_file.parent)

    seen_dirs = set()
    for search_dir in search_dirs:
        resolved_dir = search_dir.resolve()
        key = str(resolved_dir)
        if key in seen_dirs or not resolved_dir.exists():
            continue
        seen_dirs.add(key)

        patterns = [f"{llm_stem}*{date_suffix}.json"] if date_suffix else [f"{llm_stem}*.json"]
        if date_suffix:
            patterns.append(f"{llm_stem}*{date_suffix}*.json")
            patterns.append(f"*{date_suffix}.json")
            patterns.append(f"*{date_suffix}*.json")

        for pattern in patterns:
            for candidate in sorted(resolved_dir.glob(pattern)):
                strong_hints.append(candidate.stem)
                strong_hints.append(candidate.name)

    search_model = _pick_best_model(strong_hints, family=preferred_family) if preferred_family else _pick_best_model(strong_hints)

    if strong_model:
        if _model_family(search_model) == _model_family(strong_model) and _model_specificity(search_model) > _model_specificity(strong_model):
            return search_model
        return strong_model
    if search_model:
        return search_model
    return weak_model


def _read_first_csv_row(path: Path) -> Dict[str, str]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            return {str(k): str(v) for k, v in row.items()}
    return {}


def _pick_experiment_csv(summary_path: Path, summary: Dict[str, Any]) -> Path | None:
    suffix = summary_path.stem.replace("00_summary_", "", 1)
    tagged_path = summary_path.with_name(f"01_experiment_comparison_{suffix}.csv")
    if tagged_path.exists():
        return tagged_path

    outputs = summary.get("outputs", {})
    raw_output_path = outputs.get("experiment_comparison_csv") if isinstance(outputs, dict) else None
    output_path = _resolve_local_artifact_path(raw_output_path, summary_path.parent)
    if output_path is not None and output_path.exists():
        return output_path

    legacy_path = summary_path.with_name("01_experiment_comparison.csv")
    if legacy_path.exists():
        return legacy_path
    return None


def _build_recent_results_rows(metrics_root: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    summary_paths = sorted(metrics_root.glob("**/00_summary*.json"), key=lambda p: p.stat().st_mtime, reverse=True)

    for summary_path in summary_paths:
        summary = _read_json(summary_path)
        exp_csv = _pick_experiment_csv(summary_path, summary)
        if exp_csv is None or not exp_csv.exists():
            continue

        exp_row = _read_first_csv_row(exp_csv)
        if not exp_row:
            continue

        summary_suffix = summary_path.stem.replace("00_summary_", "", 1)
        date_suffix = _extract_date_suffix(summary_suffix) or _extract_date_suffix(exp_csv.stem)
        data_root = summary_path.parents[2] if len(summary_path.parents) > 2 else metrics_root.parent
        pred_dir = data_root / "predictions_pipeline_agent"
        metrics_dir = data_root / "metrics_pipeline_agent"

        raw_pred = summary.get("pred_file")
        raw_metrics = summary.get("metrics_file")
        pred_file = _resolve_local_artifact_path(raw_pred, pred_dir)
        metrics_file = _resolve_local_artifact_path(raw_metrics, metrics_dir)

        llm_stem = str(summary.get("llm_stem", "")).strip()
        if not llm_stem:
            pred_name = Path(str(raw_pred or "")).stem
            if pred_name:
                llm_stem = pred_name
                if date_suffix and llm_stem.endswith(f"_{date_suffix}"):
                    llm_stem = llm_stem[: -(len(date_suffix) + 1)]
        if not llm_stem:
            llm_stem = "pipeline_orchestrator_agent"

        if pred_file is None or not pred_file.exists():
            pred_file = _infer_run_artifact_path(pred_dir, llm_stem, summary_suffix, date_suffix)
        if metrics_file is None or not metrics_file.exists():
            metrics_file = _infer_run_artifact_path(metrics_dir, llm_stem, summary_suffix, date_suffix)

        metrics_payload: Dict[str, Any] = {}
        if metrics_file is not None and metrics_file.exists():
            metrics_payload = _read_json(metrics_file)

        llm_label = str(exp_row.get("LLM", "")).strip() or str(summary.get("llm_label", "")).strip()
        model = str(summary.get("model", "")).strip() or str(exp_row.get("Model", "")).strip()
        if not model:
            model = _infer_model_label(
                llm_stem=llm_stem,
                date_suffix=date_suffix,
                llm_label=llm_label,
                pred_file=pred_file,
                metrics_file=metrics_file,
                extra_hints=[summary_path.stem, summary_path.parent.name, summary_suffix, exp_csv.stem],
            )

        badcase_stats = summary.get("badcase_stats", {})
        total_predictions = ""
        badcase_count = ""
        badcase_rate = ""
        node_ok_in_badcase_count = ""
        node_mismatch_in_badcase_count = ""
        if isinstance(badcase_stats, dict):
            total_predictions = str(badcase_stats.get("total_predictions", ""))
            badcase_count = str(badcase_stats.get("badcase_count", ""))
            rate_value = badcase_stats.get("badcase_rate")
            badcase_rate = _pct(rate_value) if rate_value not in (None, "") else ""
            node_ok_in_badcase_count = str(badcase_stats.get("node_ok_in_badcase_count", ""))
            node_mismatch_in_badcase_count = str(badcase_stats.get("node_mismatch_in_badcase_count", ""))

        overall_node_macro_f1 = ""
        overall_metrics = metrics_payload.get("overall_overall", {})
        if isinstance(overall_metrics, dict):
            macro_value = overall_metrics.get("node_macro_f1_no_matching")
            overall_node_macro_f1 = _pct(macro_value) if macro_value not in (None, "") else ""

        rows.append(
            {
                "Date": _format_date_suffix(date_suffix),
                "OutputSet": summary_path.parent.name,
                "RunTag": summary_suffix,
                "Model": model,
                "LLM": llm_label,
                "Domain": str(exp_row.get("Domain", "")).strip(),
                "Overall n-F1": str(exp_row.get("Overall n-F1", "")).strip(),
                "Overall Node Macro-F1": overall_node_macro_f1,
                "Overall e-F1": str(exp_row.get("Overall e-F1", "")).strip(),
                "Chain n-F1": str(exp_row.get("Chain n-F1", "")).strip(),
                "Chain e-F1": str(exp_row.get("Chain e-F1", "")).strip(),
                "Chain NED": str(exp_row.get("Chain NED", "")).strip(),
                "DAG n-F1": str(exp_row.get("DAG n-F1", "")).strip(),
                "DAG e-F1": str(exp_row.get("DAG e-F1", "")).strip(),
                "Total Predictions": total_predictions,
                "Badcase Count": badcase_count,
                "Badcase Rate": badcase_rate,
                "Node OK in Badcase": node_ok_in_badcase_count,
                "Node Mismatch in Badcase": node_mismatch_in_badcase_count,
            }
        )

    return rows


def _write_recent_results_comparison(metrics_root: Path) -> Path | None:
    rows = _build_recent_results_rows(metrics_root)
    if not rows:
        return None

    output_path = metrics_root / "00_recent_results_comparison.csv"
    try:
        _write_csv(output_path, rows)
        return output_path
    except PermissionError:
        fallback_path = metrics_root / "00_recent_results_comparison_updated.csv"
        _write_csv(fallback_path, rows)
        return fallback_path


def _write_all_outputs(
    target_dir: Path,
    exp_rows: List[Dict[str, Any]],
    struct_rows: List[Dict[str, Any]],
    bad_rows: List[Dict[str, Any]],
    bad_details: List[Dict[str, Any]],
    bad_stats: Dict[str, Any],
    pred_rows_count: int,
    date_suffix: str,
) -> Dict[str, str]:
    target_dir.mkdir(parents=True, exist_ok=True)
    exp_csv = target_dir / f"01_experiment_comparison_{date_suffix}.csv"
    struct_csv = target_dir / f"02_structure_breakdown_{date_suffix}.csv"
    bad_csv = target_dir / f"03_badcase_report_{date_suffix}.csv"
    bad_json = target_dir / f"03_badcase_report_{date_suffix}.json"

    _write_csv(exp_csv, exp_rows)
    _write_csv(struct_csv, struct_rows)
    _write_csv(bad_csv, bad_rows)
    bad_json.write_text(
        json.dumps(
            {
                "total_predictions": pred_rows_count,
                "badcase_count": len(bad_details),
                "stats": bad_stats,
                "badcases": bad_details,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "experiment_comparison_csv": str(exp_csv),
        "structure_breakdown_csv": str(struct_csv),
        "badcase_report_csv": str(bad_csv),
        "badcase_report_json": str(bad_json),
    }


def _select_metrics_file(metrics_dir: Path, llm_stem: str, date_suffix: str) -> Path:
    candidate = (metrics_dir / f"{llm_stem}_{date_suffix}.json").resolve()
    if not candidate.exists():
        raise FileNotFoundError(
            f"No metrics json found: {candidate}. Expected metrics file name format: {llm_stem}_yyyyMMdd.json"
        )
    return candidate


def _build_experiment_table(
    metrics: Dict[str, Any],
    llm_label: str,
    domain_label: str,
    model_label: str = "",
) -> List[Dict[str, Any]]:
    return [
        {
            "Domain": domain_label,
            "LLM": llm_label,
            "Model": model_label,
            "Node n-F1": _pct(metrics["overall_overall"].get("node_micro_f1_no_matching")),
            "Chain n-F1": _pct(metrics["chain_overall"].get("node_micro_f1_no_matching")),
            "Chain e-F1": _pct(metrics["chain_overall"].get("link_binary_f1")),
            "Chain NED": _pct(metrics["chain_overall"].get("edit_distance")),
            "DAG n-F1": _pct(metrics["dag_overall"].get("node_micro_f1_no_matching")),
            "DAG e-F1": _pct(metrics["dag_overall"].get("link_binary_f1")),
            "Overall n-F1": _pct(metrics["overall_overall"].get("node_micro_f1_no_matching")),
            "Overall e-F1": _pct(metrics["overall_overall"].get("link_binary_f1")),
        }
    ]


def _build_structure_table(metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
    mapping: List[Tuple[str, str]] = [
        ("Single", "single_overall"),
        ("Chain", "chain_overall"),
        ("DAG", "dag_overall"),
        ("Overall", "overall_overall"),
    ]
    rows: List[Dict[str, Any]] = []
    for name, key in mapping:
        block = metrics[key]
        rows.append(
            {
                "Structure": name,
                "Samples": block.get("all_samples", ""),
                "n-F1": _pct(block.get("node_micro_f1_no_matching")),
                "e-F1": _pct(block.get("link_binary_f1")),
                "NED": _pct(block.get("edit_distance")),
                "ArgName-F1": _pct(block.get("argument_task_argname_binary_f1_no_matching")),
                "ArgValue-F1": _pct(block.get("argument_task_argname_value_binary_f1_no_matching")),
                "Node Macro-F1": _pct(block.get("node_macro_f1_no_matching")),
            }
        )
    return rows


def _build_badcase_table(
    pred_rows: List[Dict[str, Any]],
    gold_rows: Dict[str, Dict[str, Any]],
    step_ref_base: str = "one",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    bad_rows: List[Dict[str, Any]] = []
    bad_details: List[Dict[str, Any]] = []
    for pred in pred_rows:
        sid = str(pred.get("id", ""))
        gold = gold_rows.get(sid)
        if not gold:
            continue

        pred_nodes = pred.get("result", {}).get("task_nodes", [])
        gold_nodes = _parse_maybe_json(gold.get("tool_nodes", []))
        if not isinstance(gold_nodes, list):
            gold_nodes = []
        pred_links_declared = pred.get("result", {}).get("task_links", [])
        gold_links_declared = _parse_maybe_json(gold.get("tool_links", []))
        if not isinstance(gold_links_declared, list):
            gold_links_declared = []

        pred_tasks, pred_links, pred_args_normalized = _materialize_semantic_graph(
            pred_nodes,
            pred_links_declared,
            step_ref_base=step_ref_base,
        )
        gold_tasks, gold_links, gold_args_normalized = _materialize_semantic_graph(
            gold_nodes,
            gold_links_declared,
            step_ref_base=step_ref_base,
        )
        node_ok = pred_tasks == gold_tasks

        link_ok = pred_links == gold_links

        arg_mismatch_count = 0
        arg_mismatches: List[Dict[str, Any]] = []
        for i in range(min(len(pred_nodes), len(gold_nodes))):
            pred_args_raw = _ensure_list(pred_nodes[i].get("arguments", []))
            gold_args_raw = _ensure_list(gold_nodes[i].get("arguments", []))
            p_args = pred_args_normalized[i]
            g_args = gold_args_normalized[i]
            if p_args != g_args:
                arg_mismatch_count += 1
                arg_mismatches.append(
                    {
                        "step": i + 1,
                        "task_pred": pred_nodes[i].get("task"),
                        "task_gold": gold_nodes[i].get("task"),
                        "pred_args": pred_args_raw,
                        "gold_args": gold_args_raw,
                    }
                )

        if len(pred_nodes) != len(gold_nodes):
            arg_mismatch_count += 1
            arg_mismatches.append(
                {
                    "step": "len",
                    "task_pred": "__len__",
                    "task_gold": "__len__",
                    "pred_args": len(pred_nodes),
                    "gold_args": len(gold_nodes),
                }
            )

        is_bad = (not node_ok) or (not link_ok) or arg_mismatch_count > 0
        if not is_bad:
            continue

        issues: List[str] = []
        if not node_ok:
            issues.append("node_mismatch")
        if not link_ok:
            issues.append("link_mismatch")
        if arg_mismatch_count > 0:
            issues.append(f"arg_mismatch({arg_mismatch_count})")

        bad_rows.append(
            {
                "ID": sid,
                "Type": str(gold.get("type", "")),
                "Node OK": "✓" if node_ok else "✗",
                "Link OK": "✓" if link_ok else "✗",
                "Arg Mismatch": arg_mismatch_count,
                "Pred Tasks": " -> ".join(pred_tasks),
                "Gold Tasks": " -> ".join(gold_tasks),
                "Error Summary": "; ".join(issues),
            }
        )
        bad_details.append(
            {
                "id": sid,
                "type": str(gold.get("type", "")),
                "node_ok": node_ok,
                "link_ok": link_ok,
                "arg_mismatch_count": arg_mismatch_count,
                "pred_tasks": pred_tasks,
                "gold_tasks": gold_tasks,
                "pred_links": pred_links,
                "gold_links": gold_links,
                "arg_mismatches": arg_mismatches,
            }
        )
    return bad_rows, bad_details


def _build_badcase_stats(bad_details: List[Dict[str, Any]], total_predictions: int) -> Dict[str, Any]:
    badcase_count = len(bad_details)
    goodcase_count = max(total_predictions - badcase_count, 0)
    badcase_rate = (badcase_count / total_predictions) if total_predictions else 0.0

    node_ok_count = sum(1 for x in bad_details if bool(x.get("node_ok")))
    link_ok_count = sum(1 for x in bad_details if bool(x.get("link_ok")))
    arg_mismatch_case_count = sum(1 for x in bad_details if int(x.get("arg_mismatch_count", 0)) > 0)
    total_arg_mismatch_steps = sum(int(x.get("arg_mismatch_count", 0)) for x in bad_details)

    error_combo_counter: Counter[str] = Counter()
    type_counter: Counter[str] = Counter()
    type_node_fail_counter: Counter[str] = Counter()
    type_link_fail_counter: Counter[str] = Counter()
    type_arg_fail_counter: Counter[str] = Counter()

    for row in bad_details:
        type_name = str(row.get("type", "") or "unknown")
        type_counter[type_name] += 1
        node_fail = not bool(row.get("node_ok"))
        link_fail = not bool(row.get("link_ok"))
        arg_fail = int(row.get("arg_mismatch_count", 0)) > 0

        if node_fail:
            type_node_fail_counter[type_name] += 1
        if link_fail:
            type_link_fail_counter[type_name] += 1
        if arg_fail:
            type_arg_fail_counter[type_name] += 1

        labels: List[str] = []
        if node_fail:
            labels.append("node")
        if link_fail:
            labels.append("link")
        if arg_fail:
            labels.append("arg")
        error_combo_counter["+".join(labels)] += 1

    by_type: Dict[str, Dict[str, Any]] = {}
    for type_name, count in sorted(type_counter.items(), key=lambda item: item[0]):
        by_type[type_name] = {
            "badcase_count": count,
            "node_mismatch_count": int(type_node_fail_counter[type_name]),
            "link_mismatch_count": int(type_link_fail_counter[type_name]),
            "arg_mismatch_case_count": int(type_arg_fail_counter[type_name]),
        }

    return {
        "total_predictions": total_predictions,
        "goodcase_count": goodcase_count,
        "badcase_count": badcase_count,
        "badcase_rate": round(badcase_rate, 4),
        "node_ok_in_badcase_count": node_ok_count,
        "node_mismatch_in_badcase_count": badcase_count - node_ok_count,
        "link_ok_in_badcase_count": link_ok_count,
        "link_mismatch_in_badcase_count": badcase_count - link_ok_count,
        "arg_mismatch_case_count": arg_mismatch_case_count,
        "total_arg_mismatch_steps": total_arg_mismatch_steps,
        "error_combo_distribution": dict(sorted(error_combo_counter.items(), key=lambda item: item[0])),
        "badcase_by_type": by_type,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Export 3 tables: experiment comparison, structure breakdown, badcase report.")
    parser.add_argument("--data_dir", type=str, default="taskbench/data_multimedia")
    parser.add_argument(
        "--pred_file",
        type=str,
        default=None,
        help="Optional prediction file path. If omitted, auto-pick predictions_pipeline_agent/{llm_stem}_yyyyMMdd.json for today.",
    )
    parser.add_argument("--gold_file", type=str, default="data.json")
    parser.add_argument("--metrics_file", type=str, default=None, help="Optional metrics json path. If omitted, auto-pick by llm stem.")
    parser.add_argument("--llm_stem", type=str, default="pipeline_orchestrator_agent")
    parser.add_argument("--llm_label", type=str, default="FlowAgent(qianwen)")
    parser.add_argument("--model_label", type=str, default=None)
    parser.add_argument("--domain_label", type=str, default="Multimedia Tool")
    parser.add_argument("--step_ref_base", type=str, default="one", choices=["one", "zero"])
    parser.add_argument("--metrics_out_subdir", type=str, default="three_tables")
    args = parser.parse_args()
    date_suffix = datetime.now().strftime("%Y%m%d")

    data_dir = _resolve_data_dir_arg(args.data_dir)
    if args.pred_file:
        pred_file = (data_dir / args.pred_file).resolve() if not Path(args.pred_file).is_absolute() else Path(args.pred_file)
    else:
        pred_file = (data_dir / "predictions_pipeline_agent" / f"{args.llm_stem}_{date_suffix}.json").resolve()
    gold_file = (data_dir / args.gold_file).resolve() if not Path(args.gold_file).is_absolute() else Path(args.gold_file)
    metrics_out_dir = (data_dir / "metrics_pipeline_agent" / args.metrics_out_subdir).resolve()
    metrics_out_dir.mkdir(parents=True, exist_ok=True)

    if args.metrics_file:
        metrics_file = Path(args.metrics_file).resolve()
    else:
        metrics_file = _select_metrics_file(data_dir / "metrics_pipeline_agent", args.llm_stem, date_suffix)

    model_label = str(args.model_label or "").strip() or _infer_model_label(
        llm_stem=args.llm_stem,
        date_suffix=date_suffix,
        llm_label=args.llm_label,
        pred_file=pred_file,
        metrics_file=metrics_file,
    )

    metrics = _read_json(metrics_file)
    pred_rows = _read_jsonl(pred_file)
    gold_rows = {str(x.get("id", "")): x for x in _read_jsonl(gold_file)}

    exp_rows = _build_experiment_table(metrics, args.llm_label, args.domain_label, model_label=model_label)
    struct_rows = _build_structure_table(metrics)
    bad_rows, bad_details = _build_badcase_table(pred_rows, gold_rows, step_ref_base=args.step_ref_base)
    bad_stats = _build_badcase_stats(bad_details, total_predictions=len(pred_rows))

    outputs = _write_all_outputs(
        target_dir=metrics_out_dir,
        exp_rows=exp_rows,
        struct_rows=struct_rows,
        bad_rows=bad_rows,
        bad_details=bad_details,
        bad_stats=bad_stats,
        pred_rows_count=len(pred_rows),
        date_suffix=date_suffix,
    )

    summary = {
        "data_dir": str(data_dir),
        "pred_file": str(pred_file),
        "gold_file": str(gold_file),
        "metrics_file": str(metrics_file),
        "llm_stem": args.llm_stem,
        "llm_label": args.llm_label,
        "model": model_label,
        "domain_label": args.domain_label,
        "metrics_out_subdir": args.metrics_out_subdir,
        "step_ref_base": args.step_ref_base,
        "rows": {
            "experiment_comparison": len(exp_rows),
            "structure_breakdown": len(struct_rows),
            "badcase_report": len(bad_rows),
        },
        "badcase_stats": bad_stats,
        "outputs": outputs,
    }
    summary_path = metrics_out_dir / f"00_summary_{date_suffix}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    comparison_path = _write_recent_results_comparison(data_dir / "metrics_pipeline_agent")
    if comparison_path is not None:
        summary["comparison_csv"] = str(comparison_path)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[DONE] experiment={outputs['experiment_comparison_csv']}")
    print(f"[DONE] structure={outputs['structure_breakdown_csv']}")
    print(f"[DONE] badcase={outputs['badcase_report_csv']}")
    print(f"[DONE] badcase_json={outputs['badcase_report_json']}")
    print(
        "[STATS] "
        f"total={bad_stats['total_predictions']}, "
        f"badcase={bad_stats['badcase_count']}, "
        f"rate={bad_stats['badcase_rate']:.2%}, "
        f"node_mismatch={bad_stats['node_mismatch_in_badcase_count']}, "
        f"link_mismatch={bad_stats['link_mismatch_in_badcase_count']}, "
        f"arg_mismatch_case={bad_stats['arg_mismatch_case_count']}"
    )
    print(f"[DONE] summary={summary_path}")
    if comparison_path is not None:
        print(f"[DONE] recent_results={comparison_path}")


if __name__ == "__main__":
    main()
