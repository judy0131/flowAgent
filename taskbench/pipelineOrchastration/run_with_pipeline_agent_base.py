import argparse
import asyncio
import json
import os
import sys
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from .export_three_tables import _materialize_semantic_graph
except ImportError:
    from export_three_tables import _materialize_semantic_graph  # type: ignore


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


def _resolve_cached_original_file(raw: str, *, data_dir: Path) -> Path:
    p = Path(raw)
    if p.is_absolute():
        if p.exists() and p.is_file():
            return p.resolve()
        raise FileNotFoundError(f"Cannot locate cached_original: {raw}")

    candidates = [
        (Path.cwd() / p).resolve(),
        (data_dir / p).resolve(),
        (data_dir / "predictions_use_demos_2_reformat_by_self" / p).resolve(),
        (data_dir / "predictions_pipeline_agent" / p).resolve(),
        (ROOT / p).resolve(),
        (TASKBENCH_ROOT / p).resolve(),
    ]
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
    raise FileNotFoundError(f"Cannot locate cached_original: {raw}\nTried:\n{attempted}")


def _resolve_orthogonal_v4_output(raw: str, *, data_dir: Path) -> Path:
    p = Path(raw)
    if p.is_absolute():
        return p.resolve()
    if p.parent == Path("."):
        return (data_dir / "candidate_dumps" / p.name).resolve()
    return (data_dir / p).resolve()


def _parse_bool_arg(value: Optional[str]) -> bool:
    if value is None:
        return True
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"expected boolean value, got: {value}")


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
            sid = str(payload.get("id") or payload.get("case_id") or "")
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


