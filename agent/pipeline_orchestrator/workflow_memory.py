from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .actions import _infer_skill_action_tags, _ordered_action_tags


NODE_REF_PATTERN = re.compile(r"<node-(\d+)>", re.IGNORECASE)
DEFAULT_TRUSTED_PRIOR_MIN_SUPPORT = 3
DEFAULT_TRUSTED_PRIOR_REQUIRE_ACTION_TAGS = True
DEFAULT_TRUSTED_PRIOR_DROP_EDGE_WITHOUT_ACTION_TAGS = True
DEFAULT_TRUSTED_PRIOR_ENFORCE_SCHEMA_COMPATIBILITY = True


def _load_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text[0] in {"[", "{", '"'}:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return text
    return value


def _normalize_task_name(value: Any) -> str:
    return str(value or "").strip()


def _iter_argument_values(value: Any) -> Iterable[str]:
    if value is None:
        return
    if isinstance(value, str):
        text = value.strip()
        if text:
            yield text
        return
    if isinstance(value, dict):
        for nested in value.values():
            yield from _iter_argument_values(nested)
        return
    if isinstance(value, (list, tuple, set)):
        for nested in value:
            yield from _iter_argument_values(nested)
        return
    text = str(value).strip()
    if text:
        yield text


def _extract_tasks(raw_nodes: Sequence[Any]) -> Tuple[str, ...]:
    return tuple(
        _normalize_task_name(node.get("task"))
        for node in raw_nodes
        if isinstance(node, dict) and _normalize_task_name(node.get("task"))
    )


def _collect_reference_values(raw_nodes: Sequence[Any], task_count: int) -> List[Tuple[int, int]]:
    references: List[Tuple[int, int]] = []
    for target_idx, node in enumerate(raw_nodes):
        if target_idx >= task_count or not isinstance(node, dict):
            continue
        arguments = _load_jsonish(node.get("arguments"))
        for text in _iter_argument_values(arguments):
            for match in NODE_REF_PATTERN.finditer(text):
                references.append((target_idx, int(match.group(1))))
    return references


def _infer_reference_base(
    references: Sequence[Tuple[int, int]],
    tasks: Sequence[str],
    raw_links: Sequence[Any],
) -> int:
    raw_link_pairs = {
        (
            _normalize_task_name(link.get("source")),
            _normalize_task_name(link.get("target")),
        )
        for link in raw_links
        if isinstance(link, dict)
        and _normalize_task_name(link.get("source"))
        and _normalize_task_name(link.get("target"))
    }
    zero_based_score = 0
    one_based_score = 0
    saw_zero = False

    for target_idx, ref_value in references:
        if ref_value == 0:
            saw_zero = True
        zero_source = ref_value
        if 0 <= zero_source < len(tasks) and zero_source != target_idx:
            if (tasks[zero_source], tasks[target_idx]) in raw_link_pairs:
                zero_based_score += 1
        one_source = ref_value - 1
        if 0 <= one_source < len(tasks) and one_source != target_idx:
            if (tasks[one_source], tasks[target_idx]) in raw_link_pairs:
                one_based_score += 1

    if one_based_score > zero_based_score:
        return 1
    if zero_based_score > one_based_score:
        return 0
    if saw_zero:
        return 0
    return 1


def _extract_reference_edges(
    raw_nodes: Sequence[Any],
    raw_links: Sequence[Any],
    tasks: Sequence[str],
) -> Set[Tuple[int, int]]:
    edges: Set[Tuple[int, int]] = set()
    references = _collect_reference_values(raw_nodes, len(tasks))
    if not references:
        return edges

    reference_base = _infer_reference_base(references, tasks, raw_links)
    for target_idx, ref_value in references:
        source_idx = ref_value if reference_base == 0 else ref_value - 1
        if 0 <= source_idx < len(tasks) and source_idx != target_idx:
            edges.add((source_idx, target_idx))
    return edges


def _resolve_name_link_to_index_pair(
    source_task: str,
    target_task: str,
    task_to_indices: Dict[str, List[int]],
) -> Optional[Tuple[int, int]]:
    source_candidates = task_to_indices.get(source_task, [])
    target_candidates = task_to_indices.get(target_task, [])
    if not source_candidates or not target_candidates:
        return None

    forward_pairs = [
        (source_idx, target_idx)
        for source_idx in source_candidates
        for target_idx in target_candidates
        if source_idx < target_idx
    ]
    if forward_pairs:
        return min(forward_pairs, key=lambda pair: (pair[1] - pair[0], pair[1], pair[0]))
    return (source_candidates[0], target_candidates[-1])


