import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .models import SkillMetadata
from .workflow_memory import _edge_is_schema_compatible, _infer_task_modalities


class WorkflowMixin:
    MEDIA_EXTENSION_MODALITIES: Dict[str, str] = {
        ".wav": "audio",
        ".mp3": "audio",
        ".m4a": "audio",
        ".aac": "audio",
        ".flac": "audio",
        ".ogg": "audio",
        ".mp4": "video",
        ".mov": "video",
        ".avi": "video",
        ".mkv": "video",
        ".webm": "video",
        ".png": "image",
        ".jpg": "image",
        ".jpeg": "image",
        ".gif": "image",
        ".bmp": "image",
        ".webp": "image",
        ".txt": "text",
        ".md": "text",
        ".csv": "text",
        ".json": "text",
        ".html": "text",
        ".htm": "text",
    }

    def _validate_step_args(self, skill: SkillMetadata, args: Dict[str, Any]) -> List[str]:
        missing = []
        for key in skill.input_schema.keys():
            if key not in args:
                missing.append(key)
        return missing

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

    def _build_compiled_workflow_graph(self, compiled_nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
        node_count = len(compiled_nodes)
        adjacency: Dict[int, Set[int]] = {idx: set() for idx in range(node_count)}
        reverse: Dict[int, Set[int]] = {idx: set() for idx in range(node_count)}
        invalid_references: List[str] = []

        for target_idx, node in enumerate(compiled_nodes):
            upstream_inputs = node.get("upstream_inputs", {})
            if not isinstance(upstream_inputs, dict):
                invalid_references.append(f"invalid_upstream_inputs:{target_idx}")
                continue
            for arg_name, source_idx in upstream_inputs.items():
                if not isinstance(source_idx, int):
                    invalid_references.append(f"invalid_reference:{target_idx}:{arg_name}:{source_idx}")
                    continue
                if source_idx < 0 or source_idx >= node_count:
                    invalid_references.append(f"out_of_range_reference:{target_idx}:{arg_name}:{source_idx}")
                    continue
                if source_idx >= target_idx:
                    invalid_references.append(f"non_earlier_reference:{target_idx}:{arg_name}:{source_idx}")
                    continue
                adjacency[source_idx].add(target_idx)
                reverse[target_idx].add(source_idx)

        indegree = {idx: len(reverse[idx]) for idx in range(node_count)}
        outdegree = {idx: len(adjacency[idx]) for idx in range(node_count)}
        roots = [idx for idx in range(node_count) if indegree[idx] == 0]
        leaves = [idx for idx in range(node_count) if outdegree[idx] == 0]
        isolated = [idx for idx in range(node_count) if indegree[idx] == 0 and outdegree[idx] == 0 and node_count > 1]

        remaining_indegree = dict(indegree)
        frontier = [idx for idx in range(node_count) if remaining_indegree[idx] == 0]
        topo_order: List[int] = []
        while frontier:
            current = frontier.pop(0)
            topo_order.append(current)
            for neighbor in adjacency[current]:
                remaining_indegree[neighbor] -= 1
                if remaining_indegree[neighbor] == 0:
                    frontier.append(neighbor)
        has_cycle = len(topo_order) != node_count

        undirected: Dict[int, Set[int]] = {idx: set() for idx in range(node_count)}
        for source_idx, targets in adjacency.items():
            for target_idx in targets:
                undirected[source_idx].add(target_idx)
                undirected[target_idx].add(source_idx)

        components: List[List[int]] = []
        remaining_nodes = set(range(node_count))
        while remaining_nodes:
            start = min(remaining_nodes)
            stack = [start]
            component: List[int] = []
            remaining_nodes.remove(start)
            while stack:
                current = stack.pop()
                component.append(current)
                for neighbor in undirected[current]:
                    if neighbor in remaining_nodes:
                        remaining_nodes.remove(neighbor)
                        stack.append(neighbor)
            components.append(sorted(component))

        edges = sorted(
            (source_idx, target_idx)
            for source_idx, targets in adjacency.items()
            for target_idx in targets
        )
        return {
            "node_count": node_count,
            "edge_count": len(edges),
            "edges": edges,
            "roots": roots,
            "leaves": leaves,
            "isolated_nodes": isolated,
            "invalid_references": invalid_references,
            "has_cycle": has_cycle,
            "topological_order": topo_order,
            "component_count": len(components),
            "components": components,
        }

    @classmethod
    def _infer_literal_modalities(cls, value: Any) -> Set[str]:
        if not isinstance(value, str):
            return set()
        text = value.strip().lower()
        if not text:
            return set()
        for suffix, modality in cls.MEDIA_EXTENSION_MODALITIES.items():
            if text.endswith(suffix):
                return {modality}
        if re.search(r"(?:https?://|www\.)", text):
            return {"text"}
        return set()

    def _primary_literal_input(self, skill: SkillMetadata, args: Dict[str, Any], upstream_inputs: Dict[str, int]) -> Tuple[Optional[str], Any]:
        candidate_keys: List[str] = []
        for preferred in ("arg1", "source_ref", "path", "url", "file", "input", "text", "audio", "image", "video"):
            if preferred not in candidate_keys:
                candidate_keys.append(preferred)
        for key in skill.input_schema.keys():
            text = str(key)
            if text not in candidate_keys:
                candidate_keys.append(text)
        for key in args.keys():
            text = str(key)
            if text not in candidate_keys:
                candidate_keys.append(text)

        for key in candidate_keys:
            if key in upstream_inputs:
                if key == "arg1":
                    return None, None
                continue
            if key not in args:
                continue
            value = args[key]
            if self._parse_workflow_node_ref(value) is not None:
                continue
            if isinstance(value, str) and value.strip():
                return key, value
        return None, None

    def _analyze_compiled_workflow(self, compiled_nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
        failures: List[str] = []
        warnings: List[str] = []
        graph = self._build_compiled_workflow_graph(compiled_nodes)
        schema_failures: List[str] = []
        schema_warnings: List[str] = []
        dependency_failures: List[str] = []
        modality_failures: List[str] = []
        modality_warnings: List[str] = []

        if not isinstance(compiled_nodes, list) or not compiled_nodes:
            failures.append("empty_workflow")
            return {
                "failures": failures,
                "warnings": warnings,
                "schema_failures": schema_failures,
                "schema_warnings": schema_warnings,
                "dependency_failures": dependency_failures,
                "modality_failures": modality_failures,
                "modality_warnings": modality_warnings,
                "graph": graph,
                "schema_ok": False,
                "dependency_ok": False,
                "modality_ok": False,
                "dag_ok": False,
            }

        if graph["invalid_references"]:
            failures.extend(graph["invalid_references"])
        if graph["has_cycle"]:
            failures.append("graph_cycle")
        if graph["isolated_nodes"]:
            failures.append(f"isolated_nodes:{','.join(str(idx) for idx in graph['isolated_nodes'])}")
        if graph["component_count"] > 1:
            failures.append(f"disconnected_components:{graph['component_count']}")

        seen: set[str] = set()
        for idx, node in enumerate(compiled_nodes):
            skill_name = node.get("task")
            args = node.get("args", {})
            upstream_inputs = node.get("upstream_inputs", {})
            if not isinstance(args, dict):
                schema_failures.append(f"invalid_args_object:{idx}")
                continue
            if not isinstance(upstream_inputs, dict):
                schema_failures.append(f"invalid_upstream_inputs_object:{idx}")
                continue

            skill = self.registry.get(str(skill_name))
            if skill is None:
                schema_warnings.append(f"unknown_skill:{idx}:{skill_name}")
                continue

            provided_arg_keys = set(args.keys()) | set(upstream_inputs.keys())
            provided_arg_keys.add("output_key")
            if "source_ref" in skill.input_schema and upstream_inputs and "source_ref" not in provided_arg_keys:
                provided_arg_keys.add("source_ref")

            missing = [key for key in skill.input_schema.keys() if key not in provided_arg_keys]
            if missing:
                schema_failures.append(f"missing_args:{idx}:{skill_name}:{','.join(str(key) for key in missing)}")

            if skill.depends_on_all:
                missed_all = [s for s in skill.depends_on_all if s not in seen]
                if missed_all:
                    dependency_failures.append(
                        f"depends_on_all:{idx}:{skill_name}:{','.join(str(item) for item in missed_all)}"
                    )

            if skill.depends_on_any and not any(s in seen for s in skill.depends_on_any):
                source_ref = args.get("source_ref")
                source_ref_idx = self._parse_workflow_node_ref(source_ref)
                source_from_plan = source_ref_idx is not None and source_ref_idx < idx
                source_external = isinstance(source_ref, str) and source_ref.strip() == "external_input"
                has_upstream_input = bool(upstream_inputs)
                if not (has_upstream_input or source_from_plan or source_external):
                    dependency_failures.append(
                        f"depends_on_any:{idx}:{skill_name}:{','.join(str(item) for item in skill.depends_on_any)}"
                    )

            for arg_name, source_idx in upstream_inputs.items():
                if not isinstance(source_idx, int) or source_idx < 0 or source_idx >= len(compiled_nodes):
                    continue
                source_task = str(compiled_nodes[source_idx].get("task", "")).strip()
                target_task = str(skill_name).strip()
                if not _edge_is_schema_compatible(source_task, target_task):
                    modality_warnings.append(
                        f"modality_edge:{source_idx}->{idx}:{source_task}->{target_task}:{arg_name}"
                    )

            _, primary_value = self._primary_literal_input(skill, args, upstream_inputs)
            literal_modalities = self._infer_literal_modalities(primary_value)
            expected_inputs = set(_infer_task_modalities(str(skill_name)).get("inputs", tuple()))
            if literal_modalities and expected_inputs and not (literal_modalities & expected_inputs):
                modality_failures.append(
                    f"root_input_modality_mismatch:{idx}:{skill_name}:{','.join(sorted(literal_modalities))}->{','.join(sorted(expected_inputs))}"
                )

            seen.add(str(skill_name))

        failures.extend(schema_failures)
        failures.extend(dependency_failures)
        failures.extend(modality_failures)
        warnings.extend(schema_warnings)
        warnings.extend(modality_warnings)
        return {
            "failures": failures,
            "warnings": warnings,
            "schema_failures": schema_failures,
            "schema_warnings": schema_warnings,
            "dependency_failures": dependency_failures,
            "modality_failures": modality_failures,
            "modality_warnings": modality_warnings,
            "graph": graph,
            "schema_ok": not schema_failures,
            "dependency_ok": not dependency_failures,
            "modality_ok": not modality_failures,
            "dag_ok": not (
                graph["invalid_references"] or graph["has_cycle"] or graph["isolated_nodes"] or graph["component_count"] > 1
            ),
        }

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
