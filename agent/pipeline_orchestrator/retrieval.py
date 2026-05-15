from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .actions import _infer_skill_action_tags
from .workflow_memory import (
    WorkflowMemoryIndex,
    WorkflowMemoryMotif,
    WorkflowStartPrior,
    WorkflowTransitionPrior,
    _edge_is_schema_compatible,
    _infer_task_modalities,
)


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
        self._raw_motifs: List[WorkflowMemoryMotif] = list(memory_index.motifs)
        self._raw_start_priors: List[WorkflowStartPrior] = self._synthesize_start_priors(memory_index.start_counts)
        self._raw_transition_priors: List[WorkflowTransitionPrior] = self._synthesize_transition_priors(
            memory_index.transition_counts
        )
        self._trusted_motifs: List[WorkflowMemoryMotif] = list(memory_index.motif_prior or [])
        self._trusted_start_priors: List[WorkflowStartPrior] = list(memory_index.start_prior or [])
        self._trusted_transition_priors: List[WorkflowTransitionPrior] = list(memory_index.transition_prior or [])
        self._retrieval_motifs: List[WorkflowMemoryMotif] = self._trusted_motifs or self._raw_motifs
        self._retrieval_start_priors: List[WorkflowStartPrior] = self._trusted_start_priors or self._raw_start_priors
        self._retrieval_transition_priors: List[WorkflowTransitionPrior] = (
            self._trusted_transition_priors or self._raw_transition_priors
        )
        self._all_motifs: List[WorkflowMemoryMotif] = list(
            {
                motif.motif_id: motif
                for motif in list(self._raw_motifs) + list(self._retrieval_motifs)
            }.values()
        )
        self._all_transition_priors: List[WorkflowTransitionPrior] = list(
            {
                (prior.source, prior.target): prior
                for prior in list(self._raw_transition_priors) + list(self._retrieval_transition_priors)
            }.values()
        )
        self._candidate_transition_priors: List[WorkflowTransitionPrior] = list(
            {
                (prior.source, prior.target): prior
                for prior in list(self._retrieval_transition_priors) + list(self._raw_transition_priors)
            }.values()
        )

        self._motif_action_tags: Dict[str, Set[str]] = {
            motif.motif_id: (
                set(motif.action_tags)
                | {
                    tag
                    for task in motif.tasks
                    for tag in _infer_skill_action_tags(task)
                }
            )
            for motif in self._all_motifs
        }
        self._motif_tokens: Dict[str, Set[str]] = {
            motif.motif_id: (_task_tokens(motif.tasks) | self._motif_action_tags.get(motif.motif_id, set()))
            for motif in self._all_motifs
        }
        all_tools: Set[str] = set(memory_index.end_counts.keys())
        all_tools.update(item.skill for item in self._raw_start_priors)
        all_tools.update(item.skill for item in self._retrieval_start_priors)
        for prior in self._all_transition_priors:
            all_tools.update((prior.source, prior.target))
        for motif in self._all_motifs:
            all_tools.update(motif.tasks)
        self._transition_tokens: Dict[Tuple[str, str], Set[str]] = {
            (prior.source, prior.target): (_task_tokens((prior.source, prior.target)) | _tokenize(f"{prior.source} {prior.target}"))
            for prior in self._all_transition_priors
        }
        self._tool_tokens: Dict[str, Set[str]] = {
            tool: _tokenize(tool.replace("-", " "))
            for tool in all_tools
        }
        self._start_tool_tokens: Dict[str, Set[str]] = {
            tool: _tokenize(tool.replace("-", " "))
            for tool in {item.skill for item in self._retrieval_start_priors}
        }
        self._end_tool_tokens: Dict[str, Set[str]] = {
            tool: _tokenize(tool.replace("-", " "))
            for tool in memory_index.end_counts.keys()
        }
        self._motifs_by_start: Dict[str, List[WorkflowMemoryMotif]] = defaultdict(list)
        self._motif_support_by_edge: Dict[Tuple[str, str], float] = defaultdict(float)
        self._edge_motif_action_tags: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
        for motif in self._all_motifs:
            if motif.tasks:
                self._motifs_by_start[motif.tasks[0]].append(motif)
            for edge in motif.links:
                self._motif_support_by_edge[edge] += float(max(motif.support, 0))
                self._edge_motif_action_tags[edge].update(self._motif_action_tags.get(motif.motif_id, set()))
        for motifs in self._motifs_by_start.values():
            motifs.sort(key=lambda item: (-item.support, -len(item.tasks), item.motif_id))
        self._start_prior_by_skill: Dict[str, WorkflowStartPrior] = {
            item.skill: item for item in self._retrieval_start_priors
        }
        self._transition_prior_by_edge: Dict[Tuple[str, str], WorkflowTransitionPrior] = {
            (item.source, item.target): item for item in self._retrieval_transition_priors
        }
        self._transition_priors_by_source: Dict[str, List[WorkflowTransitionPrior]] = defaultdict(list)
        for item in self._retrieval_transition_priors:
            self._transition_priors_by_source[item.source].append(item)
        for priors in self._transition_priors_by_source.values():
            priors.sort(key=lambda item: (-item.probability, -item.support, item.target))
        self._candidate_transition_priors.sort(
            key=lambda item: (-item.probability, -item.support, item.source, item.target)
        )
        self._tool_action_tags: Dict[str, Set[str]] = {
            tool: set(_infer_skill_action_tags(tool))
            for tool in all_tools
        }
        self._tool_modalities: Dict[str, Dict[str, Tuple[str, ...]]] = {
            tool: _infer_task_modalities(tool)
            for tool in all_tools
        }
        self._tool_modality_sets: Dict[str, Set[str]] = {
            tool: set(modalities.get("inputs", tuple())) | set(modalities.get("outputs", tuple()))
            for tool, modalities in self._tool_modalities.items()
        }
        self._motif_modalities: Dict[str, Set[str]] = {
            motif.motif_id: {
                modality
                for task in motif.tasks
                for modality in self._tool_modality_sets.get(task, set())
            }
            for motif in self._all_motifs
        }

    @staticmethod
    def _synthesize_start_priors(start_counts: Dict[str, int]) -> List[WorkflowStartPrior]:
        total_support = sum(max(int(count), 0) for count in start_counts.values())
        if total_support <= 0:
            return []
        priors = [
            WorkflowStartPrior(
                skill=tool,
                support=max(int(count), 0),
                probability=float(max(int(count), 0)) / float(total_support),
                action_tags=tuple(_infer_skill_action_tags(tool)),
            )
            for tool, count in start_counts.items()
            if int(count) > 0
        ]
        priors.sort(key=lambda item: (-item.probability, -item.support, item.skill))
        return priors

    @staticmethod
    def _synthesize_transition_priors(
        transition_counts: Dict[Tuple[str, str], int]
    ) -> List[WorkflowTransitionPrior]:
        outgoing_totals: Dict[str, int] = defaultdict(int)
        for (source, _target), count in transition_counts.items():
            outgoing_totals[source] += max(int(count), 0)
        priors: List[WorkflowTransitionPrior] = []
        for (source, target), count in transition_counts.items():
            support = max(int(count), 0)
            outgoing_total = max(outgoing_totals.get(source, 0), 1)
            priors.append(
                WorkflowTransitionPrior(
                    source=source,
                    target=target,
                    support=support,
                    probability=float(support) / float(outgoing_total),
                    action_tags=tuple(
                        set(_infer_skill_action_tags(source)) | set(_infer_skill_action_tags(target))
                    ),
                )
            )
        priors.sort(key=lambda item: (-item.probability, -item.support, item.source, item.target))
        return priors

    @staticmethod
    def _overlap_ratio(left: Set[str], right: Set[str]) -> float:
        if not left or not right:
            return 0.0
        overlap = len(left & right)
        return overlap / float(max(len(left), len(right), 1))

    @staticmethod
    def _historical_prior_score(probability: float, support: int, *, probability_weight: float, support_weight: float) -> float:
        return float(max(probability, 0.0) * probability_weight + math.log1p(max(int(support), 0)) * support_weight)

    @staticmethod
    def _action_match_score(requested_actions: Set[str], candidate_actions: Set[str]) -> float:
        if not requested_actions or not candidate_actions:
            return 0.0
        overlap = requested_actions & candidate_actions
        if not overlap:
            return 0.0
        overlap_ratio = len(overlap) / float(max(len(requested_actions), 1))
        return float(len(overlap) * 1.4 + overlap_ratio * 2.0)

    @staticmethod
    def _unrequested_action_penalty(requested_actions: Set[str], candidate_actions: Set[str]) -> float:
        if not requested_actions or not candidate_actions:
            return 0.0
        extras = candidate_actions - requested_actions
        if not extras:
            return 0.0
        penalty = len(extras) * 0.35
        if not (requested_actions & candidate_actions):
            penalty += min(len(candidate_actions), 3) * 0.45
        return float(-penalty)

    @staticmethod
    def _normalize_query_text(query: str) -> str:
        return " ".join(str(query or "").lower().split())

    @staticmethod
    def _has_pattern(text: str, pattern: str) -> bool:
        return bool(re.search(pattern, text))

    def _infer_query_modalities(self, query_text: str, detected_actions: Set[str]) -> Set[str]:
        modalities: Set[str] = set()
        if self._has_pattern(query_text, r"\b(audio|voice|voiceover|speech|narration|spoken|podcast|soundtrack)\b|\.(wav|mp3|m4a|flac|aac|ogg)\b"):
            modalities.add("audio")
        if self._has_pattern(query_text, r"\b(video|clip|movie|footage|visuals?)\b|\.(mp4|mov|avi|mkv|webm)\b"):
            modalities.add("video")
        if self._has_pattern(query_text, r"\b(image|photo|picture|screenshot|illustration|waveform|thumbnail|poster)\b|\.(png|jpg|jpeg|gif|bmp|webp)\b"):
            modalities.add("image")
        if self._has_pattern(query_text, r"\b(text|article|blog|script|transcript|caption|subtitle|paragraph|document|plain english|url|web page)\b|(?:https?://|www\.)"):
            modalities.add("text")
        if detected_actions & {"transcribe", "simplify", "expand", "grammar", "summarize", "translate", "topic", "keywords", "retrieval", "sentiment"}:
            modalities.add("text")
        if detected_actions & {"audio_effect", "voice_change"}:
            modalities.add("audio")
        if detected_actions & {"waveform"}:
            modalities.add("image")
        if detected_actions & {"video"}:
            modalities.add("video")
        return modalities

    def _modality_match_score(self, query_modalities: Set[str], candidate_modalities: Set[str]) -> float:
        if not query_modalities or not candidate_modalities:
            return 0.0
        overlap = query_modalities & candidate_modalities
        if overlap:
            return float(1.0 + (len(overlap) / float(max(len(query_modalities), 1))) * 1.5)
        return -0.75

    def _schema_connectability_score(self, source_tool: str, target_tool: Optional[str] = None) -> float:
        if not target_tool:
            return 0.0
        return 1.0 if _edge_is_schema_compatible(source_tool, target_tool) else -2.5

    def _score_motif(
        self,
        motif: WorkflowMemoryMotif,
        *,
        query_tokens: Set[str],
        detected_actions: Set[str],
        query_modalities: Optional[Set[str]] = None,
    ) -> float:
        motif_tokens = self._motif_tokens.get(motif.motif_id, set())
        motif_actions = self._motif_action_tags.get(motif.motif_id, set())
        action_overlap_score = self._action_match_score(detected_actions, motif_actions)
        token_overlap = self._overlap_ratio(query_tokens, motif_tokens)
        motif_modality_score = self._modality_match_score(
            query_modalities or set(),
            self._motif_modalities.get(motif.motif_id, set()),
        )
        support_total = sum(max(item.support, 0) for item in self._retrieval_motifs) or 1
        historical_score = self._historical_prior_score(
            float(max(motif.support, 0)) / float(support_total),
            motif.support,
            probability_weight=5.0,
            support_weight=0.35,
        )
        schema_score = 0.0
        if motif.links:
            schema_score = sum(self._schema_connectability_score(source, target) for source, target in motif.links)
            schema_score /= float(len(motif.links))
        penalty = self._unrequested_action_penalty(detected_actions, motif_actions)
        return float(historical_score + action_overlap_score + token_overlap * 3.5 + motif_modality_score + schema_score + penalty)

    def _score_transition(
        self,
        edge: Tuple[str, str],
        *,
        query_tokens: Set[str],
        detected_actions: Set[str],
        query_modalities: Optional[Set[str]] = None,
        support: int = 0,
        probability: float = 0.0,
        action_tags: Optional[Iterable[str]] = None,
    ) -> float:
        edge_tokens = self._transition_tokens.get(edge, set())
        motif_actions = self._edge_motif_action_tags.get(edge, set())
        transition_actions = (set(action_tags or ()) | motif_actions) or {
            tag
            for task in edge
            for tag in _infer_skill_action_tags(task)
        }
        action_overlap_score = self._action_match_score(detected_actions, transition_actions)
        token_overlap = self._overlap_ratio(query_tokens, edge_tokens)
        target_modalities = self._tool_modality_sets.get(edge[1], set())
        modality_score = self._modality_match_score(query_modalities or set(), target_modalities)
        historical_score = self._historical_prior_score(
            probability,
            support or int(self.memory_index.transition_counts.get(edge, 0)),
            probability_weight=6.0,
            support_weight=0.3,
        )
        schema_score = self._schema_connectability_score(edge[0], edge[1])
        penalty = self._unrequested_action_penalty(detected_actions, transition_actions)
        weak_prior_penalty = 0.0
        if not set(action_tags or ()) and not motif_actions:
            weak_prior_penalty = -2.5
        return float(
            historical_score
            + action_overlap_score
            + token_overlap * 2.8
            + modality_score
            + schema_score
            + penalty
            + weak_prior_penalty
        )

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
        if detected_actions & {"simplify", "expand", "summarize", "grammar", "translate"}:
            return True
        return bool(
            re.search(
                r"\b("
                r"easy[- ]to[- ]understand|simplif\w*|expand\w*|summar\w*|grammar|proofread|"
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
            adjustment -= 4.0
            if query_has_url or mentions_article:
                adjustment -= 1.5
            if current == "text downloader":
                adjustment -= 2.0

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
        query_modalities: Optional[Set[str]] = None,
        count: int = 0,
    ) -> float:
        tool_tokens = self._tool_tokens.get(tool, set())
        token_overlap = self._overlap_ratio(query_tokens, tool_tokens)
        tool_actions = self._tool_action_tags.get(tool, set())
        action_overlap_score = self._action_match_score(detected_actions, tool_actions)
        modality_score = self._modality_match_score(query_modalities or set(), self._tool_modality_sets.get(tool, set()))
        count_bonus = min(float(count), 10.0) * 0.15
        penalty = self._unrequested_action_penalty(detected_actions, tool_actions)
        return float(token_overlap * 3.0 + action_overlap_score + modality_score + count_bonus + penalty)

    def _rank_start_priors(
        self,
        priors: Sequence[WorkflowStartPrior],
        *,
        query_text: str,
        query_tokens: Set[str],
        detected_actions: Set[str],
        query_modalities: Set[str],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        for prior in priors:
            tool = prior.skill
            historical_score = self._historical_prior_score(
                prior.probability,
                prior.support,
                probability_weight=7.0,
                support_weight=0.35,
            )
            tool_score = self._score_tool(
                tool,
                query_tokens=query_tokens,
                detected_actions=detected_actions,
                query_modalities=query_modalities,
                count=prior.support,
            )
            start_motifs = self._motifs_by_start.get(tool, [])
            motif_bonus = 0.0
            if start_motifs:
                motif_bonus = max(
                    (
                        self._score_motif(
                            motif,
                            query_tokens=query_tokens,
                            detected_actions=detected_actions,
                            query_modalities=query_modalities,
                        )
                        for motif in start_motifs[:5]
                    ),
                    default=0.0,
                ) * 0.18
            request_adjustment = self._tool_request_adjustment(
                tool,
                query_text=query_text,
                detected_actions=detected_actions,
            )
            total = historical_score + tool_score + motif_bonus + request_adjustment
            if total <= 0:
                continue
            candidates.append(
                {
                    "skill": tool,
                    "tool": tool,
                    "score": float(total),
                    "start_count": int(prior.support),
                    "support": int(prior.support),
                    "probability": float(prior.probability),
                    "motif_bonus": float(motif_bonus),
                    "request_adjustment": float(request_adjustment),
                    "action_tags": list(prior.action_tags),
                    "reason": f"query-conditioned start prior for {tool}",
                }
            )
        candidates.sort(
            key=lambda item: (
                -float(item.get("score", 0.0)),
                -float(item.get("probability", 0.0)),
                -int(item.get("support", 0)),
                str(item.get("skill", "")),
            )
        )
        return candidates[: max(int(top_k), 0)]

    def _rank_transition_priors(
        self,
        priors: Sequence[WorkflowTransitionPrior],
        *,
        query_text: str,
        query_tokens: Set[str],
        detected_actions: Set[str],
        query_modalities: Set[str],
        current_tool: Optional[str] = None,
        visited_tools: Optional[Set[str]] = None,
        top_k: int,
        min_support: int = 1,
    ) -> List[Dict[str, Any]]:
        current = str(current_tool or "").strip()
        visited = {str(tool).strip() for tool in (visited_tools or set()) if str(tool).strip()}
        candidates: List[Dict[str, Any]] = []
        for prior in priors:
            if current and prior.source != current:
                continue
            if prior.support < int(min_support) or prior.target in visited:
                continue
            edge = (prior.source, prior.target)
            transition_score = self._score_transition(
                edge,
                query_tokens=query_tokens,
                detected_actions=detected_actions,
                query_modalities=query_modalities,
                support=prior.support,
                probability=prior.probability,
                action_tags=prior.action_tags,
            )
            target_score = self._score_tool(
                prior.target,
                query_tokens=query_tokens,
                detected_actions=detected_actions,
                query_modalities=query_modalities,
                count=prior.support,
            )
            motif_bonus = min(self._motif_support_by_edge.get(edge, 0.0), 20.0) * 0.05
            request_adjustment = self._tool_request_adjustment(
                prior.target,
                query_text=query_text,
                detected_actions=detected_actions,
                current_tool=prior.source,
            )
            total = transition_score + target_score * 0.35 + motif_bonus + request_adjustment
            if total <= 0:
                continue
            candidates.append(
                {
                    "skill": prior.target,
                    "tool": prior.target,
                    "source_tool": prior.source,
                    "source": prior.source,
                    "target": prior.target,
                    "score": float(total),
                    "edge_count": int(prior.support),
                    "support": int(prior.support),
                    "probability": float(prior.probability),
                    "motif_bonus": float(motif_bonus),
                    "request_adjustment": float(request_adjustment),
                    "action_tags": list(prior.action_tags),
                    "reason": f"query-conditioned edge prior {prior.source} -> {prior.target}",
                }
            )
        candidates.sort(
            key=lambda item: (
                -float(item.get("score", 0.0)),
                -float(item.get("probability", 0.0)),
                -int(item.get("support", 0)),
                str(item.get("source", "")),
                str(item.get("target", "")),
            )
        )
        return candidates[: max(int(top_k), 0)]

    def _rank_motifs(
        self,
        motifs: Sequence[WorkflowMemoryMotif],
        *,
        query_tokens: Set[str],
        detected_actions: Set[str],
        query_modalities: Set[str],
        top_k: int,
    ) -> List[Tuple[float, WorkflowMemoryMotif]]:
        scored: List[Tuple[float, WorkflowMemoryMotif]] = []
        for motif in motifs:
            score = self._score_motif(
                motif,
                query_tokens=query_tokens,
                detected_actions=detected_actions,
                query_modalities=query_modalities,
            )
            if score <= 0:
                continue
            scored.append((score, motif))
        scored.sort(key=lambda item: (-item[0], -item[1].support, item[1].motif_id))
        return scored[: max(int(top_k), 0)]

    def recommend_start_tools(
        self,
        query: str,
        *,
        detected_actions: Optional[Iterable[str]] = None,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        query_text = self._normalize_query_text(query)
        query_tokens = _tokenize(query_text)
        action_set = self._normalize_detected_actions(detected_actions)
        query_modalities = self._infer_query_modalities(query_text, action_set)
        candidates = self._rank_start_priors(
            self._retrieval_start_priors,
            query_text=query_text,
            query_tokens=query_tokens,
            detected_actions=action_set,
            query_modalities=query_modalities,
            top_k=top_k,
        )
        if candidates or not self._trusted_start_priors:
            return candidates
        return self._rank_start_priors(
            self._raw_start_priors,
            query_text=query_text,
            query_tokens=query_tokens,
            detected_actions=action_set,
            query_modalities=query_modalities,
            top_k=top_k,
        )

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

        query_text = self._normalize_query_text(query)
        query_tokens = _tokenize(query_text)
        action_set = self._normalize_detected_actions(detected_actions)
        query_modalities = self._infer_query_modalities(query_text, action_set)
        candidates = self._rank_transition_priors(
            self._candidate_transition_priors,
            query_text=query_text,
            query_tokens=query_tokens,
            detected_actions=action_set,
            query_modalities=query_modalities,
            current_tool=current_tool,
            visited_tools=visited_tools,
            top_k=top_k,
            min_support=min_count,
        )
        if candidates or not self._trusted_transition_priors:
            if not candidates:
                return candidates
            best_score = float(candidates[0].get("score", 0.0))
            cutoff = max(0.5, best_score * 0.25)
            return [item for item in candidates if float(item.get("score", 0.0)) >= cutoff]
        fallback = self._rank_transition_priors(
            self._raw_transition_priors,
            query_text=query_text,
            query_tokens=query_tokens,
            detected_actions=action_set,
            query_modalities=query_modalities,
            current_tool=current_tool,
            visited_tools=visited_tools,
            top_k=top_k,
            min_support=min_count,
        )
        if not fallback:
            return fallback
        best_score = float(fallback[0].get("score", 0.0))
        cutoff = max(0.5, best_score * 0.25)
        return [item for item in fallback if float(item.get("score", 0.0)) >= cutoff]

    def retrieve(
        self,
        query: str,
        *,
        detected_actions: Optional[Iterable[str]] = None,
        top_k_start_skills: int = 5,
        top_k_motifs: int = 5,
        top_k_transitions: int = 8,
    ) -> Dict[str, Any]:
        query_text = self._normalize_query_text(query)
        query_tokens = _tokenize(query_text)
        action_set = self._normalize_detected_actions(detected_actions)
        query_modalities = self._infer_query_modalities(query_text, action_set)
        top_motifs = self._rank_motifs(
            self._retrieval_motifs,
            query_tokens=query_tokens,
            detected_actions=action_set,
            query_modalities=query_modalities,
            top_k=top_k_motifs,
        )
        if not top_motifs and self._trusted_motifs:
            top_motifs = self._rank_motifs(
                self._raw_motifs,
                query_tokens=query_tokens,
                detected_actions=action_set,
                query_modalities=query_modalities,
                top_k=top_k_motifs,
            )

        top_starts = self.recommend_start_tools(
            query,
            detected_actions=action_set,
            top_k=top_k_start_skills,
        )
        ranked_transitions = self._rank_transition_priors(
            self._candidate_transition_priors,
            query_text=query_text,
            query_tokens=query_tokens,
            detected_actions=action_set,
            query_modalities=query_modalities,
            top_k=max(int(top_k_transitions) * 3, int(top_k_transitions)),
        )
        if not ranked_transitions and self._trusted_transition_priors:
            ranked_transitions = self._rank_transition_priors(
                self._raw_transition_priors,
                query_text=query_text,
                query_tokens=query_tokens,
                detected_actions=action_set,
                query_modalities=query_modalities,
                top_k=max(int(top_k_transitions) * 3, int(top_k_transitions)),
            )
        preferred_sources = {
            str(item.get("skill", "")).strip()
            for item in top_starts[:3]
            if isinstance(item, dict) and str(item.get("skill", "")).strip()
        }
        for _score, motif in top_motifs:
            for source, _target in motif.links:
                if str(source).strip():
                    preferred_sources.add(str(source).strip())
        top_transitions = ranked_transitions
        if preferred_sources:
            preferred_ranked = [
                item
                for item in ranked_transitions
                if str(item.get("source", "")).strip() in preferred_sources
            ]
            if preferred_ranked:
                top_transitions = preferred_ranked
        top_transitions = top_transitions[: max(int(top_k_transitions), 0)]

        end_scores: Counter[str] = Counter()
        for score, motif in top_motifs:
            if motif.tasks:
                end_scores[motif.tasks[-1]] += max(1.0, score)
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

        return {
            "query_tokens": sorted(query_tokens),
            "query_actions": sorted(action_set),
            "query_modalities": sorted(query_modalities),
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
            "start_skills": top_starts,
            "start_tools": top_starts,
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
