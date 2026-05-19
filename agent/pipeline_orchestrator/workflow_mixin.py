import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .actions import ACTION_CANONICAL_ORDER
from .models import SkillMetadata


class WorkflowMixin:
    def _validate_step_args(self, skill: SkillMetadata, args: Dict[str, Any]) -> List[str]:
        missing = []
        for key in skill.input_schema.keys():
            if key not in args:
                missing.append(key)
        return missing

    @staticmethod
    def _normalize_declared_types(raw_types: Any) -> Set[str]:
        normalized: Set[str] = set()
        if isinstance(raw_types, str):
            raw_values = [raw_types]
        elif isinstance(raw_types, (list, tuple, set)):
            raw_values = list(raw_types)
        else:
            raw_values = []

        for raw in raw_values:
            text = str(raw).strip().lower()
            if text:
                normalized.add(text)
        return normalized

    @staticmethod
    def _infer_output_types_from_skill_name(skill_name: str) -> Set[str]:
        text = re.sub(r"[_\-]+", " ", str(skill_name).strip().lower())
        match = re.search(r"\b(audio|video|image|text|url)\b\s+(?:to|2)\s+\b(audio|video|image|text|url)\b", text)
        if match:
            return {match.group(2)}

        found = re.findall(r"\b(audio|video|image|text|url)\b", text)
        unique_found: List[str] = []
        for item in found:
            if item not in unique_found:
                unique_found.append(item)
        if len(unique_found) == 1:
            return {unique_found[0]}
        return set()

    def _skill_input_types_for_arg(self, skill: Any, arg_name: str) -> Set[str]:
        input_types = getattr(skill, "input_types", {}) or {}
        if not isinstance(input_types, dict):
            return set()
        return self._normalize_declared_types(input_types.get(str(arg_name)))

    def _skill_output_types(self, skill_name: str, skill: Any) -> Set[str]:
        declared = self._normalize_declared_types(getattr(skill, "output_types", []))
        if declared:
            return declared
        return self._infer_output_types_from_skill_name(skill_name)

    def _validate_dependency(
        self,
        source_skill_name: str,
        source_skill: Any,
        target_skill_name: str,
        target_skill: Any,
        target_arg_name: str,
    ) -> Optional[str]:
        expected_input_types = self._skill_input_types_for_arg(target_skill, target_arg_name)
        if not expected_input_types:
            return None

        source_output_types = self._skill_output_types(source_skill_name, source_skill)
        if not source_output_types:
            return None

        if source_output_types.isdisjoint(expected_input_types):
            source_types = ", ".join(sorted(source_output_types))
            target_types = ", ".join(sorted(expected_input_types))
            return (
                f"{source_skill_name} outputs [{source_types}] but "
                f"{target_skill_name}.{target_arg_name} expects [{target_types}]"
            )
        return None

    @staticmethod
    def _get_step_output_key(step: Dict[str, Any]) -> Optional[str]:
        output_key = step.get("output_key")
        if isinstance(output_key, str) and output_key.strip():
            return output_key.strip()
        args = step.get("args", {})
        legacy_output_key = args.get("output_key") if isinstance(args, dict) else None
        if isinstance(legacy_output_key, str) and legacy_output_key.strip():
            return legacy_output_key.strip()
        return None

    @staticmethod
    def _get_step_input_map(step: Dict[str, Any]) -> Dict[str, str]:
        input_map = step.get("input_map", {})
        if not isinstance(input_map, dict):
            return {}
        normalized: Dict[str, str] = {}
        for arg_name, upstream_key in input_map.items():
            if isinstance(arg_name, str) and arg_name.strip() and isinstance(upstream_key, str) and upstream_key.strip():
                normalized[arg_name.strip()] = upstream_key.strip()
        return normalized

    @staticmethod
    def _parse_workflow_node_ref(argument: Any) -> Optional[int]:
        if not isinstance(argument, str):
            return None
        text = argument.strip()
        match = re.fullmatch(r"<node-(\d+)>", text)
        if match:
            return int(match.group(1))
        match = re.search(r"(?:output\s+of\s+step|step)\s*(\d+)", text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1)) - 1
        return None

    @staticmethod
    def _normalize_output_key_name(task_name: str, idx: int) -> str:
        stem = re.sub(r"[^a-z0-9]+", "_", task_name.lower()).strip("_") or "step"
        return f"{stem}_{idx + 1}_out"

    @staticmethod
    def _normalize_link_task_name(name: Any) -> str:
        return str(name).strip()

    def _build_expected_workflow_sources(
        self,
        task_nodes: List[Any],
        task_links: List[Any],
    ) -> Tuple[List[str], Dict[str, Set[str]]]:
        node_names = [
            self._normalize_link_task_name(node.get("task", "")) if isinstance(node, dict) else ""
            for node in task_nodes
        ]
        expected_sources: Dict[str, Set[str]] = {}
        for link in task_links:
            if not isinstance(link, dict):
                continue
            source = self._normalize_link_task_name(link.get("source", ""))
            target = self._normalize_link_task_name(link.get("target", ""))
            if not source or not target:
                continue
            expected_sources.setdefault(target, set()).add(source)
        return node_names, expected_sources

    def _canonicalize_workflow_reference(
        self,
        value: Any,
        current_index: int,
        node_names: List[str],
        expected_sources: Optional[Set[str]] = None,
    ) -> Any:
        if not isinstance(value, str):
            return value

        text = value.strip()
        node_match = re.fullmatch(r"<node-(\d+)>", text)
        if node_match:
            raw_index = int(node_match.group(1))
            if 0 <= raw_index < current_index:
                return self._workflow_node_ref(raw_index)

            candidates: List[int] = []
            if 0 <= raw_index - 1 < current_index and (raw_index - 1) not in candidates:
                candidates.append(raw_index - 1)
            if not candidates:
                return value

            if expected_sources:
                matched_candidates = [
                    idx for idx in candidates if self._normalize_link_task_name(node_names[idx]) in expected_sources
                ]
                if len(matched_candidates) == 1:
                    return self._workflow_node_ref(matched_candidates[0])
                if matched_candidates:
                    candidates = matched_candidates

            if raw_index in candidates:
                return self._workflow_node_ref(raw_index)
            return self._workflow_node_ref(max(candidates))

        step_match = re.search(r"(?:output\s+of\s+step|step)\s*(\d+)", text, flags=re.IGNORECASE)
        if step_match:
            step_index = int(step_match.group(1)) - 1
            if 0 <= step_index < current_index:
                return self._workflow_node_ref(step_index)
        return value

    def _canonicalize_workflow_task_nodes(self, task_nodes: List[Any], task_links: List[Any]) -> List[Any]:
        node_names, expected_sources_by_target = self._build_expected_workflow_sources(task_nodes, task_links)
        normalized_nodes: List[Any] = []

        for idx, node in enumerate(task_nodes):
            if not isinstance(node, dict):
                normalized_nodes.append(node)
                continue

            normalized_node = dict(node)
            raw_arguments = node.get("arguments", [])
            if not isinstance(raw_arguments, list):
                normalized_nodes.append(normalized_node)
                continue

            expected_sources = expected_sources_by_target.get(node_names[idx], set())
            normalized_arguments: List[Any] = []
            for argument in raw_arguments:
                if isinstance(argument, dict):
                    normalized_argument = dict(argument)
                    normalized_argument["value"] = self._canonicalize_workflow_reference(
                        argument.get("value"),
                        idx,
                        node_names,
                        expected_sources,
                    )
                    normalized_arguments.append(normalized_argument)
                    continue
                normalized_arguments.append(
                    self._canonicalize_workflow_reference(
                        argument,
                        idx,
                        node_names,
                        expected_sources,
                    )
                )

            normalized_node["arguments"] = normalized_arguments
            normalized_nodes.append(normalized_node)

        return normalized_nodes

    def _normalize_workflow_payload(self, workflow: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(workflow, dict):
            raise ValueError("workflow must be a JSON object")

        task_nodes = workflow.get("task_nodes", [])
        task_steps = workflow.get("task_steps", [])
        task_links = workflow.get("task_links", [])

        if not isinstance(task_nodes, list) or not task_nodes:
            raise ValueError("workflow must contain a non-empty task_nodes list")
        if task_steps is None:
            task_steps = []
        if task_links is None:
            task_links = []
        if not isinstance(task_steps, list):
            raise ValueError("workflow task_steps must be a list")
        if not isinstance(task_links, list):
            raise ValueError("workflow task_links must be a list")
        task_nodes = self._canonicalize_workflow_task_nodes(task_nodes, task_links)

        return {
            "task_steps": task_steps,
            "task_nodes": task_nodes,
            "task_links": task_links,
        }

    @staticmethod
    def _has_explicit_starting_text(user_requirement: str) -> bool:
        text = " ".join(str(user_requirement or "").lower().split())
        if "starting point" not in text:
            return False
        return "use the text" in text or "starting point" in text

    @staticmethod
    def _has_explicit_source_text(user_requirement: str) -> bool:
        raw_text = str(user_requirement or "")
        text = " ".join(raw_text.lower().split())
        if WorkflowMixin._has_explicit_starting_text(user_requirement):
            return True
        if "article's content" in text or "article content" in text or "content:" in text:
            return True
        return re.search(r"[\"'“”‘’][^\"'“”‘’]{20,}[\"'“”‘’]", raw_text) is not None

    @staticmethod
    def _get_scalar_argument(arguments: Any, index: int = 0) -> Any:
        if not isinstance(arguments, list) or index >= len(arguments):
            return None
        item = arguments[index]
        if isinstance(item, dict):
            return item.get("value")
        return item

    @staticmethod
    def _set_scalar_argument(arguments: Any, value: Any, index: int = 0) -> List[Any]:
        items = list(arguments) if isinstance(arguments, list) else []
        while len(items) <= index:
            items.append(None)
        if isinstance(items[index], dict):
            updated = dict(items[index])
            updated["value"] = value
            items[index] = updated
        else:
            items[index] = value
        return items

    def _repair_normalized_workflow(
        self,
        workflow: Dict[str, Any],
        user_requirement: str,
    ) -> Dict[str, Any]:
        task_nodes = workflow.get("task_nodes", [])
        if not isinstance(task_nodes, list) or len(task_nodes) < 2:
            return workflow

        repaired_nodes: List[Any] = [dict(node) if isinstance(node, dict) else node for node in task_nodes]

        # When the request gives an explicit starting text, prefer simplifying
        # that seed text before searching with it.
        if self._has_explicit_starting_text(user_requirement) and len(repaired_nodes) >= 2:
            first = repaired_nodes[0]
            second = repaired_nodes[1]
            if isinstance(first, dict) and isinstance(second, dict):
                first_task = str(first.get("task", "")).strip().lower()
                second_task = str(second.get("task", "")).strip().lower()
                first_arg = self._get_scalar_argument(first.get("arguments", []), 0)
                second_arg = self._get_scalar_argument(second.get("arguments", []), 0)
                if (
                    first_task == "text search"
                    and second_task == "text simplifier"
                    and isinstance(first_arg, str)
                    and self._parse_workflow_node_ref(first_arg) is None
                    and second_arg == self._workflow_node_ref(0)
                ):
                    simplified_node = dict(second)
                    simplified_node["arguments"] = self._set_scalar_argument(
                        second.get("arguments", []),
                        first_arg,
                        0,
                    )
                    search_node = dict(first)
                    search_node["arguments"] = self._set_scalar_argument(
                        first.get("arguments", []),
                        self._workflow_node_ref(0),
                        0,
                    )
                    repaired_nodes[0] = simplified_node
                    repaired_nodes[1] = search_node

        # If a topic-generation step is immediately followed by text-to-image,
        # keep the image prompt on the latest topic branch.
        for idx in range(1, len(repaired_nodes)):
            prev_node = repaired_nodes[idx - 1]
            cur_node = repaired_nodes[idx]
            if not isinstance(prev_node, dict) or not isinstance(cur_node, dict):
                continue
            prev_task = str(prev_node.get("task", "")).strip().lower()
            cur_task = str(cur_node.get("task", "")).strip().lower()
            if prev_task != "topic generator" or cur_task != "text-to-image":
                continue

            current_arg = self._get_scalar_argument(cur_node.get("arguments", []), 0)
            expected_ref = self._workflow_node_ref(idx - 1)
            if current_arg != expected_ref:
                repaired = dict(cur_node)
                repaired["arguments"] = self._set_scalar_argument(
                    cur_node.get("arguments", []),
                    expected_ref,
                    0,
                )
                repaired_nodes[idx] = repaired

        return {
            "task_steps": workflow.get("task_steps", []),
            "task_nodes": repaired_nodes,
            "task_links": workflow.get("task_links", []),
        }

    @staticmethod
    def _normalize_edge_grounding_mode_name(raw_mode: Any) -> str:
        raw_mode = str(raw_mode or "none").strip().lower()
        aliases = {
            "nearest_valid": "nearest_valid_upstream",
            "nearest": "nearest_valid_upstream",
            "semantic": "semantic_edge_scoring",
            "semantic_edge_scorer": "semantic_edge_scoring",
            "h2": "semantic_edge_scoring",
            "semantic_nearest_priority": "semantic_edge_scoring_h2a",
            "h2a": "semantic_edge_scoring_h2a",
            "semantic_semantic_priority": "semantic_edge_scoring_h2b",
            "h2b": "semantic_edge_scoring_h2b",
        }
        return aliases.get(raw_mode, raw_mode or "none")

    def _resolve_edge_grounding_mode(self) -> str:
        return self._normalize_edge_grounding_mode_name(getattr(self, "_edge_grounding_mode", "none"))

    @staticmethod
    def _copy_compiled_nodes(compiled_nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        copied_nodes: List[Dict[str, Any]] = []
        for node in compiled_nodes:
            copied_nodes.append(
                {
                    "index": int(node.get("index", 0)),
                    "node_ref": str(node.get("node_ref", "")),
                    "output_key": str(node.get("output_key", "")),
                    "task": str(node.get("task", "")),
                    "args": dict(node.get("args", {})) if isinstance(node.get("args", {}), dict) else {},
                    "upstream_inputs": (
                        dict(node.get("upstream_inputs", {}))
                        if isinstance(node.get("upstream_inputs", {}), dict)
                        else {}
                    ),
                }
            )
        return copied_nodes

    def _input_type_signature(self, skill: Any, target_arg_name: str) -> Tuple[str, ...]:
        types = tuple(sorted(self._skill_input_types_for_arg(skill, target_arg_name)))
        if types:
            return types
        return (f"__{str(target_arg_name).strip().lower()}__",)

    def _has_ambiguous_typed_input_peer(self, skill: Any, target_arg_name: str) -> bool:
        target_types = tuple(sorted(self._skill_input_types_for_arg(skill, target_arg_name)))
        if not target_types:
            return False

        match_count = 0
        input_schema = getattr(skill, "input_schema", {}) or {}
        if not isinstance(input_schema, dict):
            return False

        for arg_name in input_schema.keys():
            other_types = tuple(sorted(self._skill_input_types_for_arg(skill, str(arg_name))))
            if other_types == target_types:
                match_count += 1
                if match_count > 1:
                    return True
        return False

    def _valid_upstream_candidates_for_arg(
        self,
        compiled_nodes: List[Dict[str, Any]],
        target_index: int,
        target_skill_name: str,
        target_skill: Any,
        target_arg_name: str,
    ) -> List[int]:
        expected_input_types = self._skill_input_types_for_arg(target_skill, target_arg_name)
        if not expected_input_types:
            return []

        valid_sources: List[int] = []
        for source_idx in range(target_index):
            source_node = compiled_nodes[source_idx]
            source_skill_name = str(source_node.get("task", "")).strip()
            source_skill = self.registry.get(source_skill_name)
            if source_skill is None:
                continue
            dependency_error = self._validate_dependency(
                source_skill_name=source_skill_name,
                source_skill=source_skill,
                target_skill_name=target_skill_name,
                target_skill=target_skill,
                target_arg_name=target_arg_name,
            )
            if dependency_error is None:
                valid_sources.append(source_idx)
        return valid_sources

    def _ground_compiled_workflow_nearest_valid_upstream(
        self,
        compiled_nodes: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        grounded_nodes = self._copy_compiled_nodes(compiled_nodes)

        changes: List[Dict[str, Any]] = []
        for target_idx, node in enumerate(grounded_nodes):
            task_name = str(node.get("task", "")).strip()
            skill = self.registry.get(task_name)
            if skill is None:
                continue

            upstream_inputs = node.get("upstream_inputs", {})
            if not isinstance(upstream_inputs, dict) or not upstream_inputs:
                continue

            updated_upstream_inputs = dict(upstream_inputs)
            for arg_name, current_source_idx in upstream_inputs.items():
                arg_key = str(arg_name).strip()
                if not arg_key:
                    continue
                if self._has_ambiguous_typed_input_peer(skill, arg_key):
                    continue

                valid_sources = self._valid_upstream_candidates_for_arg(
                    grounded_nodes,
                    target_idx,
                    task_name,
                    skill,
                    arg_key,
                )
                if not valid_sources:
                    continue

                chosen_source_idx = max(valid_sources)
                if int(current_source_idx) == chosen_source_idx:
                    continue

                updated_upstream_inputs[arg_key] = chosen_source_idx
                changes.append(
                    {
                        "target_index": target_idx,
                        "target_task": task_name,
                        "arg_name": arg_key,
                        "from": int(current_source_idx),
                        "to": chosen_source_idx,
                        "strategy": "nearest_valid_upstream",
                    }
                )

            node["upstream_inputs"] = updated_upstream_inputs

        return grounded_nodes, changes

    def _get_edge_grounding_memory_context(self, user_requirement: str) -> Dict[str, Any]:
        retriever = getattr(self, "_workflow_retriever", None)
        requirement = str(user_requirement or "").strip()
        if retriever is None or not requirement:
            return {}

        match_actions = getattr(self, "_match_requirement_actions", None)
        query_actions = tuple(match_actions(requirement)) if callable(match_actions) else ()

        cache = getattr(self, "_edge_grounding_retrieval_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._edge_grounding_retrieval_cache = cache

        cache_key = (requirement, query_actions)
        if cache_key not in cache:
            cache[cache_key] = retriever.retrieve(requirement, detected_actions=query_actions)
        value = cache.get(cache_key, {})
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _nearest_grounding_bonus(source_idx: int, target_idx: int) -> float:
        distance = max(int(target_idx) - int(source_idx), 1)
        return 1.0 / float(distance)

    def _memory_transition_score_for_edge(
        self,
        source_skill_name: str,
        target_skill_name: str,
        memory_context: Dict[str, Any],
    ) -> float:
        if not isinstance(memory_context, dict):
            return 0.0

        edge_score = 0.0
        source_best = 0.0
        transitions = memory_context.get("transitions", [])
        if not isinstance(transitions, list):
            return 0.0

        for item in transitions:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source", "")).strip()
            target = str(item.get("target", "")).strip()
            score = max(float(item.get("score", 0.0)), 0.0)
            if source != source_skill_name:
                continue
            source_best = max(source_best, score)
            if target == target_skill_name:
                edge_score = max(edge_score, score)

        if edge_score <= 0.0:
            return 0.0
        if source_best <= 0.0:
            return min(edge_score / 6.0, 1.0)
        return min(edge_score / source_best, 1.0)

    def _action_dependency_score_for_edge(
        self,
        source_skill_name: str,
        target_skill_name: str,
        user_requirement: str,
    ) -> float:
        tag_getter = getattr(self, "_action_tags_for_skill_name", None)
        action_matcher = getattr(self, "_match_requirement_actions", None)
        if not callable(tag_getter):
            return 0.0

        source_tags = [str(tag).strip() for tag in tag_getter(source_skill_name) if str(tag).strip()]
        target_tags = [str(tag).strip() for tag in tag_getter(target_skill_name) if str(tag).strip()]
        if not source_tags or not target_tags:
            return 0.0

        required_actions = (
            {str(item).strip() for item in action_matcher(user_requirement)}
            if callable(action_matcher)
            else set()
        )
        action_positions = {action: idx for idx, action in enumerate(ACTION_CANONICAL_ORDER)}

        best = 0.0
        for source_tag in source_tags:
            source_pos = action_positions.get(source_tag)
            if source_pos is None:
                continue
            for target_tag in target_tags:
                target_pos = action_positions.get(target_tag)
                if target_pos is None:
                    continue

                delta = target_pos - source_pos
                if delta < 0:
                    continue
                if delta == 0:
                    pair_score = 0.4
                else:
                    pair_score = 1.0 / float(max(delta - 1, 1))

                if required_actions:
                    if source_tag in required_actions and target_tag in required_actions:
                        pair_score += 0.25
                    elif target_tag in required_actions:
                        pair_score += 0.15
                    elif source_tag in required_actions:
                        pair_score += 0.05

                best = max(best, pair_score)

        return min(best, 1.25)

    def _modality_match_score_for_edge(
        self,
        source_skill_name: str,
        source_skill: Any,
        target_skill_name: str,
        target_skill: Any,
        target_arg_name: str,
    ) -> float:
        compatibility_getter = getattr(self, "_infer_name_based_link_compatibility", None)
        compatibility = (
            compatibility_getter(source_skill_name, target_skill_name)
            if callable(compatibility_getter)
            else None
        )
        if compatibility is True:
            return 1.0
        if compatibility is False:
            return 0.0

        expected_input_types = self._skill_input_types_for_arg(target_skill, target_arg_name)
        source_output_types = self._skill_output_types(source_skill_name, source_skill)
        if expected_input_types and source_output_types and not source_output_types.isdisjoint(expected_input_types):
            return 0.75
        if expected_input_types or source_output_types:
            return 0.5
        return 0.0

    @staticmethod
    def _semantic_edge_score_weights_for_mode(mode: str) -> Dict[str, float]:
        normalized_mode = str(mode or "semantic_edge_scoring").strip().lower()
        if normalized_mode == "semantic_edge_scoring_h2a":
            return {
                "nearest_bonus": 2.5,
                "memory_transition_score": 0.5,
                "action_dependency_score": 1.0,
                "modality_match_score": 1.0,
            }
        if normalized_mode == "semantic_edge_scoring_h2b":
            return {
                "nearest_bonus": 2.0,
                "memory_transition_score": 0.5,
                "action_dependency_score": 1.5,
                "modality_match_score": 1.0,
            }
        return {
            "nearest_bonus": 2.0,
            "memory_transition_score": 1.5,
            "action_dependency_score": 1.0,
            "modality_match_score": 1.0,
        }

    def _semantic_edge_score(
        self,
        *,
        source_idx: int,
        target_idx: int,
        source_skill_name: str,
        source_skill: Any,
        target_skill_name: str,
        target_skill: Any,
        target_arg_name: str,
        user_requirement: str,
        memory_context: Dict[str, Any],
        score_weights: Dict[str, float],
    ) -> Dict[str, float]:
        nearest_bonus = self._nearest_grounding_bonus(source_idx, target_idx)
        memory_transition_score = self._memory_transition_score_for_edge(
            source_skill_name,
            target_skill_name,
            memory_context,
        )
        action_dependency_score = self._action_dependency_score_for_edge(
            source_skill_name,
            target_skill_name,
            user_requirement,
        )
        modality_match_score = self._modality_match_score_for_edge(
            source_skill_name,
            source_skill,
            target_skill_name,
            target_skill,
            target_arg_name,
        )
        total = (
            float(score_weights.get("nearest_bonus", 0.0)) * nearest_bonus
            + float(score_weights.get("memory_transition_score", 0.0)) * memory_transition_score
            + float(score_weights.get("action_dependency_score", 0.0)) * action_dependency_score
            + float(score_weights.get("modality_match_score", 0.0)) * modality_match_score
        )
        return {
            "nearest_bonus": nearest_bonus,
            "memory_transition_score": memory_transition_score,
            "action_dependency_score": action_dependency_score,
            "modality_match_score": modality_match_score,
            "total": total,
        }

    @staticmethod
    def _semantic_edge_candidate_sort_key(
        item: Dict[str, Any],
        score_weights: Dict[str, float],
    ) -> Tuple[float, float, float, float, float, int, int]:
        score_components = item.get("score_components", {}) or {}
        return (
            float(item.get("score", 0.0)),
            float(score_weights.get("nearest_bonus", 0.0))
            * float(score_components.get("nearest_bonus", 0.0)),
            float(score_weights.get("action_dependency_score", 0.0))
            * float(score_components.get("action_dependency_score", 0.0)),
            float(score_weights.get("memory_transition_score", 0.0))
            * float(score_components.get("memory_transition_score", 0.0)),
            float(score_weights.get("modality_match_score", 0.0))
            * float(score_components.get("modality_match_score", 0.0)),
            int(item.get("is_current", 0)),
            int(item.get("source_idx", -1)),
        )

    def _semantic_edge_candidates_for_arg(
        self,
        compiled_nodes: List[Dict[str, Any]],
        target_idx: int,
        target_skill_name: str,
        target_skill: Any,
        target_arg_name: str,
        current_source_idx: int,
        user_requirement: str,
        memory_context: Dict[str, Any],
        score_weights: Dict[str, float],
    ) -> List[Dict[str, Any]]:
        valid_sources = self._valid_upstream_candidates_for_arg(
            compiled_nodes,
            target_idx,
            target_skill_name,
            target_skill,
            target_arg_name,
        )
        candidates: List[Dict[str, Any]] = []
        for source_idx in valid_sources:
            source_node = compiled_nodes[source_idx]
            source_skill_name = str(source_node.get("task", "")).strip()
            source_skill = self.registry.get(source_skill_name)
            if source_skill is None:
                continue
            score_components = self._semantic_edge_score(
                source_idx=source_idx,
                target_idx=target_idx,
                source_skill_name=source_skill_name,
                source_skill=source_skill,
                target_skill_name=target_skill_name,
                target_skill=target_skill,
                target_arg_name=target_arg_name,
                user_requirement=user_requirement,
                memory_context=memory_context,
                score_weights=score_weights,
            )
            candidates.append(
                {
                    "source_idx": int(source_idx),
                    "source_task": source_skill_name,
                    "score": float(score_components["total"]),
                    "score_components": score_components,
                    "is_current": int(int(source_idx) == int(current_source_idx)),
                }
            )

        candidates.sort(
            key=lambda item: self._semantic_edge_candidate_sort_key(item, score_weights),
            reverse=True,
        )
        return candidates

    def _select_semantic_edge_assignments_for_group(
        self,
        *,
        arg_names: List[str],
        candidate_lists: Dict[str, List[Dict[str, Any]]],
        current_upstream_inputs: Dict[str, int],
    ) -> Dict[str, Dict[str, Any]]:
        ordered_arg_names = sorted(arg_names, key=self._generic_argument_sort_key)
        best_assignment: Dict[str, Dict[str, Any]] = {}
        best_key: Optional[Tuple[float, float, float, int, str]] = None

        def _assignment_key(assignment: Dict[str, Dict[str, Any]]) -> Tuple[float, float, float, int, str]:
            total_score = sum(float(item.get("score", 0.0)) for item in assignment.values())
            current_keep = float(
                sum(
                    1
                    for arg_name, item in assignment.items()
                    if int(item.get("source_idx", -1)) == int(current_upstream_inputs.get(arg_name, -10**9))
                )
            )
            memory_total = sum(
                float(item.get("score_components", {}).get("memory_transition_score", 0.0))
                for item in assignment.values()
            )
            unique_sources = len({int(item.get("source_idx", -1)) for item in assignment.values()})
            source_signature = ",".join(
                f"{arg_name}:{int(assignment[arg_name].get('source_idx', -1))}"
                for arg_name in ordered_arg_names
            )
            return (round(total_score, 8), round(memory_total, 8), current_keep, unique_sources, source_signature)

        def _backtrack(
            position: int,
            used_sources: Set[int],
            assignment: Dict[str, Dict[str, Any]],
        ) -> None:
            nonlocal best_assignment, best_key
            if position >= len(ordered_arg_names):
                key = _assignment_key(assignment)
                if best_key is None or key > best_key:
                    best_key = key
                    best_assignment = dict(assignment)
                return

            arg_name = ordered_arg_names[position]
            for candidate in candidate_lists.get(arg_name, []):
                source_idx = int(candidate.get("source_idx", -1))
                if source_idx in used_sources:
                    continue
                assignment[arg_name] = candidate
                used_sources.add(source_idx)
                _backtrack(position + 1, used_sources, assignment)
                used_sources.remove(source_idx)
                assignment.pop(arg_name, None)

        _backtrack(0, set(), {})
        if best_assignment:
            return best_assignment

        fallback_assignment: Dict[str, Dict[str, Any]] = {}
        for arg_name in ordered_arg_names:
            candidates = candidate_lists.get(arg_name, [])
            if candidates:
                fallback_assignment[arg_name] = candidates[0]
        return fallback_assignment

    def _ground_compiled_workflow_semantic_edge_scoring(
        self,
        compiled_nodes: List[Dict[str, Any]],
        user_requirement: str,
        mode: str,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        grounded_nodes = self._copy_compiled_nodes(compiled_nodes)
        memory_context = self._get_edge_grounding_memory_context(user_requirement)
        score_weights = self._semantic_edge_score_weights_for_mode(mode)
        changes: List[Dict[str, Any]] = []

        for target_idx, node in enumerate(grounded_nodes):
            task_name = str(node.get("task", "")).strip()
            skill = self.registry.get(task_name)
            if skill is None:
                continue

            upstream_inputs = node.get("upstream_inputs", {})
            if not isinstance(upstream_inputs, dict) or not upstream_inputs:
                continue

            updated_upstream_inputs = dict(upstream_inputs)
            grouped_arg_names: Dict[Tuple[str, ...], List[str]] = {}
            for arg_name in upstream_inputs.keys():
                arg_key = str(arg_name).strip()
                if not arg_key:
                    continue
                signature = self._input_type_signature(skill, arg_key)
                grouped_arg_names.setdefault(signature, []).append(arg_key)

            for arg_names in grouped_arg_names.values():
                candidate_lists: Dict[str, List[Dict[str, Any]]] = {}
                for arg_name in arg_names:
                    current_source_idx = int(upstream_inputs.get(arg_name, -1))
                    candidates = self._semantic_edge_candidates_for_arg(
                        grounded_nodes,
                        target_idx,
                        task_name,
                        skill,
                        arg_name,
                        current_source_idx,
                        user_requirement,
                        memory_context,
                        score_weights,
                    )
                    if candidates:
                        candidate_lists[arg_name] = candidates

                if not candidate_lists:
                    continue

                assignments = self._select_semantic_edge_assignments_for_group(
                    arg_names=arg_names,
                    candidate_lists=candidate_lists,
                    current_upstream_inputs=upstream_inputs,
                )
                for arg_name, selected in assignments.items():
                    current_source_idx = int(upstream_inputs.get(arg_name, -1))
                    chosen_source_idx = int(selected.get("source_idx", current_source_idx))
                    if chosen_source_idx == current_source_idx:
                        continue

                    updated_upstream_inputs[arg_name] = chosen_source_idx
                    changes.append(
                        {
                            "target_index": target_idx,
                            "target_task": task_name,
                            "arg_name": arg_name,
                            "from": current_source_idx,
                            "to": chosen_source_idx,
                            "strategy": mode,
                            "score_total": round(float(selected.get("score", 0.0)), 6),
                            "score_weights": {
                                key: round(float(value), 6)
                                for key, value in score_weights.items()
                            },
                            "score_components": {
                                key: round(float(value), 6)
                                for key, value in (selected.get("score_components", {}) or {}).items()
                            },
                        }
                    )

            node["upstream_inputs"] = updated_upstream_inputs

        return grounded_nodes, changes

    def _detect_compiled_workflow_structure(
        self,
        compiled_nodes: List[Dict[str, Any]],
    ) -> str:
        node_count = len(compiled_nodes)
        if node_count <= 1:
            return "single"

        incoming_by_target: Dict[int, Set[int]] = {idx: set() for idx in range(node_count)}
        outgoing_by_source: Dict[int, Set[int]] = {idx: set() for idx in range(node_count)}
        edge_count = 0

        for target_idx, node in enumerate(compiled_nodes):
            upstream_inputs = node.get("upstream_inputs", {})
            if not isinstance(upstream_inputs, dict):
                continue
            unique_sources = {
                int(source_idx)
                for source_idx in upstream_inputs.values()
                if isinstance(source_idx, int)
            }
            for source_idx in unique_sources:
                if source_idx < 0 or source_idx >= node_count or source_idx >= target_idx:
                    return "dag"
                incoming_by_target[target_idx].add(source_idx)
                outgoing_by_source[source_idx].add(target_idx)
                edge_count += 1

        if edge_count != node_count - 1:
            return "dag"
        if incoming_by_target[0]:
            return "dag"

        for target_idx in range(1, node_count):
            incoming = sorted(incoming_by_target[target_idx])
            if len(incoming) != 1 or incoming[0] != target_idx - 1:
                return "dag"

        for source_idx in range(node_count - 1):
            if outgoing_by_source[source_idx] != {source_idx + 1}:
                return "dag"
        if outgoing_by_source[node_count - 1]:
            return "dag"

        return "chain"

    def _detect_workflow_structure(
        self,
        workflow: Dict[str, Any],
    ) -> str:
        normalized_workflow, compiled_nodes = self._prepare_workflow(workflow)
        _ = normalized_workflow
        return self._detect_compiled_workflow_structure(compiled_nodes)

    def _apply_specific_edge_grounding_mode(
        self,
        workflow: Dict[str, Any],
        *,
        user_requirement: str = "",
        mode: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        mode = self._normalize_edge_grounding_mode_name(
            getattr(self, "_edge_grounding_mode", "none") if mode is None else mode
        )
        normalized_workflow, compiled_nodes = self._prepare_workflow(workflow)
        if mode in {"", "none"}:
            return normalized_workflow, {
                "mode": "none",
                "applied": False,
                "change_count": 0,
                "changes": [],
            }

        if mode == "nearest_valid_upstream":
            grounded_compiled_nodes, changes = self._ground_compiled_workflow_nearest_valid_upstream(compiled_nodes)
        elif mode in {"semantic_edge_scoring", "semantic_edge_scoring_h2a", "semantic_edge_scoring_h2b"}:
            grounded_compiled_nodes, changes = self._ground_compiled_workflow_semantic_edge_scoring(
                compiled_nodes,
                user_requirement=user_requirement,
                mode=mode,
            )
        else:
            raise ValueError(f"unsupported edge_grounding_mode: {mode}")

        grounded_workflow = self._canonicalize_compiled_workflow_view(grounded_compiled_nodes)
        grounding_meta = {
            "mode": mode,
            "applied": bool(changes),
            "change_count": len(changes),
            "changes": changes,
        }
        if mode in {"semantic_edge_scoring", "semantic_edge_scoring_h2a", "semantic_edge_scoring_h2b"}:
            grounding_meta["score_weights"] = {
                key: round(float(value), 6)
                for key, value in self._semantic_edge_score_weights_for_mode(mode).items()
            }
        return grounded_workflow, grounding_meta

    def _apply_edge_grounding_mode(
        self,
        workflow: Dict[str, Any],
        user_requirement: str = "",
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        return self._apply_specific_edge_grounding_mode(
            workflow,
            user_requirement=user_requirement,
            mode=None,
        )

    @staticmethod
    def _workflow_node_ref(index: int) -> str:
        return f"<node-{index}>"

    @staticmethod
    def _is_generic_argument_name(name: str) -> bool:
        return bool(re.fullmatch(r"arg\d+", str(name).strip(), flags=re.IGNORECASE))

    @staticmethod
    def _generic_argument_sort_key(name: str) -> Tuple[int, str]:
        match = re.fullmatch(r"arg(\d+)", str(name).strip(), flags=re.IGNORECASE)
        if match:
            return int(match.group(1)), str(name)
        return 10**9, str(name)

    def _ordered_argument_keys_for_task(
        self,
        task_name: str,
        args: Dict[str, Any],
        upstream_inputs: Dict[str, int],
    ) -> List[str]:
        skill = self.registry.get(task_name)
        schema_keys = list(skill.input_schema.keys()) if skill is not None else []

        ordered_keys: List[str] = []
        for key in schema_keys:
            if key in args or key in upstream_inputs:
                ordered_keys.append(str(key))

        for key in list(upstream_inputs.keys()) + list(args.keys()):
            text = str(key)
            if text == "output_key" or text in ordered_keys:
                continue
            ordered_keys.append(text)

        if ordered_keys and all(self._is_generic_argument_name(key) for key in ordered_keys):
            ordered_keys.sort(key=self._generic_argument_sort_key)

        return ordered_keys

    def _canonicalize_compiled_workflow_view(
        self,
        compiled_nodes: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        task_steps: List[str] = []
        task_nodes: List[Dict[str, Any]] = []

        for idx, node in enumerate(compiled_nodes):
            task_name = str(node.get("task", ""))
            args = node.get("args", {})
            upstream_inputs = node.get("upstream_inputs", {})
            if not isinstance(args, dict):
                args = {}
            if not isinstance(upstream_inputs, dict):
                upstream_inputs = {}

            ordered_keys = self._ordered_argument_keys_for_task(task_name, args, upstream_inputs)
            ordered_args: Dict[str, Any] = {}
            for key in ordered_keys:
                if key in upstream_inputs:
                    ordered_args[key] = self._workflow_node_ref(int(upstream_inputs[key]))
                elif key in args:
                    ordered_args[key] = args[key]

            task_steps.append(self._step_text(idx + 1, task_name, ordered_args))

            if ordered_keys and all(self._is_generic_argument_name(key) for key in ordered_keys):
                node_arguments: List[Any] = [ordered_args[key] for key in ordered_keys if key in ordered_args]
            else:
                node_arguments = [
                    {"name": key, "value": ordered_args[key]}
                    for key in ordered_keys
                    if key in ordered_args
                ]

            task_nodes.append({"task": task_name, "arguments": node_arguments})

        return {
            "task_steps": task_steps,
            "task_nodes": task_nodes,
            "task_links": self._infer_workflow_links(compiled_nodes),
        }

    def _compile_task_nodes(self, task_nodes: List[Any]) -> List[Dict[str, Any]]:
        generic_arg_pattern = re.compile(r"arg\d+$", flags=re.IGNORECASE)

        compiled: List[Dict[str, Any]] = []
        for idx, node in enumerate(task_nodes):
            if not isinstance(node, dict):
                raise ValueError(f"workflow output invalid: task_nodes[{idx}] must be an object")

            task_name = str(node.get("task", "")).strip()
            if not task_name:
                raise ValueError(f"workflow output invalid: task_nodes[{idx}].task must be a non-empty string")

            skill = self.registry.get(task_name)
            schema_keys = list(skill.input_schema.keys()) if skill is not None else []
            raw_arguments = node.get("arguments", [])
            if not isinstance(raw_arguments, list):
                raise ValueError(f"workflow output invalid: task_nodes[{idx}].arguments must be a list")

            args: Dict[str, Any] = {}
            upstream_inputs: Dict[str, int] = {}
            dict_items = [arg for arg in raw_arguments if isinstance(arg, dict)]
            scalar_items = [arg for arg in raw_arguments if not isinstance(arg, dict)]

            for arg_item in dict_items:
                arg_name = str(arg_item.get("name", "")).strip()
                arg_value = arg_item.get("value")
                if not arg_name:
                    continue
                ref_index = self._parse_workflow_node_ref(arg_value)
                if ref_index is not None:
                    if ref_index >= idx:
                        raise ValueError(
                            f"workflow output invalid: task_nodes[{idx}].arguments[{arg_name}] "
                            "must reference an earlier node"
                        )
                    upstream_inputs[arg_name] = ref_index
                    continue
                args[arg_name] = arg_value

            if scalar_items:
                if not schema_keys:
                    schema_keys = [f"arg{i + 1}" for i in range(len(scalar_items))]
                unnamed_keys = [key for key in schema_keys if key not in args and key not in upstream_inputs]
                refs = [item for item in scalar_items if self._parse_workflow_node_ref(item) is not None]
                literals = [item for item in scalar_items if self._parse_workflow_node_ref(item) is None]

                if unnamed_keys and all(generic_arg_pattern.fullmatch(key or "") for key in unnamed_keys):
                    ordered_values = refs + literals
                else:
                    ordered_values = scalar_items

                for key, value in zip(unnamed_keys, ordered_values):
                    ref_index = self._parse_workflow_node_ref(value)
                    if ref_index is not None:
                        if ref_index >= idx:
                            raise ValueError(
                                f"workflow output invalid: task_nodes[{idx}].arguments[{key}] "
                                "must reference an earlier node"
                            )
                        upstream_inputs[key] = ref_index
                        continue
                    args[key] = value

            compiled.append(
                {
                    "index": idx,
                    "node_ref": self._workflow_node_ref(idx),
                    "output_key": self._normalize_output_key_name(task_name, idx),
                    "task": task_name,
                    "args": args,
                    "upstream_inputs": upstream_inputs,
                }
            )

        return compiled

    def _prepare_workflow(self, workflow: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        normalized_workflow = self._normalize_workflow_payload(workflow)
        compiled_nodes = self._compile_task_nodes(normalized_workflow["task_nodes"])
        return normalized_workflow, compiled_nodes

    def _compile_workflow(self, workflow: Dict[str, Any]) -> List[Dict[str, Any]]:
        _, compiled_nodes = self._prepare_workflow(workflow)
        return compiled_nodes

    def _infer_workflow_links(self, compiled_nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        links: List[Dict[str, str]] = []
        task_names = [str(node["task"]) for node in compiled_nodes]
        for node in compiled_nodes:
            target = str(node["task"])
            for source_idx in set(node.get("upstream_inputs", {}).values()):
                if 0 <= source_idx < len(task_names):
                    links.append({"source": task_names[source_idx], "target": target})
        return self._dedupe_links(links)

    def _validate_workflow_payload(
        self,
        normalized_workflow: Dict[str, Any],
        compiled_nodes: List[Dict[str, Any]],
    ) -> None:
        for idx, step_text in enumerate(normalized_workflow["task_steps"]):
            if not isinstance(step_text, str) or not step_text.strip():
                raise ValueError(f"workflow task_steps[{idx}] must be a non-empty string")

        if normalized_workflow["task_steps"] and len(normalized_workflow["task_steps"]) != len(normalized_workflow["task_nodes"]):
            raise ValueError("workflow task_steps length must match task_nodes length")

        normalized_links = self._dedupe_links(
            [link for link in normalized_workflow["task_links"] if isinstance(link, dict)]
        )
        inferred_links = self._infer_workflow_links(compiled_nodes)

        declared_pairs = {(link["source"], link["target"]) for link in normalized_links}
        inferred_pairs = {(link["source"], link["target"]) for link in inferred_links}
        if declared_pairs != inferred_pairs:
            missing = sorted(f"{src}->{dst}" for src, dst in inferred_pairs - declared_pairs)
            extra = sorted(f"{src}->{dst}" for src, dst in declared_pairs - inferred_pairs)
            issues: List[str] = []
            if missing:
                issues.append(f"missing={missing}")
            if extra:
                issues.append(f"extra={extra}")
            raise ValueError("workflow task_links inconsistent with task_nodes: " + "; ".join(issues))

    def _validate_prepared_workflow(
        self,
        normalized_workflow: Dict[str, Any],
        compiled_nodes: List[Dict[str, Any]],
    ) -> None:
        self._validate_workflow_payload(normalized_workflow, compiled_nodes)
        self._validate_compiled_workflow(compiled_nodes)

    @staticmethod
    def _extract_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: List[str] = []
            for chunk in content:
                if isinstance(chunk, dict):
                    text = chunk.get("text")
                    if text:
                        chunks.append(str(text))
                elif isinstance(chunk, str):
                    chunks.append(chunk)
            return "\n".join(chunks).strip()
        return str(content or "")

    @staticmethod
    def _build_execution_from_ctx(ctx: Dict[str, Any]) -> Dict[str, Any]:
        trace = ctx.get("trace", [])
        results: List[Dict[str, Any]] = []
        for idx, item in enumerate(trace, start=1):
            results.append(
                {
                    "id": idx,
                    "skill": item.get("skill"),
                    "ok": item.get("ok", False),
                    "args": item.get("args", {}),
                    "output": item.get("output"),
                    "error": item.get("error"),
                }
            )
        return {"results": results, "context": ctx}

    @staticmethod
    def _step_to_node_ref(step_payload: Dict[str, Any], fallback_idx: int) -> str:
        step_id = step_payload.get("id")
        if isinstance(step_id, int) and step_id > 0:
            return f"<node-{step_id - 1}>"
        if isinstance(step_id, str):
            text = step_id.strip()
            if text:
                match = re.fullmatch(r"<node-(\d+)>", text)
                if match:
                    return f"<node-{int(match.group(1))}>"
                match = re.fullmatch(r"(?i)node[-_]?(\d+)", text)
                if match:
                    return f"<node-{int(match.group(1))}>"
                if text.isdigit() and int(text) >= 0:
                    return f"<node-{int(text)}>"
        return f"<node-{fallback_idx}>"

    @staticmethod
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

    @staticmethod
    def _step_text(index: int, task_name: str, ordered_args: Dict[str, Any]) -> str:
        arg_pairs = [f"{key}={value}" for key, value in ordered_args.items()]
        suffix = ", ".join(arg_pairs) if arg_pairs else "no arguments"
        return f"Step {index}: Call {task_name} with {suffix}."

    def _build_workflow_view(self, steps: List[Dict[str, Any]]) -> Dict[str, Any]:
        task_steps: List[str] = []
        task_nodes: List[Dict[str, Any]] = []
        task_links: List[Dict[str, str]] = []
        output_key_to_task_name: Dict[str, str] = {}
        output_key_to_node_ref: Dict[str, str] = {}

        for seq_idx, step in enumerate(steps):
            if not isinstance(step, dict):
                continue

            task_name = str(step.get("skill", "unknown_skill"))
            args = step.get("args", {})
            if not isinstance(args, dict):
                args = {}
            input_map = self._get_step_input_map(step)

            node_ref = self._step_to_node_ref(step, seq_idx)
            ordered_args = dict(args)
            for arg_name, upstream_key in input_map.items():
                ordered_args[arg_name] = output_key_to_node_ref.get(upstream_key, upstream_key)

            task_steps.append(self._step_text(seq_idx + 1, task_name, ordered_args))

            node_arguments: List[Any] = []
            mapped_refs: Set[str] = set()
            for key, value in args.items():
                if key == "output_key":
                    continue
                if key == "source_ref" and isinstance(value, str) and value in output_key_to_node_ref:
                    ref = output_key_to_node_ref[value]
                    if ref not in mapped_refs:
                        node_arguments.append(ref)
                        mapped_refs.add(ref)
                    continue
                if isinstance(value, str) and value in output_key_to_node_ref:
                    ref = output_key_to_node_ref[value]
                    if ref not in mapped_refs:
                        node_arguments.append(ref)
                        mapped_refs.add(ref)
                    continue
                node_arguments.append(value)

            for upstream_key in input_map.values():
                ref = output_key_to_node_ref.get(upstream_key, upstream_key)
                if ref in mapped_refs:
                    continue
                node_arguments.append(ref)
                mapped_refs.add(ref)

            task_nodes.append({"task": task_name, "arguments": node_arguments})

            source_ref = args.get("source_ref")
            if isinstance(source_ref, str) and source_ref in output_key_to_task_name:
                task_links.append({"source": output_key_to_task_name[source_ref], "target": task_name})
            for upstream_key in input_map.values():
                source_task = output_key_to_task_name.get(upstream_key)
                if source_task:
                    task_links.append({"source": source_task, "target": task_name})

            output_key = self._get_step_output_key(step)
            if output_key:
                output_key_to_task_name[output_key] = task_name
                output_key_to_node_ref[output_key] = node_ref

        return {
            "task_steps": task_steps,
            "task_nodes": task_nodes,
            "task_links": self._dedupe_links(task_links),
        }


    def _validate_compiled_workflow(self, compiled_nodes: List[Dict[str, Any]]) -> None:
        if not isinstance(compiled_nodes, list) or not compiled_nodes:
            raise ValueError("workflow must contain at least one task node")

        seen: set[str] = set()
        for idx, node in enumerate(compiled_nodes):
            skill_name = node.get("task")
            args = node.get("args", {})
            upstream_inputs = node.get("upstream_inputs", {})
            if not isinstance(args, dict):
                raise ValueError(f"task_nodes[{idx}] arguments must resolve to an object")
            if not isinstance(upstream_inputs, dict):
                raise ValueError(f"task_nodes[{idx}] upstream inputs must resolve to an object")

            skill = self.registry.get(str(skill_name))
            if skill is None:
                raise ValueError(f"task_nodes[{idx}] unknown skill: {skill_name}")

            provided_arg_keys = set(args.keys()) | set(upstream_inputs.keys())
            provided_arg_keys.add("output_key")
            if "source_ref" in skill.input_schema and upstream_inputs and "source_ref" not in provided_arg_keys:
                provided_arg_keys.add("source_ref")

            missing = [key for key in skill.input_schema.keys() if key not in provided_arg_keys]
            if missing:
                raise ValueError(f"task_nodes[{idx}] missing args for {skill_name}: {missing}")

            for arg_name, source_idx in upstream_inputs.items():
                if not isinstance(source_idx, int) or source_idx < 0 or source_idx >= idx:
                    raise ValueError(
                        f"task_nodes[{idx}] upstream input {arg_name} for {skill_name} "
                        f"references invalid source index {source_idx}"
                    )
                source_node = compiled_nodes[source_idx]
                source_skill_name = str(source_node.get("task", "")).strip()
                source_skill = self.registry.get(source_skill_name)
                if source_skill is None:
                    raise ValueError(
                        f"task_nodes[{idx}] upstream input {arg_name} for {skill_name} "
                        f"references unknown skill: {source_skill_name}"
                    )
                dependency_error = self._validate_dependency(
                    source_skill_name=source_skill_name,
                    source_skill=source_skill,
                    target_skill_name=str(skill_name),
                    target_skill=skill,
                    target_arg_name=str(arg_name),
                )
                if dependency_error:
                    raise ValueError(f"task_nodes[{idx}] invalid dependency grounding: {dependency_error}")

            if skill.depends_on_all:
                missed_all = [s for s in skill.depends_on_all if s not in seen]
                if missed_all:
                    raise ValueError(
                        f"task_nodes[{idx}] dependency not satisfied for {skill_name}: missing depends_on_all={missed_all}"
                    )

            if skill.depends_on_any and not any(s in seen for s in skill.depends_on_any):
                source_ref = args.get("source_ref")
                source_ref_idx = self._parse_workflow_node_ref(source_ref)
                source_from_plan = source_ref_idx is not None and source_ref_idx < idx
                source_external = isinstance(source_ref, str) and source_ref.strip() == "external_input"
                has_upstream_input = bool(upstream_inputs)
                if not (has_upstream_input or source_from_plan or source_external):
                    raise ValueError(
                        f"task_nodes[{idx}] dependency not satisfied for {skill_name}: "
                        f"requires one of depends_on_any={skill.depends_on_any}"
                    )

            seen.add(str(skill_name))

    def validate_plan(self, workflow: Dict[str, Any]) -> None:
        normalized_workflow, compiled_nodes = self._prepare_workflow(workflow)
        self._validate_prepared_workflow(normalized_workflow, compiled_nodes)

    async def execute_plan(self, workflow: Dict[str, Any]) -> Dict[str, Any]:
        normalized_workflow, compiled_nodes = self._prepare_workflow(workflow)
        self._validate_prepared_workflow(normalized_workflow, compiled_nodes)

        ctx: Dict[str, Any] = {"trace": [], "artifacts": {}}
        results: List[Dict[str, Any]] = []
        artifacts = ctx["artifacts"]

        for node in compiled_nodes:
            node_index = int(node["index"])
            node_ref = str(node["node_ref"])
            output_key = str(node["output_key"])
            skill_name = str(node["task"])
            resolved_args: Dict[str, Any] = dict(node.get("args", {}))
            upstream_inputs = node.get("upstream_inputs", {})
            try:
                for arg_name, source_idx in upstream_inputs.items():
                    source_ref = self._workflow_node_ref(int(source_idx))
                    source_output_key = str(compiled_nodes[int(source_idx)]["output_key"])
                    if arg_name == "source_ref":
                        resolved_args[arg_name] = source_output_key
                        continue
                    if source_ref in artifacts:
                        resolved_args[arg_name] = artifacts[source_ref]
                    elif source_output_key in artifacts:
                        resolved_args[arg_name] = artifacts[source_output_key]
                    else:
                        raise ValueError(f"missing upstream artifact: {source_ref}")

                if "source_ref" not in resolved_args and upstream_inputs:
                    unique_sources = sorted(set(int(idx) for idx in upstream_inputs.values()))
                    if len(unique_sources) == 1:
                        resolved_args["source_ref"] = str(compiled_nodes[unique_sources[0]]["output_key"])

                resolved_args["output_key"] = output_key

                skill_pkg = self.registry.load_skill(skill_name)
                output = skill_pkg.run(resolved_args, ctx)
                artifacts[node_ref] = output
                artifacts[output_key] = output
                item = {
                    "id": node_index + 1,
                    "node_ref": node_ref,
                    "skill": skill_name,
                    "args": resolved_args,
                    "upstream_inputs": {
                        str(arg_name): self._workflow_node_ref(int(source_idx))
                        for arg_name, source_idx in upstream_inputs.items()
                    },
                    "output_key": output_key,
                    "ok": True,
                    "output": output,
                }
            except Exception as e:
                item = {
                    "id": node_index + 1,
                    "node_ref": node_ref,
                    "skill": skill_name,
                    "args": resolved_args,
                    "upstream_inputs": {
                        str(arg_name): self._workflow_node_ref(int(source_idx))
                        for arg_name, source_idx in upstream_inputs.items()
                    },
                    "output_key": output_key,
                    "ok": False,
                    "error": str(e),
                }
            results.append(item)
            ctx["trace"].append(item)

        return {"results": results, "context": ctx}