def _extract_index_edges(
    raw_nodes: Sequence[Any],
    raw_links: Sequence[Any],
    tasks: Sequence[str],
) -> Tuple[Tuple[int, int], ...]:
    reference_edges = _extract_reference_edges(raw_nodes, raw_links, tasks)
    resolved_edges: Set[Tuple[int, int]] = set(reference_edges)

    task_to_indices: Dict[str, List[int]] = defaultdict(list)
    for idx, task in enumerate(tasks):
        task_to_indices[task].append(idx)

    if isinstance(raw_links, list):
        for link in raw_links:
            if not isinstance(link, dict):
                continue
            source_task = _normalize_task_name(link.get("source"))
            target_task = _normalize_task_name(link.get("target"))
            if not source_task or not target_task:
                continue
            resolved_pair = _resolve_name_link_to_index_pair(source_task, target_task, task_to_indices)
            if resolved_pair is not None and resolved_pair[0] != resolved_pair[1]:
                resolved_edges.add(resolved_pair)

    return tuple(sorted(resolved_edges))


def _edge_task_pairs(tasks: Sequence[str], index_edges: Sequence[Tuple[int, int]]) -> Tuple[Tuple[str, str], ...]:
    pairs: List[Tuple[str, str]] = []
    for source_idx, target_idx in index_edges:
        if not (0 <= source_idx < len(tasks) and 0 <= target_idx < len(tasks)):
            continue
        source_task = _normalize_task_name(tasks[source_idx])
        target_task = _normalize_task_name(tasks[target_idx])
        if source_task and target_task:
            pairs.append((source_task, target_task))
    return tuple(pairs)


def _graph_boundary_indices(node_count: int, index_edges: Sequence[Tuple[int, int]]) -> Tuple[List[int], List[int]]:
    if node_count <= 0:
        return [], []

    incoming = [0] * node_count
    outgoing = [0] * node_count
    for source_idx, target_idx in index_edges:
        if 0 <= source_idx < node_count and 0 <= target_idx < node_count:
            outgoing[source_idx] += 1
            incoming[target_idx] += 1

    roots = [idx for idx in range(node_count) if incoming[idx] == 0]
    leaves = [idx for idx in range(node_count) if outgoing[idx] == 0]
    if not index_edges:
        return [0], [node_count - 1]
    return roots, leaves


def _action_tags_for_tasks(tasks: Sequence[str]) -> Tuple[str, ...]:
    return tuple(
        _ordered_action_tags(
            {
                tag
                for task in tasks
                for tag in _infer_skill_action_tags(task)
            }
        )
    )


