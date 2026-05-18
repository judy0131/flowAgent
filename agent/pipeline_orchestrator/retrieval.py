from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .actions import _infer_skill_action_tags
from .workflow_memory import WorkflowMemoryIndex, WorkflowMemoryMotif


STOPWORDS: Set[str] = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "based",
    "by",
    "for",
    "from",
    "help",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "please",
    "that",
    "the",
    "then",
    "this",
    "to",
    "use",
    "using",
    "want",
    "with",
}


NODE_REF_PATTERN = re.compile(r"<node-(\d+)>", re.IGNORECASE)


def _tokenize(text: str) -> Set[str]:
    raw_tokens = re.findall(r"[a-z0-9]+", str(text or "").lower())
    return {token for token in raw_tokens if len(token) >= 3 and token not in STOPWORDS}


def _task_tokens(tasks: Sequence[str]) -> Set[str]:
    tokens: Set[str] = set()
    for task in tasks:
        tokens.update(_tokenize(task.replace("-", " ")))
    return tokens


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


def _extract_candidate_index_edges(compiled_nodes: Sequence[Dict[str, Any]]) -> Tuple[Tuple[int, int], ...]:
    reference_values: List[Tuple[int, int]] = []
    saw_zero = False
    saw_positive = False
    for target_idx, node in enumerate(compiled_nodes):
        if not isinstance(node, dict):
            continue
        arguments = node.get("arguments")
        for text in _iter_argument_values(arguments):
            for match in NODE_REF_PATTERN.finditer(text):
                ref_value = int(match.group(1))
                reference_values.append((target_idx, ref_value))
                if ref_value == 0:
                    saw_zero = True
                elif ref_value > 0:
                    saw_positive = True

    edges: Set[Tuple[int, int]] = set()
    reference_base = 0 if saw_zero or not saw_positive else 1
    for target_idx, ref_value in reference_values:
        source_idx = ref_value if reference_base == 0 else ref_value - 1
        if 0 <= source_idx < len(compiled_nodes) and source_idx != target_idx:
            edges.add((source_idx, target_idx))
    if edges:
        return tuple(sorted(edges))
    return tuple((idx - 1, idx) for idx in range(1, len(compiled_nodes)))


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


def _extract_candidate_paths(
    task_names: Sequence[str],
    index_edges: Sequence[Tuple[int, int]],
    *,
    max_path_len: int,
) -> Set[Tuple[str, ...]]:
    if len(task_names) < 2:
        return set()
    if not index_edges:
        return {
            tuple(task_names[start_idx : start_idx + size])
            for size in range(2, min(len(task_names), max_path_len) + 1)
            for start_idx in range(0, len(task_names) - size + 1)
        }

    adjacency: Dict[int, List[int]] = defaultdict(list)
    for source_idx, target_idx in index_edges:
        adjacency[source_idx].append(target_idx)

    paths: Set[Tuple[str, ...]] = set()

    def _dfs(path: List[int]) -> None:
        if 2 <= len(path) <= max_path_len:
            paths.add(tuple(task_names[idx] for idx in path))
        if len(path) >= max_path_len:
            return
        for next_idx in adjacency.get(path[-1], []):
            if next_idx in path:
                continue
            _dfs(path + [next_idx])

    for start_idx in range(len(task_names)):
        _dfs([start_idx])

    return paths


