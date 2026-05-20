import argparse
import asyncio
import json
import os
import sys
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv


SCRIPT_DIR = Path(__file__).resolve().parent


def _find_project_root(start: Path) -> Path:
    cur = start.resolve()
    candidates = [cur] + list(cur.parents)
    for c in candidates:
        if (c / "agent").exists() and (c / "taskbench").exists():
            return c
    raise FileNotFoundError(f"Cannot locate project root from: {start}")


ROOT = _find_project_root(SCRIPT_DIR)
TASKBENCH_ROOT = ROOT / "taskbench"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env", override=False)
load_dotenv(ROOT / ".env.local", override=False)

from agent.pipeline_orchestrator_agent import PipelineOrchestratorAgent


def _resolve_data_dir(raw: str) -> Path:
    p = Path(raw)
    candidates: List[Path] = []

    if p.is_absolute():
        candidates.append(p)
    else:
        cwd = Path.cwd()
        candidates.append((SCRIPT_DIR / p).resolve())
        candidates.append((SCRIPT_DIR.parent / p).resolve())
        candidates.append((cwd / p).resolve())
        candidates.append((ROOT / p).resolve())
        candidates.append((TASKBENCH_ROOT / p).resolve())
        if p.name in {"data_huggingface", "data_multimedia", "data_dailylifeapis"}:
            candidates.append((TASKBENCH_ROOT / p.name).resolve())

    seen: Set[str] = set()
    unique_candidates: List[Path] = []
    for c in candidates:
        key = str(c)
        if key in seen:
            continue
        seen.add(key)
        unique_candidates.append(c)

    for c in unique_candidates:
        if (c / "user_requests.json").exists() and (c / "tool_desc.json").exists():
            return c

    attempted = "\n".join(f"- {c}" for c in unique_candidates)
    raise FileNotFoundError(
        "Cannot locate valid TaskBench data_dir. Expected folder containing user_requests.json and tool_desc.json.\n"
        f"Given: {raw}\nTried:\n{attempted}"
    )


def _resolve_skills_root(raw: str) -> Path:
    p = Path(raw)
    candidates: List[Path] = []

    if p.is_absolute():
        candidates.append(p)
    else:
        cwd = Path.cwd()
        candidates.append((SCRIPT_DIR / p).resolve())
        candidates.append((SCRIPT_DIR.parent / p).resolve())
        candidates.append((cwd / p).resolve())
        candidates.append((ROOT / p).resolve())
        candidates.append((TASKBENCH_ROOT / p).resolve())
        candidates.append((TASKBENCH_ROOT / "pipelineOrchastration" / p).resolve())

    seen: Set[str] = set()
    unique_candidates: List[Path] = []
    for c in candidates:
        key = str(c)
        if key in seen:
            continue
        seen.add(key)
        unique_candidates.append(c)

    for c in unique_candidates:
        if c.exists() and c.is_dir():
            if any(c.glob("*/skill.json")):
                return c

    attempted = "\n".join(f"- {c}" for c in unique_candidates)
    raise FileNotFoundError(
        "Cannot locate valid skills_root. Expected folder containing subfolders with skill.json.\n"
        f"Given: {raw}\nTried:\n{attempted}"
    )