def _normalize_task_name_for_inference(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[\(\)\[\]\{\}/]+", " ", text)
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _ordered_modalities(values: Iterable[str]) -> Tuple[str, ...]:
    canonical_order = ("text", "audio", "image", "video")
    normalized = {str(value).strip().lower() for value in values if str(value).strip()}
    ordered = [modality for modality in canonical_order if modality in normalized]
    extras = sorted(modality for modality in normalized if modality not in canonical_order)
    return tuple(ordered + extras)


def _infer_task_modalities(task_name: str) -> Dict[str, Tuple[str, ...]]:
    text = _normalize_task_name_for_inference(task_name)
    modality_pattern = r"(audio|video|image|text)"

    conversion_match = re.search(
        rf"\b(?P<input>{modality_pattern})\b\s+(?:to|2)\s+\b(?P<output>{modality_pattern})\b",
        text,
    )
    if conversion_match:
        return {
            "inputs": (conversion_match.group("input"),),
            "outputs": (conversion_match.group("output"),),
        }

    if "video voiceover" in text:
        return {"inputs": ("video", "audio", "text"), "outputs": ("video",)}
    if "video synchronization" in text:
        return {"inputs": ("video", "audio"), "outputs": ("video",)}
    if "audio effects" in text or ("effect" in text and "audio" in text):
        return {"inputs": ("audio", "text"), "outputs": ("audio",)}
    if "voice changer" in text:
        return {"inputs": ("audio", "text"), "outputs": ("audio",)}
    if "audio noise reduction" in text or ("noise reduction" in text and "audio" in text):
        return {"inputs": ("audio",), "outputs": ("audio",)}
    if "audio splicer" in text:
        return {"inputs": ("audio",), "outputs": ("audio",)}
    if "image stitcher" in text or "image style transfer" in text or "image colorizer" in text:
        return {"inputs": ("image",), "outputs": ("image",)}
    if "video stabilizer" in text or "video speed" in text:
        return {"inputs": ("video",), "outputs": ("video",)}
    if "url extractor" in text:
        return {"inputs": ("text",), "outputs": ("text",)}
    if "text downloader" in text:
        return {"inputs": ("text",), "outputs": ("text",)}
    if "audio downloader" in text:
        return {"inputs": ("text",), "outputs": ("audio",)}
    if "image downloader" in text:
        return {"inputs": ("text",), "outputs": ("image",)}
    if "video downloader" in text:
        return {"inputs": ("text",), "outputs": ("video",)}
    if "text search" in text:
        return {"inputs": ("text",), "outputs": ("text",)}
    if "video search" in text:
        return {"inputs": ("text",), "outputs": ("video",)}
    if "audio search" in text:
        return {"inputs": ("text",), "outputs": ("audio",)}
    if "image search" in text:
        if "by image" in text:
            return {"inputs": ("image",), "outputs": ("image",)}
        return {"inputs": ("text",), "outputs": ("image",)}
    if (
        "text simplifier" in text
        or "text summarizer" in text
        or "text grammar checker" in text
        or "text sentiment analysis" in text
        or "keyword extractor" in text
        or "topic generator" in text
        or "text expander" in text
        or "article spinner" in text
        or "text paraphraser" in text
        or "text translator" in text
    ):
        return {"inputs": ("text",), "outputs": ("text",)}

    found_modalities = re.findall(rf"\b{modality_pattern}\b", text)
    unique_modalities = _ordered_modalities(found_modalities)
    if len(unique_modalities) == 1:
        modality = unique_modalities[0]
        return {"inputs": (modality,), "outputs": (modality,)}

    return {"inputs": tuple(), "outputs": tuple()}


def _edge_action_tags(source_task: str, target_task: str) -> Tuple[str, ...]:
    return tuple(
        _ordered_action_tags(
            set(_infer_skill_action_tags(source_task)) | set(_infer_skill_action_tags(target_task))
        )
    )


def _edge_is_schema_compatible(source_task: str, target_task: str) -> bool:
    source_modalities = _infer_task_modalities(source_task)
    target_modalities = _infer_task_modalities(target_task)
    source_outputs = set(source_modalities.get("outputs", tuple()))
    target_inputs = set(target_modalities.get("inputs", tuple()))
    if not source_outputs or not target_inputs:
        return True
    return bool(source_outputs & target_inputs)


def _extract_path_motifs(
    tasks: Sequence[str],
    index_edges: Sequence[Tuple[int, int]],
    *,
    max_motif_size: int,
) -> List[Tuple[Tuple[str, ...], Tuple[Tuple[str, str], ...]]]:
    if not tasks:
        return []

    max_size = max(2, int(max_motif_size))
    if not index_edges:
        motifs: List[Tuple[Tuple[str, ...], Tuple[Tuple[str, str], ...]]] = []
        for size in range(2, min(len(tasks), max_size) + 1):
            for start_idx in range(0, len(tasks) - size + 1):
                motif_tasks = tuple(tasks[start_idx : start_idx + size])
                motif_links = tuple((tasks[idx], tasks[idx + 1]) for idx in range(start_idx, start_idx + size - 1))
                motifs.append((motif_tasks, motif_links))
        return motifs

    adjacency: Dict[int, List[int]] = defaultdict(list)
    for source_idx, target_idx in sorted(index_edges):
        adjacency[source_idx].append(target_idx)

    motifs = []

    def _dfs(path: List[int]) -> None:
        if 2 <= len(path) <= max_size:
            motif_tasks = tuple(tasks[idx] for idx in path)
            motif_links = tuple((tasks[path[idx]], tasks[path[idx + 1]]) for idx in range(len(path) - 1))
            motifs.append((motif_tasks, motif_links))
        if len(path) >= max_size:
            return
        for next_idx in adjacency.get(path[-1], []):
            if next_idx in path:
                continue
            _dfs(path + [next_idx])

    for start_idx in range(len(tasks)):
        _dfs([start_idx])

    return motifs


def load_case_id_file(path: Path) -> Set[str]:
    case_ids: Set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        case_ids.add(text)
    return case_ids


def assign_case_id_to_fold(case_id: str, num_folds: int) -> int:
    if num_folds < 2:
        raise ValueError("num_folds must be >= 2")
    text = str(case_id).strip()
    if not text:
        raise ValueError("case_id must be non-empty for fold assignment")
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % num_folds


def select_taskbench_records(
    records: Iterable[Dict[str, Any]],
    *,
    include_ids: Optional[Set[str]] = None,
    exclude_ids: Optional[Set[str]] = None,
    num_folds: Optional[int] = None,
    fold_index: Optional[int] = None,
    fold_mode: str = "exclude",
) -> List[Dict[str, Any]]:
    if (num_folds is None) != (fold_index is None):
        raise ValueError("num_folds and fold_index must be provided together")
    if num_folds is not None and num_folds < 2:
        raise ValueError("num_folds must be >= 2")
    if fold_index is not None and (fold_index < 0 or (num_folds is not None and fold_index >= num_folds)):
        raise ValueError("fold_index must be within [0, num_folds)")
    if fold_mode not in {"include", "exclude"}:
        raise ValueError("fold_mode must be 'include' or 'exclude'")

    selected: List[Dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue

        case_id = str(record.get("id", "")).strip()
        if include_ids is not None and case_id not in include_ids:
            continue
        if exclude_ids is not None and case_id in exclude_ids:
            continue

        if num_folds is not None:
            if not case_id:
                continue
            in_fold = assign_case_id_to_fold(case_id, num_folds) == fold_index
            if fold_mode == "include" and not in_fold:
                continue
            if fold_mode == "exclude" and in_fold:
                continue

        selected.append(record)
    return selected


@dataclass(frozen=True)
class WorkflowMemoryMotif:
    motif_id: str
    tasks: Tuple[str, ...]
    links: Tuple[Tuple[str, str], ...]
    action_tags: Tuple[str, ...]
    support: int


@dataclass(frozen=True)
class WorkflowStartPrior:
    skill: str
    support: int
    probability: float
    action_tags: Tuple[str, ...]


@dataclass(frozen=True)
class WorkflowTransitionPrior:
    source: str
    target: str
    support: int
    probability: float
    action_tags: Tuple[str, ...]


class WorkflowMemoryIndex:
    def __init__(
        self,
        *,
        motifs: Sequence[WorkflowMemoryMotif],
        transition_counts: Optional[Dict[Tuple[str, str], int]] = None,
        start_counts: Optional[Dict[str, int]] = None,
        end_counts: Optional[Dict[str, int]] = None,
        motif_prior: Optional[Sequence[WorkflowMemoryMotif]] = None,
        start_prior: Optional[Sequence[WorkflowStartPrior]] = None,
        transition_prior: Optional[Sequence[WorkflowTransitionPrior]] = None,
        prior_filters: Optional[Dict[str, Any]] = None,
        prior_stats: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.motifs: List[WorkflowMemoryMotif] = list(motifs)
        self.transition_counts: Dict[Tuple[str, str], int] = dict(transition_counts or {})
        self.start_counts: Dict[str, int] = dict(start_counts or {})
        self.end_counts: Dict[str, int] = dict(end_counts or {})
        self.prior_filters: Dict[str, Any] = self._normalize_prior_filters(prior_filters)

        computed_motif_prior: Optional[List[WorkflowMemoryMotif]] = None
        computed_start_prior: Optional[List[WorkflowStartPrior]] = None
        computed_transition_prior: Optional[List[WorkflowTransitionPrior]] = None
        computed_stats: Optional[Dict[str, Any]] = None
        if motif_prior is None or start_prior is None or transition_prior is None or prior_stats is None:
            (
                computed_motif_prior,
                computed_start_prior,
                computed_transition_prior,
                computed_stats,
            ) = self._build_trusted_prior_tables(
                self.motifs,
                prior_filters=self.prior_filters,
                raw_start_counts=self.start_counts,
            )

        self.motif_prior: List[WorkflowMemoryMotif] = list(
            motif_prior if motif_prior is not None else computed_motif_prior or []
        )
        self.start_prior: List[WorkflowStartPrior] = list(
            start_prior if start_prior is not None else computed_start_prior or []
        )
        self.transition_prior: List[WorkflowTransitionPrior] = list(
            transition_prior if transition_prior is not None else computed_transition_prior or []
        )
        self.prior_stats: Dict[str, Any] = dict(prior_stats if prior_stats is not None else computed_stats or {})

    @staticmethod
    def _default_prior_filters() -> Dict[str, Any]:
        return {
            "min_support": DEFAULT_TRUSTED_PRIOR_MIN_SUPPORT,
            "require_action_tags": DEFAULT_TRUSTED_PRIOR_REQUIRE_ACTION_TAGS,
            "drop_edge_without_action_tags": DEFAULT_TRUSTED_PRIOR_DROP_EDGE_WITHOUT_ACTION_TAGS,
            "enforce_schema_compatibility": DEFAULT_TRUSTED_PRIOR_ENFORCE_SCHEMA_COMPATIBILITY,
        }

    @classmethod
    def _normalize_prior_filters(cls, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        filters = cls._default_prior_filters()
        if isinstance(payload, dict):
            filters.update(
                {
                    "min_support": max(1, int(payload.get("min_support", filters["min_support"]))),
                    "require_action_tags": bool(payload.get("require_action_tags", filters["require_action_tags"])),
                    "drop_edge_without_action_tags": bool(
                        payload.get("drop_edge_without_action_tags", filters["drop_edge_without_action_tags"])
                    ),
                    "enforce_schema_compatibility": bool(
                        payload.get("enforce_schema_compatibility", filters["enforce_schema_compatibility"])
                    ),
                }
            )
        return filters

    @staticmethod
    def _build_trusted_prior_tables(
        motifs: Sequence[WorkflowMemoryMotif],
        *,
        prior_filters: Dict[str, Any],
        raw_start_counts: Optional[Dict[str, int]] = None,
    ) -> Tuple[List[WorkflowMemoryMotif], List[WorkflowStartPrior], List[WorkflowTransitionPrior], Dict[str, Any]]:
        min_support = max(1, int(prior_filters.get("min_support", DEFAULT_TRUSTED_PRIOR_MIN_SUPPORT)))
        require_action_tags = bool(prior_filters.get("require_action_tags", DEFAULT_TRUSTED_PRIOR_REQUIRE_ACTION_TAGS))
        drop_edge_without_action_tags = bool(
            prior_filters.get(
                "drop_edge_without_action_tags",
                DEFAULT_TRUSTED_PRIOR_DROP_EDGE_WITHOUT_ACTION_TAGS,
            )
        )
        enforce_schema_compatibility = bool(
            prior_filters.get(
                "enforce_schema_compatibility",
                DEFAULT_TRUSTED_PRIOR_ENFORCE_SCHEMA_COMPATIBILITY,
            )
        )

        trusted_motifs: List[WorkflowMemoryMotif] = []
        transition_support: Counter[Tuple[str, str]] = Counter()
        transition_action_tags: Dict[Tuple[str, str], Set[str]] = defaultdict(set)

        stats: Dict[str, Any] = {
            "raw_motif_count": len(motifs),
            "raw_transition_instance_count": sum(len(motif.links) for motif in motifs),
            "dropped_low_support": 0,
            "dropped_missing_action_tags": 0,
            "dropped_edge_without_action_tags": 0,
            "dropped_incompatible_edge": 0,
        }

        for motif in motifs:
            inferred_motif_tags = tuple(
                _ordered_action_tags(
                    set(motif.action_tags)
                    | {
                        tag
                        for task in motif.tasks
                        for tag in _infer_skill_action_tags(task)
                    }
                )
            )
            if int(motif.support) < min_support:
                stats["dropped_low_support"] += 1
                continue
            if require_action_tags and not inferred_motif_tags:
                stats["dropped_missing_action_tags"] += 1
                continue

            trusted_edges: List[Tuple[str, str, Tuple[str, ...]]] = []
            motif_rejected = False
            for source_task, target_task in motif.links:
                edge_tags = _edge_action_tags(source_task, target_task)
                if enforce_schema_compatibility and not _edge_is_schema_compatible(source_task, target_task):
                    stats["dropped_incompatible_edge"] += 1
                    motif_rejected = True
                    break
                if drop_edge_without_action_tags and not edge_tags:
                    stats["dropped_edge_without_action_tags"] += 1
                    continue
                trusted_edges.append((source_task, target_task, edge_tags))

            if motif_rejected:
                continue

            trusted_motif = WorkflowMemoryMotif(
                motif_id=motif.motif_id,
                tasks=motif.tasks,
                links=motif.links,
                action_tags=inferred_motif_tags,
                support=int(motif.support),
            )
            trusted_motifs.append(trusted_motif)

            for source_task, target_task, edge_tags in trusted_edges:
                transition_support[(source_task, target_task)] += trusted_motif.support
                transition_action_tags[(source_task, target_task)].update(edge_tags)

        trusted_start_skills = {
            motif.tasks[0]
            for motif in trusted_motifs
            if motif.tasks and motif.tasks[0]
        }
        start_support: Counter[str] = Counter()
        if raw_start_counts:
            for skill, support in raw_start_counts.items():
                if skill in trusted_start_skills and int(support) >= min_support:
                    start_support[skill] += int(support)
        else:
            for skill in trusted_start_skills:
                start_support[skill] = sum(
                    motif.support
                    for motif in trusted_motifs
                    if motif.tasks and motif.tasks[0] == skill
                )

        total_start_support = float(sum(start_support.values()) or 1.0)
        start_prior = [
            WorkflowStartPrior(
                skill=skill,
                support=int(support),
                probability=float(support) / total_start_support,
                action_tags=tuple(_infer_skill_action_tags(skill)),
            )
            for skill, support in sorted(
                start_support.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]

        outgoing_support: Counter[str] = Counter()
        for (source_task, _), support in transition_support.items():
            outgoing_support[source_task] += support

        transition_prior = [
            WorkflowTransitionPrior(
                source=source_task,
                target=target_task,
                support=int(support),
                probability=float(support) / float(outgoing_support[source_task] or 1.0),
                action_tags=tuple(_ordered_action_tags(transition_action_tags.get((source_task, target_task), set()))),
            )
            for (source_task, target_task), support in sorted(
                transition_support.items(),
                key=lambda item: (-item[1], item[0][0], item[0][1]),
            )
        ]

        stats["trusted_motif_count"] = len(trusted_motifs)
        stats["trusted_start_count"] = len(start_prior)
        stats["trusted_transition_count"] = len(transition_prior)
        stats["trusted_start_support_total"] = int(sum(start_support.values()))
        stats["trusted_transition_support_total"] = int(sum(transition_support.values()))
        return trusted_motifs, start_prior, transition_prior, stats

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "WorkflowMemoryIndex":
        raw_motifs = payload.get("motifs", [])
        raw_transition_counts = payload.get("transition_counts", [])
        raw_start_counts = payload.get("start_counts", {})
        raw_end_counts = payload.get("end_counts", {})
        raw_trusted_priors = payload.get("trusted_priors", {})

        motifs = [
            WorkflowMemoryMotif(
                motif_id=str(item.get("motif_id", "")),
                tasks=tuple(_normalize_task_name(task) for task in item.get("tasks", []) if _normalize_task_name(task)),
                links=tuple(
                    (
                        _normalize_task_name(link[0]),
                        _normalize_task_name(link[1]),
                    )
                    for link in item.get("links", [])
                    if isinstance(link, (list, tuple)) and len(link) == 2
                ),
                action_tags=tuple(str(tag) for tag in item.get("action_tags", []) if str(tag).strip()),
                support=int(item.get("support", 0)),
            )
            for item in raw_motifs
            if isinstance(item, dict)
        ]

        transition_counts: Dict[Tuple[str, str], int] = {}
        for item in raw_transition_counts:
            if not isinstance(item, dict):
                continue
            source = _normalize_task_name(item.get("source"))
            target = _normalize_task_name(item.get("target"))
            if not source or not target:
                continue
            transition_counts[(source, target)] = int(item.get("count", 0))

        start_counts = {str(k): int(v) for k, v in dict(raw_start_counts).items()} if isinstance(raw_start_counts, dict) else {}
        end_counts = {str(k): int(v) for k, v in dict(raw_end_counts).items()} if isinstance(raw_end_counts, dict) else {}

        prior_filters = {}
        prior_stats = {}
        motif_prior: Optional[List[WorkflowMemoryMotif]] = None
        start_prior: Optional[List[WorkflowStartPrior]] = None
        transition_prior: Optional[List[WorkflowTransitionPrior]] = None
        if isinstance(raw_trusted_priors, dict):
            prior_filters = dict(raw_trusted_priors.get("config", {})) if isinstance(raw_trusted_priors.get("config", {}), dict) else {}
            prior_stats = dict(raw_trusted_priors.get("stats", {})) if isinstance(raw_trusted_priors.get("stats", {}), dict) else {}

            if "motif_prior" in raw_trusted_priors and isinstance(raw_trusted_priors.get("motif_prior"), list):
                raw_motif_prior = raw_trusted_priors.get("motif_prior", [])
                motif_prior = [
                    WorkflowMemoryMotif(
                        motif_id=str(item.get("motif_id", "")),
                        tasks=tuple(
                            _normalize_task_name(task)
                            for task in item.get("tasks", [])
                            if _normalize_task_name(task)
                        ),
                        links=tuple(
                            (
                                _normalize_task_name(link[0]),
                                _normalize_task_name(link[1]),
                            )
                            for link in item.get("links", [])
                            if isinstance(link, (list, tuple)) and len(link) == 2
                        ),
                        action_tags=tuple(
                            str(tag)
                            for tag in item.get("action_tags", item.get("scenario_tags", []))
                            if str(tag).strip()
                        ),
                        support=int(item.get("support", 0)),
                    )
                    for item in raw_motif_prior
                    if isinstance(item, dict)
                ]

            if "start_prior" in raw_trusted_priors and isinstance(raw_trusted_priors.get("start_prior"), list):
                raw_start_prior = raw_trusted_priors.get("start_prior", [])
                start_prior = [
                    WorkflowStartPrior(
                        skill=_normalize_task_name(item.get("skill")),
                        support=int(item.get("support", 0)),
                        probability=float(item.get("probability", 0.0)),
                        action_tags=tuple(str(tag) for tag in item.get("action_tags", []) if str(tag).strip()),
                    )
                    for item in raw_start_prior
                    if isinstance(item, dict) and _normalize_task_name(item.get("skill"))
                ]

            if "transition_prior" in raw_trusted_priors and isinstance(raw_trusted_priors.get("transition_prior"), list):
                raw_transition_prior = raw_trusted_priors.get("transition_prior", [])
                transition_prior = [
                    WorkflowTransitionPrior(
                        source=_normalize_task_name(item.get("source")),
                        target=_normalize_task_name(item.get("target")),
                        support=int(item.get("support", 0)),
                        probability=float(item.get("probability", 0.0)),
                        action_tags=tuple(str(tag) for tag in item.get("action_tags", []) if str(tag).strip()),
                    )
                    for item in raw_transition_prior
                    if isinstance(item, dict)
                    and _normalize_task_name(item.get("source"))
                    and _normalize_task_name(item.get("target"))
                ]

        return cls(
            motifs=motifs,
            transition_counts=transition_counts,
            start_counts=start_counts,
            end_counts=end_counts,
            motif_prior=motif_prior,
            start_prior=start_prior,
            transition_prior=transition_prior,
            prior_filters=prior_filters,
            prior_stats=prior_stats,
        )

    @classmethod
    def from_json(cls, path: Path) -> "WorkflowMemoryIndex":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("workflow memory file must contain a JSON object")
        return cls.from_dict(payload)

    def to_dict(self) -> Dict[str, Any]:
        transition_counts = [
            {"source": source, "target": target, "count": count}
            for (source, target), count in sorted(
                self.transition_counts.items(),
                key=lambda item: (-item[1], item[0][0], item[0][1]),
            )
        ]
        return {
            "version": 4,
            "motifs": [
                {
                    "motif_id": motif.motif_id,
                    "tasks": list(motif.tasks),
                    "links": [list(link) for link in motif.links],
                    "action_tags": list(motif.action_tags),
                    "support": motif.support,
                }
                for motif in self.motifs
            ],
            "transition_counts": transition_counts,
            "start_counts": dict(sorted(self.start_counts.items())),
            "end_counts": dict(sorted(self.end_counts.items())),
            "trusted_priors": {
                "config": dict(self.prior_filters),
                "stats": dict(self.prior_stats),
                "start_prior": [
                    {
                        "skill": item.skill,
                        "support": item.support,
                        "probability": item.probability,
                        "action_tags": list(item.action_tags),
                    }
                    for item in self.start_prior
                ],
                "transition_prior": [
                    {
                        "source": item.source,
                        "target": item.target,
                        "support": item.support,
                        "probability": item.probability,
                        "action_tags": list(item.action_tags),
                    }
                    for item in self.transition_prior
                ],
                "motif_prior": [
                    {
                        "motif_id": motif.motif_id,
                        "tasks": list(motif.tasks),
                        "links": [list(link) for link in motif.links],
                        "action_tags": list(motif.action_tags),
                        "scenario_tags": list(motif.action_tags),
                        "support": motif.support,
                    }
                    for motif in self.motif_prior
                ],
            },
        }

    def to_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _iter_json_records(path: Path) -> Iterable[Dict[str, Any]]:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return []
        if text.startswith("["):
            payload = json.loads(text)
            if not isinstance(payload, list):
                raise ValueError("JSON array input expected when file starts with '['")
            return [item for item in payload if isinstance(item, dict)]
        records: List[Dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                records.append(item)
        return records

    @classmethod
    def load_taskbench_records(cls, path: Path) -> List[Dict[str, Any]]:
        return list(cls._iter_json_records(path))

    @classmethod
    def build_from_taskbench_path(
        cls,
        path: Path,
        *,
        source_name: str = "taskbench",
        max_motif_size: int = 4,
        trusted_min_support: int = DEFAULT_TRUSTED_PRIOR_MIN_SUPPORT,
    ) -> "WorkflowMemoryIndex":
        return cls.build_from_taskbench_records(
            cls._iter_json_records(path),
            source_name=source_name,
            max_motif_size=max_motif_size,
            trusted_min_support=trusted_min_support,
        )

    @classmethod
    def build_from_taskbench_records(
        cls,
        records: Iterable[Dict[str, Any]],
        *,
        source_name: str = "taskbench",
        max_motif_size: int = 4,
        trusted_min_support: int = DEFAULT_TRUSTED_PRIOR_MIN_SUPPORT,
    ) -> "WorkflowMemoryIndex":
        transition_counts: Counter[Tuple[str, str]] = Counter()
        start_counts: Counter[str] = Counter()
        end_counts: Counter[str] = Counter()
        motif_support: Counter[Tuple[str, ...]] = Counter()
        motif_links_by_tasks: Dict[Tuple[str, ...], Tuple[Tuple[str, str], ...]] = {}

        for record in records:
            if not isinstance(record, dict):
                continue

            raw_nodes = _load_jsonish(record.get("tool_nodes") or record.get("task_nodes") or [])
            raw_links = _load_jsonish(record.get("tool_links") or record.get("task_links") or [])

            if not isinstance(raw_nodes, list) or not raw_nodes:
                continue

            tasks = _extract_tasks(raw_nodes)
            if not tasks:
                continue

            index_edges = _extract_index_edges(raw_nodes, raw_links if isinstance(raw_links, list) else [], tasks)
            links = _edge_task_pairs(tasks, index_edges)
            for edge in links:
                transition_counts[edge] += 1

            action_tags = _action_tags_for_tasks(tasks)

            root_indices, leaf_indices = _graph_boundary_indices(len(tasks), index_edges)
            for root_idx in root_indices:
                start_counts[tasks[root_idx]] += 1
            for leaf_idx in leaf_indices:
                end_counts[tasks[leaf_idx]] += 1

            for motif_tasks, motif_links in _extract_path_motifs(tasks, index_edges, max_motif_size=max_motif_size):
                motif_support[motif_tasks] += 1
                motif_links_by_tasks.setdefault(motif_tasks, motif_links)

        motifs: List[WorkflowMemoryMotif] = []
        for motif_tasks, support in motif_support.items():
            motif_tags = _action_tags_for_tasks(motif_tasks)
            motif_id = " -> ".join(motif_tasks)
            motifs.append(
                WorkflowMemoryMotif(
                    motif_id=motif_id,
                    tasks=motif_tasks,
                    links=motif_links_by_tasks.get(motif_tasks, tuple()),
                    action_tags=tuple(motif_tags),
                    support=int(support),
                )
            )

        motifs.sort(key=lambda motif: (-motif.support, -len(motif.tasks), motif.motif_id))
        return cls(
            motifs=motifs,
            transition_counts=dict(transition_counts),
            start_counts=dict(start_counts),
            end_counts=dict(end_counts),
            prior_filters={"min_support": max(1, int(trusted_min_support))},
        )
