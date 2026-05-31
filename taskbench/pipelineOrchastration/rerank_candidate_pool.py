from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args: Any, **kwargs: Any) -> bool:
        return False

SCRIPT_DIR = Path(__file__).resolve().parent


def _find_project_root(start: Path) -> Path:
    cur = start.resolve()
    for candidate in [cur] + list(cur.parents):
        if (candidate / "agent").exists() and (candidate / "taskbench").exists():
            return candidate
    raise FileNotFoundError(f"Cannot locate project root from: {start}")


ROOT = _find_project_root(SCRIPT_DIR)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env", override=False)
load_dotenv(ROOT / ".env.local", override=False)

from agent.pipeline_orchestrator_agent import PipelineOrchestratorAgent

try:
    from .export_three_tables import _materialize_semantic_graph, _normalize_task_name
except ImportError:
    from export_three_tables import _materialize_semantic_graph, _normalize_task_name  # type: ignore


FAMILY_ORDER = [
    "minimal",
    "action_coverage",
    "typed_dependency",
    "parallel_dag",
    "materialization",
]
DEFAULT_CHAIN_FAMILIES = ",".join(FAMILY_ORDER)
DEFAULT_DAG_FAMILIES = ",".join(FAMILY_ORDER)
NODE_REF_RE = re.compile(r"^<node-(\d+)>$")


EXPLICIT_TASK_KEYWORDS: Dict[str, List[str]] = {
    "article spinner": ["unique", "rewrite", "rewritten", "spin", "paraphrase", "make unique"],
    "audio effects": ["reverb", "equalization", "effect", "effects", "enhance audio"],
    "audio noise reduction": ["noise", "denoise", "background noise", "reduce noise"],
    "audio-to-image": ["waveform", "spectrogram", "visual representation", "image representation"],
    "audio-to-text": ["transcribe", "transcript", "audio into text", "audio to text"],
    "image colorizer": ["colorize", "colorized", "black and white", "colourize"],
    "image search": ["image search", "search for images", "relevant images", "suitable images"],
    "image search (by image)": ["using that image", "using this image", "similar images", "search using"],
    "text grammar checker": ["grammar", "grammatical", "errors", "corrected text", "checking"],
    "text search": ["search", "find information", "look up"],
    "text summarizer": ["summarize", "summary", "summarise"],
    "text-to-image": ["generate an image", "create an image", "image from text", "depicting", "based on the text"],
    "text-to-audio": ["generate audio", "audio file", "text to audio", "text-to-audio"],
    "text-to-video": ["generate a video", "create a video", "video from text", "text to video", "text-to-video"],
    "text translator": ["translate", "translation", "english", "foreign language", "another language"],
    "topic generator": ["topic", "topics", "ideas", "interesting ideas"],
    "image-to-video": ["slideshow", "image to video", "image-to-video", "turn image into video"],
    "image-to-text": ["extract text", "image to text", "image-to-text", "ocr"],
    "video speed changer": ["speed", "playback speed", "slow down", "speed up"],
    "video stabilizer": ["stabilize", "stabilise", "shaky", "stabilized"],
    "video synchronization": ["synchronize", "synchronise", "sync", "align", "audio-visual"],
    "video voiceover": ["voiceover", "voice-over", "narration"],
    "video-to-audio": ["extract audio", "audio track", "audio from video"],
    "video-to-image": ["still image", "extract image", "frame", "image from video"],
    "video-to-text": ["video content", "content of video", "video into text", "transcribe video"],
    "voice changer": ["voice", "tone", "pitch", "gender", "voice characteristics"],
}


def _resolve_data_dir(raw: str) -> Path:
    path = Path(raw)
    candidates: List[Path]
    if path.is_absolute():
        candidates = [path.resolve()]
    else:
        candidates = [
            (Path.cwd() / path).resolve(),
            (ROOT / path).resolve(),
            (ROOT / "taskbench" / path).resolve(),
        ]
        if path.name.startswith("data_"):
            candidates.append((ROOT / "taskbench" / path.name).resolve())
    for candidate in _dedupe_paths(candidates):
        if (candidate / "data.json").exists():
            return candidate
    attempted = "\n".join(f"- {candidate}" for candidate in _dedupe_paths(candidates))
    raise FileNotFoundError(f"Cannot locate data_dir: {raw}\nTried:\n{attempted}")


def _resolve_existing_file(raw: str, *, data_dir: Optional[Path] = None, label: str = "file") -> Path:
    path = Path(raw)
    candidates: List[Path]
    if path.is_absolute():
        candidates = [path.resolve()]
    else:
        candidates = [(Path.cwd() / path).resolve(), (ROOT / path).resolve()]
        if data_dir is not None:
            candidates.insert(0, (data_dir / path).resolve())
    for candidate in _dedupe_paths(candidates):
        if candidate.exists() and candidate.is_file():
            return candidate
    attempted = "\n".join(f"- {candidate}" for candidate in _dedupe_paths(candidates))
    raise FileNotFoundError(f"Cannot locate {label}: {raw}\nTried:\n{attempted}")


def _resolve_output_file(raw: str, *, data_dir: Path) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path.resolve()
    return (data_dir / path).resolve()