class WorkflowMemoryRetriever:
    def __init__(self, memory_index: WorkflowMemoryIndex):
        self.memory_index = memory_index
        self._motif_tokens: Dict[str, Set[str]] = {
            motif.motif_id: (_task_tokens(motif.tasks) | set(motif.action_tags))
            for motif in memory_index.motifs
        }
        all_tools: Set[str] = set(memory_index.start_counts.keys()) | set(memory_index.end_counts.keys())
        for edge in memory_index.transition_counts.keys():
            all_tools.update(edge)
        for motif in memory_index.motifs:
            all_tools.update(motif.tasks)
        self._transition_tokens: Dict[Tuple[str, str], Set[str]] = {
            edge: (_task_tokens(edge) | _tokenize(" ".join(edge)))
            for edge in memory_index.transition_counts.keys()
        }
        self._tool_tokens: Dict[str, Set[str]] = {
            tool: _tokenize(tool.replace("-", " "))
            for tool in all_tools
        }
        self._start_tool_tokens: Dict[str, Set[str]] = {
            tool: _tokenize(tool.replace("-", " "))
            for tool in memory_index.start_counts.keys()
        }
        self._end_tool_tokens: Dict[str, Set[str]] = {
            tool: _tokenize(tool.replace("-", " "))
            for tool in memory_index.end_counts.keys()
        }
        self._motifs_by_start: Dict[str, List[WorkflowMemoryMotif]] = defaultdict(list)
        self._motif_support_by_edge: Dict[Tuple[str, str], float] = defaultdict(float)
        for motif in memory_index.motifs:
            if motif.tasks:
                self._motifs_by_start[motif.tasks[0]].append(motif)
            for edge in motif.links:
                self._motif_support_by_edge[edge] += float(max(motif.support, 0))
        for motifs in self._motifs_by_start.values():
            motifs.sort(key=lambda item: (-item.support, -len(item.tasks), item.motif_id))

    @staticmethod
    def _overlap_ratio(left: Set[str], right: Set[str]) -> float:
        if not left or not right:
            return 0.0
        overlap = len(left & right)
        return overlap / float(max(len(left), len(right), 1))

    def _score_motif(
        self,
        motif: WorkflowMemoryMotif,
        *,
        query_tokens: Set[str],
        detected_actions: Set[str],
    ) -> float:
        motif_tokens = self._motif_tokens.get(motif.motif_id, set())
        action_overlap = len(detected_actions & set(motif.action_tags))
        token_overlap = self._overlap_ratio(query_tokens, motif_tokens)
        support_bonus = min(float(motif.support), 10.0) * 0.2
        return float(action_overlap * 2.0 + token_overlap * 4.0 + support_bonus)

    def _score_transition(
        self,
        edge: Tuple[str, str],
        *,
        query_tokens: Set[str],
        detected_actions: Set[str],
    ) -> float:
        edge_tokens = self._transition_tokens.get(edge, set())
        transition_actions = {
            tag
            for task in edge
            for tag in _infer_skill_action_tags(task)
        }
        action_overlap = len(detected_actions & transition_actions)
        token_overlap = self._overlap_ratio(query_tokens, edge_tokens)
        count_bonus = min(float(self.memory_index.transition_counts.get(edge, 0)), 10.0) * 0.15
        return float(action_overlap * 1.5 + token_overlap * 3.0 + count_bonus)

    def _score_boundary_tool(
        self,
        tool: str,
        *,
        query_tokens: Set[str],
        count: int,
        token_cache: Dict[str, Set[str]],
    ) -> float:
        tool_tokens = token_cache.get(tool, set())
        token_overlap = self._overlap_ratio(query_tokens, tool_tokens)
        count_bonus = min(float(count), 10.0) * 0.2
        return float(token_overlap * 3.0 + count_bonus)

    @staticmethod
    def _normalize_detected_actions(detected_actions: Optional[Iterable[str]]) -> Set[str]:
        return {str(action).strip() for action in (detected_actions or []) if str(action).strip()}

    @staticmethod
    def _query_has_url(query_text: str) -> bool:
        return bool(re.search(r"(?:https?://|www\.)", query_text))

    @staticmethod
    def _query_requests_text_rewrite(query_text: str, detected_actions: Set[str]) -> bool:
        if detected_actions & {"simplify", "summarize", "grammar", "translate"}:
            return True
        return bool(
            re.search(
                r"\b("
                r"easy[- ]to[- ]understand|simplif\w*|summar\w*|grammar|proofread|"
                r"paraphras\w*|rewrit\w*|spin\w*"
                r")\b",
                query_text,
            )
        )

    @staticmethod
    def _query_mentions_article_text(query_text: str) -> bool:
        return bool(re.search(r"\b(article|blog post|web page|text content|online article|url)\b", query_text))

    @staticmethod
    def _query_mentions_audio_goal(query_text: str) -> bool:
        return bool(re.search(r"\b(audio|voiceover|speech|narration|spoken)\b", query_text))

    @staticmethod
    def _query_mentions_video_goal(query_text: str) -> bool:
        return bool(re.search(r"\b(video|visuals?)\b|\.mp4\b", query_text))

    def _tool_request_adjustment(
        self,
        tool: str,
        *,
        query_text: str,
        detected_actions: Set[str],
        current_tool: Optional[str] = None,
    ) -> float:
        lowered = str(tool or "").strip().lower()
        current = str(current_tool or "").strip().lower()
        query_has_url = self._query_has_url(query_text)
        mentions_article = self._query_mentions_article_text(query_text)
        mentions_audio_goal = self._query_mentions_audio_goal(query_text)
        mentions_video_goal = self._query_mentions_video_goal(query_text)
        requests_text_rewrite = self._query_requests_text_rewrite(query_text, detected_actions)

        adjustment = 0.0

        if lowered == "article spinner" and not requests_text_rewrite:
            adjustment -= 2.5
            if query_has_url or mentions_article:
                adjustment -= 0.75
            if current == "text downloader":
                adjustment -= 1.25

        if query_has_url and "downloader" in lowered:
            if "text" in lowered and mentions_article:
                adjustment += 3.0
            elif "audio" in lowered and mentions_audio_goal:
                adjustment += 2.0
            elif "video" in lowered and mentions_video_goal:
                adjustment += 2.0
            else:
                adjustment -= 0.5

        if current == "text downloader" and lowered == "text-to-audio" and mentions_audio_goal:
            adjustment += 1.5

        if current == "text-to-audio" and lowered == "video voiceover" and mentions_video_goal:
            adjustment += 1.5

        return adjustment

    def _score_tool(
        self,
        tool: str,
        *,
        query_tokens: Set[str],
        detected_actions: Set[str],
        count: int = 0,
    ) -> float:
        tool_tokens = self._tool_tokens.get(tool, set())
        token_overlap = self._overlap_ratio(query_tokens, tool_tokens)
        action_overlap = len(detected_actions & set(_infer_skill_action_tags(tool)))
        count_bonus = min(float(count), 10.0) * 0.15
        return float(token_overlap * 3.0 + action_overlap * 1.5 + count_bonus)

    def recommend_start_tools(
        self,
        query: str,
        *,
        detected_actions: Optional[Iterable[str]] = None,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        query_text = " ".join(str(query or "").lower().split())
        query_tokens = _tokenize(query_text)
        action_set = self._normalize_detected_actions(detected_actions)
        candidates: List[Dict[str, Any]] = []

        for tool, count in self.memory_index.start_counts.items():
            score = self._score_tool(
                tool,
                query_tokens=query_tokens,
                detected_actions=action_set,
                count=count,
            )
            start_motifs = self._motifs_by_start.get(tool, [])
            motif_bonus = 0.0
            if start_motifs:
                motif_bonus = max(
                    (
                        self._score_motif(
                            motif,
                            query_tokens=query_tokens,
                            detected_actions=action_set,
                        )
                        for motif in start_motifs[:5]
                    ),
                    default=0.0,
                ) * 0.25
            request_adjustment = self._tool_request_adjustment(
                tool,
                query_text=query_text,
                detected_actions=action_set,
            )
            total = score + motif_bonus + request_adjustment
            if total <= 0:
                continue
            candidates.append(
                {
                    "skill": tool,
                    "tool": tool,
                    "score": float(total),
                    "start_count": int(count),
                    "motif_bonus": float(motif_bonus),
                    "request_adjustment": float(request_adjustment),
                    "reason": f"query-conditioned start prior for {tool}",
                }
            )

        candidates.sort(
            key=lambda item: (
                -float(item.get("score", 0.0)),
                -int(item.get("start_count", 0)),
                str(item.get("skill", "")),
            )
        )
        return candidates[: max(int(top_k), 0)]

    def recommend_next_tools(
        self,
        query: str,
        current_tool: Optional[str],
        *,
        visited_tools: Optional[Set[str]] = None,
        detected_actions: Optional[Iterable[str]] = None,
        top_k: int = 5,
        min_count: int = 1,
    ) -> List[Dict[str, Any]]:
        if not current_tool:
            return self.recommend_start_tools(query, detected_actions=detected_actions, top_k=top_k)

        query_text = " ".join(str(query or "").lower().split())
        query_tokens = _tokenize(query_text)
        action_set = self._normalize_detected_actions(detected_actions)
        visited = {str(tool).strip() for tool in (visited_tools or set()) if str(tool).strip()}
        candidates: List[Dict[str, Any]] = []

        for edge, count in self.memory_index.transition_counts.items():
            source, target = edge
            if source != current_tool or count < int(min_count) or target in visited:
                continue

            transition_score = self._score_transition(
                edge,
                query_tokens=query_tokens,
                detected_actions=action_set,
            )
            target_score = self._score_tool(
                target,
                query_tokens=query_tokens,
                detected_actions=action_set,
                count=count,
            )
            motif_bonus = min(self._motif_support_by_edge.get(edge, 0.0), 20.0) * 0.05
            request_adjustment = self._tool_request_adjustment(
                target,
                query_text=query_text,
                detected_actions=action_set,
                current_tool=source,
            )
            total = transition_score + target_score * 0.35 + motif_bonus + request_adjustment
            if total <= 0:
                continue
            candidates.append(
                {
                    "skill": target,
                    "tool": target,
                    "source_tool": source,
                    "score": float(total),
                    "edge_count": int(count),
                    "motif_bonus": float(motif_bonus),
                    "request_adjustment": float(request_adjustment),
                    "reason": f"query-conditioned edge prior {source} -> {target}",
                }
            )

        candidates.sort(
            key=lambda item: (
                -float(item.get("score", 0.0)),
                -int(item.get("edge_count", 0)),
                str(item.get("skill", "")),
            )
        )
        return candidates[: max(int(top_k), 0)]

    def retrieve(
        self,
        query: str,
        *,
        detected_actions: Optional[Iterable[str]] = None,
        top_k_motifs: int = 5,
        top_k_transitions: int = 8,
    ) -> Dict[str, Any]:
        query_tokens = _tokenize(query)
        action_set = self._normalize_detected_actions(detected_actions)

        scored_motifs: List[Tuple[float, WorkflowMemoryMotif]] = []
        for motif in self.memory_index.motifs:
            score = self._score_motif(motif, query_tokens=query_tokens, detected_actions=action_set)
            if score <= 0:
                continue
            scored_motifs.append((score, motif))
        scored_motifs.sort(key=lambda item: (-item[0], -item[1].support, item[1].motif_id))
        top_motifs = scored_motifs[: max(int(top_k_motifs), 0)]

        transition_scores: Counter[Tuple[str, str]] = Counter()
        start_scores: Counter[str] = Counter()
        end_scores: Counter[str] = Counter()

        for score, motif in top_motifs:
            weight = max(1.0, score)
            if motif.tasks:
                start_scores[motif.tasks[0]] += weight
                end_scores[motif.tasks[-1]] += weight
            for edge in motif.links:
                transition_scores[edge] += weight + min(float(self.memory_index.transition_counts.get(edge, 0)), 5.0) * 0.2

        if not transition_scores:
            for edge in self.memory_index.transition_counts.keys():
                score = self._score_transition(edge, query_tokens=query_tokens, detected_actions=action_set)
                if score > 0:
                    transition_scores[edge] += score

        if not start_scores:
            for tool, count in self.memory_index.start_counts.items():
                score = self._score_boundary_tool(
                    tool,
                    query_tokens=query_tokens,
                    count=count,
                    token_cache=self._start_tool_tokens,
                )
                if score > 0:
                    start_scores[tool] += score

        if not end_scores:
            for tool, count in self.memory_index.end_counts.items():
                score = self._score_boundary_tool(
                    tool,
                    query_tokens=query_tokens,
                    count=count,
                    token_cache=self._end_tool_tokens,
                )
                if score > 0:
                    end_scores[tool] += score

        top_transitions = [
            {"source": source, "target": target, "score": float(score)}
            for (source, target), score in transition_scores.most_common(max(int(top_k_transitions), 0))
        ]

        return {
            "query_tokens": sorted(query_tokens),
            "query_actions": sorted(action_set),
            "motifs": [
                {
                    "motif_id": motif.motif_id,
                    "score": float(score),
                    "tasks": list(motif.tasks),
                    "links": [list(edge) for edge in motif.links],
                    "action_tags": list(motif.action_tags),
                    "support": motif.support,
                }
                for score, motif in top_motifs
            ],
            "transitions": top_transitions,
            "start_tools": [
                {"tool": tool, "score": float(score)}
                for tool, score in start_scores.most_common(5)
            ],
            "end_tools": [
                {"tool": tool, "score": float(score)}
                for tool, score in end_scores.most_common(5)
            ],
        }


def format_workflow_memory_prompt_block(context: Dict[str, Any]) -> str:
    if not isinstance(context, dict):
        return ""
    motifs = context.get("motifs", [])
    transitions = context.get("transitions", [])
    if not motifs and not transitions:
        return ""

    lines: List[str] = [
        "Retrieved workflow priors from aggregated workflow memory:",
    ]

    for motif in motifs[:3]:
        if not isinstance(motif, dict):
            continue
        tasks = " -> ".join(str(task) for task in motif.get("tasks", []) if str(task).strip())
        if tasks:
            lines.append(
                f"- Frequent path motif: {tasks} (support={int(motif.get('support', 0))})"
            )

    for transition in transitions[:4]:
        if not isinstance(transition, dict):
            continue
        source = str(transition.get("source", "")).strip()
        target = str(transition.get("target", "")).strip()
        if source and target:
            lines.append(
                f"- Observed transition prior: {source} -> {target} (score={transition.get('score', 0):.2f})"
            )

    lines.append("Use these priors as soft hints only. Follow the user request and skill schemas when they conflict.")
    return "\n".join(lines)


def score_workflow_with_retrieval_context(
    compiled_nodes: Sequence[Dict[str, Any]],
    context: Dict[str, Any],
) -> Dict[str, float]:
    if not compiled_nodes or not isinstance(context, dict):
        return {
            "bonus": 0.0,
            "penalty": 0.0,
            "transition_bonus": 0.0,
            "transition_penalty": 0.0,
            "motif_bonus": 0.0,
            "start_bonus": 0.0,
            "end_bonus": 0.0,
        }

    transitions = context.get("transitions", [])
    motifs = context.get("motifs", [])
    start_tools = context.get("start_tools", [])
    end_tools = context.get("end_tools", [])

    transition_scores_by_edge: Dict[Tuple[str, str], float] = {}
    transition_scores_by_source: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for item in transitions:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source", "")).strip()
        target = str(item.get("target", "")).strip()
        score = float(item.get("score", 0.0))
        if not source or not target:
            continue
        transition_scores_by_edge[(source, target)] = score
        transition_scores_by_source[source].append((target, score))

    motif_task_sequences: List[Tuple[Tuple[str, ...], float]] = []
    for item in motifs:
        if not isinstance(item, dict):
            continue
        tasks = tuple(str(task).strip() for task in item.get("tasks", []) if str(task).strip())
        if len(tasks) < 2:
            continue
        motif_task_sequences.append((tasks, float(item.get("score", 0.0))))

    start_score_by_tool = {
        str(item.get("tool", "")).strip(): float(item.get("score", 0.0))
        for item in start_tools
        if isinstance(item, dict) and str(item.get("tool", "")).strip()
    }
    end_score_by_tool = {
        str(item.get("tool", "")).strip(): float(item.get("score", 0.0))
        for item in end_tools
        if isinstance(item, dict) and str(item.get("tool", "")).strip()
    }

    task_names = [str(node.get("task", "")).strip() for node in compiled_nodes]
    index_edges = _extract_candidate_index_edges(compiled_nodes)
    candidate_edges = {
        (task_names[source_idx], task_names[target_idx])
        for source_idx, target_idx in index_edges
        if 0 <= source_idx < len(task_names) and 0 <= target_idx < len(task_names)
    }
    max_motif_len = max((len(tasks) for tasks, _score in motif_task_sequences), default=2)
    candidate_paths = _extract_candidate_paths(task_names, index_edges, max_path_len=max_motif_len)
    root_indices, leaf_indices = _graph_boundary_indices(len(task_names), index_edges)

    transition_bonus = 0.0
    transition_penalty = 0.0
    for edge in candidate_edges:
        if edge in transition_scores_by_edge:
            transition_bonus += min(transition_scores_by_edge[edge], 6.0) * 0.4
            continue
        source_priors = transition_scores_by_source.get(edge[0], [])
        if source_priors:
            top_target, top_score = max(source_priors, key=lambda item: item[1])
            if top_score >= 2.0 and top_target != edge[1]:
                transition_penalty -= min(top_score, 6.0) * 0.25

    motif_bonus = 0.0
    if candidate_paths:
        for motif_tasks, motif_score in motif_task_sequences:
            if motif_tasks in candidate_paths:
                motif_bonus += min(motif_score, 8.0) * 0.25

    start_bonus = 0.0
    end_bonus = 0.0
    for root_idx in root_indices:
        tool = task_names[root_idx]
        if tool in start_score_by_tool:
            start_bonus += min(start_score_by_tool[tool], 6.0) * 0.2
    for leaf_idx in leaf_indices:
        tool = task_names[leaf_idx]
        if tool in end_score_by_tool:
            end_bonus += min(end_score_by_tool[tool], 6.0) * 0.2

    bonus = transition_bonus + motif_bonus + start_bonus + end_bonus
    penalty = transition_penalty
    return {
        "bonus": bonus,
        "penalty": penalty,
        "transition_bonus": transition_bonus,
        "transition_penalty": transition_penalty,
        "motif_bonus": motif_bonus,
        "start_bonus": start_bonus,
        "end_bonus": end_bonus,
    }