def _load_json_or_jsonl_rows(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            return [payload]
    except json.JSONDecodeError:
        pass

    rows: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _load_cached_original_predictions(path: Path) -> Dict[str, Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    for row in _load_json_or_jsonl_rows(path):
        sid = str(row.get("id") or row.get("case_id") or "").strip()
        result = row.get("result")
        if sid and isinstance(result, dict):
            rows[sid] = row
    return rows


def _canonical_cached_original_result(row: Dict[str, Any]) -> Dict[str, Any]:
    result = row.get("result")
    if not isinstance(result, dict):
        return {"task_steps": [], "task_nodes": []}
    task_steps = result.get("task_steps", [])
    task_nodes = result.get("task_nodes", [])
    output = {
        "task_steps": task_steps if isinstance(task_steps, list) else [],
        "task_nodes": task_nodes if isinstance(task_nodes, list) else [],
    }
    return output


def _recover_dependency_edges_from_result(result: Dict[str, Any]) -> Tuple[List[Tuple[int, int]], bool]:
    task_nodes = result.get("task_nodes", [])
    nodes = task_nodes if isinstance(task_nodes, list) else []
    edges: Set[Tuple[int, int]] = set()
    failed = False
    for target_idx, node in enumerate(nodes):
        if not isinstance(node, dict):
            failed = True
            continue
        arguments = node.get("arguments", [])
        if not isinstance(arguments, list):
            failed = True
            continue
        for argument in arguments:
            values: List[Any]
            if isinstance(argument, dict):
                values = list(argument.values())
            elif isinstance(argument, list):
                values = argument
            else:
                values = [argument]
            for value in values:
                for match in re.finditer(r"<node-(\d+)>", str(value)):
                    source_idx = int(match.group(1))
                    if source_idx < 0 or source_idx >= target_idx:
                        failed = True
                        continue
                    edges.add((source_idx, target_idx))
    if len(nodes) > 1 and not edges:
        failed = True
    return sorted(edges), failed


def _infer_workflow_type_from_result(result: Dict[str, Any]) -> Tuple[str, str]:
    task_nodes = result.get("task_nodes", [])
    node_count = len(task_nodes) if isinstance(task_nodes, list) else 0
    if node_count == 1:
        return "single", "high"
    if node_count <= 0:
        return "chain", "low"

    edges, recovery_failed = _recover_dependency_edges_from_result(result)
    if recovery_failed:
        return "chain", "low"

    indegree = Counter(target for _, target in edges)
    outdegree = Counter(source for source, _ in edges)
    if any(indegree[idx] > 1 or outdegree[idx] > 1 for idx in range(node_count)):
        return "dag", "high"

    adjacency: Dict[int, Set[int]] = {idx: set() for idx in range(node_count)}
    for source, target in edges:
        adjacency[source].add(target)
        adjacency[target].add(source)

    seen = {0}
    stack = [0]
    while stack:
        current = stack.pop()
        for nxt in adjacency[current]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    if len(seen) == node_count:
        return "chain", "high"
    return "chain", "low"


def _orthogonal_v4_families_for_workflow_type(workflow_type: str) -> List[str]:
    if workflow_type == "chain":
        return ["minimal", "action_coverage", "typed_dependency"]
    if workflow_type == "dag":
        return ["action_coverage", "typed_dependency", "parallel_dag", "branch_preserving"]
    return []


def _build_original_baseline_candidate(original_result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "candidate_id": "original_baseline",
        "family": "original",
        "source": "cached",
        "result": original_result,
        "parse_success": True,
        "parse_error": "",
    }


def _load_demo_field(data: Dict[str, Any], primary_key: str, fallback_key: Optional[str] = None) -> Any:
    value = data.get(primary_key)
    if value is None and fallback_key:
        value = data.get(fallback_key)
    if value is None:
        keys = primary_key if not fallback_key else f"{primary_key}/{fallback_key}"
        raise KeyError(f"Demo {data.get('id')} is missing {keys}")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _load_taskbench_prompt_tool_list(data_dir: Path, dependency_type: str) -> List[Dict[str, Any]]:
    tool_list = json.loads((data_dir / "tool_desc.json").read_text(encoding="utf-8"))["nodes"]
    tool_list = json.loads(json.dumps(tool_list, ensure_ascii=False))
    if tool_list and "input-type" not in tool_list[0]:
        if dependency_type != "temporal":
            raise AssertionError(
                "Tool type is not ignored, but the tool list does not contain input-type and output-type"
            )
    if dependency_type == "temporal":
        for tool in tool_list:
            parameters = tool.get("parameters", [])
            if isinstance(parameters, list):
                tool["parameters"] = [
                    str(parameter.get("name", ""))
                    for parameter in parameters
                    if isinstance(parameter, dict) and str(parameter.get("name", ""))
                ]
    return tool_list


def _taskbench_demo_ids(data_dir: Path, dependency_type: str, use_demos: int) -> List[str]:
    if use_demos <= 0:
        return []
    if dependency_type == "temporal":
        demos_id = ["38563456", "27267145", "91005535"]
    elif "huggingface" in str(data_dir).lower():
        demos_id = ["10523150", "14611002", "22067492"]
    elif "multimedia" in str(data_dir).lower():
        demos_id = ["30934207", "20566230", "19003517"]
    else:
        demos_id = []
    return demos_id[:use_demos]


def _load_taskbench_prompt_demos(data_dir: Path, dependency_type: str, use_demos: int) -> List[Dict[str, Any]]:
    demos_id = set(_taskbench_demo_ids(data_dir, dependency_type, max(use_demos, 0)))
    if not demos_id:
        return []

    demos: List[Dict[str, Any]] = []
    with (data_dir / "data.json").open("r", encoding="utf-8") as f:
        for line in f:
            text = line.strip()
            if not text:
                continue
            data = json.loads(text)
            if str(data.get("id", "")) not in demos_id:
                continue
            user_request = _load_demo_field(data, "user_request", "instruction")
            task_steps = _load_demo_field(data, "task_steps", "tool_steps")
            task_nodes = _load_demo_field(data, "task_nodes", "tool_nodes")
            if dependency_type == "temporal":
                task_links = _load_demo_field(data, "task_links", "tool_links")
                result = {
                    "task_steps": task_steps,
                    "task_nodes": task_nodes,
                    "task_links": task_links,
                }
            else:
                result = {
                    "task_steps": task_steps,
                    "task_nodes": task_nodes,
                }
            demos.append({"user_request": user_request, "result": result})
    return demos


def _build_taskbench_prompt_for_debug(
    tool_list: List[Dict[str, Any]],
    demos: List[Dict[str, Any]],
    dependency_type: str,
    user_request: str,
    strategy_hint: Optional[str] = None,
) -> str:
    tool_string = "# TASK LIST #:\n"
    for tool in tool_list:
        tool_string += json.dumps(tool) + "\n"

    if dependency_type == "resource":
        prompt = """\n# GOAL #: Based on the above tools, I want you generate task steps and task nodes to solve the # USER REQUEST #. The format must in a strict JSON format, like: {"task_steps": [ step description of one or more steps ], "task_nodes": [{"task": "tool name must be from # TOOL LIST #", "arguments": [ a concise list of arguments for the tool. Either original text, or user-mentioned filename, or tag '<node-j>' (start from 0) to refer to the output of the j-th node. ]}]} """
        prompt += """\n\n# REQUIREMENTS #: \n1. the generated task steps and task nodes can resolve the given user request # USER REQUEST # perfectly. Task name must be selected from # TASK LIST #; \n2. the task steps should strictly aligned with the task nodes, and the number of task steps should be same with the task nodes; \n3. the dependencies among task steps should align with the argument dependencies of the task nodes; \n4. the tool arguments should be align with the input-type field of # TASK LIST #;"""
    else:
        prompt = """\n# GOAL #:\nBased on the above tools, I want you generate task steps and task nodes to solve the # USER REQUEST #. The format must in a strict JSON format, like: {"task_steps": [ "concrete steps, format as Step x: Call xxx tool with xxx: 'xxx' and xxx: 'xxx'" ], "task_nodes": [{"task": "task name must be from # TASK LIST #", "arguments": [ {"name": "parameter name", "value": "parameter value, either user-specified text or the specific name of the tool whose result is required by this node"} ]}], "task_links": [{"source": "task name i", "target": "task name j"}]}"""
        prompt += """\n\n# REQUIREMENTS #: \n1. the generated task steps and task nodes can resolve the given user request # USER REQUEST # perfectly. Task name must be selected from # TASK LIST #; \n2. the task steps should strictly aligned with the task nodes, and the number of task steps should be same with the task nodes; \n3. The task links (task_links) should reflect the temporal dependencies among task nodes, i.e. the order in which the APIs are invoked;"""

    if demos:
        prompt += "\n"
        for demo in demos:
            prompt += (
                f"""\n# EXAMPLE #:\n# USER REQUEST #: {demo["user_request"]}\n"""
                f"""# RESULT #: {json.dumps(demo["result"])}"""
            )

    if strategy_hint:
        prompt += f"""\n\n# PLANNING STRATEGY #:\n{strategy_hint.strip()}"""

    prompt += """\n\n# USER REQUEST #: {{user_request}}\nnow please generate your result in a strict JSON format:\n# RESULT #:"""
    return tool_string + prompt.replace("{{user_request}}", user_request)


def _prompt_debug_strategy_hints(candidate_prompt_mode: str) -> List[Tuple[str, str]]:
    mode = str(candidate_prompt_mode or "legacy").strip().lower()
    if mode in {"orthogonal_v3", "orthogonal_v4"}:
        return [
            (
                "minimal",
                "\n".join(
                    [
                        "Prefer the shortest executable workflow.",
                        "Do not add tools that are not strictly required by the user request.",
                        "Collapse optional intermediate processing steps whenever correctness is preserved.",
                        "Avoid bridge tools, enhancement tools, and redundant transformations.",
                        "However, do not remove retrieval, download, save, export, or other steps that are necessary to deliver the requested artifact.",
                    ]
                ),
            ),
            (
                "action_coverage",
                "\n".join(
                    [
                        "Identify every explicit executable action mentioned in the user request before planning.",
                        "Ensure every executable action is represented by at least one workflow node.",
                        "Do not skip retrieval, extraction, download, save, export, transformation, generation, modification, composition, or conversion operations when they are explicitly requested.",
                        "Prefer complete executable coverage over workflow brevity.",
                        "Do not output the action checklist; output only the workflow JSON.",
                    ]
                ),
            ),
            (
                "typed_dependency",
                "\n".join(
                    [
                        "Focus on executable artifact flow.",
                        "For each downstream tool, connect it to the upstream node whose output artifact is directly consumed.",
                        "Ensure artifact-type continuity between connected nodes.",
                        "The output-type of the producer should be compatible with the input-type of the consumer.",
                        "Avoid introducing dependencies that require unsupported modality transitions.",
                        "Preserve intermediate artifacts whenever they are required by downstream processing.",
                    ]
                ),
            ),
            (
                "parallel_dag",
                "\n".join(
                    [
                        "Preserve independent branches when multiple downstream operations consume the same upstream artifact.",
                        "If two or more nodes can consume the same intermediate result, connect them directly to that result.",
                        "Do not convert parallel branches into a linear chain merely to shorten the workflow.",
                        "Prefer executable DAG structures over sequentialized approximations.",
                    ]
                ),
            ),
            (
                "materialization" if mode == "orthogonal_v3" else "branch_preserving",
                "\n".join(
                    [
                        "Preserve explicit artifact delivery steps.",
                        "If the user requests:",
                        "- a filename",
                        "- a downloadable result",
                        "- a saved output",
                        "- an exported artifact",
                        "- a specific output format",
                        "include the workflow step that produces or delivers that artifact.",
                        "Do not stop at an intermediate semantic result when the request requires a final deliverable artifact.",
                        "Prefer workflows that generate a usable output artifact rather than only an intermediate processing result.",
                    ] if mode == "orthogonal_v3" else [
                        "Identify shared intermediate artifacts before planning.",
                        "If multiple requested outputs depend on the same artifact, create independent downstream branches from that artifact.",
                        "Do not serialize independent operations unless one explicitly consumes the other's output.",
                        "Preserve producer-consumer relationships even when a linear chain appears simpler.",
                        "Prefer DAG structures that maintain correct branching behavior.",
                    ]
                ),
            ),
        ]
    if mode == "orthogonal":
        return [
            (
                "minimal",
                "\n".join(
                    [
                        "Prefer the shortest valid workflow.",
                        "Do not add any tool unless it is explicitly required.",
                        "If one tool can satisfy the request, use one tool.",
                        "Avoid bridge tools and optional enhancement steps.",
                    ]
                ),
            ),
            (
                "action_coverage",
                "\n".join(
                    [
                        "First identify every explicit action in the user request.",
                        "Ensure each explicit action is covered by at least one tool.",
                        "Do not skip actions such as search, summarize, transcribe, denoise, combine, generate, or convert.",
                        "Do not output the action checklist; output only the workflow JSON.",
                    ]
                ),
            ),
            (
                "dependency_first",
                "\n".join(
                    [
                        "Focus on correct dataflow dependencies.",
                        "For each downstream tool, choose the upstream node whose output is directly consumed.",
                        "Do not connect a tool to an earlier node only because the modality matches.",
                        "Prefer semantic dataflow continuity over superficial schema compatibility.",
                    ]
                ),
            ),
            (
                "parameter_copy",
                "\n".join(
                    [
                        "Prioritize exact parameter grounding.",
                        "Copy all user-provided filenames, topics, phrases, styles, speeds, genders, and effect names exactly.",
                        "Do not paraphrase parameter values.",
                        "Use literal user values unless the argument must be an upstream <node-i> output.",
                    ]
                ),
            ),
            (
                "parallel_dag",
                "\n".join(
                    [
                        "Preserve independent branches when the user asks for multiple outputs or parallel post-processing.",
                        "If two downstream tools consume the same upstream artifact, connect both to that artifact.",
                        "Do not force independent branches into a linear chain.",
                    ]
                ),
            ),
        ]
    if mode == "orthogonal_v2":
        return [
            (
                "fewest_tools",
                "\n".join(
                    [
                        "Use the fewest tools possible while still satisfying the explicit request.",
                        "Collapse optional intermediate steps unless they are required for correctness.",
                        "Prefer a shorter workflow over a more descriptive workflow when both are valid.",
                    ]
                ),
            ),
            (
                "fewest_transformations",
                "\n".join(
                    [
                        "Minimize the number of transformations applied to the artifact.",
                        "Avoid adding rewrites, cleanup, or conversion hops unless the user explicitly requested them.",
                        "Prefer a direct producer-to-consumer path over multi-hop reformulation.",
                    ]
                ),
            ),
            (
                "strict_explicit_action_coverage",
                "\n".join(
                    [
                        "Enumerate every explicit user-requested action internally before planning.",
                        "Ensure each explicit action is covered by at least one tool.",
                        "Do not skip search, summarize, transcribe, denoise, combine, generate, or convert when explicitly requested.",
                    ]
                ),
            ),
        ]
    return [
        ("minimal", "Prefer the minimal valid workflow with the fewest steps."),
        ("explicit", "Prefer explicit intermediate transformations and validation-friendly dependencies."),
        ("parallel", "Prefer structurally distinct workflows with independent parallel branches when valid."),
    ]


def _format_prompt_for_debug(prompt: str, preview_chars: int) -> str:
    if preview_chars <= 0 or len(prompt) <= preview_chars:
        return prompt
    return prompt[:preview_chars] + f"\n...[truncated, total_chars={len(prompt)}]"


def _print_taskbench_prompt_debug(
    requests: List[Dict[str, Any]],
    tool_list: List[Dict[str, Any]],
    demos: List[Dict[str, Any]],
    dependency_type: str,
    args: argparse.Namespace,
) -> None:
    try:
        strategy_count = max(0, int(getattr(args, "print_prompt_strategy_count", 1) or 0))
    except (TypeError, ValueError):
        strategy_count = 1
    try:
        preview_chars = max(0, int(getattr(args, "prompt_preview_chars", 0) or 0))
    except (TypeError, ValueError):
        preview_chars = 0

    print("[INFO] prompt_debug=True")
    print(f"[INFO] prompt_debug_cases={len(requests)}")
    print(f"[INFO] prompt_debug_strategy_count={strategy_count}")
    for index, item in enumerate(requests, start=1):
        sid = str(item.get("id", ""))
        user_request = str(item.get("user_request", "") or item.get("instruction", ""))
        if not user_request:
            print(f"[WARN] prompt_debug skip invalid sample: id={sid}")
            continue

        candidate_prompt_mode = str(getattr(args, "candidate_prompt_mode", "legacy") or "legacy").strip().lower()
        if candidate_prompt_mode not in {"orthogonal_v3", "orthogonal_v4"}:
            original_prompt = _build_taskbench_prompt_for_debug(
                tool_list=tool_list,
                demos=demos,
                dependency_type=dependency_type,
                user_request=user_request,
            )
            print("=" * 80)
            print(f"[PROMPT_DEBUG] case={index} id={sid} variant=inference_original_or_base_original")
            print(f"[PROMPT_DEBUG] chars={len(original_prompt)}")
            print(_format_prompt_for_debug(original_prompt, preview_chars))

        if str(getattr(args, "planning_mode", "single")) != "multi" or strategy_count <= 0:
            continue
        hints = _prompt_debug_strategy_hints(candidate_prompt_mode)
        for strategy_name, strategy_hint in hints[:strategy_count]:
            strategy_prompt = _build_taskbench_prompt_for_debug(
                tool_list=tool_list,
                demos=demos,
                dependency_type=dependency_type,
                user_request=user_request,
                strategy_hint=strategy_hint,
            )
            print("=" * 80)
            print(f"[PROMPT_DEBUG] case={index} id={sid} variant=base_multi_{strategy_name}")
            print(f"[PROMPT_DEBUG] chars={len(strategy_prompt)}")
            print(_format_prompt_for_debug(strategy_prompt, preview_chars))


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


class _NullPredictionOutput:
    def __enter__(self) -> "_NullPredictionOutput":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False

    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        return None


def _build_case_candidate_dump_record(sid: str, candidate_dump: Any) -> Optional[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if isinstance(candidate_dump, dict):
        rows = [candidate_dump]
    elif isinstance(candidate_dump, list):
        rows = [row for row in candidate_dump if isinstance(row, dict)]
    if not rows:
        return None

    user_request = ""
    for row in rows:
        raw_user_request = row.get("user_request")
        if isinstance(raw_user_request, str) and raw_user_request.strip():
            user_request = raw_user_request.strip()
            break

    return {
        "case_id": sid,
        "user_request": user_request,
        "candidate_count": len(rows),
        "candidates": rows,
    }


def _summarize_orthogonal_v4_candidate_pool(output_path: Path) -> Dict[str, Any]:
    if not output_path.exists():
        return {
            "total_cases": 0,
            "single_cases": 0,
            "chain_cases": 0,
            "dag_cases": 0,
            "skipped_single_cases": 0,
            "generated_candidates": 0,
            "generated_candidates_by_family": {},
            "parse_success_rate_by_family": {},
            "average_candidates_per_case": 0.0,
            "low_confidence_workflow_type_count": 0,
        }

    records = _load_json_or_jsonl_rows(output_path)
    workflow_counts = Counter(str(row.get("workflow_type", "")) for row in records)
    generated_by_family: Counter[str] = Counter()
    family_totals: Counter[str] = Counter()
    family_success: Counter[str] = Counter()
    candidate_counts: List[int] = []
    generated_candidates = 0

    for row in records:
        candidates = row.get("candidates", [])
        if not isinstance(candidates, list):
            candidates = []
        candidate_counts.append(len(candidates))
        for candidate in candidates:
            if not isinstance(candidate, dict) or candidate.get("source") != "generated":
                continue
            family = str(candidate.get("family") or "").strip()
            generated_candidates += 1
            generated_by_family[family] += 1
            family_totals[family] += 1
            if bool(candidate.get("parse_success", False)):
                family_success[family] += 1

    parse_success_rate_by_family = {
        family: (family_success[family] / total if total else 0.0)
        for family, total in sorted(family_totals.items())
    }
    return {
        "total_cases": len(records),
        "single_cases": workflow_counts.get("single", 0),
        "chain_cases": workflow_counts.get("chain", 0),
        "dag_cases": workflow_counts.get("dag", 0),
        "skipped_single_cases": workflow_counts.get("single", 0),
        "generated_candidates": generated_candidates,
        "generated_candidates_by_family": dict(sorted(generated_by_family.items())),
        "parse_success_rate_by_family": parse_success_rate_by_family,
        "average_candidates_per_case": (sum(candidate_counts) / len(candidate_counts) if candidate_counts else 0.0),
        "low_confidence_workflow_type_count": sum(
            1 for row in records if row.get("workflow_type_confidence") == "low"
        ),
    }


def _load_tool_map_override(path: Optional[Path]) -> Dict[str, str]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("tool_map_override must be a JSON object: {\"skill_name\": \"task_name\"}")
    return {str(k): str(v) for k, v in payload.items()}


def _load_gold_rows(data_dir: Path) -> Dict[str, Dict[str, Any]]:
    gold_path = data_dir / "data.json"
    rows: Dict[str, Dict[str, Any]] = {}
    with gold_path.open("r", encoding="utf-8") as f:
        for line in f:
            text = line.strip()
            if not text:
                continue
            payload = json.loads(text)
            sid = str(payload.get("id", "")).strip()
            if sid:
                rows[sid] = payload
    return rows


def _stringify_json_field(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


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


def _parse_raw_response_task_steps(raw_response: Any) -> Optional[List[Any]]:
    if not isinstance(raw_response, str):
        return None
    text = raw_response.strip()
    if not text:
        return None

    fence_match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None

    if not isinstance(payload, dict):
        return None
    task_steps = payload.get("task_steps")
    return task_steps if isinstance(task_steps, list) else None


def _multiset_f1(pred_items: List[str], gold_items: List[str]) -> Tuple[float, float, float]:
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


def _normalize_case_graph_for_eval(record: Dict[str, Any]) -> Dict[str, Any]:
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
        step_ref_base="one",
    )
    flattened_arguments: List[str] = []
    for idx, tokens in enumerate(normalized_arguments):
        node_name = node_names[idx] if idx < len(node_names) else ""
        for token in tokens:
            flattened_arguments.append(f"{node_name}|{token}")
    link_pairs = sorted((str(link.get("source", "")), str(link.get("target", ""))) for link in normalized_links)
    return {
        "node_names": node_names,
        "link_pairs": link_pairs,
        "link_pair_set": set(link_pairs),
        "flattened_arguments": sorted(flattened_arguments),
        "has_edges": bool(link_pairs),
    }


def _evaluate_candidate_result_for_dump(
    candidate_result: Dict[str, Any],
    gold_result: Dict[str, Any],
) -> Dict[str, Any]:
    pred_graph = _normalize_case_graph_for_eval(candidate_result)
    gold_graph = _normalize_case_graph_for_eval(gold_result)

    _, _, node_f1 = _multiset_f1(pred_graph["node_names"], gold_graph["node_names"])
    _, _, arg_value_f1 = _multiset_f1(pred_graph["flattened_arguments"], gold_graph["flattened_arguments"])
    if gold_graph["has_edges"]:
        _, _, edge_f1 = _multiset_f1(list(pred_graph["link_pair_set"]), list(gold_graph["link_pair_set"]))
    else:
        edge_f1 = None

    exact_match = (
        pred_graph["node_names"] == gold_graph["node_names"]
        and pred_graph["link_pairs"] == gold_graph["link_pairs"]
        and pred_graph["flattened_arguments"] == gold_graph["flattened_arguments"]
    )
    edge_component = 1.0 if edge_f1 is None else edge_f1
    quality_score = (
        4.0 * float(exact_match)
        + 2.0 * node_f1
        + 2.0 * edge_component
        + 1.0 * arg_value_f1
    ) / 9.0
    return {
        "exact_match": exact_match,
        "node_f1": node_f1,
        "edge_f1": edge_f1,
        "arg_value_f1": arg_value_f1,
        "quality_score": quality_score,
    }


def _candidate_signature_from_result(taskbench_result: Dict[str, Any]) -> str:
    task_nodes = taskbench_result.get("task_nodes", [])
    if not isinstance(task_nodes, list):
        task_nodes = []
    task_links = taskbench_result.get("task_links", [])
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


def _candidate_structure_signature_from_result(taskbench_result: Dict[str, Any]) -> str:
    task_nodes = taskbench_result.get("task_nodes", [])
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
            text = str(arg).strip()
            match = re.fullmatch(r"<node-(\d+)>", text)
            if match:
                upstream_inputs[f"arg{arg_idx}"] = int(match.group(1))
        normalized_nodes.append(
            {
                "task": node.get("task"),
                "upstream_inputs": upstream_inputs,
            }
        )
    return json.dumps(normalized_nodes, ensure_ascii=False, sort_keys=True)


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


def _build_action_dag_record(
    sid: str,
    instruction: str,
    raw_result: Dict[str, Any],
) -> Dict[str, Any]:
    def _safe_int(value: Any, default: int = 10**9) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    action_dag_json = raw_result.get("action_dag_json")
    atomic_actions: List[Any] = []
    dependencies: List[Any] = []
    materializations: List[Any] = []
    if isinstance(action_dag_json, dict):
        if isinstance(action_dag_json.get("atomic_actions"), list):
            atomic_actions = action_dag_json["atomic_actions"]
        if isinstance(action_dag_json.get("dependencies"), list):
            dependencies = action_dag_json["dependencies"]
        if isinstance(action_dag_json.get("materializations"), list):
            materializations = action_dag_json["materializations"]
    atomic_actions = sorted(
        atomic_actions,
        key=lambda item: _safe_int(item.get("id")) if isinstance(item, dict) else 10**9,
    )
    dependencies = sorted(
        dependencies,
        key=lambda item: (
            _safe_int(item.get("source")) if isinstance(item, dict) else 10**9,
            _safe_int(item.get("target")) if isinstance(item, dict) else 10**9,
        ),
    )
    materializations = sorted(
        materializations,
        key=lambda item: (
            _safe_int(item.get("producer")) if isinstance(item, dict) else 10**9,
            str(item.get("artifact", "")) if isinstance(item, dict) else "",
        ),
    )
    action_labels = [
        str(item.get("action", "")).strip()
        for item in atomic_actions
        if isinstance(item, dict) and str(item.get("action", "")).strip()
    ]
    materialized_artifacts = [
        str(item.get("artifact", "")).strip()
        for item in materializations
        if isinstance(item, dict) and str(item.get("artifact", "")).strip()
    ]
    stable_action_dag = {
        "atomic_actions": atomic_actions,
        "dependencies": dependencies,
        "materializations": materializations,
    }
    return {
        "id": sid,
        "case_id": sid,
        "instruction": instruction,
        "user_requirement": instruction,
        "planning_mode": "action_dag",
        "n_tools": 0,
        "parse_success": bool(raw_result.get("parse_success", False)),
        "parse_error": str(raw_result.get("parse_error", "") or ""),
        "atomic_action_count": len(atomic_actions),
        "dependency_count": len(dependencies),
        "materialization_count": len(materializations),
        "action_labels": action_labels,
        "materialized_artifacts": materialized_artifacts,
        "atomic_actions": atomic_actions,
        "dependencies": dependencies,
        "materializations": materializations,
        "action_dag_json": stable_action_dag,
        "result": stable_action_dag,
    }


def _build_candidate_dump_record(
    sid: str,
    instruction: str,
    taskbench_result: Dict[str, Any],
    raw_result: Dict[str, Any],
    gold_row: Optional[Dict[str, Any]],
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
        offline_metrics = (
            _evaluate_candidate_result_for_dump(candidate_result, gold_row)
            if isinstance(gold_row, dict)
            else {
                "exact_match": None,
                "node_f1": None,
                "edge_f1": None,
                "arg_value_f1": None,
                "quality_score": None,
            }
        )
        candidate_row = {
            "id": item.get("id"),
            "candidate_id": item.get("id"),
            "generation_index": item.get("generation_index"),
            "strategy_name": item.get("strategy_name"),
            "family_name": item.get("family_name", item.get("strategy_name")),
            "variant_name": item.get("variant_name", item.get("strategy_name")),
            "strategy_hint": item.get("strategy_hint"),
            "sampling_temperature": item.get("sampling_temperature"),
            "score": item.get("score"),
            "score_details": item.get("score_details"),
            "validation_status": item.get("validation_status"),
            "selection_meta": item.get("selection_meta"),
            "verification_meta": item.get("verification_meta"),
            "dependency_check": item.get("dependency_check"),
            "dependency_check_result": item.get("dependency_check_result", item.get("dependency_check")),
            "repair_meta": item.get("repair_meta"),
            "edge_grounding_meta": item.get("edge_grounding_meta"),
            "workflow_signature": item.get("workflow_signature")
            or item.get("signature")
            or _candidate_signature_from_result(candidate_result),
            "structure_signature": item.get("structure_signature")
            or _candidate_structure_signature_from_result(candidate_result),
            "signature": item.get("workflow_signature")
            or item.get("signature")
            or _candidate_signature_from_result(candidate_result),
            "workflow": workflow,
            "result": candidate_result,
            "exact_match": offline_metrics.get("exact_match"),
            "node_f1": offline_metrics.get("node_f1"),
            "edge_f1": offline_metrics.get("edge_f1"),
            "arg_value_f1": offline_metrics.get("arg_value_f1"),
            "quality_score": offline_metrics.get("quality_score"),
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


async def _run_prediction_case(
    agent: PipelineOrchestratorAgent,
    item: Dict[str, Any],
    args: argparse.Namespace,
    *,
    tool_names: List[str],
    tool_map_override: Dict[str, str],
    dependency_type: str,
    save_candidate_pool: bool,
    gold_rows: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    sid = str(item.get("id", ""))
    user_request = str(item.get("user_request", "")).strip()
    if not sid or not user_request:
        return {
            "status": "invalid",
            "id": sid,
            "message": f"skip invalid sample: id={sid}",
        }

    raw_result = await _run_one(
        agent=agent,
        user_request=user_request,
        planning_mode=args.planning_mode,
        execution_mode=args.execution_mode,
        candidate_count=args.candidate_count,
        include_summary=bool(getattr(args, "include_summary", False)),
    )
    if str(args.planning_mode).strip() == "action_dag":
        return {
            "status": "success",
            "id": sid,
            "prediction": _build_action_dag_record(
                sid=sid,
                instruction=user_request,
                raw_result=raw_result,
            ),
            "candidate_dump": None,
        }

    plan = _extract_selected_plan(raw_result)
    taskbench_result = _convert_plan_to_taskbench_result(
        plan=plan,
        tool_names=tool_names,
        dependency_type=dependency_type,
        tool_map_override=tool_map_override,
        link_mode=args.link_mode,
    )
    prediction = _build_prediction_record(
        sid=sid,
        instruction=user_request,
        taskbench_result=taskbench_result,
    )
    candidate_dump = None
    if save_candidate_pool:
        candidate_dump = _build_candidate_dump_record(
            sid=sid,
            instruction=user_request,
            taskbench_result=taskbench_result,
            raw_result=raw_result,
            gold_row=gold_rows.get(sid),
            tool_names=tool_names,
            dependency_type=dependency_type,
            tool_map_override=tool_map_override,
            link_mode=args.link_mode,
        )
    return {
        "status": "success",
        "id": sid,
        "prediction": prediction,
        "candidate_dump": candidate_dump,
    }


async def _run_orthogonal_v3_candidate_pool_case(
    agent: PipelineOrchestratorAgent,
    item: Dict[str, Any],
    args: argparse.Namespace,
    *,
    tool_names: List[str],
    tool_map_override: Dict[str, str],
    dependency_type: str,
) -> Dict[str, Any]:
    sid = str(item.get("id", ""))
    user_request = str(item.get("user_request", "")).strip()
    if not sid or not user_request:
        return {
            "status": "invalid",
            "id": sid,
            "message": f"skip invalid sample: id={sid}",
        }

    raw_records = await agent.generate_orthogonal_v3_candidate_records(user_request)
    candidate_rows: List[Dict[str, Any]] = []
    for raw_record in raw_records:
        if not isinstance(raw_record, dict):
            continue
        workflow = raw_record.get("result")
        parse_success = bool(raw_record.get("parse_success", False))
        parse_error = str(raw_record.get("parse_error", "") or "")
        raw_response = str(raw_record.get("raw_response", "") or "")
        taskbench_result: Any = {}
        if parse_success and isinstance(workflow, dict):
            try:
                taskbench_result = _convert_plan_to_taskbench_result(
                    plan=workflow,
                    tool_names=tool_names,
                    dependency_type=dependency_type,
                    tool_map_override=tool_map_override,
                    link_mode=args.link_mode,
                )
            except Exception as exc:
                parse_success = False
                suffix = f"{type(exc).__name__}: {exc}"
                parse_error = f"{parse_error} | {suffix}" if parse_error else suffix
                taskbench_result = {}

        raw_task_steps = _parse_raw_response_task_steps(raw_response)
        if isinstance(taskbench_result, dict) and raw_task_steps is not None:
            taskbench_result = dict(taskbench_result)
            taskbench_result["task_steps"] = raw_task_steps

        if isinstance(taskbench_result, dict) and "task_links" in taskbench_result:
            taskbench_result = {
                key: value for key, value in taskbench_result.items() if key != "task_links"
            }

        candidate_rows.append(
            {
                "case_id": sid,
                "user_request": user_request,
                "family": str(raw_record.get("family", "")),
                "variant": str(raw_record.get("variant", "")),
                "temperature": raw_record.get("temperature"),
                "prompt_strategy": str(raw_record.get("prompt_strategy", "") or ""),
                "raw_response": raw_response,
                "result": taskbench_result,
                "parse_success": parse_success,
                "parse_error": parse_error,
                "candidate_id": raw_record.get("candidate_id"),
            }
        )

    return {
        "status": "success",
        "id": sid,
        "prediction": None,
        "candidate_dump": candidate_rows,
    }


async def _run_orthogonal_v4_candidate_pool_case(
    agent: PipelineOrchestratorAgent,
    item: Dict[str, Any],
    args: argparse.Namespace,
    *,
    cached_original_by_id: Dict[str, Dict[str, Any]],
    tool_names: List[str],
    tool_map_override: Dict[str, str],
    dependency_type: str,
) -> Dict[str, Any]:
    sid = str(item.get("id", "")).strip()
    cached_original = cached_original_by_id.get(sid)
    if not sid or cached_original is None:
        return {
            "status": "invalid",
            "id": sid,
            "message": f"skip sample without cached original baseline: id={sid}",
        }

    original_result = _canonical_cached_original_result(cached_original)
    user_request = str(
        item.get("user_request")
        or cached_original.get("user_request")
        or cached_original.get("instruction")
        or ""
    ).strip()
    if not user_request:
        return {
            "status": "invalid",
            "id": sid,
            "message": f"skip sample without user_request: id={sid}",
        }

    workflow_type, workflow_type_confidence = _infer_workflow_type_from_result(original_result)
    original_candidate = _build_original_baseline_candidate(original_result)
    candidates: List[Dict[str, Any]] = [original_candidate]

    families = _orthogonal_v4_families_for_workflow_type(workflow_type)
    if families:
        raw_records = await agent.generate_orthogonal_v4_candidate_records(user_request, families)
        for raw_record in raw_records:
            if not isinstance(raw_record, dict):
                continue
            workflow = raw_record.get("result")
            parse_success = bool(raw_record.get("parse_success", False))
            parse_error = str(raw_record.get("parse_error", "") or "")
            raw_response = str(raw_record.get("raw_response", "") or "")
            taskbench_result: Any = {}
            if parse_success and isinstance(workflow, dict):
                try:
                    taskbench_result = _convert_plan_to_taskbench_result(
                        plan=workflow,
                        tool_names=tool_names,
                        dependency_type=dependency_type,
                        tool_map_override=tool_map_override,
                        link_mode=args.link_mode,
                    )
                except Exception as exc:
                    parse_success = False
                    suffix = f"{type(exc).__name__}: {exc}"
                    parse_error = f"{parse_error} | {suffix}" if parse_error else suffix
                    taskbench_result = {}

            raw_task_steps = _parse_raw_response_task_steps(raw_response)
            if isinstance(taskbench_result, dict) and raw_task_steps is not None:
                taskbench_result = dict(taskbench_result)
                taskbench_result["task_steps"] = raw_task_steps

            if isinstance(taskbench_result, dict) and "task_links" in taskbench_result:
                taskbench_result = {
                    key: value for key, value in taskbench_result.items() if key != "task_links"
                }

            candidates.append(
                {
                    "candidate_id": str(raw_record.get("candidate_id") or raw_record.get("family") or ""),
                    "family": str(raw_record.get("family", "")),
                    "source": "generated",
                    "temperature": raw_record.get("temperature"),
                    "prompt_strategy": str(raw_record.get("prompt_strategy", "") or ""),
                    "raw_response": raw_response,
                    "result": taskbench_result,
                    "parse_success": parse_success,
                    "parse_error": parse_error,
                }
            )

    candidate_dump = {
        "case_id": sid,
        "user_request": user_request,
        "workflow_type": workflow_type,
        "workflow_type_source": "original_baseline",
        "workflow_type_confidence": workflow_type_confidence,
        "original_baseline": original_candidate,
        "candidates": candidates,
    }
    return {
        "status": "success",
        "id": sid,
        "prediction": None,
        "candidate_dump": candidate_dump,
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
    planning_prompt_mode = str(getattr(args, "planning_prompt_mode", "agent") or "agent").strip().lower()
    if planning_prompt_mode not in {"agent", "taskbench"}:
        raise ValueError("planning_prompt_mode must be 'agent' or 'taskbench'")
    candidate_prompt_mode = str(getattr(args, "candidate_prompt_mode", "legacy") or "legacy").strip().lower()
    orthogonal_v3_candidate_pool = candidate_prompt_mode == "orthogonal_v3"
    orthogonal_v4_candidate_pool = candidate_prompt_mode == "orthogonal_v4"
    if orthogonal_v3_candidate_pool:
        args.candidate_count = 5
        args.candidate_selection_mode = "none"
        args.include_original_candidate = False
        args.force_generate_all_candidate_families = True
        args.save_candidate_pool = True
    if orthogonal_v4_candidate_pool:
        args.candidate_selection_mode = "none"
        args.include_original_candidate = False
        args.force_generate_all_candidate_families = True
        args.save_candidate_pool = True
    try:
        use_demos = max(0, int(getattr(args, "use_demos", 0) or 0))
    except (TypeError, ValueError):
        use_demos = 0
    taskbench_tool_list: Optional[List[Dict[str, Any]]] = None
    taskbench_demos: List[Dict[str, Any]] = []
    if planning_prompt_mode == "taskbench":
        taskbench_tool_list = _load_taskbench_prompt_tool_list(data_dir, dependency_type)
        taskbench_demos = _load_taskbench_prompt_demos(data_dir, dependency_type, use_demos)

    prediction_dir_name = args.prediction_dir
    prediction_dir = data_dir / prediction_dir_name
    prediction_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d")
    output_path = prediction_dir / f"{args.llm}_{args.model_name}_{timestamp}.json"
    save_candidate_pool = bool(getattr(args, "save_candidate_pool", False)) or orthogonal_v3_candidate_pool or orthogonal_v4_candidate_pool
    candidate_dump_dir = prediction_dir.parent / "candidate_dumps"
    if orthogonal_v4_candidate_pool:
        candidate_dump_path = _resolve_orthogonal_v4_output(str(getattr(args, "output", "orthogonal_v4_candidate_pool.jsonl")), data_dir=data_dir)
    else:
        candidate_dump_path = candidate_dump_dir / f"{args.llm}_{args.model_name}_{timestamp}.jsonl"
    dry_run_prompts = bool(getattr(args, "dry_run_prompts", False))
    print_prompts = bool(getattr(args, "print_prompts", False)) or dry_run_prompts

    resume_path = candidate_dump_path if orthogonal_v3_candidate_pool else output_path
    if orthogonal_v4_candidate_pool:
        resume_path = candidate_dump_path
    overwrite = bool(getattr(args, "overwrite", False))
    resume_enabled = bool(args.resume) and not overwrite
    done_ids = _load_existing_ids(resume_path) if resume_enabled and not dry_run_prompts else set()
    cached_original_path: Optional[Path] = None
    cached_original_by_id: Dict[str, Dict[str, Any]] = {}
    if orthogonal_v4_candidate_pool:
        cached_original_path = _resolve_cached_original_file(str(getattr(args, "cached_original", "")), data_dir=data_dir)
        cached_original_by_id = _load_cached_original_predictions(cached_original_path)
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
    gold_rows = _load_gold_rows(data_dir) if save_candidate_pool and not orthogonal_v4_candidate_pool else {}
    try:
        multiworker = max(1, int(getattr(args, "multiworker", 1) or 1))
    except (TypeError, ValueError):
        multiworker = 1
    try:
        log_every = max(1, int(getattr(args, "log_every", 10) or 10))
    except (TypeError, ValueError):
        log_every = 10

    print(f"[INFO] data_dir={data_dir}")
    print(f"[INFO] dependency_type={dependency_type}")
    print(f"[INFO] output={output_path}")
    if bool(getattr(args, "save_action_dag", False)):
        print(f"[INFO] save_action_dag=True")
    if save_candidate_pool:
        print(f"[INFO] candidate_dump={candidate_dump_path}")
    print(f"[INFO] total_to_run={len(requests)} (resume={resume_enabled}, skipped={len(done_ids)})")
    print(f"[INFO] multiworker={multiworker}")
    print(f"[INFO] planning_prompt_mode={planning_prompt_mode}")
    print(f"[INFO] candidate_prompt_mode={candidate_prompt_mode}")
    if orthogonal_v3_candidate_pool:
        print("[INFO] candidate_pool_only=True")
        print("[INFO] orthogonal_v3 families=minimal,action_coverage,typed_dependency,parallel_dag,materialization")
    if orthogonal_v4_candidate_pool:
        print("[INFO] candidate_pool_only=True")
        print(f"[INFO] cached_original={cached_original_path}")
        print("[INFO] orthogonal_v4 policy=single:original | chain:original+minimal,action_coverage,typed_dependency | dag:original+action_coverage,typed_dependency,parallel_dag,branch_preserving")
    if planning_prompt_mode == "taskbench":
        print(f"[INFO] taskbench_prompt_demos={len(taskbench_demos)}")
    if print_prompts:
        print(f"[INFO] print_prompts=True")
    if dry_run_prompts:
        print(f"[INFO] dry_run_prompts=True")
    if args.llm_profile:
        print(f"[INFO] llm_profile={args.llm_profile}")
    if llm_config_path:
        print(f"[INFO] llm_config_path={llm_config_path}")
    print(f"[INFO] enable_workflow_memory={enable_workflow_memory}")
    if workflow_memory_path:
        print(f"[INFO] workflow_memory_path={workflow_memory_path}")

    if print_prompts:
        if planning_prompt_mode != "taskbench":
            print("[WARN] prompt debug is only implemented for planning_prompt_mode=taskbench")
        else:
            _print_taskbench_prompt_debug(
                requests=requests,
                tool_list=taskbench_tool_list or [],
                demos=taskbench_demos,
                dependency_type=dependency_type,
                args=args,
            )
        if dry_run_prompts:
            print("[INFO] dry_run_prompts=True; skip PipelineOrchestratorAgent execution.")
            return {
                "output_path": str(output_path),
                "prediction_dir": str(prediction_dir),
                "candidate_dump_path": str(candidate_dump_path) if save_candidate_pool else None,
                "success": 0,
                "failed": 0,
                "validation_failed": 0,
                "failure_counts": {},
                "failure_details": [],
                "total": len(requests),
                "dry_run_prompts": True,
            }

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
        candidate_prompt_mode=str(getattr(args, "candidate_prompt_mode", "legacy")),
        force_generate_all_candidate_families=bool(
            getattr(args, "force_generate_all_candidate_families", False)
        ),
        disable_early_stop=bool(getattr(args, "disable_early_stop", False)),
        enable_strict_planning_prompt=bool(getattr(args, "enable_strict_planning_prompt", False)),
        enable_action_checklist=bool(getattr(args, "enable_action_checklist", False)),
        enable_parameter_normalization=bool(getattr(args, "enable_parameter_normalization", False)),
        planning_prompt_mode=planning_prompt_mode,
        taskbench_tool_list=taskbench_tool_list,
        taskbench_demos=taskbench_demos,
        taskbench_dependency_type=dependency_type,
    )
    success = 0
    failed = 0
    validation_failed = 0
    failure_counts: Dict[str, int] = {}
    failure_details: List[Dict[str, str]] = []
    candidate_wf = None
    if save_candidate_pool:
        candidate_dump_dir.mkdir(parents=True, exist_ok=True)
        candidate_dump_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_wf = _open_prediction_output(candidate_dump_path, resume=resume_enabled)

    def _record_case_result(result: Dict[str, Any], wf) -> None:
        nonlocal success, failed, validation_failed

        status = str(result.get("status", "error"))
        sid = str(result.get("id", ""))
        if status == "success":
            prediction = result.get("prediction")
            if isinstance(prediction, dict):
                wf.write(json.dumps(prediction, ensure_ascii=False) + "\n")
                wf.flush()
            candidate_dump = result.get("candidate_dump")
            if candidate_wf is not None:
                if orthogonal_v4_candidate_pool and isinstance(candidate_dump, dict):
                    candidate_wf.write(json.dumps(candidate_dump, ensure_ascii=False) + "\n")
                    candidate_wf.flush()
                elif orthogonal_v3_candidate_pool:
                    case_candidate_record = _build_case_candidate_dump_record(sid, candidate_dump)
                    if case_candidate_record is not None:
                        candidate_wf.write(json.dumps(case_candidate_record, ensure_ascii=False) + "\n")
                        candidate_wf.flush()
                elif isinstance(candidate_dump, dict):
                    candidate_wf.write(json.dumps(candidate_dump, ensure_ascii=False) + "\n")
                    candidate_wf.flush()
                elif isinstance(candidate_dump, list):
                    for row in candidate_dump:
                        if isinstance(row, dict):
                            candidate_wf.write(json.dumps(row, ensure_ascii=False) + "\n")
                    candidate_wf.flush()
            success += 1
            return

        if status == "invalid":
            failed += 1
            print(f"[WARN] {result.get('message', f'skip invalid sample: id={sid}')}")
            return

        failed += 1
        error = result.get("error")
        if isinstance(error, Exception):
            failure_category = _classify_case_failure(error)
            error_type = type(error).__name__
            error_message = str(error)
        else:
            failure_category = "other_failure"
            error_type = str(result.get("error_type", "Exception"))
            error_message = str(result.get("error", ""))
        failure_counts[failure_category] = failure_counts.get(failure_category, 0) + 1
        if failure_category == "validation_failure":
            validation_failed += 1
        failure_details.append(
            {
                "id": sid,
                "category": failure_category,
                "error_type": error_type,
                "error": error_message,
            }
        )
        print(f"[ERROR] id={sid} failed ({failure_category}): {error_type}: {error_message}")

    async def _worker(index: int, item: Dict[str, Any], sem: asyncio.Semaphore) -> Dict[str, Any]:
        async with sem:
            sid = str(item.get("id", ""))
            try:
                if orthogonal_v4_candidate_pool:
                    result = await _run_orthogonal_v4_candidate_pool_case(
                        agent=agent,
                        item=item,
                        args=args,
                        cached_original_by_id=cached_original_by_id,
                        tool_names=tool_names,
                        tool_map_override=tool_map_override,
                        dependency_type=dependency_type,
                    )
                elif orthogonal_v3_candidate_pool:
                    result = await _run_orthogonal_v3_candidate_pool_case(
                        agent=agent,
                        item=item,
                        args=args,
                        tool_names=tool_names,
                        tool_map_override=tool_map_override,
                        dependency_type=dependency_type,
                    )
                else:
                    result = await _run_prediction_case(
                        agent=agent,
                        item=item,
                        args=args,
                        tool_names=tool_names,
                        tool_map_override=tool_map_override,
                        dependency_type=dependency_type,
                        save_candidate_pool=save_candidate_pool,
                        gold_rows=gold_rows,
                    )
                result["index"] = index
                return result
            except Exception as e:
                return {
                    "status": "error",
                    "index": index,
                    "id": sid,
                    "error": e,
                    "error_type": type(e).__name__,
                }

    try:
        prediction_output = (
            _NullPredictionOutput()
            if orthogonal_v4_candidate_pool
            else _open_prediction_output(output_path, resume=resume_enabled)
        )
        with prediction_output as wf:
            sem = asyncio.Semaphore(multiworker)
            tasks = [
                asyncio.create_task(_worker(idx, item, sem))
                for idx, item in enumerate(requests, start=1)
            ]
            processed = 0
            for task in asyncio.as_completed(tasks):
                result = await task
                _record_case_result(result, wf)
                processed += 1
                if processed % log_every == 0 or processed == len(requests):
                    print(f"[INFO] progress={processed}/{len(requests)} success={success} failed={failed}")
                if str(result.get("status", "")) == "error" and bool(getattr(args, "stop_on_error", False)):
                    for pending in tasks:
                        if not pending.done():
                            pending.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    error = result.get("error")
                    if isinstance(error, Exception):
                        raise error
                    raise RuntimeError(str(result.get("error", "case failed")))
    finally:
        if candidate_wf is not None:
            candidate_wf.close()

    print(f"[DONE] success={success}, failed={failed}, output={output_path}")
    orthogonal_v4_summary = None
    if orthogonal_v4_candidate_pool:
        orthogonal_v4_summary = _summarize_orthogonal_v4_candidate_pool(candidate_dump_path)
        print("[SUMMARY] orthogonal_v4_candidate_pool")
        print(json.dumps(orthogonal_v4_summary, ensure_ascii=False, indent=2))
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
        "candidate_pool_only": orthogonal_v3_candidate_pool or orthogonal_v4_candidate_pool,
        "orthogonal_v4_summary": orthogonal_v4_summary,
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
    parser.add_argument(
        "--planning_prompt_mode",
        type=str,
        default="agent",
        choices=["agent", "taskbench"],
        help="Use the native agent planner prompt or the inference.py-compatible TaskBench prompt.",
    )
    parser.add_argument("--planning_mode", type=str, default="single", choices=["single", "multi", "action_dag"])
    parser.add_argument("--execution_mode", type=str, default="best", choices=["best", "all"])
    parser.add_argument("--candidate_count", type=int, default=3)
    parser.add_argument(
        "--candidate_selection_mode",
        type=str,
        default="rerank",
        choices=[
            "none",
            "rerank",
            "first",
            "original_first_fallback",
            "collect_all_then_original",
            "original_dependency_filter_first_valid",
            "structure_aware",
        ],
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
        "--candidate_prompt_mode",
        type=str,
        default="legacy",
        choices=["legacy", "orthogonal", "orthogonal_v2", "orthogonal_v3", "orthogonal_v4"],
        help="Candidate prompt family mode used for multi-candidate generation.",
    )
    parser.add_argument(
        "--cached_original",
        type=str,
        default="qwen3-14b_20260527.json",
        help="Cached original TaskBench prediction JSON/JSONL used by candidate_prompt_mode=orthogonal_v4.",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="orthogonal_v4_candidate_pool.jsonl",
        help="Output JSONL path for candidate_prompt_mode=orthogonal_v4. Relative filenames are written under data_dir/candidate_dumps.",
    )
    parser.add_argument(
        "--overwrite",
        nargs="?",
        const=True,
        default=False,
        type=_parse_bool_arg,
        help="Overwrite existing output instead of resuming. Accepts optional true/false.",
    )
    parser.add_argument(
        "--force_generate_all_candidate_families",
        dest="force_generate_all_candidate_families",
        action="store_true",
        default=False,
        help="Generate and retain one candidate for every configured strategy family without pool-size truncation or cross-family early stop.",
    )
    parser.add_argument(
        "--disable_force_generate_all_candidate_families",
        dest="force_generate_all_candidate_families",
        action="store_false",
        help="Use the default pool-size-limited candidate generation behavior.",
    )
    parser.add_argument(
        "--disable_early_stop",
        dest="disable_early_stop",
        action="store_true",
        default=False,
        help="Disable original-pass early stop and continue collecting the full candidate pool before final selection.",
    )
    parser.add_argument(
        "--enable_early_stop",
        dest="disable_early_stop",
        action="store_false",
        help="Allow original-pass early stop when the selection mode supports it.",
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
    parser.add_argument("--use_demos", type=int, default=0, help="Number of inference.py-compatible demos to include when planning_prompt_mode=taskbench.")
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
    parser.add_argument(
        "--print-prompts",
        "--print_prompts",
        dest="print_prompts",
        action="store_true",
        default=False,
        help="Print the TaskBench-compatible planning prompts before running cases.",
    )
    parser.add_argument(
        "--dry-run-prompts",
        "--dry_run_prompts",
        dest="dry_run_prompts",
        action="store_true",
        default=False,
        help="Print prompts and stop before initializing PipelineOrchestratorAgent.",
    )
    parser.add_argument(
        "--print-prompt-strategy-count",
        "--print_prompt_strategy_count",
        dest="print_prompt_strategy_count",
        type=int,
        default=1,
        help="Number of non-original multi-strategy prompts to print per case.",
    )
    parser.add_argument(
        "--prompt-preview-chars",
        "--prompt_preview_chars",
        dest="prompt_preview_chars",
        type=int,
        default=0,
        help="If positive, truncate each printed prompt to this many characters.",
    )
    parser.add_argument(
        "--multiworker",
        type=int,
        default=1,
        help="Maximum number of TaskBench cases to run concurrently.",
    )
    parser.add_argument("--resume", action="store_true", default=True)
    parser.add_argument("--no-resume", dest="resume", action="store_false")
    parser.add_argument(
        "--save_candidate_pool",
        action="store_true",
        default=True,
        help="Write all generated candidate workflows plus selection metadata to a separate candidate_dumps JSONL file.",
    )
    parser.add_argument(
        "--no-save_candidate_pool",
        "--no_save_candidate_pool",
        dest="save_candidate_pool",
        action="store_false",
        help="Disable candidate pool dump writing.",
    )
    parser.add_argument(
        "--save_action_dag",
        action="store_true",
        default=False,
        help="Write action-DAG probe records instead of TaskBench workflow predictions when planning_mode=action_dag.",
    )
    parser.add_argument("--log_every", type=int, default=10)
    parser.add_argument("--stop_on_error", action="store_true", default=False)
    return parser


PIPELINE_AGENT_INFERENCE_LLM_NAMES = {
    "pipeline_agent",
    "pipeline_orchestrator_agent",
}

PIPELINE_AGENT_INFERENCE_DEFAULTS: Dict[str, Any] = {
    "prediction_dir": "predictions_pipeline_agent",
    "provider": "openai",
    "model_name": "qwen3-14b",
    "llm_config_path": "configs/qwen.json",
    "skills_root": "skills_multimedia",
    "planning_prompt_mode": "taskbench",
    "planning_mode": "multi",
    "execution_mode": "best",
    "candidate_count": 5,
    "candidate_selection_mode": "original_first_fallback",
    "include_original_candidate": True,
    "enable_candidate_verifier": False,
    "enable_candidate_repair": False,
    "enable_workflow_memory": False,
    "fixed_candidate_temperature": 0.0,
    "edge_grounding_mode": "none",
    "candidate_prompt_mode": "orthogonal_v3",
    "force_generate_all_candidate_families": True,
    "disable_early_stop": False,
    "save_candidate_pool": True,
    "link_mode": "chain_fallback",
    "limit": None,
    "print_prompts": False,
    "dry_run_prompts": False,
    "print_prompt_strategy_count": 5,
    "prompt_preview_chars": 0,
    "resume": True,
}


def is_pipeline_agent_inference_llm(llm: str) -> bool:
    return str(llm or "").strip().lower() in PIPELINE_AGENT_INFERENCE_LLM_NAMES


async def run_from_taskbench_inference(
    *,
    data_dir: str,
    llm: str,
    multiworker: int,
    dependency_type: str,
    use_demos: int = 0,
) -> Dict[str, Any]:
    args = build_parser().parse_args([])
    for key, value in PIPELINE_AGENT_INFERENCE_DEFAULTS.items():
        setattr(args, key, value)
    args.data_dir = data_dir
    args.llm = str(llm or "pipeline_agent")
    args.multiworker = multiworker
    args.dependency_type = dependency_type
    args.use_demos = use_demos
    return await _run(args)


if __name__ == "__main__":
    cli_args = build_parser().parse_args()
    if not cli_args.skills_root:
        cli_args.skills_root = "skills_multimedia"  # 或绝对路径
    asyncio.run(_run(cli_args))