def _dedupe_paths(paths: Iterable[Path]) -> List[Path]:
    seen: Set[str] = set()
    result: List[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _load_json_or_jsonl(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text[0] == "[":
        payload = json.loads(text)
        return payload if isinstance(payload, list) else []
    rows: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _read_done_ids(path: Path) -> Set[str]:
    if not path.exists():
        return set()
    done: Set[str] = set()
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            case_id = str(row.get("id") or row.get("case_id") or "").strip()
            if case_id:
                done.add(case_id)
    return done


def _case_id(row: Dict[str, Any]) -> str:
    return str(row.get("id") or row.get("case_id") or row.get("index") or "").strip()


def _parse_maybe_json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return json.loads(text)
        except Exception:
            return default
    return value if value is not None else default


def _canonical_result(row: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    if isinstance(row.get("result"), dict):
        return row["result"]
    if isinstance(row.get("answer"), dict):
        return row["answer"]
    if "tool_nodes" in row:
        return {
            "task_steps": _parse_maybe_json(row.get("tool_steps"), []),
            "task_nodes": _parse_maybe_json(row.get("tool_nodes"), []),
            "task_links": _parse_maybe_json(row.get("tool_links"), []),
        }
    if "task_nodes" in row:
        return row
    return {}


def _prediction_result(result: Dict[str, Any], *, include_task_links: bool) -> Dict[str, Any]:
    task_steps = result.get("task_steps", [])
    if not isinstance(task_steps, list):
        task_steps = []
    task_nodes = result.get("task_nodes", [])
    if not isinstance(task_nodes, list):
        task_nodes = []
    output = {
        "task_steps": task_steps,
        "task_nodes": task_nodes,
    }
    if include_task_links:
        task_links = result.get("task_links", [])
        output["task_links"] = task_links if isinstance(task_links, list) else []
    return output


def _load_candidate_pool(path: Path) -> Dict[str, List[Dict[str, Any]]]:
    rows = _load_json_or_jsonl(path)
    pool: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        case_id = str(row.get("case_id") or row.get("id") or "").strip()
        if not case_id:
            continue
        if isinstance(row.get("candidates"), list):
            pool[case_id] = [item for item in row["candidates"] if isinstance(item, dict)]
        else:
            pool.setdefault(case_id, []).append(row)
    return pool


def _task_nodes(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    nodes = result.get("task_nodes", []) if isinstance(result, dict) else []
    return [node for node in nodes if isinstance(node, dict)] if isinstance(nodes, list) else []


def _task_links(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    links = result.get("task_links", []) if isinstance(result, dict) else []
    return [link for link in links if isinstance(link, dict)] if isinstance(links, list) else []


def _graph_view(result: Dict[str, Any]) -> Dict[str, Any]:
    nodes = _task_nodes(result)
    links = _task_links(result)
    try:
        node_names, normalized_links, normalized_arguments = _materialize_semantic_graph(nodes, links)
    except Exception:
        node_names, normalized_links, normalized_arguments = [], [], []
    return {
        "node_names": node_names,
        "links": normalized_links,
        "arguments": normalized_arguments,
    }


def _workflow_signature(result: Dict[str, Any]) -> str:
    view = _graph_view(result)
    return json.dumps(
        {
            "nodes": view["node_names"],
            "links": sorted(
                [
                    (str(link.get("source", "")), str(link.get("target", "")))
                    for link in view["links"]
                    if isinstance(link, dict)
                ]
            ),
            "arguments": view["arguments"],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _normalized_task_sequence(result: Dict[str, Any]) -> List[str]:
    return [
        _normalize_task_name(str(node.get("task", ""))).lower()
        for node in _task_nodes(result)
    ]


def _normalized_task_counter(result: Dict[str, Any]) -> Counter[str]:
    return Counter(_normalized_task_sequence(result))


def _positive_counter_diff(left: Counter[str], right: Counter[str]) -> List[str]:
    items: List[str] = []
    for key, value in left.items():
        missing = value - right.get(key, 0)
        if missing > 0:
            items.extend([key] * missing)
    return items


def _edge_set(result: Dict[str, Any]) -> Set[Tuple[str, str]]:
    return {
        (str(link.get("source", "")), str(link.get("target", "")))
        for link in _graph_view(result)["links"]
        if isinstance(link, dict)
    }


def _tool_key(task_name: str) -> str:
    return _normalize_task_name(task_name).lower()


def _types_compatible(output_types: Set[str], input_types: Set[str]) -> bool:
    if not output_types or not input_types:
        return False
    if output_types & input_types:
        return True
    return "any" in output_types or "any" in input_types


def _tool_output_types(tool_type_map: Dict[str, Dict[str, Set[str]]], task_name: str) -> Set[str]:
    return tool_type_map.get(_tool_key(task_name), {}).get("output_types", set())


def _tool_input_types(tool_type_map: Dict[str, Dict[str, Set[str]]], task_name: str) -> Set[str]:
    return tool_type_map.get(_tool_key(task_name), {}).get("input_types", set())


def _explicit_task_requested(user_request: str, task_name: str) -> bool:
    text = str(user_request or "").lower()
    task = _normalize_task_name(task_name).lower()
    if not task:
        return False
    keywords = EXPLICIT_TASK_KEYWORDS.get(task, [])
    if any(keyword in text for keyword in keywords):
        return True
    ignored_tokens = {"a", "an", "and", "by", "from", "of", "or", "the", "to", "with", "text", "video", "audio", "image"}
    task_tokens = [token for token in re.split(r"[^a-z0-9]+", task) if token and token not in ignored_tokens]
    return bool(task_tokens) and all(token in text for token in task_tokens)


def _candidate_diff_against_baseline(
    *,
    user_request: str,
    baseline_result: Dict[str, Any],
    candidate_result: Dict[str, Any],
) -> Dict[str, Any]:
    baseline_counter = _normalized_task_counter(baseline_result)
    candidate_counter = _normalized_task_counter(candidate_result)
    added_tasks = _positive_counter_diff(candidate_counter, baseline_counter)
    removed_tasks = _positive_counter_diff(baseline_counter, candidate_counter)
    baseline_edges = _edge_set(baseline_result)
    candidate_edges = _edge_set(candidate_result)
    explicit_added_tasks = [
        task for task in added_tasks if _explicit_task_requested(user_request, task)
    ]
    added_edge_set = candidate_edges - baseline_edges
    removed_edge_set = baseline_edges - candidate_edges
    inserted_edges: List[Dict[str, Any]] = []
    for source, target in sorted(removed_edge_set):
        for task in explicit_added_tasks:
            if (source, task) in candidate_edges and (task, target) in candidate_edges:
                inserted_edges.append(
                    {
                        "removed_edge": [source, target],
                        "inserted_task": task,
                        "replacement_edges": [[source, task], [task, target]],
                    }
                )
    return {
        "added_tasks": added_tasks,
        "removed_tasks": removed_tasks,
        "explicit_added_tasks": explicit_added_tasks,
        "added_edges": sorted(added_edge_set),
        "removed_edges": sorted(removed_edge_set),
        "inserted_explicit_tasks_on_removed_edges": inserted_edges,
        "node_count_delta": len(_task_nodes(candidate_result)) - len(_task_nodes(baseline_result)),
        "edge_count_delta": len(candidate_edges) - len(baseline_edges),
        "baseline_topology": _classify_topology(baseline_result),
        "candidate_topology": _classify_topology(candidate_result),
    }


def _classify_topology(result: Dict[str, Any]) -> str:
    view = _graph_view(result)
    nodes = list(view["node_names"])
    edges = [
        (str(link.get("source", "")), str(link.get("target", "")))
        for link in view["links"]
        if isinstance(link, dict)
    ]
    if len(nodes) <= 1:
        return "single"
    if not edges:
        return "disconnected"

    node_set = set(nodes)
    indeg = Counter(target for _, target in edges)
    outdeg = Counter(source for source, _ in edges)
    if len(set(edges)) == len(nodes) - 1 and all(indeg[node] <= 1 and outdeg[node] <= 1 for node in node_set):
        adj: Dict[str, Set[str]] = defaultdict(set)
        for source, target in edges:
            adj[source].add(target)
            adj[target].add(source)
        start = next(iter(node_set))
        seen = {start}
        queue: deque[str] = deque([start])
        while queue:
            cur = queue.popleft()
            for nxt in adj[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        if seen == node_set:
            return "chain"
    return "dag"


def _normalize_family_list(raw: str, *, default: Sequence[str]) -> List[str]:
    text = str(raw or "").strip()
    if not text or text.lower() == "default":
        return list(default)
    if text.lower() == "all":
        return list(FAMILY_ORDER)
    families = [item.strip() for item in text.split(",") if item.strip()]
    unknown = [item for item in families if item not in FAMILY_ORDER]
    if unknown:
        raise ValueError(f"unknown candidate families: {unknown}; allowed={FAMILY_ORDER}")
    return families


def _validate_workflow(
    result: Dict[str, Any],
    *,
    allowed_tools: Set[str],
    parse_success: bool = True,
) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    if not parse_success:
        errors.append("parse_failed")
    nodes = _task_nodes(result)
    if not nodes:
        errors.append("no_task_nodes")
        return errors, warnings

    for idx, node in enumerate(nodes):
        task = str(node.get("task", "")).strip()
        if not task:
            errors.append(f"empty_task@{idx}")
        elif allowed_tools and task not in allowed_tools:
            errors.append(f"unknown_task@{idx}:{task}")
        args = node.get("arguments", [])
        if not isinstance(args, list):
            warnings.append(f"non_list_arguments@{idx}")
            args = []
        for arg in args:
            match = NODE_REF_RE.fullmatch(str(arg).strip())
            if not match:
                continue
            ref_idx = int(match.group(1))
            if ref_idx < 0 or ref_idx >= idx:
                errors.append(f"invalid_node_ref@{idx}:<node-{ref_idx}>")

    steps = result.get("task_steps", [])
    if isinstance(steps, list) and steps and len(steps) != len(nodes):
        warnings.append("task_steps_count_mismatch")
    return errors, warnings


def _compact_workflow_view(result: Dict[str, Any]) -> Dict[str, Any]:
    view = _graph_view(result)
    nodes = _task_nodes(result)
    compact_nodes: List[Dict[str, Any]] = []
    for idx, node in enumerate(nodes):
        args = node.get("arguments", [])
        compact_nodes.append(
            {
                "index": idx,
                "task": node.get("task", ""),
                "arguments": args if isinstance(args, list) else [],
            }
        )
    return {
        "node_count": len(compact_nodes),
        "edge_count": len(view["links"]),
        "topology": _classify_topology(result),
        "task_nodes": compact_nodes,
        "inferred_links": view["links"],
    }


def _build_judge_prompt(
    *,
    case_id: str,
    user_request: str,
    topology: str,
    baseline_result: Dict[str, Any],
    candidates: List[Dict[str, Any]],
) -> str:
    candidate_views = []
    for item in candidates:
        candidate_views.append(
            {
                "id": item["id"],
                "family": item["family"],
                "workflow": _compact_workflow_view(item["result"]),
                "diff_vs_baseline": item.get("diff_vs_baseline", {}),
                "filter_warnings": item.get("filter_warnings", []),
            }
        )

    payload = {
        "case_id": case_id,
        "user_request": user_request,
        "topology_group": topology,
        "baseline": {
            "id": "baseline",
            "workflow": _compact_workflow_view(baseline_result),
        },
        "candidates": candidate_views,
    }

    return f"""
You are a conservative TaskBench workflow reranker.

Your job is to decide whether any candidate workflow is clearly better than the cached baseline.
The cached baseline is the default choice.

Selection policy:
1. Choose a candidate only if it is clearly better than baseline for the user request.
2. Prefer baseline when the difference is minor, stylistic, or uncertain.
3. A candidate is clearly better when it fixes a missing explicit action, removes an extra/unrequested action, improves direct artifact dependencies, preserves a needed DAG branch, or produces a required final deliverable.
4. Do not prefer a longer workflow just because it has more steps.
5. Do not prefer a shorter workflow if it drops an explicit requested action or final artifact.
6. For DAG cases, pay special attention to independent branches and shared upstream artifacts.
7. For chain cases, pay special attention to sequential artifact flow and correct <node-i> references.
8. Compare each candidate against the baseline, not against the other candidates.
9. Exact tool names matter. Do not treat a different tool as equivalent unless the request clearly supports the substitution.
10. If the user says one operation happens after another, treat that as evidence that the downstream operation should consume the upstream artifact unless the request clearly asks for independent branches.
11. Use diff_vs_baseline carefully:
   - explicit_added_tasks are tools added by the candidate and heuristically matched to explicit wording in the user request.
   - If explicit_added_tasks fixes an action that baseline misses and removed_tasks is empty, this is strong evidence for replacement.
   - inserted_explicit_tasks_on_removed_edges means a baseline dependency A -> B became A -> explicit_added_task -> B; this is not a dependency regression by itself.
   - If removed_tasks or removed_edges removes a baseline operation/dependency that the request needs, prefer baseline.
   - If added_tasks are not clearly requested, do not treat them as improvements.

Output JSON only with this schema:
{{
  "selected_id": "baseline or one candidate id",
  "should_replace_baseline": true or false,
  "confidence": "high, medium, or low",
  "reason_codes": ["missing_explicit_action_fixed", "covers_missing_action", "better_dependency", "preserves_parallel_branch", "better_final_artifact", "task_name_mismatch", "dependency_regression", "baseline_safer", "uncertain"],
  "rationale": "one concise sentence"
}}

Only set should_replace_baseline=true when selected_id is a candidate and confidence is high.
If no candidate is clearly better, select baseline with should_replace_baseline=false.

Input:
{json.dumps(payload, ensure_ascii=False, indent=2)}
""".strip()


def _strip_json_markdown_fence(text: str) -> str:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    return raw


def _parse_json_object(text: str) -> Dict[str, Any]:
    raw = _strip_json_markdown_fence(text)
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(raw[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        raise


async def _call_judge(
    client: Any,
    prompt: str,
    *,
    max_retries: int,
    retry_sleep: float,
) -> Tuple[Dict[str, Any], str, str]:
    from langchain_core.messages import HumanMessage, SystemMessage

    last_error = ""
    for attempt in range(max_retries + 1):
        try:
            resp = await client.ainvoke(
                [
                    SystemMessage(content="Output valid JSON only."),
                    HumanMessage(content=prompt),
                ]
            )
            raw_response = (getattr(resp, "content", "") or "").strip()
            parsed = _parse_json_object(raw_response)
            return parsed, raw_response, ""
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < max_retries:
                await asyncio.sleep(retry_sleep)
    return {}, "", last_error


def _normalize_judge_decision(
    parsed: Dict[str, Any],
    *,
    candidate_ids: Set[str],
    allow_medium_confidence: bool,
) -> Dict[str, Any]:
    selected_id = str(parsed.get("selected_id") or "baseline").strip()
    if selected_id not in candidate_ids and selected_id != "baseline":
        selected_id = "baseline"

    confidence = str(parsed.get("confidence") or "low").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"

    should_replace = bool(parsed.get("should_replace_baseline", False))
    confidence_ok = confidence == "high" or (allow_medium_confidence and confidence == "medium")
    if selected_id == "baseline" or not should_replace or not confidence_ok:
        selected_id = "baseline"
        should_replace = False

    reason_codes = parsed.get("reason_codes", [])
    if not isinstance(reason_codes, list):
        reason_codes = [str(reason_codes)]

    return {
        "selected_id": selected_id,
        "should_replace_baseline": should_replace,
        "confidence": confidence,
        "reason_codes": [str(item) for item in reason_codes],
        "rationale": str(parsed.get("rationale") or "").strip(),
    }


def _missing_action_override_candidate(candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    explicit_candidates: List[Dict[str, Any]] = []
    for item in candidates:
        diff = item.get("diff_vs_baseline", {})
        explicit_added = diff.get("explicit_added_tasks", [])
        removed_tasks = diff.get("removed_tasks", [])
        removed_edges = diff.get("removed_edges", [])
        inserted_edges = diff.get("inserted_explicit_tasks_on_removed_edges", [])
        if not explicit_added or removed_tasks or removed_edges:
            if not explicit_added or removed_tasks:
                continue
            if not removed_edges or len(inserted_edges) != len(removed_edges):
                continue
        if int(diff.get("node_count_delta") or 0) < 1:
            continue
        explicit_candidates.append(item)

    if len(explicit_candidates) != 1:
        return None
    return explicit_candidates[0]


def _apply_missing_action_override(
    decision: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    *,
    enabled: bool,
) -> Dict[str, Any]:
    if not enabled:
        return decision
    if decision.get("selected_id") != "baseline":
        return decision
    confidence = str(decision.get("confidence") or "low").lower()
    reason_codes = {str(item) for item in decision.get("reason_codes", [])}
    if confidence == "high" and "baseline_safer" in reason_codes and "uncertain" not in reason_codes:
        return decision

    candidate = _missing_action_override_candidate(candidates)
    if candidate is None:
        return decision

    diff = candidate.get("diff_vs_baseline", {})
    return {
        "selected_id": candidate["id"],
        "should_replace_baseline": True,
        "confidence": "high",
        "reason_codes": [
            "missing_explicit_action_fixed",
            "override_after_uncertain_baseline_gate",
        ],
        "rationale": (
            "Candidate adds explicit requested task(s) missing from baseline without "
            f"dropping baseline tasks or dependencies: {', '.join(diff.get('explicit_added_tasks', []))}."
        ),
        "override_from": decision,
    }


def _candidate_id(family: str, index: int) -> str:
    return f"{family}#{index}"


def _is_dag_edge_only_candidate(item: Dict[str, Any]) -> bool:
    diff = item.get("diff_vs_baseline", {})
    if not isinstance(diff, dict):
        return False
    return bool(diff.get("added_edges")) and not any(
        diff.get(key)
        for key in (
            "added_tasks",
            "removed_tasks",
            "explicit_added_tasks",
            "removed_edges",
        )
    )


def _filter_dag_edge_only_candidates(
    candidates: List[Dict[str, Any]],
    *,
    topology: str,
    enabled: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not enabled or topology != "dag":
        return candidates, []

    kept: List[Dict[str, Any]] = []
    filtered: List[Dict[str, Any]] = []
    for item in candidates:
        if not _is_dag_edge_only_candidate(item):
            kept.append(item)
            continue
        filtered.append(
            {
                "family": item.get("family", ""),
                "candidate_id": item.get("source_candidate_id", item.get("id", "")),
                "reason": "dag_edge_only_candidate",
                "diff_vs_baseline": item.get("diff_vs_baseline", {}),
                "warnings": item.get("filter_warnings", []),
            }
        )
    return kept, filtered


def _find_redundant_bridge_insertions(
    *,
    user_request: str,
    baseline_result: Dict[str, Any],
    candidate_result: Dict[str, Any],
    tool_type_map: Dict[str, Dict[str, Set[str]]],
) -> List[Dict[str, Any]]:
    if not tool_type_map:
        return []

    added_tasks = sorted(set(_positive_counter_diff(
        _normalized_task_counter(candidate_result),
        _normalized_task_counter(baseline_result),
    )))
    if not added_tasks:
        return []

    baseline_edges = _edge_set(baseline_result)
    candidate_edges = _edge_set(candidate_result)
    findings: List[Dict[str, Any]] = []

    for source, target in sorted(baseline_edges):
        for bridge in added_tasks:
            if (source, bridge) not in candidate_edges or (bridge, target) not in candidate_edges:
                continue

            source_outputs = _tool_output_types(tool_type_map, source)
            bridge_inputs = _tool_input_types(tool_type_map, bridge)
            bridge_outputs = _tool_output_types(tool_type_map, bridge)
            target_inputs = _tool_input_types(tool_type_map, target)
            if not source_outputs or not bridge_inputs or not bridge_outputs or not target_inputs:
                continue

            explicit = _explicit_task_requested(user_request, bridge)
            source_feeds_bridge = _types_compatible(source_outputs, bridge_inputs)
            bridge_feeds_target = _types_compatible(bridge_outputs, target_inputs)
            source_feeds_target = _types_compatible(source_outputs, target_inputs)

            reason = ""
            if not source_feeds_bridge:
                reason = "unsupported_bridge_input"
            elif not bridge_feeds_target:
                reason = "unsupported_bridge_output"
            elif source_feeds_target and not explicit:
                reason = "redundant_unrequested_bridge"
            if not reason:
                continue

            findings.append(
                {
                    "reason": reason,
                    "baseline_edge": [source, target],
                    "bridge_task": bridge,
                    "baseline_edge_removed": (source, target) not in candidate_edges,
                    "bridge_explicitly_requested": explicit,
                    "source_output_types": sorted(source_outputs),
                    "bridge_input_types": sorted(bridge_inputs),
                    "bridge_output_types": sorted(bridge_outputs),
                    "target_input_types": sorted(target_inputs),
                }
            )
    return findings


def _prepare_candidates(
    *,
    raw_candidates: List[Dict[str, Any]],
    user_request: str,
    baseline_result: Dict[str, Any],
    topology: str,
    allowed_tools: Set[str],
    tool_type_map: Dict[str, Dict[str, Set[str]]],
    allowed_families: Sequence[str],
    allow_task_drops: bool,
    allow_chain_edge_drops: bool,
    filter_redundant_bridges: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    baseline_signature = _workflow_signature(baseline_result)
    seen_signatures = {baseline_signature}
    baseline_task_counter = Counter(
        _normalize_task_name(str(node.get("task", ""))).lower()
        for node in _task_nodes(baseline_result)
    )
    baseline_task_sequence = [
        _normalize_task_name(str(node.get("task", ""))).lower()
        for node in _task_nodes(baseline_result)
    ]
    baseline_edge_set = {
        (str(link.get("source", "")), str(link.get("target", "")))
        for link in _graph_view(baseline_result)["links"]
        if isinstance(link, dict)
    }
    accepted: List[Dict[str, Any]] = []
    filtered: List[Dict[str, Any]] = []
    family_counts: Counter[str] = Counter()

    for idx, candidate in enumerate(raw_candidates, start=1):
        family = str(candidate.get("family") or candidate.get("variant") or "").strip()
        if family not in allowed_families:
            filtered.append(
                {
                    "family": family,
                    "candidate_id": candidate.get("candidate_id", idx),
                    "reason": "family_not_enabled",
                }
            )
            continue

        result = candidate.get("result") if isinstance(candidate.get("result"), dict) else {}
        errors, warnings = _validate_workflow(
            result,
            allowed_tools=allowed_tools,
            parse_success=bool(candidate.get("parse_success", True)),
        )
        if errors:
            filtered.append(
                {
                    "family": family,
                    "candidate_id": candidate.get("candidate_id", idx),
                    "reason": "invalid_candidate",
                    "errors": errors,
                    "warnings": warnings,
                }
            )
            continue

        candidate_task_counter = Counter(
            _normalize_task_name(str(node.get("task", ""))).lower()
            for node in _task_nodes(result)
        )
        if not allow_task_drops:
            missing_tasks: List[str] = []
            for task_name, count in baseline_task_counter.items():
                missing_count = count - candidate_task_counter.get(task_name, 0)
                if missing_count > 0:
                    missing_tasks.extend([task_name] * missing_count)
            if missing_tasks:
                filtered.append(
                    {
                        "family": family,
                        "candidate_id": candidate.get("candidate_id", idx),
                        "reason": "drops_baseline_task",
                        "missing_tasks": missing_tasks,
                        "warnings": warnings,
                    }
                )
                continue

        candidate_task_sequence = [
            _normalize_task_name(str(node.get("task", ""))).lower()
            for node in _task_nodes(result)
        ]
        candidate_edge_set = {
            (str(link.get("source", "")), str(link.get("target", "")))
            for link in _graph_view(result)["links"]
            if isinstance(link, dict)
        }
        if (
            topology == "chain"
            and not allow_chain_edge_drops
            and candidate_task_sequence == baseline_task_sequence
            and not baseline_edge_set.issubset(candidate_edge_set)
        ):
            filtered.append(
                {
                    "family": family,
                    "candidate_id": candidate.get("candidate_id", idx),
                    "reason": "drops_chain_baseline_dependency",
                    "missing_edges": sorted(baseline_edge_set - candidate_edge_set),
                    "warnings": warnings,
                }
            )
            continue

        bridge_findings = _find_redundant_bridge_insertions(
            user_request=user_request,
            baseline_result=baseline_result,
            candidate_result=result,
            tool_type_map=tool_type_map,
        )
        if filter_redundant_bridges and bridge_findings:
            filtered.append(
                {
                    "family": family,
                    "candidate_id": candidate.get("candidate_id", idx),
                    "reason": "redundant_bridge_tool",
                    "bridge_findings": bridge_findings,
                    "warnings": warnings,
                }
            )
            continue

        signature = _workflow_signature(result)
        if signature in seen_signatures:
            filtered.append(
                {
                    "family": family,
                    "candidate_id": candidate.get("candidate_id", idx),
                    "reason": "duplicate_or_same_as_baseline",
                    "warnings": warnings,
                }
            )
            continue
        seen_signatures.add(signature)

        family_counts[family] += 1
        item_id = _candidate_id(family, family_counts[family])
        accepted.append(
            {
                "id": item_id,
                "family": family,
                "variant": candidate.get("variant", family),
                "temperature": candidate.get("temperature"),
                "result": result,
                "filter_warnings": warnings,
                "source_candidate_id": candidate.get("candidate_id", idx),
            }
        )
    return accepted, filtered


def _load_tool_names(data_dir: Path) -> Set[str]:
    tool_desc_path = data_dir / "tool_desc.json"
    if not tool_desc_path.exists():
        return set()
    payload = json.loads(tool_desc_path.read_text(encoding="utf-8"))
    nodes = payload.get("nodes", [])
    if not isinstance(nodes, list):
        return set()
    return {str(item.get("id", "")).strip() for item in nodes if isinstance(item, dict) and item.get("id")}


def _normalize_type_values(value: Any) -> Set[str]:
    if not isinstance(value, list):
        return set()
    return {
        str(item).strip().lower()
        for item in value
        if str(item).strip()
    }


def _load_tool_type_map(data_dir: Path) -> Dict[str, Dict[str, Set[str]]]:
    tool_desc_path = data_dir / "tool_desc.json"
    if not tool_desc_path.exists():
        return {}
    payload = json.loads(tool_desc_path.read_text(encoding="utf-8"))
    nodes = payload.get("nodes", [])
    if not isinstance(nodes, list):
        return {}
    tool_type_map: Dict[str, Dict[str, Set[str]]] = {}
    for item in nodes:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        task_key = _tool_key(str(item.get("id", "")))
        tool_type_map[task_key] = {
            "input_types": _normalize_type_values(item.get("input-type", [])),
            "output_types": _normalize_type_values(item.get("output-type", [])),
        }
    return tool_type_map


def _topology_for_case(
    *,
    case_id: str,
    gold_by_id: Dict[str, Dict[str, Any]],
    baseline_result: Dict[str, Any],
    source: str,
) -> str:
    if source == "gold":
        gold = gold_by_id.get(case_id, {})
        topology = str(gold.get("type") or "").strip().lower()
        if topology:
            return topology
        return _classify_topology(baseline_result)
    if source == "baseline":
        return _classify_topology(baseline_result)
    if source == "auto":
        gold = gold_by_id.get(case_id, {})
        topology = str(gold.get("type") or "").strip().lower()
        return topology or _classify_topology(baseline_result)
    raise ValueError("topology_source must be 'gold', 'baseline' or 'auto'")


def _user_request_for_case(
    *,
    case_id: str,
    candidate_rows: Dict[str, List[Dict[str, Any]]],
    baseline_by_id: Dict[str, Dict[str, Any]],
    gold_by_id: Dict[str, Dict[str, Any]],
) -> str:
    for item in candidate_rows.get(case_id, []):
        text = str(item.get("user_request") or "").strip()
        if text:
            return text
    baseline = baseline_by_id.get(case_id, {})
    text = str(baseline.get("user_request") or "").strip()
    if text:
        return text
    gold = gold_by_id.get(case_id, {})
    return str(gold.get("user_request") or gold.get("instruction") or "").strip()


def _make_prediction_row(case_id: str, user_request: str, result: Dict[str, Any], *, include_task_links: bool) -> Dict[str, Any]:
    return {
        "id": case_id,
        "user_request": user_request,
        "result": _prediction_result(result, include_task_links=include_task_links),
    }


async def _rerank_case(
    *,
    case_id: str,
    user_request: str,
    topology: str,
    baseline_result: Dict[str, Any],
    raw_candidates: List[Dict[str, Any]],
    allowed_tools: Set[str],
    tool_type_map: Dict[str, Dict[str, Set[str]]],
    chain_families: Sequence[str],
    dag_families: Sequence[str],
    allow_task_drops: bool,
    allow_chain_edge_drops: bool,
    filter_redundant_bridges: bool,
    filter_dag_edge_only_candidates: bool,
    enable_missing_action_override: bool,
    client: Any,
    judge_mode: str,
    allow_medium_confidence: bool,
    max_retries: int,
    retry_sleep: float,
    include_task_links: bool,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if topology == "single":
        prediction = _make_prediction_row(case_id, user_request, baseline_result, include_task_links=include_task_links)
        detail = {
            "case_id": case_id,
            "topology": topology,
            "selection_route": "skip_single_baseline",
            "selected_id": "baseline",
            "selected_family": "baseline",
            "candidate_count": len(raw_candidates),
            "viable_candidate_count": 0,
            "filtered_candidates": [],
            "judge": {},
            "raw_response": "",
            "error": "",
        }
        return prediction, detail

    allowed_families = dag_families if topology == "dag" else chain_families
    candidates, filtered = _prepare_candidates(
        raw_candidates=raw_candidates,
        user_request=user_request,
        baseline_result=baseline_result,
        topology=topology,
        allowed_tools=allowed_tools,
        tool_type_map=tool_type_map,
        allowed_families=allowed_families,
        allow_task_drops=allow_task_drops,
        allow_chain_edge_drops=allow_chain_edge_drops,
        filter_redundant_bridges=filter_redundant_bridges,
    )

    if not candidates:
        prediction = _make_prediction_row(case_id, user_request, baseline_result, include_task_links=include_task_links)
        detail = {
            "case_id": case_id,
            "topology": topology,
            "selection_route": "baseline_no_viable_candidate",
            "selected_id": "baseline",
            "selected_family": "baseline",
            "candidate_count": len(raw_candidates),
            "viable_candidate_count": 0,
            "filtered_candidates": filtered,
            "judge": {},
            "raw_response": "",
            "error": "",
        }
        return prediction, detail

    for item in candidates:
        item["diff_vs_baseline"] = _candidate_diff_against_baseline(
            user_request=user_request,
            baseline_result=baseline_result,
            candidate_result=item["result"],
        )

    candidates, dag_edge_only_filtered = _filter_dag_edge_only_candidates(
        candidates,
        topology=topology,
        enabled=filter_dag_edge_only_candidates,
    )
    filtered.extend(dag_edge_only_filtered)

    if not candidates:
        prediction = _make_prediction_row(case_id, user_request, baseline_result, include_task_links=include_task_links)
        detail = {
            "case_id": case_id,
            "topology": topology,
            "selection_route": "baseline_no_viable_candidate",
            "selected_id": "baseline",
            "selected_family": "baseline",
            "candidate_count": len(raw_candidates),
            "viable_candidate_count": 0,
            "filtered_candidates": filtered,
            "judge": {},
            "raw_response": "",
            "error": "",
        }
        return prediction, detail

    if judge_mode == "baseline":
        decision = {
            "selected_id": "baseline",
            "should_replace_baseline": False,
            "confidence": "high",
            "reason_codes": ["baseline_safer"],
            "rationale": "Judge mode forced baseline.",
        }
        raw_response = json.dumps(decision, ensure_ascii=False)
        error = ""
    elif judge_mode == "first_candidate":
        decision = {
            "selected_id": candidates[0]["id"],
            "should_replace_baseline": True,
            "confidence": "high",
            "reason_codes": ["debug_first_candidate"],
            "rationale": "Judge mode forced first candidate.",
        }
        raw_response = json.dumps(decision, ensure_ascii=False)
        error = ""
    else:
        prompt = _build_judge_prompt(
            case_id=case_id,
            user_request=user_request,
            topology=topology,
            baseline_result=baseline_result,
            candidates=candidates,
        )
        parsed, raw_response, error = await _call_judge(
            client,
            prompt,
            max_retries=max_retries,
            retry_sleep=retry_sleep,
        )
        decision = _normalize_judge_decision(
            parsed,
            candidate_ids={item["id"] for item in candidates},
            allow_medium_confidence=allow_medium_confidence,
        )

    decision_before_override = dict(decision)
    decision = _apply_missing_action_override(
        decision,
        candidates,
        enabled=enable_missing_action_override,
    )

    selected_id = str(decision.get("selected_id") or "baseline")
    selected_candidate = next((item for item in candidates if item["id"] == selected_id), None)
    if selected_candidate is None:
        selected_id = "baseline"
        selected_result = baseline_result
        selected_family = "baseline"
        route = "baseline_gate"
    else:
        selected_result = selected_candidate["result"]
        selected_family = selected_candidate["family"]
        route = "llm_high_confidence_replace" if judge_mode == "llm" else f"{judge_mode}_replace"

    prediction = _make_prediction_row(case_id, user_request, selected_result, include_task_links=include_task_links)
    detail = {
        "case_id": case_id,
        "topology": topology,
        "selection_route": route,
        "selected_id": selected_id,
        "selected_family": selected_family,
        "candidate_count": len(raw_candidates),
        "viable_candidate_count": len(candidates),
        "available_candidates": [
            {
                "id": item["id"],
                "family": item["family"],
                "variant": item.get("variant"),
                "source_candidate_id": item.get("source_candidate_id"),
                "topology": _classify_topology(item["result"]),
                "node_count": len(_task_nodes(item["result"])),
                "edge_count": len(_graph_view(item["result"])["links"]),
                "diff_vs_baseline": item.get("diff_vs_baseline", {}),
                "warnings": item.get("filter_warnings", []),
            }
            for item in candidates
        ],
        "filtered_candidates": filtered,
        "judge": decision,
        "judge_before_override": decision_before_override,
        "raw_response": raw_response,
        "error": error,
    }
    return prediction, detail


def _write_jsonl_row(handle: Any, row: Dict[str, Any]) -> None:
    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    handle.flush()


def _summarize_details(detail_path: Path, summary_path: Path) -> Dict[str, Any]:
    details = _load_json_or_jsonl(detail_path) if detail_path.exists() else []
    topology_counts = Counter(str(row.get("topology", "")) for row in details)
    route_counts = Counter(str(row.get("selection_route", "")) for row in details)
    selected_family_counts = Counter(str(row.get("selected_family", "")) for row in details)
    replaced = [row for row in details if row.get("selected_family") not in {"", "baseline"}]
    summary = {
        "case_count": len(details),
        "topology_counts": dict(topology_counts),
        "selection_route_counts": dict(route_counts),
        "selected_family_counts": dict(selected_family_counts),
        "replacement_count": len(replaced),
        "replacement_rate": len(replaced) / len(details) if details else 0.0,
        "judge_error_count": sum(1 for row in details if row.get("error")),
        "mean_viable_candidate_count": (
            sum(int(row.get("viable_candidate_count") or 0) for row in details) / len(details)
            if details
            else 0.0
        ),
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


async def _run(args: argparse.Namespace) -> Dict[str, Any]:
    data_dir = _resolve_data_dir(args.data_dir)
    candidate_path = _resolve_existing_file(args.candidate_dump, data_dir=data_dir, label="candidate dump")
    baseline_path = _resolve_existing_file(args.baseline_predictions, data_dir=data_dir, label="baseline predictions")
    gold_path = _resolve_existing_file(args.gold_file, data_dir=data_dir, label="gold data")
    output_path = _resolve_output_file(args.output_path, data_dir=data_dir)
    detail_path = _resolve_output_file(args.detail_path, data_dir=data_dir)
    summary_path = _resolve_output_file(args.summary_path, data_dir=data_dir)

    chain_families = _normalize_family_list(args.chain_families, default=FAMILY_ORDER)
    dag_families = _normalize_family_list(args.dag_families, default=FAMILY_ORDER)

    gold_rows = _load_json_or_jsonl(gold_path)
    baseline_rows = _load_json_or_jsonl(baseline_path)
    candidate_rows = _load_candidate_pool(candidate_path)
    gold_by_id = {_case_id(row): row for row in gold_rows if _case_id(row)}
    baseline_by_id = {_case_id(row): row for row in baseline_rows if _case_id(row)}
    allowed_tools = _load_tool_names(data_dir)
    tool_type_map = _load_tool_type_map(data_dir)

    aligned_set = set(baseline_by_id) & set(candidate_rows)
    if args.require_gold:
        aligned_set &= set(gold_by_id)
    aligned_ids = sorted(aligned_set)
    if args.case_ids:
        requested_ids = {item.strip() for item in str(args.case_ids).split(",") if item.strip()}
        aligned_ids = [case_id for case_id in aligned_ids if case_id in requested_ids]

    offset = max(int(args.offset or 0), 0)
    limit = args.limit
    selected_ids = aligned_ids[offset:]
    if limit is not None:
        selected_ids = selected_ids[: max(int(limit), 0)]

    if args.dry_run_prompts:
        for case_id in selected_ids:
            baseline_result = _canonical_result(baseline_by_id[case_id])
            topology = _topology_for_case(
                case_id=case_id,
                gold_by_id=gold_by_id,
                baseline_result=baseline_result,
                source=args.topology_source,
            )
            if topology == "single":
                continue
            user_request = _user_request_for_case(
                case_id=case_id,
                candidate_rows=candidate_rows,
                baseline_by_id=baseline_by_id,
                gold_by_id=gold_by_id,
            )
            allowed_families = dag_families if topology == "dag" else chain_families
            candidates, filtered = _prepare_candidates(
                raw_candidates=candidate_rows[case_id],
                user_request=user_request,
                baseline_result=baseline_result,
                topology=topology,
                allowed_tools=allowed_tools,
                tool_type_map=tool_type_map,
                allowed_families=allowed_families,
                allow_task_drops=bool(args.allow_task_drops),
                allow_chain_edge_drops=bool(args.allow_chain_edge_drops),
                filter_redundant_bridges=bool(args.filter_redundant_bridges),
            )
            if not candidates:
                continue
            for item in candidates:
                item["diff_vs_baseline"] = _candidate_diff_against_baseline(
                    user_request=user_request,
                    baseline_result=baseline_result,
                    candidate_result=item["result"],
                )
            candidates, dag_edge_only_filtered = _filter_dag_edge_only_candidates(
                candidates,
                topology=topology,
                enabled=bool(args.filter_dag_edge_only_candidates),
            )
            filtered.extend(dag_edge_only_filtered)
            if not candidates:
                continue
            prompt = _build_judge_prompt(
                case_id=case_id,
                user_request=user_request,
                topology=topology,
                baseline_result=baseline_result,
                candidates=candidates,
            )
            print(prompt)
            print("\n# DRY RUN META #")
            print(
                json.dumps(
                    {
                        "case_id": case_id,
                        "topology": topology,
                        "viable_candidate_count": len(candidates),
                        "filtered_candidate_count": len(filtered),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return {"dry_run": True}
        print("[WARN] no non-single case found for dry run")
        return {"dry_run": True}

    client = None
    if args.judge_mode == "llm":
        runtime_config = PipelineOrchestratorAgent._resolve_llm_runtime_config(
            model_name=args.model_name,
            provider=args.provider,
            llm_profile=args.llm_profile,
            llm_config_path=args.llm_config_path,
        )
        client = PipelineOrchestratorAgent._build_llm_client(runtime_config)

    if not args.resume:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        detail_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("", encoding="utf-8")
        detail_path.write_text("", encoding="utf-8")

    done_ids = _read_done_ids(output_path) if args.resume else set()
    work_ids = [case_id for case_id in selected_ids if case_id not in done_ids]

    sem = asyncio.Semaphore(max(int(args.multiworker or 1), 1))

    async def _worker(case_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        async with sem:
            baseline_result = _canonical_result(baseline_by_id[case_id])
            topology = _topology_for_case(
                case_id=case_id,
                gold_by_id=gold_by_id,
                baseline_result=baseline_result,
                source=args.topology_source,
            )
            user_request = _user_request_for_case(
                case_id=case_id,
                candidate_rows=candidate_rows,
                baseline_by_id=baseline_by_id,
                gold_by_id=gold_by_id,
            )
            return await _rerank_case(
                case_id=case_id,
                user_request=user_request,
                topology=topology,
                baseline_result=baseline_result,
                raw_candidates=candidate_rows[case_id],
                allowed_tools=allowed_tools,
                tool_type_map=tool_type_map,
                chain_families=chain_families,
                dag_families=dag_families,
                allow_task_drops=bool(args.allow_task_drops),
                allow_chain_edge_drops=bool(args.allow_chain_edge_drops),
                filter_redundant_bridges=bool(args.filter_redundant_bridges),
                filter_dag_edge_only_candidates=bool(args.filter_dag_edge_only_candidates),
                enable_missing_action_override=bool(args.enable_missing_action_override),
                client=client,
                judge_mode=args.judge_mode,
                allow_medium_confidence=bool(args.allow_medium_confidence),
                max_retries=max(int(args.max_retries), 0),
                retry_sleep=float(args.retry_sleep),
                include_task_links=bool(args.include_task_links),
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    detail_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.resume else "w"
    success = 0
    with output_path.open(mode, encoding="utf-8") as pred_f, detail_path.open(mode, encoding="utf-8") as detail_f:
        tasks = [asyncio.create_task(_worker(case_id)) for case_id in work_ids]
        for task in asyncio.as_completed(tasks):
            prediction, detail = await task
            _write_jsonl_row(pred_f, prediction)
            _write_jsonl_row(detail_f, detail)
            success += 1
            if success % int(args.log_every) == 0 or success == len(work_ids):
                print(f"[INFO] progress={success}/{len(work_ids)} output={output_path}")

    summary = _summarize_details(detail_path, summary_path)
    print(f"[DONE] reranked={success}, skipped_existing={len(done_ids)}, output={output_path}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return {
        "output_path": str(output_path),
        "detail_path": str(detail_path),
        "summary_path": str(summary_path),
        "reranked": success,
        "skipped_existing": len(done_ids),
        "summary": summary,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Baseline-preserving reranker for orthogonal_v3 TaskBench candidate pools."
    )
    parser.add_argument("--data_dir", default="taskbench/data_multimedia")
    parser.add_argument(
        "--candidate_dump",
        default="candidate_dumps/pipeline_agent_qwen3-14b_20260529.jsonl",
    )
    parser.add_argument(
        "--baseline_predictions",
        default="predictions_use_demos_2_reformat_by_self/qwen3-14b_20260527.json",
    )
    parser.add_argument("--gold_file", default="data.json")
    parser.add_argument(
        "--output_path",
        default="predictions_pipeline_agent_rerank_v1/pipeline_agent_qwen3-14b_rerank_v1.json",
    )
    parser.add_argument(
        "--detail_path",
        default="candidate_dumps/rerank_v1/pipeline_agent_qwen3-14b_rerank_v1_details.jsonl",
    )
    parser.add_argument(
        "--summary_path",
        default="candidate_dumps/rerank_v1/pipeline_agent_qwen3-14b_rerank_v1_summary.json",
    )
    parser.add_argument("--provider", default="openai", choices=["openai", "tongyi", "gemini"])
    parser.add_argument("--model_name", default="qwen3-14b")
    parser.add_argument("--llm_profile", default=None)
    parser.add_argument("--llm_config_path", default="configs/qwen.json")
    parser.add_argument("--judge_mode", default="llm", choices=["llm", "baseline", "first_candidate"])
    parser.add_argument("--topology_source", default="gold", choices=["gold", "baseline", "auto"])
    parser.add_argument("--chain_families", default=DEFAULT_CHAIN_FAMILIES)
    parser.add_argument("--dag_families", default=DEFAULT_DAG_FAMILIES)
    parser.add_argument("--allow_medium_confidence", action="store_true", default=False)
    parser.add_argument("--allow_task_drops", action="store_true", default=False)
    parser.add_argument("--allow_chain_edge_drops", action="store_true", default=False)
    parser.add_argument("--filter_redundant_bridges", dest="filter_redundant_bridges", action="store_true", default=True)
    parser.add_argument("--allow_redundant_bridges", dest="filter_redundant_bridges", action="store_false")
    parser.add_argument("--filter_dag_edge_only_candidates", dest="filter_dag_edge_only_candidates", action="store_true", default=True)
    parser.add_argument("--allow_dag_edge_only_candidates", dest="filter_dag_edge_only_candidates", action="store_false")
    parser.add_argument(
        "--enable_missing_action_override",
        dest="enable_missing_action_override",
        action="store_true",
        default=True,
    )
    parser.add_argument(
        "--disable_missing_action_override",
        dest="enable_missing_action_override",
        action="store_false",
    )
    parser.add_argument("--require_gold", dest="require_gold", action="store_true", default=True)
    parser.add_argument("--allow_missing_gold", dest="require_gold", action="store_false")
    parser.add_argument("--include_task_links", action="store_true", default=False)
    parser.add_argument("--multiworker", type=int, default=4)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--case_ids", default="")
    parser.add_argument("--max_retries", type=int, default=2)
    parser.add_argument("--retry_sleep", type=float, default=2.0)
    parser.add_argument("--log_every", type=int, default=20)
    parser.add_argument("--resume", dest="resume", action="store_true", default=True)
    parser.add_argument("--no-resume", dest="resume", action="store_false")
    parser.add_argument("--dry_run_prompts", action="store_true", default=False)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
