from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from collections import Counter
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.pipeline_orchestrator_agent import PipelineOrchestratorAgent


def _load_module(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_runner_module(name: str) -> ModuleType:
    mapping = {
        "base": "run_with_pipeline_agent_base.py",
        "openai": "run_with_pipeline_agent_openAI.py",
        "qianwen": "run_with_pipeline_agent_qianwen.py",
        "gemini": "run_with_pipeline_agent_gemini.py",
    }
    file_name = mapping.get(name)
    if not file_name:
        raise ValueError(f"Unsupported runner: {name}")
    return _load_module(SCRIPT_DIR / file_name, f"rollback_runner_{name}")


EXPORT = _load_module(
    SCRIPT_DIR / "export_three_tables.py",
    "rollback_export_three_tables",
)
ORACLE = _load_module(
    SCRIPT_DIR / "oracle_candidate_analysis.py",
    "rollback_oracle_candidate_analysis",
)

DEFAULT_RUNNER = "qianwen"
DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL_NAME = "qwen3-14b"
DEFAULT_LLM_CONFIG_PATH = "configs/qwen.json"
DEFAULT_CASE_COUNT = 100
DEFAULT_GROUP_TAGS = ["A", "O2"]
ORACLE_SOURCE_TAG = "O2"


def _default_group_specs() -> List[Dict[str, Any]]:
    return [
        {
            "tag": "A",
            "label": "original_single",
            "planning_mode": "single",
            "candidate_selection_mode": "rerank",
            "enable_candidate_verifier": False,
            "enable_candidate_repair": False,
            "enable_workflow_memory": False,
            "include_original_candidate": False,
            "edge_grounding_mode": "none",
        },
        {
            "tag": "B",
            "label": "multi_first_original",
            "planning_mode": "multi",
            "candidate_selection_mode": "first",
            "enable_candidate_verifier": False,
            "enable_candidate_repair": False,
            "enable_workflow_memory": False,
            "include_original_candidate": True,
            "edge_grounding_mode": "none",
        },
        {
            "tag": "C",
            "label": "multi_rerank_only",
            "planning_mode": "multi",
            "candidate_selection_mode": "rerank",
            "enable_candidate_verifier": False,
            "enable_candidate_repair": False,
            "enable_workflow_memory": False,
            "include_original_candidate": True,
            "edge_grounding_mode": "none",
        },
        {
            "tag": "D",
            "label": "multi_rerank_verifier_repair",
            "planning_mode": "multi",
            "candidate_selection_mode": "rerank",
            "enable_candidate_verifier": True,
            "enable_candidate_repair": True,
            "enable_workflow_memory": False,
            "include_original_candidate": True,
            "edge_grounding_mode": "none",
        },
        {
            "tag": "E",
            "label": "multi_rerank_verifier_repair_memory",
            "planning_mode": "multi",
            "candidate_selection_mode": "rerank",
            "enable_candidate_verifier": True,
            "enable_candidate_repair": True,
            "enable_workflow_memory": True,
            "include_original_candidate": True,
            "edge_grounding_mode": "none",
        },
        {
            "tag": "F",
            "label": "original_first_verifier_fallback",
            "planning_mode": "multi",
            "candidate_selection_mode": "original_first_fallback",
            "enable_candidate_verifier": True,
            "enable_candidate_repair": True,
            "enable_workflow_memory": False,
            "include_original_candidate": True,
            "edge_grounding_mode": "none",
        },
        {
            "tag": "G",
            "label": "original_dependency_filter_first_valid",
            "planning_mode": "multi",
            "candidate_selection_mode": "original_dependency_filter_first_valid",
            "enable_candidate_verifier": True,
            "enable_candidate_repair": False,
            "enable_workflow_memory": False,
            "include_original_candidate": True,
            "edge_grounding_mode": "none",
        },
        {
            "tag": "H",
            "label": "multi_first_original_nearest_valid_grounding",
            "planning_mode": "multi",
            "candidate_selection_mode": "first",
            "enable_candidate_verifier": False,
            "enable_candidate_repair": False,
            "enable_workflow_memory": False,
            "include_original_candidate": True,
            "edge_grounding_mode": "nearest_valid_upstream",
        },
        {
            "tag": "H2",
            "label": "multi_first_original_semantic_edge_grounding",
            "planning_mode": "multi",
            "candidate_selection_mode": "first",
            "enable_candidate_verifier": False,
            "enable_candidate_repair": False,
            "enable_workflow_memory": False,
            "include_original_candidate": True,
            "edge_grounding_mode": "semantic_edge_scoring",
        },
        {
            "tag": "H2A",
            "label": "multi_first_original_semantic_edge_grounding_nearest_priority",
            "planning_mode": "multi",
            "candidate_selection_mode": "first",
            "enable_candidate_verifier": False,
            "enable_candidate_repair": False,
            "enable_workflow_memory": False,
            "include_original_candidate": True,
            "edge_grounding_mode": "semantic_edge_scoring_h2a",
        },
        {
            "tag": "H2B",
            "label": "multi_first_original_semantic_edge_grounding_semantic_priority",
            "planning_mode": "multi",
            "candidate_selection_mode": "first",
            "enable_candidate_verifier": False,
            "enable_candidate_repair": False,
            "enable_workflow_memory": False,
            "include_original_candidate": True,
            "edge_grounding_mode": "semantic_edge_scoring_h2b",
        },
        {
            "tag": "I",
            "label": "structure_aware_grounding",
            "planning_mode": "multi",
            "candidate_selection_mode": "structure_aware",
            "enable_candidate_verifier": True,
            "enable_candidate_repair": False,
            "enable_workflow_memory": False,
            "include_original_candidate": True,
            "enable_semantic_edge_grounding": True,
            "edge_grounding_mode": "semantic_edge_scoring",
        },
        {
            "tag": "J",
            "label": "strict_prompt_action_checklist_parameter_normalization",
            "planning_mode": "multi",
            "candidate_selection_mode": "first",
            "enable_candidate_verifier": False,
            "enable_candidate_repair": False,
            "enable_workflow_memory": False,
            "include_original_candidate": True,
            "enable_semantic_edge_grounding": False,
            "edge_grounding_mode": "none",
            "enable_strict_planning_prompt": True,
            "enable_action_checklist": True,
            "enable_parameter_normalization": True,
        },
        {
            "tag": "K",
            "label": "multi_first_original_strict_prompt_action_checklist_parameter_normalization",
            "planning_mode": "multi",
            "candidate_selection_mode": "first",
            "enable_candidate_verifier": False,
            "enable_candidate_repair": False,
            "enable_workflow_memory": False,
            "include_original_candidate": True,
            "enable_semantic_edge_grounding": False,
            "edge_grounding_mode": "none",
            "enable_strict_planning_prompt": True,
            "enable_action_checklist": True,
            "enable_parameter_normalization": True,
        },
        {
            "tag": "O",
            "label": "orthogonal_prompt_candidates",
            "planning_mode": "multi",
            "candidate_selection_mode": "original_first_fallback",
            "enable_candidate_verifier": False,
            "enable_candidate_repair": False,
            "enable_workflow_memory": False,
            "include_original_candidate": True,
            "enable_semantic_edge_grounding": False,
            "edge_grounding_mode": "none",
            "candidate_prompt_mode": "orthogonal",
            "candidate_count_override": 6,
            "force_generate_all_candidate_families": True,
        },
        {
            "tag": "O2",
            "label": "orthogonal_prompt_candidates_v2",
            "planning_mode": "multi",
            "candidate_selection_mode": "collect_all_then_original",
            "enable_candidate_verifier": True,
            "enable_candidate_repair": False,
            "enable_workflow_memory": False,
            "include_original_candidate": True,
            "enable_semantic_edge_grounding": False,
            "edge_grounding_mode": "none",
            "candidate_prompt_mode": "orthogonal_v2",
            "candidate_count_override": 10,
            "force_generate_all_candidate_families": True,
            "disable_early_stop": True,
            "save_all_candidates": True,
        },
        {
            "tag": "O3",
            "label": "orthogonal_prompt_candidates_v3_pool_only",
            "planning_mode": "multi",
            "candidate_selection_mode": "none",
            "enable_candidate_verifier": False,
            "enable_candidate_repair": False,
            "enable_workflow_memory": False,
            "include_original_candidate": False,
            "enable_semantic_edge_grounding": False,
            "edge_grounding_mode": "none",
            "candidate_prompt_mode": "orthogonal_v3",
            "candidate_count_override": 5,
            "force_generate_all_candidate_families": True,
            "disable_early_stop": True,
            "save_all_candidates": True,
            "candidate_pool_only": True,
        },
        {
            "tag": "T",
            "label": "action_dag_planning_probe",
            "planning_mode": "action_dag",
            "candidate_selection_mode": "none",
            "enable_candidate_verifier": False,
            "enable_candidate_repair": False,
            "enable_workflow_memory": False,
            "include_original_candidate": False,
            "edge_grounding_mode": "none",
            "save_action_dag": True,
        },
    ]


def _select_group_specs(group_specs: List[Dict[str, Any]], selected_tags: Optional[List[str]]) -> List[Dict[str, Any]]:
    if not selected_tags:
        return list(group_specs)

    normalized_tags = [str(tag).strip().upper() for tag in selected_tags if str(tag).strip()]
    if not normalized_tags:
        return list(group_specs)

    tag_to_spec = {
        str(item.get("tag", "")).strip().upper(): item
        for item in group_specs
        if str(item.get("tag", "")).strip()
    }
    missing = [tag for tag in normalized_tags if tag not in tag_to_spec]
    if missing:
        available = ", ".join(sorted(tag_to_spec.keys()))
        raise ValueError(f"Unknown group_tags: {missing}. Available tags: {available}")

    selected_specs: List[Dict[str, Any]] = []
    for tag in normalized_tags:
        selected_specs.append(tag_to_spec[tag])
    return selected_specs


def _load_case_ids(path: Path) -> List[str]:
    case_ids: List[str] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            case_ids.append(text)
    return case_ids


def _select_case_ids(
    runner_module: ModuleType,
    data_dir: Path,
    case_count: int,
    offset: int,
    case_ids_file: Optional[Path],
) -> List[str]:
    if case_ids_file is not None:
        case_ids = _load_case_ids(case_ids_file)
        if not case_ids:
            raise ValueError(f"No case ids found in: {case_ids_file}")
        return case_ids

    requests = runner_module._load_requests(data_dir)
    if offset > 0:
        requests = requests[offset:]
    selected = [
        str(item.get("id", "")).strip()
        for item in requests
        if str(item.get("id", "")).strip()
    ][: max(case_count, 0)]
    if len(selected) < case_count:
        raise ValueError(
            f"Requested {case_count} cases but only found {len(selected)} after offset={offset}"
        )
    return selected


def _resolve_fixed_temperature(
    runner_module: ModuleType,
    runner_args: argparse.Namespace,
    requested_temperature: Optional[float],
) -> Optional[float]:
    if requested_temperature is not None:
        return float(requested_temperature)

    llm_config_path = None
    if getattr(runner_args, "llm_config_path", None):
        llm_config_path = runner_module._resolve_existing_file(
            runner_args.llm_config_path,
            label="llm_config_path",
        )

    cfg = PipelineOrchestratorAgent._resolve_llm_runtime_config(
        model_name=str(getattr(runner_args, "model_name", "qwen-max")),
        provider=str(getattr(runner_args, "provider", "tongyi")),
        llm_profile=getattr(runner_args, "llm_profile", None),
        llm_config_path=llm_config_path,
    )
    return float(cfg.temperature)


def _build_runner_args(
    runner_parser: argparse.ArgumentParser,
    base_args: argparse.Namespace,
    group_spec: Dict[str, Any],
    prediction_dir: str,
    case_ids_file: Path,
    candidate_count: int,
    fixed_temperature: Optional[float],
) -> argparse.Namespace:
    args = deepcopy(base_args)
    args.prediction_dir = prediction_dir
    args.case_ids_file = str(case_ids_file)
    args.resume = True
    args.limit = None
    args.offset = 0
    args.log_every = 1
    args.stop_on_error = bool(getattr(base_args, "stop_on_error", False))
    args.candidate_count = candidate_count
    if group_spec.get("candidate_count_override") is not None:
        args.candidate_count = int(group_spec["candidate_count_override"])
    args.fixed_candidate_temperature = fixed_temperature
    args.planning_mode = str(group_spec["planning_mode"])
    args.candidate_selection_mode = str(group_spec["candidate_selection_mode"])
    args.enable_candidate_verifier = bool(group_spec["enable_candidate_verifier"])
    args.enable_candidate_repair = bool(group_spec["enable_candidate_repair"])
    args.enable_workflow_memory = bool(group_spec["enable_workflow_memory"])
    args.include_original_candidate = bool(group_spec["include_original_candidate"])
    args.enable_semantic_edge_grounding = bool(group_spec.get("enable_semantic_edge_grounding", False))
    args.edge_grounding_mode = str(group_spec.get("edge_grounding_mode", "none"))
    args.candidate_prompt_mode = str(group_spec.get("candidate_prompt_mode", "legacy"))
    args.force_generate_all_candidate_families = bool(
        group_spec.get("force_generate_all_candidate_families", False)
    )
    args.disable_early_stop = bool(group_spec.get("disable_early_stop", False))
    args.enable_strict_planning_prompt = bool(group_spec.get("enable_strict_planning_prompt", False))
    args.enable_action_checklist = bool(group_spec.get("enable_action_checklist", False))
    args.enable_parameter_normalization = bool(group_spec.get("enable_parameter_normalization", False))
    args.save_candidate_pool = bool(
        group_spec.get("save_all_candidates", getattr(base_args, "save_candidate_pool", False))
    )
    args.save_action_dag = bool(group_spec.get("save_action_dag", False))

    if args.planning_mode == "single":
        args.execution_mode = "best"
        args.include_original_candidate = False
    if args.planning_mode == "action_dag":
        args.execution_mode = "best"
        args.include_original_candidate = False
        args.save_candidate_pool = False
        args.save_action_dag = True
    if bool(group_spec.get("candidate_pool_only", False)):
        args.execution_mode = "best"
        args.include_original_candidate = False
        args.save_candidate_pool = True
        args.save_action_dag = False

    if args.enable_workflow_memory and not getattr(args, "workflow_memory_path", None):
        raise ValueError("Memory-enabled experiment requires workflow_memory_path")

    return args


def _build_group_summary_config(group_spec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "planning_mode": group_spec["planning_mode"],
        "candidate_selection_mode": group_spec["candidate_selection_mode"],
        "enable_candidate_verifier": group_spec["enable_candidate_verifier"],
        "enable_candidate_repair": group_spec["enable_candidate_repair"],
        "enable_workflow_memory": group_spec["enable_workflow_memory"],
        "include_original_candidate": group_spec["include_original_candidate"],
        "enable_semantic_edge_grounding": bool(group_spec.get("enable_semantic_edge_grounding", False)),
        "edge_grounding_mode": group_spec.get("edge_grounding_mode", "none"),
        "candidate_prompt_mode": group_spec.get("candidate_prompt_mode", "legacy"),
        "candidate_count_override": group_spec.get("candidate_count_override"),
        "force_generate_all_candidate_families": bool(
            group_spec.get("force_generate_all_candidate_families", False)
        ),
        "disable_early_stop": bool(group_spec.get("disable_early_stop", False)),
        "save_all_candidates": bool(group_spec.get("save_all_candidates", False)),
        "save_action_dag": bool(group_spec.get("save_action_dag", False)),
        "candidate_pool_only": bool(group_spec.get("candidate_pool_only", False)),
        "enable_strict_planning_prompt": bool(group_spec.get("enable_strict_planning_prompt", False)),
        "enable_action_checklist": bool(group_spec.get("enable_action_checklist", False)),
        "enable_parameter_normalization": bool(group_spec.get("enable_parameter_normalization", False)),
    }


def _summarize_group_result(
    group_spec: Dict[str, Any],
    meta: Dict[str, Any],
    data_dir: Path,
    gold_file: Path,
    step_ref_base: str,
) -> Dict[str, Any]:
    pred_file = Path(str(meta["output_path"])).resolve()
    pred_rows = EXPORT._read_jsonl(pred_file)
    requested_cases = int(meta.get("total_to_run", len(pred_rows)))
    successful_predictions = int(meta.get("success", len(pred_rows)))
    failed_cases = int(meta.get("failed", max(requested_cases - successful_predictions, 0)))
    validation_failure_count = int(meta.get("validation_failed", 0))
    other_failure_count = int(meta.get("other_failed", failed_cases - validation_failure_count))
    failure_details = meta.get("failure_details", [])
    if not isinstance(failure_details, list):
        failure_details = []
    failure_counts = meta.get("failure_counts", {})
    if not isinstance(failure_counts, dict):
        failure_counts = {}

    if bool(meta.get("candidate_pool_only", False)) or bool(group_spec.get("candidate_pool_only", False)):
        candidate_dump_path = meta.get("candidate_dump_path")
        candidate_case_rows: List[Dict[str, Any]] = []
        candidate_rows: List[Dict[str, Any]] = []
        if candidate_dump_path and Path(str(candidate_dump_path)).exists():
            raw_rows = EXPORT._read_jsonl(Path(str(candidate_dump_path)))
            for row in raw_rows:
                if not isinstance(row, dict):
                    continue
                candidates = row.get("candidates")
                if isinstance(candidates, list):
                    candidate_case_rows.append(row)
                    candidate_rows.extend(item for item in candidates if isinstance(item, dict))
                else:
                    candidate_rows.append(row)
        case_ids = {
            str(row.get("case_id", "")).strip()
            for row in candidate_rows
            if str(row.get("case_id", "")).strip()
        }
        case_ids.update(
            str(row.get("case_id", "")).strip()
            for row in candidate_case_rows
            if str(row.get("case_id", "")).strip()
        )
        family_counts: Counter[str] = Counter(
            str(row.get("family", "")).strip()
            for row in candidate_rows
            if str(row.get("family", "")).strip()
        )
        parse_success_count = sum(1 for row in candidate_rows if bool(row.get("parse_success", False)))
        parse_error_counts: Counter[str] = Counter(
            str(row.get("parse_error", "")).strip()
            for row in candidate_rows
            if not bool(row.get("parse_success", False)) and str(row.get("parse_error", "")).strip()
        )
        return {
            "tag": group_spec["tag"],
            "label": group_spec["label"],
            "prediction_file": str(pred_file),
            "prediction_dir": str(meta["prediction_dir"]),
            "candidate_dump_path": candidate_dump_path,
            "data_dir": str(data_dir),
            "requested_cases": requested_cases,
            "successful_predictions": successful_predictions,
            "case_coverage_rate": (float(successful_predictions) / float(requested_cases)) if requested_cases else 0.0,
            "failed_cases": failed_cases,
            "validation_failure_count": validation_failure_count,
            "other_failure_count": other_failure_count,
            "failure_counts": failure_counts,
            "failure_details": failure_details,
            "candidate_pool_only": True,
            "excluded_from_taskbench_eval": True,
            "taskbench_eval_skipped": True,
            "candidate_record_count": len(candidate_rows),
            "candidate_case_record_count": len(candidate_case_rows),
            "candidate_case_count": len(case_ids),
            "expected_candidate_record_count": requested_cases * 5,
            "parse_success_count": parse_success_count,
            "parse_failure_count": len(candidate_rows) - parse_success_count,
            "parse_success_rate": (float(parse_success_count) / float(len(candidate_rows))) if candidate_rows else 0.0,
            "candidate_family_counts": dict(sorted(family_counts.items())),
            "parse_error_counts": dict(parse_error_counts.most_common()),
            "badcase_count": 0,
            "badcase_rate": 0.0,
            "badcase_ids": [],
            "badcase_stats": {},
            "badcase_rows": [],
            "badcase_details": [],
            "config": _build_group_summary_config(group_spec),
        }

    if str(group_spec.get("planning_mode", "")).strip() == "action_dag":
        total_predictions = len(pred_rows)
        parse_success_count = sum(1 for row in pred_rows if bool(row.get("parse_success", False)))
        atomic_action_total = sum(int(row.get("atomic_action_count") or 0) for row in pred_rows)
        dependency_total = sum(int(row.get("dependency_count") or 0) for row in pred_rows)
        materialization_total = sum(int(row.get("materialization_count") or 0) for row in pred_rows)
        action_label_counts: Counter[str] = Counter()
        case_action_summaries: List[Dict[str, Any]] = []
        for row in pred_rows:
            raw_labels = row.get("action_labels")
            labels = [
                str(label).strip()
                for label in raw_labels
                if str(label).strip()
            ] if isinstance(raw_labels, list) else []
            if not labels:
                action_dag_json = row.get("action_dag_json")
                raw_actions = (
                    action_dag_json.get("atomic_actions", [])
                    if isinstance(action_dag_json, dict)
                    else []
                )
                labels = [
                    str(item.get("action", "")).strip()
                    for item in raw_actions
                    if isinstance(item, dict) and str(item.get("action", "")).strip()
                ]
            action_label_counts.update(labels)
            raw_artifacts = row.get("materialized_artifacts")
            artifacts = [
                str(artifact).strip()
                for artifact in raw_artifacts
                if str(artifact).strip()
            ] if isinstance(raw_artifacts, list) else []
            case_action_summaries.append(
                {
                    "id": str(row.get("id") or row.get("case_id") or ""),
                    "parse_success": bool(row.get("parse_success", False)),
                    "action_labels": labels,
                    "atomic_action_count": int(row.get("atomic_action_count") or len(labels)),
                    "dependency_count": int(row.get("dependency_count") or 0),
                    "materialized_artifacts": artifacts,
                    "parse_error": str(row.get("parse_error", "") or ""),
                }
            )
        denominator = float(total_predictions) if total_predictions else 1.0
        return {
            "tag": group_spec["tag"],
            "label": group_spec["label"],
            "prediction_file": str(pred_file),
            "prediction_dir": str(meta["prediction_dir"]),
            "candidate_dump_path": meta.get("candidate_dump_path"),
            "data_dir": str(data_dir),
            "requested_cases": requested_cases,
            "successful_predictions": successful_predictions,
            "case_coverage_rate": (float(successful_predictions) / float(requested_cases)) if requested_cases else 0.0,
            "failed_cases": failed_cases,
            "validation_failure_count": validation_failure_count,
            "other_failure_count": other_failure_count,
            "failure_counts": failure_counts,
            "failure_details": failure_details,
            "total_predictions": total_predictions,
            "excluded_from_taskbench_eval": True,
            "taskbench_eval_skipped": True,
            "parse_success_count": parse_success_count,
            "parse_failure_count": total_predictions - parse_success_count,
            "parse_success_rate": (float(parse_success_count) / float(total_predictions)) if total_predictions else 0.0,
            "atomic_action_count_mean": float(atomic_action_total) / denominator,
            "dependency_count_mean": float(dependency_total) / denominator,
            "materialization_count_mean": float(materialization_total) / denominator,
            "action_label_counts": dict(sorted(action_label_counts.items())),
            "case_action_summaries": case_action_summaries,
            "badcase_count": 0,
            "badcase_rate": 0.0,
            "badcase_ids": [],
            "badcase_stats": {},
            "badcase_rows": [],
            "badcase_details": [],
            "config": _build_group_summary_config(group_spec),
        }

    gold_rows = {
        str(row.get("id", "")): row
        for row in EXPORT._read_jsonl(gold_file)
    }
    bad_rows, bad_details = EXPORT._build_badcase_table(
        pred_rows,
        gold_rows,
        step_ref_base=step_ref_base,
    )
    bad_stats = EXPORT._build_badcase_stats(
        bad_details,
        total_predictions=len(pred_rows),
    )
    badcase_ids = [str(item.get("id", "")) for item in bad_details if str(item.get("id", ""))]
    return {
        "tag": group_spec["tag"],
        "label": group_spec["label"],
        "prediction_file": str(pred_file),
        "prediction_dir": str(meta["prediction_dir"]),
        "candidate_dump_path": meta.get("candidate_dump_path"),
        "data_dir": str(data_dir),
        "requested_cases": requested_cases,
        "successful_predictions": successful_predictions,
        "case_coverage_rate": (float(successful_predictions) / float(requested_cases)) if requested_cases else 0.0,
        "failed_cases": failed_cases,
        "validation_failure_count": validation_failure_count,
        "other_failure_count": other_failure_count,
        "failure_counts": failure_counts,
        "failure_details": failure_details,
        "total_predictions": len(pred_rows),
        "badcase_count": int(bad_stats["badcase_count"]),
        "badcase_rate": float(bad_stats["badcase_rate"]),
        "node_mismatch_in_badcase_count": int(bad_stats["node_mismatch_in_badcase_count"]),
        "link_mismatch_in_badcase_count": int(bad_stats["link_mismatch_in_badcase_count"]),
        "arg_mismatch_case_count": int(bad_stats["arg_mismatch_case_count"]),
        "badcase_ids": badcase_ids,
        "badcase_stats": bad_stats,
        "badcase_rows": bad_rows,
        "badcase_details": bad_details,
        "config": _build_group_summary_config(group_spec),
    }


def _build_o2_oracle_analysis(
    source_result: Dict[str, Any],
    data_dir: Path,
    output_root: Path,
    step_ref_base: str,
) -> Dict[str, Any]:
    candidate_dump_raw = source_result.get("candidate_dump_path")
    if not candidate_dump_raw:
        raise ValueError(f"{ORACLE_SOURCE_TAG} oracle analysis requires candidate_dump_path.")

    candidate_dump_path = Path(str(candidate_dump_raw)).resolve()
    if not candidate_dump_path.exists():
        raise FileNotFoundError(f"candidate_dump_path not found: {candidate_dump_path}")

    save_dir = (output_root / ORACLE_SOURCE_TAG / "oracle_analysis").resolve()
    oracle_summary = ORACLE.analyze_candidate_dump(
        data_dir=data_dir,
        candidate_dump_path=candidate_dump_path,
        save_dir=save_dir,
        step_ref_base=step_ref_base,
    )
    overall = next(
        (
            row for row in oracle_summary.get("summary_rows", [])
            if row.get("split") == "overall"
        ),
        {},
    )
    return {
        "candidate_dump_path": str(candidate_dump_path),
        "data_dir": str(data_dir),
        "save_dir": str(save_dir),
        "case_count": int(oracle_summary.get("case_count", 0)),
        "skipped_ids": oracle_summary.get("skipped_ids", []),
        "summary_rows": oracle_summary.get("summary_rows", []),
        "oracle_better_rate": float(overall.get("oracle_better_rate", 0.0)),
        "mean_node_oracle_gain": float(overall.get("mean_node_oracle_gain", 0.0)),
        "mean_edge_oracle_gain": float(overall.get("mean_edge_oracle_gain", 0.0)),
        "mean_quality_oracle_gain": float(overall.get("mean_quality_oracle_gain", 0.0)),
        "summary_csv_path": oracle_summary.get("summary_csv_path"),
        "case_details_path": oracle_summary.get("case_details_path"),
        "case_family_regret_report_path": oracle_summary.get("case_family_regret_report_path"),
        "case_family_regret_summary_path": oracle_summary.get("case_family_regret_summary_path"),
    }


def _compare_groups(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    comparable_results = [
        result for result in results if not bool(result.get("excluded_from_taskbench_eval", False))
    ]
    skipped_tags = [
        str(result.get("tag", ""))
        for result in results
        if bool(result.get("excluded_from_taskbench_eval", False))
    ]
    if not comparable_results:
        return {
            "comparisons": [],
            "first_badcase_increase": None,
            "diagnosis": "No TaskBench-evaluable groups were run.",
            "skipped_tags": skipped_tags,
        }

    comparisons: List[Dict[str, Any]] = []
    baseline = comparable_results[0]
    first_badcase_increase = None
    for idx, result in enumerate(comparable_results):
        prev = comparable_results[idx - 1] if idx > 0 else None
        prev_badcase_ids = set(prev["badcase_ids"]) if prev is not None else set()
        curr_badcase_ids = set(result["badcase_ids"])
        baseline_badcase_ids = set(baseline["badcase_ids"])
        item = {
            "tag": result["tag"],
            "label": result["label"],
            "badcase_count": result["badcase_count"],
            "badcase_rate": result["badcase_rate"],
            "delta_vs_prev": (
                result["badcase_count"] - prev["badcase_count"]
                if prev is not None
                else 0
            ),
            "delta_vs_A": result["badcase_count"] - baseline["badcase_count"],
            "new_badcases_vs_prev": sorted(curr_badcase_ids - prev_badcase_ids),
            "resolved_badcases_vs_prev": sorted(prev_badcase_ids - curr_badcase_ids),
            "new_badcases_vs_A": sorted(curr_badcase_ids - baseline_badcase_ids),
        }
        if (
            prev is not None
            and result["badcase_count"] > prev["badcase_count"]
            and first_badcase_increase is None
        ):
            first_badcase_increase = {
                "from": prev["tag"],
                "to": result["tag"],
                "delta": result["badcase_count"] - prev["badcase_count"],
            }
        comparisons.append(item)

    diagnosis = ""
    if first_badcase_increase is not None:
        transition = f"{first_badcase_increase['from']}->{first_badcase_increase['to']}"
        if transition == "A->B":
            diagnosis = "B 就变差：候选生成路径本身已经扰动了原始策略。"
        elif transition == "B->C":
            diagnosis = "C 才变差：rerank 选错。"
        elif transition == "C->D":
            diagnosis = "D 才变差：verifier/repair 修坏了。"
        elif transition == "D->E":
            diagnosis = "E 才变差：memory 带偏。"
        else:
            diagnosis = f"首次 badcase 上升发生在 {transition}。"
    else:
        diagnosis = "A-E 五组没有出现顺序上的 badcase 上升。"

    return {
        "comparisons": comparisons,
        "first_badcase_increase": first_badcase_increase,
        "diagnosis": diagnosis,
        "skipped_tags": skipped_tags,
    }


async def _run(args: argparse.Namespace) -> Dict[str, Any]:
    runner_module = _load_runner_module(args.runner)
    helper_module = getattr(runner_module, "_BASE", runner_module)
    runner_parser = runner_module.build_parser()
    base_runner_args = runner_parser.parse_args([])
    if not getattr(base_runner_args, "skills_root", None):
        base_runner_args.skills_root = "skills_multimedia"

    for key, value in vars(args).items():
        if hasattr(base_runner_args, key) and value is not None:
            setattr(base_runner_args, key, value)

    data_dir = helper_module._resolve_data_dir(base_runner_args.data_dir)
    gold_file = (
        data_dir / args.gold_file
        if not Path(args.gold_file).is_absolute()
        else Path(args.gold_file)
    ).resolve()

    input_case_ids_file = (
        helper_module._resolve_existing_file(args.case_ids_file, label="case_ids_file")
        if args.case_ids_file
        else None
    )

    timestamp = args.run_tag or datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = (data_dir / "rollback_experiments" / timestamp).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    case_ids = _select_case_ids(
        runner_module=helper_module,
        data_dir=data_dir,
        case_count=args.case_count,
        offset=args.offset,
        case_ids_file=input_case_ids_file,
    )
    case_ids_path = (output_root / "case_ids.txt").resolve()
    case_ids_path.write_text("\n".join(case_ids) + "\n", encoding="utf-8")

    fixed_temperature = _resolve_fixed_temperature(
        runner_module=helper_module,
        runner_args=base_runner_args,
        requested_temperature=args.fixed_candidate_temperature,
    )

    selected_group_specs = _select_group_specs(
        _default_group_specs(),
        getattr(args, "group_tags", None) or DEFAULT_GROUP_TAGS,
    )

    group_results: List[Dict[str, Any]] = []
    for group_spec in selected_group_specs:
        prediction_dir = (
            Path("rollback_experiments")
            / timestamp
            / group_spec["tag"]
            / "predictions"
        ).as_posix()
        runner_args = _build_runner_args(
            runner_parser=runner_parser,
            base_args=base_runner_args,
            group_spec=group_spec,
            prediction_dir=prediction_dir,
            case_ids_file=case_ids_path,
            candidate_count=args.candidate_count,
            fixed_temperature=fixed_temperature,
        )
        meta = await runner_module._run(runner_args)
        result = _summarize_group_result(
            group_spec=group_spec,
            meta=meta,
            data_dir=data_dir,
            gold_file=gold_file,
            step_ref_base=args.step_ref_base,
        )
        if str(group_spec.get("tag", "")).strip().upper() == ORACLE_SOURCE_TAG:
            result["oracle_analysis"] = _build_o2_oracle_analysis(
                source_result=result,
                data_dir=data_dir,
                output_root=output_root,
                step_ref_base=args.step_ref_base,
            )
        result_path = output_root / f"{group_spec['tag']}_summary.json"
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        group_results.append(result)

    comparison = _compare_groups(group_results)
    summary = {
        "runner": args.runner,
        "data_dir": str(data_dir),
        "gold_file": str(gold_file),
        "case_count": len(case_ids),
        "case_ids_file": str(case_ids_path),
        "candidate_count": args.candidate_count,
        "fixed_candidate_temperature": fixed_temperature,
        "selected_group_tags": [str(item.get("tag", "")) for item in selected_group_specs],
        "results": group_results,
        "comparison": comparison,
    }
    summary_path = output_root / "rollback_summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "output_root": str(output_root),
        "summary_path": str(summary_path),
        "comparison": comparison,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the rollback experiment groups, including action-DAG probe group T."
    )
    parser.add_argument(
        "--runner",
        choices=["base", "openai", "qianwen", "gemini"],
        default=DEFAULT_RUNNER,
        help="Which existing pipeline-agent runner to reuse for defaults.",
    )
    parser.add_argument("--data_dir", type=str, default="taskbench/data_multimedia")
    parser.add_argument("--gold_file", type=str, default="data.json")
    parser.add_argument("--case_count", type=int, default=DEFAULT_CASE_COUNT)
    parser.add_argument("--case_ids_file", type=str, default=None)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--candidate_count", type=int, default=3)
    parser.add_argument(
        "--group_tags",
        nargs="+",
        default=DEFAULT_GROUP_TAGS,
        help="Optional subset of experiment groups to run, e.g. --group_tags T or --group_tags A O2.",
    )
    parser.add_argument("--fixed_candidate_temperature", type=float, default=None)
    parser.add_argument("--step_ref_base", choices=["one", "zero"], default="one")
    parser.add_argument("--run_tag", type=str, default=None)
    parser.add_argument(
        "--save_candidate_pool",
        action="store_true",
        default=True,
        help="Persist all candidate workflows for each successful case into candidate_dumps/ for post-hoc oracle analysis.",
    )
    parser.add_argument(
        "--stop_on_error",
        action="store_true",
        default=False,
        help="Stop the rollback experiment on the first case failure instead of continuing.",
    )



    parser.add_argument("--provider", type=str, default=DEFAULT_PROVIDER)
    parser.add_argument("--model_name", type=str, default=DEFAULT_MODEL_NAME)
    parser.add_argument("--llm_profile", type=str, default=None)
    parser.add_argument("--llm_config_path", type=str, default=DEFAULT_LLM_CONFIG_PATH)
    parser.add_argument("--workflow_memory_path", type=str, default=None)
    parser.add_argument("--edge_grounding_mode", type=str, default=None)
    parser.add_argument("--skills_root", type=str, default=None)
    parser.add_argument("--planning_prompt_mode", choices=["agent", "taskbench"], default=None)
    parser.add_argument("--use_demos", type=int, default=None)
    parser.add_argument("--dependency_type", type=str, default=None)
    parser.add_argument("--link_mode", type=str, default=None)
    parser.add_argument("--tool_map_override", type=str, default=None)
    parser.add_argument("--llm", type=str, default=None)
    parser.add_argument("--prediction_dir", type=str, default=None)
    parser.add_argument("--multiworker", type=int, default=None)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = asyncio.run(_run(args))
    comparison = result["comparison"]
    print(f"[DONE] output_root={result['output_root']}")
    print(f"[DONE] summary={result['summary_path']}")
    print(f"[RESULT] {comparison['diagnosis']}")
    first_increase = comparison.get("first_badcase_increase")
    if first_increase:
        print(
            f"[RESULT] first_badcase_increase="
            f"{first_increase['from']}->{first_increase['to']} "
            f"(delta={first_increase['delta']})"
        )


if __name__ == "__main__":
    main()