def _resolve_existing_file(raw: str, *, label: str) -> Path:
    p = Path(raw)
    candidates: List[Path] = []

    if p.is_absolute():
        candidates.append(p.resolve())
    else:
        cwd = Path.cwd()
        candidates.append((cwd / p).resolve())
        candidates.append((SCRIPT_DIR / p).resolve())
        candidates.append((SCRIPT_DIR.parent / p).resolve())
        candidates.append((ROOT / p).resolve())
        candidates.append((TASKBENCH_ROOT / p).resolve())
        candidates.append((TASKBENCH_ROOT / "pipelineOrchastration" / p).resolve())

    seen: Set[str] = set()
    unique_candidates: List[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique_candidates.append(candidate)

    for candidate in unique_candidates:
        if candidate.exists() and candidate.is_file():
            return candidate

    attempted = "\n".join(f"- {candidate}" for candidate in unique_candidates)
    raise FileNotFoundError(
        f"Cannot locate {label}.\n"
        f"Given: {raw}\n"
        f"Tried:\n{attempted}"
    )


def _normalize_name(name: str) -> str:
    return "".join(ch.lower() for ch in name if ch.isalnum())


def _load_tool_names(data_dir: Path) -> List[str]:
    tool_desc_path = data_dir / "tool_desc.json"
    payload = json.loads(tool_desc_path.read_text(encoding="utf-8"))
    return [str(item["id"]) for item in payload.get("nodes", []) if "id" in item]


def _pick_task_name(
    skill_name: str,
    tool_names: List[str],
    tool_map_override: Dict[str, str],
) -> str:
    if skill_name in tool_map_override:
        return tool_map_override[skill_name]

    tool_set = set(tool_names)
    if skill_name in tool_set:
        return skill_name

    lower_to_tool = {t.lower(): t for t in tool_names}
    if skill_name.lower() in lower_to_tool:
        return lower_to_tool[skill_name.lower()]

    norm_target = _normalize_name(skill_name)
    norm_to_tools: Dict[str, List[str]] = {}
    for tool in tool_names:
        norm_to_tools.setdefault(_normalize_name(tool), []).append(tool)
    if norm_target in norm_to_tools and len(norm_to_tools[norm_target]) == 1:
        return norm_to_tools[norm_target][0]

    guess = skill_name.replace("_", " ")
    if guess in tool_set:
        return guess
    if guess.lower() in lower_to_tool:
        return lower_to_tool[guess.lower()]

    return skill_name


def _extract_selected_plan(result: Dict[str, Any]) -> Any:
    if isinstance(result.get("selected_plan"), dict):
        return result["selected_plan"]
    if isinstance(result.get("plan"), dict):
        return result["plan"]
    if isinstance(result.get("workflow"), dict):
        return result["workflow"]
    if isinstance(result.get("selected_plan"), list):
        return result["selected_plan"]
    if isinstance(result.get("plan"), list):
        return result["plan"]
    candidates = result.get("candidate_plans")
    if isinstance(candidates, list) and candidates:
        best = max(candidates, key=lambda x: float(x.get("score", 0.0)))
        if isinstance(best.get("workflow"), dict):
            return best["workflow"]
        if isinstance(best.get("steps"), list):
            return best["steps"]
    return {}


def _to_step_text(index: int, task_name: str, args: Dict[str, Any]) -> str:
    arg_pairs: List[str] = []
    for k, v in args.items():
        arg_pairs.append(f"{k}={v}")
    suffix = ", ".join(arg_pairs) if arg_pairs else "no arguments"
    return f"Step {index}: Call {task_name} with {suffix}."


def _normalize_resource_arg_ref(value: Any) -> str:
    text = str(value).strip()
    if not text:
        return text

    # Accept wrapped natural-language refs like "{output of step 2}".
    if len(text) >= 2 and text[0] == "{" and text[-1] == "}":
        text = text[1:-1].strip()
        if not text:
            return text

    if re.fullmatch(r"<node-\d+>", text):
        return text

    return text


def _get_plan_step_output_key(step: Dict[str, Any]) -> Optional[str]:
    output_key = step.get("output_key")
    if isinstance(output_key, str) and output_key.strip():
        return output_key.strip()

    args = step.get("args", {})
    legacy_output_key = args.get("output_key") if isinstance(args, dict) else None
    if isinstance(legacy_output_key, str) and legacy_output_key.strip():
        return legacy_output_key.strip()
    return None


def _get_plan_step_input_map(step: Dict[str, Any]) -> Dict[str, str]:
    input_map = step.get("input_map", {})
    if not isinstance(input_map, dict):
        return {}

    normalized: Dict[str, str] = {}
    for arg_name, upstream_key in input_map.items():
        if isinstance(arg_name, str) and arg_name.strip() and isinstance(upstream_key, str) and upstream_key.strip():
            normalized[arg_name.strip()] = upstream_key.strip()
    return normalized


def _dedupe_links(links: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen: Set[Tuple[str, str]] = set()
    out: List[Dict[str, str]] = []
    for link in links:
        pair = (str(link.get("source", "")), str(link.get("target", "")))
        if not pair[0] or not pair[1]:
            continue
        if pair in seen:
            continue
        seen.add(pair)
        out.append({"source": pair[0], "target": pair[1]})
    return out


def _convert_plan_to_taskbench_result(
    plan: Any,
    tool_names: List[str],
    dependency_type: str,
    tool_map_override: Dict[str, str],
    link_mode: str,
) -> Dict[str, Any]:
    if isinstance(plan, dict):
        task_steps: List[str] = []
        task_nodes: List[Dict[str, Any]] = []
        task_links: List[Dict[str, str]] = []
        raw_task_nodes = plan.get("task_nodes", [])
        raw_task_steps = plan.get("task_steps", [])
        raw_task_links = plan.get("task_links", [])
        mapped_task_names: List[str] = []

        if not isinstance(raw_task_nodes, list):
            raw_task_nodes = []
        if not isinstance(raw_task_steps, list):
            raw_task_steps = []
        if not isinstance(raw_task_links, list):
            raw_task_links = []

        for seq_idx, node in enumerate(raw_task_nodes):
            if not isinstance(node, dict):
                continue
            skill_name = str(node.get("task", "unknown_skill"))
            task_name = _pick_task_name(skill_name, tool_names, tool_map_override)
            mapped_task_names.append(task_name)

            raw_arguments = node.get("arguments", [])
            if not isinstance(raw_arguments, list):
                raw_arguments = []

            step_args: Dict[str, Any] = {}
            temporal_args: List[Dict[str, str]] = []
            resource_args: List[str] = []

            for arg_idx, arg in enumerate(raw_arguments, start=1):
                if isinstance(arg, dict):
                    arg_name = str(arg.get("name", "")).strip() or f"arg{arg_idx}"
                    arg_value = arg.get("value")
                else:
                    arg_name = f"arg{arg_idx}"
                    arg_value = arg

                normalized_value = _normalize_resource_arg_ref(arg_value)
                step_args[arg_name] = normalized_value

                if dependency_type == "temporal":
                    temporal_args.append({"name": arg_name, "value": str(arg_value)})
                else:
                    resource_args.append(normalized_value)

                if isinstance(normalized_value, str):
                    m = re.fullmatch(r"<node-(\d+)>", normalized_value)
                    if m:
                        source_idx = int(m.group(1))
                        if 0 <= source_idx < len(mapped_task_names) - 1:
                            task_links.append({"source": mapped_task_names[source_idx], "target": task_name})

            if seq_idx < len(raw_task_steps) and isinstance(raw_task_steps[seq_idx], str) and raw_task_steps[seq_idx].strip():
                task_steps.append(raw_task_steps[seq_idx].strip())
            else:
                task_steps.append(_to_step_text(seq_idx + 1, task_name, step_args))

            if dependency_type == "temporal":
                task_nodes.append({"task": task_name, "arguments": temporal_args})
            else:
                task_nodes.append({"task": task_name, "arguments": resource_args})

        if not task_links:
            for link in raw_task_links:
                if not isinstance(link, dict):
                    continue
                source = _pick_task_name(str(link.get("source", "")), tool_names, tool_map_override)
                target = _pick_task_name(str(link.get("target", "")), tool_names, tool_map_override)
                if source and target:
                    task_links.append({"source": source, "target": target})

        if link_mode == "chain_fallback" and not task_links and len(mapped_task_names) > 1:
            for i in range(1, len(mapped_task_names)):
                task_links.append({"source": mapped_task_names[i - 1], "target": mapped_task_names[i]})

        return {
            "task_steps": task_steps,
            "task_nodes": task_nodes,
            "task_links": _dedupe_links(task_links),
        }

    task_steps: List[str] = []
    task_nodes: List[Dict[str, Any]] = []
    task_links: List[Dict[str, str]] = []
    output_key_to_task_idx: Dict[str, int] = {}
    output_key_to_node_ref: Dict[str, str] = {}

    mapped_task_names: List[str] = []

    def _step_to_node_ref(step_payload: Dict[str, Any], fallback_idx: int) -> str:
        step_id = step_payload.get("id") - 1
        if isinstance(step_id, int) and step_id >= 0:
            return f"<node-{step_id}>"
        if isinstance(step_id, str):
            text = step_id.strip()
            if text:
                m = re.fullmatch(r"<node-(\d+)>", text)
                if m:
                    return f"<node-{int(m.group(1))}>"
                m = re.fullmatch(r"(?i)node[-_]?(\d+)", text)
                if m:
                    return f"<node-{int(m.group(1))}>"
                if text.isdigit() and int(text) >= 0:
                    return f"<node-{int(text)}>"
        return f"<node-{fallback_idx}>"

    for seq_idx, step in enumerate(plan):
        idx = seq_idx
        node_ref_for_step = _step_to_node_ref(step, seq_idx)
        m = re.fullmatch(r"<node-(\d+)>", node_ref_for_step)
        if m:
            idx = int(m.group(1))

        args = step.get("args", {})
        if not isinstance(args, dict):
            args = {}
        input_map = _get_plan_step_input_map(step)
        current_step_node_ref = f"<node-{idx}>"

        skill_name = str(step.get("skill", "unknown_skill"))
        task_name = _pick_task_name(skill_name, tool_names, tool_map_override)
        mapped_task_names.append(task_name)

        step_args_for_text = args
        if dependency_type == "resource":
            step_args_for_text = dict(args)
            for arg_name, upstream_key in input_map.items():
                ref = output_key_to_node_ref.get(upstream_key, upstream_key)
                step_args_for_text[arg_name] = ref
            step_args_for_text = {
                key: (_normalize_resource_arg_ref(val) if key != "output_key" else val)
                for key, val in step_args_for_text.items()
            }
        else:
            step_args_for_text = dict(args)
            for arg_name, upstream_key in input_map.items():
                if arg_name not in step_args_for_text:
                    step_args_for_text[arg_name] = upstream_key
        task_steps.append(_to_step_text(seq_idx + 1, task_name, step_args_for_text))

        if dependency_type == "temporal":
            node_args: List[Dict[str, str]] = []
            temporal_args = dict(args)
            for arg_name, upstream_key in input_map.items():
                if arg_name not in temporal_args:
                    temporal_args[arg_name] = upstream_key
            for k, v in temporal_args.items():
                if k in {"output_key"}:
                    continue
                if k == "source_ref" and isinstance(v, str) and v in output_key_to_task_idx:
                    continue
                node_args.append({"name": str(k), "value": str(v)})
            task_nodes.append({"task": task_name, "arguments": node_args})
        else:
            node_args_resource: List[str] = []
            mapped_node_refs: Set[str] = set()
            for k, v in args.items():
                if k == "output_key":
                    continue
                if k == "source_ref":
                    if not isinstance(v, str):
                        continue
                    ref = output_key_to_node_ref.get(v)
                    if not ref:
                        continue
                    if ref not in mapped_node_refs:
                        node_args_resource.append(ref)
                        mapped_node_refs.add(ref)
                    continue
                # If arg value itself is a known produced output key, use node ref only.
                if isinstance(v, str) and v in output_key_to_node_ref:
                    ref = output_key_to_node_ref[v]
                    if ref not in mapped_node_refs:
                        node_args_resource.append(ref)
                        mapped_node_refs.add(ref)
                    continue
                node_args_resource.append(_normalize_resource_arg_ref(v))
            for upstream_key in input_map.values():
                ref = output_key_to_node_ref.get(upstream_key)
                if not ref or ref in mapped_node_refs:
                    continue
                node_args_resource.append(ref)
                mapped_node_refs.add(ref)
            task_nodes.append({"task": task_name, "arguments": node_args_resource})

        source_ref = args.get("source_ref")
        if isinstance(source_ref, str) and source_ref in output_key_to_task_idx:
            src_idx = output_key_to_task_idx[source_ref]
            if 0 <= src_idx < len(mapped_task_names):
                task_links.append({"source": mapped_task_names[src_idx], "target": task_name})
        for upstream_key in input_map.values():
            if upstream_key not in output_key_to_task_idx:
                continue
            src_idx = output_key_to_task_idx[upstream_key]
            if 0 <= src_idx < len(mapped_task_names):
                task_links.append({"source": mapped_task_names[src_idx], "target": task_name})

        output_key = _get_plan_step_output_key(step)
        if output_key:
            output_key_to_task_idx[output_key] = seq_idx
            output_key_to_node_ref[output_key] = current_step_node_ref

    if link_mode == "chain_fallback" and not task_links and len(mapped_task_names) > 1:
        for i in range(1, len(mapped_task_names)):
            task_links.append({"source": mapped_task_names[i - 1], "target": mapped_task_names[i]})

    deduped_links = _dedupe_links(task_links)
    return {
        "task_steps": task_steps,
        "task_nodes": task_nodes,
        "task_links": deduped_links,
    }


async def _run_one(
    agent: PipelineOrchestratorAgent,
    user_request: str,
    planning_mode: str,
    execution_mode: str,
    candidate_count: int,
    include_summary: bool,
) -> Dict[str, Any]:
    return await agent.run(
        user_requirement=user_request,
        planning_mode=planning_mode,
        execution_mode=execution_mode,
        candidate_count=candidate_count,
        include_summary=include_summary,
    )


def _load_existing_ids(output_path: Path) -> Set[str]:
    done: Set[str] = set()
    if not output_path.exists():
        return done
    with output_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = str(payload.get("id", ""))
            if sid:
                done.add(sid)
    return done


def _load_requests(data_dir: Path) -> List[Dict[str, Any]]:
    req_path = data_dir / "user_requests.json"
    items: List[Dict[str, Any]] = []
    with req_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


def _load_case_ids(path: Path) -> List[str]:
    case_ids: List[str] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            case_ids.append(text)
    return case_ids


def _open_prediction_output(output_path: Path, *, resume: bool):
    mode = "a" if resume else "w"
    return output_path.open(mode, encoding="utf-8")


def _load_tool_map_override(path: Optional[Path]) -> Dict[str, str]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("tool_map_override must be a JSON object: {\"skill_name\": \"task_name\"}")
    return {str(k): str(v) for k, v in payload.items()}


def _stringify_json_field(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _classify_case_failure(error: Exception) -> str:
    if isinstance(error, ValueError):
        lowered = str(error).strip().lower()
        validation_prefixes = (
            "workflow output invalid:",
            "workflow task_steps",
            "workflow task_links inconsistent with task_nodes:",
            "workflow must contain",
            "task_nodes[",
        )
        if any(lowered.startswith(prefix) for prefix in validation_prefixes):
            return "validation_failure"
    return "other_failure"


def _build_prediction_record(
    sid: str,
    instruction: str,
    taskbench_result: Dict[str, Any],
) -> Dict[str, Any]:
    task_steps = taskbench_result.get("task_steps", [])
    task_nodes = taskbench_result.get("task_nodes", [])
    task_links = taskbench_result.get("task_links", [])
    return {
        "id": sid,
        "instruction": instruction,
        "n_tools": len(task_nodes),
        "tool_steps": _stringify_json_field(task_steps),
        "tool_nodes": _stringify_json_field(task_nodes),
        "tool_links": _stringify_json_field(task_links),
        "result": {
            "task_steps": task_steps,
            "task_nodes": task_nodes,
            "task_links": task_links,
        },
    }


def _build_candidate_dump_record(
    sid: str,
    instruction: str,
    taskbench_result: Dict[str, Any],
    raw_result: Dict[str, Any],
    *,
    tool_names: List[str],
    dependency_type: str,
    tool_map_override: Dict[str, str],
    link_mode: str,
) -> Dict[str, Any]:
    candidate_rows: List[Dict[str, Any]] = []
    raw_candidates = raw_result.get("candidate_plans", [])
    if not isinstance(raw_candidates, list):
        raw_candidates = []

    selected_plan_id = raw_result.get("selected_plan_id")
    selected_candidate_score: Optional[float] = None

    for item in raw_candidates:
        if not isinstance(item, dict):
            continue
        workflow = item.get("workflow")
        if not isinstance(workflow, dict):
            continue

        candidate_result = _convert_plan_to_taskbench_result(
            plan=workflow,
            tool_names=tool_names,
            dependency_type=dependency_type,
            tool_map_override=tool_map_override,
            link_mode=link_mode,
        )
        candidate_row = {
            "id": item.get("id"),
            "generation_index": item.get("generation_index"),
            "strategy_name": item.get("strategy_name"),
            "strategy_hint": item.get("strategy_hint"),
            "sampling_temperature": item.get("sampling_temperature"),
            "score": item.get("score"),
            "score_details": item.get("score_details"),
            "selection_meta": item.get("selection_meta"),
            "verification_meta": item.get("verification_meta"),
            "dependency_check": item.get("dependency_check"),
            "repair_meta": item.get("repair_meta"),
            "edge_grounding_meta": item.get("edge_grounding_meta"),
            "workflow": workflow,
            "result": candidate_result,
        }
        if item.get("id") == selected_plan_id:
            try:
                selected_candidate_score = float(item.get("score"))
            except (TypeError, ValueError):
                selected_candidate_score = None
        candidate_rows.append(candidate_row)

    return {
        "id": sid,
        "instruction": instruction,
        "selected_plan_id": selected_plan_id,
        "selected_candidate_score": selected_candidate_score,
        "selection_route": raw_result.get("selection_route"),
        "structure_aware_meta": raw_result.get("structure_aware_meta"),
        "selected_result": taskbench_result,
        "candidates": candidate_rows,
    }


def _should_load_workflow_memory_for_run(args: argparse.Namespace) -> bool:
    if bool(getattr(args, "enable_workflow_memory", False)):
        return True
    if str(getattr(args, "candidate_selection_mode", "rerank") or "rerank").strip().lower() == "structure_aware":
        return True
    edge_grounding_mode = str(getattr(args, "edge_grounding_mode", "none") or "none").strip().lower()
    return edge_grounding_mode in {
        "semantic_edge_scoring",
        "semantic",
        "semantic_edge_scorer",
        "h2",
        "semantic_edge_scoring_h2a",
        "semantic_nearest_priority",
        "h2a",
        "semantic_edge_scoring_h2b",
        "semantic_semantic_priority",
        "h2b",
    }


async def _run(args: argparse.Namespace) -> Dict[str, Any]:
    data_dir = _resolve_data_dir(args.data_dir)
    llm_config_path = _resolve_existing_file(args.llm_config_path, label="llm_config_path") if args.llm_config_path else None
    enable_workflow_memory = bool(getattr(args, "enable_workflow_memory", False))
    load_workflow_memory = _should_load_workflow_memory_for_run(args)
    workflow_memory_path = (
        _resolve_existing_file(args.workflow_memory_path, label="workflow_memory_path")
        if load_workflow_memory and args.workflow_memory_path
        else None
    )
    tool_map_override_path = (
        _resolve_existing_file(args.tool_map_override, label="tool_map_override") if args.tool_map_override else None
    )
    case_ids_file = (
        _resolve_existing_file(args.case_ids_file, label="case_ids_file")
        if getattr(args, "case_ids_file", None)
        else None
    )

    dependency_type = args.dependency_type
    if dependency_type == "auto":
        dependency_type = "temporal" if "dailylife" in data_dir.name.lower() else "resource"

    prediction_dir_name = args.prediction_dir
    prediction_dir = data_dir / prediction_dir_name
    prediction_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d")
    output_path = prediction_dir / f"{args.llm}_{args.model_name}_{timestamp}.json"
    save_candidate_pool = bool(getattr(args, "save_candidate_pool", False))
    candidate_dump_dir = prediction_dir.parent / "candidate_dumps"
    candidate_dump_path = candidate_dump_dir / f"{args.llm}_{args.model_name}_{timestamp}.jsonl"

    done_ids = _load_existing_ids(output_path) if args.resume else set()
    requests = _load_requests(data_dir)
    selected_case_ids: List[str] = []
    if case_ids_file is not None:
        selected_case_ids = _load_case_ids(case_ids_file)
        selected_case_id_set = set(selected_case_ids)
        requests = [x for x in requests if str(x.get("id", "")) in selected_case_id_set]
        order_map = {case_id: idx for idx, case_id in enumerate(selected_case_ids)}
        requests.sort(key=lambda item: order_map.get(str(item.get("id", "")), 10**9))
    if args.offset > 0:
        requests = requests[args.offset :]
    if args.limit is not None:
        requests = requests[: args.limit]
    if done_ids:
        requests = [x for x in requests if str(x.get("id", "")) not in done_ids]

    tool_names = _load_tool_names(data_dir)
    tool_map_override = _load_tool_map_override(tool_map_override_path)

    print(f"[INFO] data_dir={data_dir}")
    print(f"[INFO] dependency_type={dependency_type}")
    print(f"[INFO] output={output_path}")
    if save_candidate_pool:
        print(f"[INFO] candidate_dump={candidate_dump_path}")
    print(f"[INFO] total_to_run={len(requests)} (resume={args.resume}, skipped={len(done_ids)})")
    if args.llm_profile:
        print(f"[INFO] llm_profile={args.llm_profile}")
    if llm_config_path:
        print(f"[INFO] llm_config_path={llm_config_path}")
    print(f"[INFO] enable_workflow_memory={enable_workflow_memory}")
    if workflow_memory_path:
        print(f"[INFO] workflow_memory_path={workflow_memory_path}")

    skills_root = _resolve_skills_root(args.skills_root) if args.skills_root else None
    model_name = args.model_name
    if not args.llm_profile and not llm_config_path and args.provider == "openai" and model_name == "qwen-max":
        model_name = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    agent = PipelineOrchestratorAgent(
        model_name=model_name,
        skills_root=skills_root,
        provider=args.provider,
        llm_profile=args.llm_profile,
        llm_config_path=llm_config_path,
        workflow_memory_path=workflow_memory_path,
        enable_workflow_memory=enable_workflow_memory,
        enable_candidate_verifier=bool(getattr(args, "enable_candidate_verifier", True)),
        enable_candidate_repair=bool(getattr(args, "enable_candidate_repair", True)),
        candidate_selection_mode=str(getattr(args, "candidate_selection_mode", "rerank")),
        include_original_candidate=bool(getattr(args, "include_original_candidate", False)),
        fixed_candidate_temperature=getattr(args, "fixed_candidate_temperature", None),
        edge_grounding_mode=str(getattr(args, "edge_grounding_mode", "none")),
        enable_strict_planning_prompt=bool(getattr(args, "enable_strict_planning_prompt", False)),
        enable_action_checklist=bool(getattr(args, "enable_action_checklist", False)),
        enable_parameter_normalization=bool(getattr(args, "enable_parameter_normalization", False)),
    )
    success = 0
    failed = 0
    validation_failed = 0
    failure_counts: Dict[str, int] = {}
    failure_details: List[Dict[str, str]] = []
    candidate_wf = None
    if save_candidate_pool:
        candidate_dump_dir.mkdir(parents=True, exist_ok=True)
        candidate_wf = _open_prediction_output(candidate_dump_path, resume=args.resume)

    try:
        with _open_prediction_output(output_path, resume=args.resume) as wf:
            for idx, item in enumerate(requests, start=1):
                sid = str(item.get("id", ""))
                user_request = str(item.get("user_request", "")).strip()
                if not sid or not user_request:
                    failed += 1
                    print(f"[WARN] skip invalid sample at #{idx}: id={sid}")
                    continue

                try:
                    raw_result = await _run_one(
                        agent=agent,
                        user_request=user_request,
                        planning_mode=args.planning_mode,
                        execution_mode=args.execution_mode,
                        candidate_count=args.candidate_count,
                        include_summary=bool(getattr(args, "include_summary", False)),
                    )
                    plan = _extract_selected_plan(raw_result)
                    taskbench_result = _convert_plan_to_taskbench_result(
                        plan=plan,
                        tool_names=tool_names,
                        dependency_type=dependency_type,
                        tool_map_override=tool_map_override,
                        link_mode=args.link_mode,
                    )
                    out = _build_prediction_record(
                        sid=sid,
                        instruction=user_request,
                        taskbench_result=taskbench_result,
                    )
                    wf.write(json.dumps(out, ensure_ascii=False) + "\n")
                    wf.flush()
                    if candidate_wf is not None:
                        candidate_dump = _build_candidate_dump_record(
                            sid=sid,
                            instruction=user_request,
                            taskbench_result=taskbench_result,
                            raw_result=raw_result,
                            tool_names=tool_names,
                            dependency_type=dependency_type,
                            tool_map_override=tool_map_override,
                            link_mode=args.link_mode,
                        )
                        candidate_wf.write(json.dumps(candidate_dump, ensure_ascii=False) + "\n")
                        candidate_wf.flush()
                    success += 1
                    if idx % args.log_every == 0:
                        print(f"[INFO] progress={idx}/{len(requests)} success={success} failed={failed}")
                except Exception as e:
                    failed += 1
                    failure_category = _classify_case_failure(e)
                    failure_counts[failure_category] = failure_counts.get(failure_category, 0) + 1
                    if failure_category == "validation_failure":
                        validation_failed += 1
                    failure_details.append(
                        {
                            "id": sid,
                            "category": failure_category,
                            "error_type": type(e).__name__,
                            "error": str(e),
                        }
                    )
                    print(f"[ERROR] id={sid} failed ({failure_category}): {type(e).__name__}: {e}")
                    if args.stop_on_error:
                        raise
    finally:
        if candidate_wf is not None:
            candidate_wf.close()

    print(f"[DONE] success={success}, failed={failed}, output={output_path}")
    return {
        "data_dir": str(data_dir),
        "output_path": str(output_path),
        "prediction_dir": str(prediction_dir),
        "candidate_dump_path": str(candidate_dump_path) if save_candidate_pool else None,
        "success": success,
        "failed": failed,
        "validation_failed": validation_failed,
        "other_failed": failed - validation_failed,
        "failure_counts": failure_counts,
        "failure_details": failure_details,
        "total_to_run": len(requests),
        "selected_case_ids": selected_case_ids,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run TaskBench inference with PipelineOrchestratorAgent.")
    parser.add_argument("--data_dir", type=str, default="taskbench/data_multimedia")
    parser.add_argument("--prediction_dir", type=str, default="predictions_pipeline_agent")
    parser.add_argument("--llm", type=str, default="pipeline_orchestrator_agent")
    parser.add_argument("--provider", type=str, default="openai", choices=["tongyi", "openai", "gemini"])
    parser.add_argument("--model_name", type=str, default="gpt-5.4")
    parser.add_argument("--llm_profile", type=str, default=None, help="Named LLM profile, e.g. qwen-max or gpt4.")
    parser.add_argument("--llm_config_path", type=str, default="configs/openai.json", help="Path to JSON config containing LLM profiles.")
    parser.add_argument(
        "--workflow_memory_path",
        type=str,
        default=None,
        help="Optional workflow memory JSON path for aggregated motif/transition priors.",
    )
    parser.add_argument(
        "--enable-workflow-memory",
        "--enable_workflow_memory",
        dest="enable_workflow_memory",
        action="store_true",
        default=False,
        help="Enable workflow memory retrieval and prior scoring. Disabled by default.",
    )
    parser.add_argument(
        "--disable-workflow-memory",
        "--disable_workflow_memory",
        dest="enable_workflow_memory",
        action="store_false",
        help="Disable workflow memory retrieval and prior scoring.",
    )
    parser.add_argument("--skills_root", type=str, default=None, help="Custom skills root for PipelineOrchestratorAgent.")
    parser.add_argument("--planning_mode", type=str, default="single", choices=["single", "multi"])
    parser.add_argument("--execution_mode", type=str, default="best", choices=["best", "all"])
    parser.add_argument("--candidate_count", type=int, default=3)
    parser.add_argument(
        "--candidate_selection_mode",
        type=str,
        default="rerank",
        choices=["rerank", "first", "original_first_fallback", "original_dependency_filter_first_valid", "structure_aware"],
        help="How to choose the final plan from the generated candidate pool.",
    )
    parser.add_argument(
        "--include_original_candidate",
        action="store_true",
        default=False,
        help="Prepend the original no-hint planning call into the multi-candidate pool.",
    )
    parser.add_argument(
        "--fixed_candidate_temperature",
        type=float,
        default=None,
        help="If set, force every candidate generation call to use the same temperature.",
    )
    parser.add_argument(
        "--enable_candidate_verifier",
        dest="enable_candidate_verifier",
        action="store_true",
        default=True,
        help="Enable verifier signals during candidate selection.",
    )
    parser.add_argument(
        "--disable_candidate_verifier",
        dest="enable_candidate_verifier",
        action="store_false",
        help="Disable verifier signals during candidate selection.",
    )
    parser.add_argument(
        "--enable_candidate_repair",
        dest="enable_candidate_repair",
        action="store_true",
        default=True,
        help="Enable LLM-based repair for verifier-marked candidates.",
    )
    parser.add_argument(
        "--disable_candidate_repair",
        dest="enable_candidate_repair",
        action="store_false",
        help="Disable LLM-based repair for verifier-marked candidates.",
    )
    parser.add_argument(
        "--edge_grounding_mode",
        type=str,
        default="none",
        choices=[
            "none",
            "nearest_valid_upstream",
            "nearest_valid",
            "nearest",
            "semantic_edge_scoring",
            "semantic",
            "semantic_edge_scorer",
            "h2",
            "semantic_edge_scoring_h2a",
            "semantic_nearest_priority",
            "h2a",
            "semantic_edge_scoring_h2b",
            "semantic_semantic_priority",
            "h2b",
        ],
        help="Optional post-generation dependency grounding strategy applied before candidate scoring.",
    )
    parser.add_argument(
        "--enable_strict_planning_prompt",
        dest="enable_strict_planning_prompt",
        action="store_true",
        default=False,
        help="Enable stricter planning prompt constraints for minimum-tool and no-extra-action behavior.",
    )
    parser.add_argument(
        "--disable_strict_planning_prompt",
        dest="enable_strict_planning_prompt",
        action="store_false",
        help="Disable stricter planning prompt constraints.",
    )
    parser.add_argument(
        "--enable_action_checklist",
        dest="enable_action_checklist",
        action="store_true",
        default=False,
        help="Enable an internal explicit-action checklist in the planning prompt.",
    )
    parser.add_argument(
        "--disable_action_checklist",
        dest="enable_action_checklist",
        action="store_false",
        help="Disable the planning action checklist.",
    )
    parser.add_argument(
        "--enable_parameter_normalization",
        dest="enable_parameter_normalization",
        action="store_true",
        default=False,
        help="Normalize short parameter values such as speed and voice variants before validation/scoring.",
    )
    parser.add_argument(
        "--disable_parameter_normalization",
        dest="enable_parameter_normalization",
        action="store_false",
        help="Disable parameter normalization.",
    )
    parser.add_argument("--dependency_type", type=str, default="auto", choices=["auto", "resource", "temporal"])
    parser.add_argument("--link_mode", type=str, default="chain_fallback", choices=["explicit_only", "chain_fallback"])
    parser.add_argument("--tool_map_override", type=str, default=None, help="JSON file: {skill_name: task_name}.")
    parser.add_argument("--case_ids_file", type=str, default=None, help="Optional newline-delimited case-id file.")
    parser.add_argument(
        "--include_summary",
        action="store_true",
        default=False,
        help="Request a final natural-language summary from the LLM after execution.",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--resume", action="store_true", default=True)
    parser.add_argument("--no-resume", dest="resume", action="store_false")
    parser.add_argument(
        "--save_candidate_pool",
        action="store_true",
        default=False,
        help="Write all generated candidate workflows plus selection metadata to a separate candidate_dumps JSONL file.",
    )
    parser.add_argument("--log_every", type=int, default=10)
    parser.add_argument("--stop_on_error", action="store_true", default=False)
    return parser


if __name__ == "__main__":
    cli_args = build_parser().parse_args()
    if not cli_args.skills_root:
        cli_args.skills_root = "skills_multimedia"  # 或绝对路径
    cli_args.limit = 1
    asyncio.run(_run(cli_args))
