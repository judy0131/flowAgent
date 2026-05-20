import json
import re
from dataclasses import replace
from typing import Any, Dict, List, Optional, Set, Tuple

from .retrieval import score_workflow_with_retrieval_context
from .serialization import _safe_json_dumps


class PlanningMixin:
    def _get_candidate_prompt_mode(self) -> str:
        return str(getattr(self, "_candidate_prompt_mode", "legacy") or "legacy").strip().lower()

    def _strict_planning_prompt_enabled(self) -> bool:
        # Compatibility flag retained for existing configs and CLI plumbing.
        return bool(getattr(self, "_enable_strict_planning_prompt", False))

    def _action_checklist_enabled(self) -> bool:
        # Compatibility flag retained for existing configs and CLI plumbing.
        return bool(getattr(self, "_enable_action_checklist", False))

    def _strict_planning_prompt_block(self) -> str:
        # The strict rules are now part of the global base prompt.
        return ""

    @staticmethod
    def _base_constrained_planner_prompt_block() -> str:
        return """
You are a constrained workflow planner.

Your task is to convert the user instruction into the minimal executable tool invocation graph.

Important rules:
1. Use only tools from the available skill list.
2. Every selected tool must correspond to an explicit user-requested action.
3. Do not add optional, helpful, bridge, or intermediate tools unless the user explicitly requires them.
4. Do not omit any explicit user-requested action.
5. Do not replace one tool with a multi-tool workaround if the exact tool exists.
6. Copy user-provided file names, phrases, topics, styles, and parameter values exactly.
7. Use <node-i> only when the downstream tool directly consumes the output of node i.
8. task_nodes must be in execution order.
9. task_links must exactly match the <node-i> references in arguments.
10. Return JSON only.
"""

    def _action_checklist_prompt_block(self, required_actions: List[str]) -> str:
        required_lines = ""
        if required_actions:
            required_lines = "\nDetected explicit actions:\n" + "\n".join(
                f"- {self._action_prompt_text(action)}" for action in required_actions
            )
        return f"""
Action checklist:
- First extract explicit user actions internally before selecting tools.
- Then map each explicit action to exactly one required tool whenever the skill schema allows it.
- Every extracted action must be covered by at least one tool.
- No extra action is allowed.
- Do not output the checklist itself; output only the workflow JSON.{required_lines}
"""

    @staticmethod
    def _workflow_output_schema() -> str:
        return """
{
  "task_steps": [
    "Step 1: Call Text Search with arg1=climate change policy.",
    "Step 2: Call Text Summarizer with arg1=<node-0>.",
    "Step 3: Call Keyword Extractor with arg1=<node-1>."
  ],
  "task_nodes": [
    {
      "task": "Text Search",
      "arguments": ["climate change policy"]
    },
    {
      "task": "Text Summarizer",
      "arguments": ["<node-0>"]
    },
    {
      "task": "Keyword Extractor",
      "arguments": ["<node-1>"]
    }
  ],
  "task_links": [
    {
      "source": "Text Search",
      "target": "Text Summarizer"
    },
    {
      "source": "Text Summarizer",
      "target": "Keyword Extractor"
    }
  ]
}"""

    @staticmethod
    def _workflow_good_examples() -> str:
        return """
Example 1: valid single-step workflow
{
  "task_steps": [
    "Step 1: Call Text Simplifier with arg1=Climate change and its impact on polar bears."
  ],
  "task_nodes": [
    {
      "task": "Text Simplifier",
      "arguments": ["Climate change and its impact on polar bears"]
    }
  ],
  "task_links": []
}

Example 2: valid sequential workflow
{
  "task_steps": [
    "Step 1: Call Text Simplifier with arg1=Climate change and its impact on polar bears.",
    "Step 2: Call Text Search with arg1=<node-0>.",
    "Step 3: Call Text Grammar Checker with arg1=<node-1>.",
    "Step 4: Call Topic Generator with arg1=<node-2>.",
    "Step 5: Call Text-to-Image with arg1=<node-3>."
  ],
  "task_nodes": [
    {
      "task": "Text Simplifier",
      "arguments": ["Climate change and its impact on polar bears"]
    },
    {
      "task": "Text Search",
      "arguments": ["<node-0>"]
    },
    {
      "task": "Text Grammar Checker",
      "arguments": ["<node-1>"]
    },
    {
      "task": "Topic Generator",
      "arguments": ["<node-2>"]
    },
    {
      "task": "Text-to-Image",
      "arguments": ["<node-3>"]
    }
  ],
  "task_links": [
    {
      "source": "Text Simplifier",
      "target": "Text Search"
    },
    {
      "source": "Text Search",
      "target": "Text Grammar Checker"
    },
    {
      "source": "Text Grammar Checker",
      "target": "Topic Generator"
    },
    {
      "source": "Topic Generator",
      "target": "Text-to-Image"
    }
  ]
}

Example 3: valid parallel workflow
{
  "task_steps": [
    "Step 1: Call Text Search with arg1=climate change policy.",
    "Step 2: Call Text Summarizer with arg1=<node-0>.",
    "Step 3: Call Keyword Extractor with arg1=<node-0>."
  ],
  "task_nodes": [
    {
      "task": "Text Search",
      "arguments": ["climate change policy"]
    },
    {
      "task": "Text Summarizer",
      "arguments": ["<node-0>"]
    },
    {
      "task": "Keyword Extractor",
      "arguments": ["<node-0>"]
    }
  ],
  "task_links": [
    {
      "source": "Text Search",
      "target": "Text Summarizer"
    },
    {
      "source": "Text Search",
      "target": "Keyword Extractor"
    }
  ]
}"""

    def _required_action_coverage_block(self, required_actions: List[str]) -> str:
        if not required_actions:
            return ""
        coverage_lines = "\n".join(
            f"- {self._action_prompt_text(action)}" for action in required_actions
        )
        return (
            "Detected explicit actions that must be covered:\n"
            f"{coverage_lines}\n"
        )

    def _build_plan_prompt(self, user_requirement: str, strategy_hint: Optional[str] = None) -> str:
        required_actions = self._match_requirement_actions(user_requirement)
        checklist_block = self._action_checklist_prompt_block(required_actions)
        strategy_block = ""
        if strategy_hint:
            strategy_block = (
                "\nPlanning strategy:\n"
                f"{strategy_hint.strip()}\n"
            )
        coverage_block = self._required_action_coverage_block(required_actions)
        output_schema = self._workflow_output_schema()
        good_examples = self._workflow_good_examples()
        prompt = f"""
{self._base_constrained_planner_prompt_block()}

{checklist_block}
{strategy_block}
{coverage_block}

Workflow construction rules:
- Build the minimal executable workflow that satisfies the explicit user request.
- Use exact skill names from the available skills list.
- Every task node must contain:
  - task: exact skill name from the available skills list
  - arguments: list of argument values
- Use literal user values directly in arguments unless the downstream tool must consume an upstream output.
- Use <node-i> only when the downstream tool directly consumes the output of node i.
- If the request explicitly asks to find, search, browse, or retrieve information, include a retrieval step unless the full source content is already present in the request.
- Preserve independent branches when the user asks for multiple outputs or parallel post-processing.
- If two downstream tools consume the same upstream artifact, connect both to that artifact.
- Do not force independent branches into a linear chain.
- A single-task workflow is valid when one exact tool fully satisfies the request.

Self-check before returning JSON:
- Every explicit user-requested action is covered by at least one task.
- No selected tool is extra, optional, or only helpful.
- task_nodes are in execution order.
- Every <node-i> reference points to an earlier node.
- task_links exactly match the dependencies implied by the <node-i> references.
- task_steps, task_nodes, and task_links describe the same workflow.
- There are no cycles, orphan nodes, or disconnected extra branches.
- No simpler valid workflow exists using the available skills.

Output schema:
{output_schema}

Output requirements:
- Return JSON only.
- Do not wrap the JSON in markdown fences.
- Do not include any explanation outside the JSON.
- Do not include null fields.
- Do not include fields other than task_steps, task_nodes, task_links.

Good examples:
{good_examples}

Available skills:
{json.dumps(self.registry.list_for_prompt())}

User requirement:
{user_requirement.strip()}
"""
        return prompt

    def _build_workflow_repair_prompt(
        self,
        user_requirement: str,
        workflow: Dict[str, Any],
        verification_meta: Dict[str, Any],
    ) -> str:
        failures = [
            str(item)
            for item in verification_meta.get("failures", [])
            if isinstance(item, str) and item
        ]
        warnings = [
            str(item)
            for item in verification_meta.get("warnings", [])
            if isinstance(item, str) and item
        ]
        issue_lines = "\n".join(f"- {item}" for item in failures + warnings) or "- no explicit verifier findings provided"
        workflow_json = _safe_json_dumps(workflow, ensure_ascii=False)
        return f"""
You are a workflow repair assistant.

Your job is to minimally repair the candidate workflow so it better satisfies the user request.
Return strict JSON only using the same schema: task_steps, task_nodes, task_links.

Repair rules:
- Preserve valid parts of the workflow when possible.
- Prefer rewiring or deleting an incorrect node over regenerating the whole plan.
- Only add a new node when it is needed to satisfy an explicit missing user-requested action.
- Keep literal phrases copied from the user requirement unchanged unless the user explicitly asked to rewrite them.
- Use only skills from the available skills list.
- Keep task_nodes in execution order.
- Keep task_links consistent with node references.
- Every <node-i> reference must point to an earlier node.

Verifier findings:
{issue_lines}

Available skills:
{json.dumps(self.registry.list_for_prompt())}

Current workflow:
{workflow_json}

User requirement:
{user_requirement}
"""

    async def _repair_candidate_with_llm(
        self,
        user_requirement: str,
        workflow: Dict[str, Any],
        verification_meta: Dict[str, Any],
        llm_client: Any = None,
    ) -> Dict[str, Any]:
        from langchain_core.messages import HumanMessage, SystemMessage

        prompt = self._build_workflow_repair_prompt(user_requirement, workflow, verification_meta)
        client = llm_client or self.llm
        resp = await client.ainvoke(
            [
                SystemMessage(content="Repair the workflow and output valid JSON only."),
                HumanMessage(content=prompt),
            ]
        )
        raw = (resp.content or "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`").replace("json", "", 1).strip()
        parsed = json.loads(raw)
        normalized = self._normalize_workflow_payload(parsed)
        normalized = self._repair_normalized_workflow(normalized, user_requirement)
        compiled_nodes = self._compile_task_nodes(normalized["task_nodes"])
        return self._canonicalize_compiled_workflow_view(compiled_nodes)

    async def _plan_with_client(
        self,
        user_requirement: str,
        strategy_hint: Optional[str] = None,
        llm_client: Any = None,
    ) -> Dict[str, Any]:
        from langchain_core.messages import HumanMessage, SystemMessage

        prompt = self._build_plan_prompt(user_requirement, strategy_hint=strategy_hint)

        client = llm_client or self.llm
        resp = await client.ainvoke(
            [
                SystemMessage(content="Output valid JSON only."),
                HumanMessage(content=prompt),
            ]
        )
        raw = (resp.content or "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`").replace("json", "", 1).strip()
        parsed = json.loads(raw)
        normalized = self._normalize_workflow_payload(parsed)
        normalized = self._repair_normalized_workflow(normalized, user_requirement)
        compiled_nodes = self._compile_task_nodes(normalized["task_nodes"])
        return self._canonicalize_compiled_workflow_view(compiled_nodes)

    async def plan(self, user_requirement: str, strategy_hint: Optional[str] = None) -> Dict[str, Any]:
        return await self._plan_with_client(user_requirement, strategy_hint=strategy_hint)

    @staticmethod
    def _plan_signature_from_compiled(compiled_nodes: List[Dict[str, Any]]) -> str:
        normalized = [
            {
                "task": node["task"],
                "args": node["args"],
                "upstream_inputs": node["upstream_inputs"],
            }
            for node in compiled_nodes
        ]
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True)

    def _plan_signature(self, workflow: Dict[str, Any]) -> str:
        _, compiled_nodes = self._prepare_workflow(workflow)
        return self._plan_signature_from_compiled(compiled_nodes)

    @staticmethod
    def _plan_structure_signature_from_compiled(compiled_nodes: List[Dict[str, Any]]) -> str:
        normalized = [
            {
                "task": node["task"],
                "upstream_inputs": node["upstream_inputs"],
            }
            for node in compiled_nodes
        ]
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True)

    def _plan_structure_signature(self, workflow: Dict[str, Any]) -> str:
        _, compiled_nodes = self._prepare_workflow(workflow)
        return self._plan_structure_signature_from_compiled(compiled_nodes)

    def _score_text_planning_preferences(
        self,
        compiled_nodes: List[Dict[str, Any]],
        user_requirement: str,
    ) -> Dict[str, float]:
        bonus = 0.0
        penalty = 0.0
        if not compiled_nodes:
            return {"bonus": bonus, "penalty": penalty}

        lowered = [str(node.get("task", "")).strip().lower() for node in compiled_nodes]
        has_source_text = self._has_explicit_source_text(user_requirement)
        has_explicit_starting_text = self._has_explicit_starting_text(user_requirement)
        required_actions = set(self._match_requirement_actions(user_requirement))
        requirement_text = " ".join(str(user_requirement or "").lower().split())

        if (
            has_explicit_starting_text
            and len(compiled_nodes) >= 2
            and lowered[0] == "text simplifier"
            and lowered[1] == "text search"
        ):
            first_arg = compiled_nodes[0].get("args", {}).get("arg1")
            second_source = compiled_nodes[1].get("upstream_inputs", {}).get("arg1")
            if isinstance(first_arg, str) and second_source == 0 and "summarize" not in required_actions:
                bonus += 6.0

        if (
            has_explicit_starting_text
            and len(compiled_nodes) >= 2
            and lowered[0] == "text search"
            and lowered[1] == "text simplifier"
        ):
            first_arg = compiled_nodes[0].get("args", {}).get("arg1")
            second_source = compiled_nodes[1].get("upstream_inputs", {}).get("arg1")
            if isinstance(first_arg, str) and second_source == 0:
                penalty -= 8.0

        for idx in range(1, len(compiled_nodes)):
            if lowered[idx - 1] != "topic generator" or lowered[idx] != "text-to-image":
                continue
            source_idx = compiled_nodes[idx].get("upstream_inputs", {}).get("arg1")
            if source_idx == idx - 1:
                bonus += 4.0
            elif isinstance(source_idx, int) and source_idx < idx - 1:
                penalty -= 6.0

        if has_source_text:
            late_search_sources = {
                "text grammar checker",
                "topic generator",
                "text sentiment analysis",
                "keyword extractor",
            }
            for idx, node in enumerate(compiled_nodes):
                if lowered[idx] != "text search":
                    continue
                source_idx = node.get("upstream_inputs", {}).get("arg1")
                if not isinstance(source_idx, int) or source_idx >= idx or source_idx < 0:
                    continue
                source_task = lowered[source_idx]
                if source_task == "text summarizer":
                    if "summarize" in required_actions:
                        bonus += 5.0
                    else:
                        bonus += 2.0
                elif source_task == "text simplifier":
                    if "summarize" in required_actions and "text summarizer" in lowered[:idx]:
                        penalty -= 3.0
                    else:
                        bonus += 4.0
                elif source_task in late_search_sources:
                    penalty -= 4.0

        if (
            has_source_text
            and lowered
            and lowered[0] == "text simplifier"
            and "simplify" not in required_actions
            and "retrieval" not in required_actions
            and "topic" in required_actions
            and any(task in lowered[1:] for task in {"topic generator", "image search", "text-to-image"})
        ):
            penalty -= 5.0

        if "text translator" in lowered and "translate" not in required_actions:
            penalty -= 6.0 * float(lowered.count("text translator"))

        mentions_download = any(token in requirement_text for token in ("download", "downloading", "save the video", "save it locally"))
        if not mentions_download:
            video_edit_tasks = {
                "video speed changer",
                "video stabilizer",
                "video synchronization",
                "video voiceover",
            }
            for idx, task in enumerate(lowered):
                if task != "video downloader" or idx == 0:
                    continue
                if lowered[idx - 1] != "video search":
                    continue
                if any(next_task in video_edit_tasks for next_task in lowered[idx + 1 :]):
                    penalty -= 6.0

        content_driven_audio_effects = (
            "content of the speech" in requirement_text
            or "speech content" in requirement_text
            or ("based on the content" in requirement_text and "speech" in requirement_text)
        )
        if content_driven_audio_effects:
            reasoning_tasks = {
                "audio-to-text",
                "topic generator",
                "text summarizer",
                "keyword extractor",
                "text grammar checker",
            }
            for idx, task in enumerate(lowered):
                if task != "audio effects":
                    continue
                upstream_sources = [
                    lowered[int(source_idx)]
                    for source_idx in compiled_nodes[idx].get("upstream_inputs", {}).values()
                    if isinstance(source_idx, int) and 0 <= int(source_idx) < len(lowered)
                ]
                if any(source_task in reasoning_tasks for source_task in upstream_sources):
                    bonus += 4.0
                elif any(source_task in reasoning_tasks for source_task in lowered[:idx]):
                    bonus += 2.0
                else:
                    penalty -= 5.0

        return {"bonus": bonus, "penalty": penalty}

    def _score_compiled_workflow(
        self,
        workflow: Dict[str, Any],
        compiled_nodes: List[Dict[str, Any]],
        user_requirement: str = "",
    ) -> Dict[str, Any]:
        score = 100.0
        details: Dict[str, Any] = {
            "base": 100.0,
            "length_penalty": 0.0,
            "dependency_bonus": 0.0,
            "dependency_penalty": 0.0,
            "transition_bonus": 0.0,
            "transition_penalty": 0.0,
            "name_modality_bonus": 0.0,
            "name_modality_penalty": 0.0,
            "redundancy_penalty": 0.0,
            "coverage_bonus": 0.0,
            "action_coverage_bonus": 0.0,
            "action_coverage_penalty": 0.0,
            "graph_transition_bonus": 0.0,
            "graph_transition_penalty": 0.0,
            "text_preference_bonus": 0.0,
            "text_preference_penalty": 0.0,
            "memory_bonus": 0.0,
            "memory_penalty": 0.0,
            "memory_transition_bonus": 0.0,
            "memory_transition_penalty": 0.0,
            "memory_motif_bonus": 0.0,
            "memory_start_bonus": 0.0,
            "memory_end_bonus": 0.0,
        }

        skill_names = [str(node.get("task", "")).lower() for node in compiled_nodes]
        action_coverage = self._score_requirement_action_coverage(user_requirement, skill_names)
        details["action_coverage_bonus"] = action_coverage["bonus"]
        details["action_coverage_penalty"] = action_coverage["penalty"]
        details["required_actions"] = action_coverage["required_actions"]
        details["covered_actions"] = action_coverage["covered_actions"]
        details["missing_actions"] = action_coverage["missing_actions"]

        steps_count = len(compiled_nodes)
        if steps_count > 1:
            required_action_count = len(action_coverage["required_actions"])
            if required_action_count > 0:
                expected_step_ceiling = required_action_count + 1
                extra_steps = max(steps_count - expected_step_ceiling, 0)
                penalty = float(extra_steps * 3)
            else:
                penalty = float((steps_count - 1) * 2)
            score -= penalty
            details["length_penalty"] = -penalty

        seen_skills: set[str] = set()
        repeated_skill_count = 0

        for idx, node in enumerate(compiled_nodes):
            skill_name = node.get("task")
            args = node.get("args", {})
            upstream_inputs = node.get("upstream_inputs", {})
            if not isinstance(skill_name, str):
                details["dependency_penalty"] -= 25.0
                score -= 25.0
                continue

            skill = self.registry.get(skill_name)
            if skill is None:
                details["dependency_penalty"] -= 20.0
                score -= 20.0
                continue

            if skill_name in seen_skills:
                repeated_skill_count += 1
            seen_skills.add(skill_name)

            if skill.depends_on_all:
                if all(dep in seen_skills for dep in skill.depends_on_all):
                    details["dependency_bonus"] += 8.0
                    score += 8.0
                else:
                    details["dependency_penalty"] -= 25.0
                    score -= 25.0
            if skill.depends_on_any:
                source_ref = args.get("source_ref") if isinstance(args, dict) else None
                source_ref_idx = self._parse_workflow_node_ref(source_ref)
                has_upstream_ref = bool(upstream_inputs) or (source_ref_idx is not None and source_ref_idx < idx)
                if any(dep in seen_skills for dep in skill.depends_on_any) or has_upstream_ref:
                    details["dependency_bonus"] += 6.0
                    score += 6.0
                else:
                    details["dependency_penalty"] -= 20.0
                    score -= 20.0

            source_ref_idx = self._parse_workflow_node_ref(args.get("source_ref")) if isinstance(args, dict) else None
            if source_ref_idx is not None:
                if source_ref_idx < idx:
                    details["transition_bonus"] += 4.0
                    score += 4.0
                    source_skill_name = str(compiled_nodes[int(source_ref_idx)].get("task", ""))
                    compatibility = self._infer_name_based_link_compatibility(source_skill_name, skill_name)
                    if compatibility is True:
                        details["name_modality_bonus"] += 2.0
                        score += 2.0
                    elif compatibility is False:
                        details["name_modality_penalty"] -= 14.0
                        score -= 14.0
                else:
                    details["transition_penalty"] -= 2.0
                    score -= 2.0
            for source_idx in upstream_inputs.values():
                if source_idx < idx:
                    details["transition_bonus"] += 4.0
                    score += 4.0
                else:
                    details["transition_penalty"] -= 2.0
                    score -= 2.0

                if 0 <= int(source_idx) < len(compiled_nodes):
                    source_skill_name = str(compiled_nodes[int(source_idx)].get("task", ""))
                    compatibility = self._infer_name_based_link_compatibility(source_skill_name, skill_name)
                    if compatibility is True:
                        details["name_modality_bonus"] += 2.0
                        score += 2.0
                    elif compatibility is False:
                        details["name_modality_penalty"] -= 14.0
                        score -= 14.0

        if repeated_skill_count:
            redundancy_penalty = float(repeated_skill_count * 8)
            details["redundancy_penalty"] = -redundancy_penalty
            score -= redundancy_penalty

        requirement_text = user_requirement.lower()
        skill_names = [str(node.get("task", "")).lower() for node in compiled_nodes]
        keyword_to_skill = {
            "质控": {"fastp", "fastqc"},
            "质量": {"fastp", "fastqc", "multiqc"},
            "比对": {"map_with_bwa_mem", "map_with_minimap2"},
            "汇总": {"multiqc", "generate_report"},
            "报告": {"multiqc", "generate_report"},
            "csv": {"load_csv", "filter_rows", "aggregate_sum"},
        }
        covered = 0
        matched_keywords = 0
        for keyword, expected_skills in keyword_to_skill.items():
            if keyword in requirement_text:
                matched_keywords += 1
                if any(name in expected_skills for name in skill_names):
                    covered += 1
        if matched_keywords > 0:
            coverage_bonus = float((covered / matched_keywords) * 15.0)
            details["coverage_bonus"] = coverage_bonus
            score += coverage_bonus

        score += action_coverage["bonus"] + action_coverage["penalty"]

        graph_meta = self._graph_score_compiled(compiled_nodes)
        details["graph_transition_bonus"] = graph_meta["graph_transition_bonus"]
        details["graph_transition_penalty"] = graph_meta["graph_transition_penalty"]
        score += graph_meta["graph_transition_bonus"] + graph_meta["graph_transition_penalty"]

        text_pref_meta = self._score_text_planning_preferences(compiled_nodes, user_requirement)
        details["text_preference_bonus"] = text_pref_meta["bonus"]
        details["text_preference_penalty"] = text_pref_meta["penalty"]
        score += text_pref_meta["bonus"] + text_pref_meta["penalty"]

        memory_meta = score_workflow_with_retrieval_context(
            compiled_nodes,
            self._get_workflow_memory_context(user_requirement),
        )
        details["memory_bonus"] = memory_meta["bonus"]
        details["memory_penalty"] = memory_meta["penalty"]
        details["memory_transition_bonus"] = memory_meta["transition_bonus"]
        details["memory_transition_penalty"] = memory_meta["transition_penalty"]
        details["memory_motif_bonus"] = memory_meta["motif_bonus"]
        details["memory_start_bonus"] = memory_meta["start_bonus"]
        details["memory_end_bonus"] = memory_meta["end_bonus"]
        score += memory_meta["bonus"] + memory_meta["penalty"]

        return {
            "score": round(score, 3),
            "details": details,
        }

    def _score_plan(self, workflow: Dict[str, Any], user_requirement: str = "") -> Dict[str, Any]:
        _, compiled_nodes = self._prepare_workflow(workflow)
        return self._score_compiled_workflow(workflow, compiled_nodes, user_requirement=user_requirement)

    def _get_candidate_llm(self, temperature: float) -> Any:
        rounded = round(float(temperature), 2)
        cache = getattr(self, "_candidate_llm_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._candidate_llm_cache = cache
        if rounded in cache:
            return cache[rounded]

        if not hasattr(self, "llm_config"):
            raise AttributeError("PipelineOrchestratorAgent.llm_config is required for candidate sampling")

        cfg = replace(self.llm_config, temperature=rounded)
        cache[rounded] = self._build_llm_client(cfg)
        return cache[rounded]

    def _candidate_temperature_for_spec(self, spec: Dict[str, Any], round_idx: int) -> float:
        fixed_temperature = getattr(self, "_fixed_candidate_temperature", None)
        if fixed_temperature is not None:
            return float(fixed_temperature)
        base_temperature = float(spec.get("temperature", 0.0))
        return min(base_temperature + round_idx * 0.1, 0.6)

    def _force_generate_all_candidate_families_enabled(self) -> bool:
        return bool(getattr(self, "_force_generate_all_candidate_families", False)) or (
            self._get_candidate_prompt_mode() == "orthogonal_v2"
        )

    def _disable_early_stop_enabled(self) -> bool:
        return bool(getattr(self, "_disable_early_stop", False))

    def _candidate_verifier_enabled(self) -> bool:
        return bool(getattr(self, "_enable_candidate_verifier", True))

    def _candidate_repair_enabled(self) -> bool:
        return self._candidate_verifier_enabled() and bool(
            getattr(self, "_enable_candidate_repair", True)
        )

    @staticmethod
    def _default_candidate_selection_meta() -> Dict[str, Any]:
        return {
            "hard_filter_tier": 1,
            "coverage_complete": True,
            "search_source_ok": True,
            "retrieval_before_topic_ok": True,
            "grammar_after_retrieval_ok": True,
            "video_after_waveform_ok": True,
            "bridge_tool_ok": True,
            "unrequested_bridge_tool_count": 0,
            "required_actions": [],
            "missing_actions": [],
            "failures": [],
            "warnings": [],
            "warning_count": 0,
            "validation_error": None,
            "verifier_pass": True,
            "repairable": False,
            "verifier_enabled": False,
        }

    @staticmethod
    def _strategy_slug(text: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", str(text or "").strip().lower()).strip("_")
        return slug or "memory_graph"

    def _build_memory_graph_strategy_specs(self, user_requirement: str) -> List[Dict[str, Any]]:
        recommend_starts = getattr(self, "recommend_memory_start_skills", None)
        recommend_next = getattr(self, "recommend_memory_next_skills", None)
        if not callable(recommend_starts) or not callable(recommend_next):
            return []

        start_recs = recommend_starts(user_requirement, top_k=4)
        if not isinstance(start_recs, list) or not start_recs:
            return []

        specs: List[Dict[str, Any]] = []
        start_labels = [
            f"{str(item.get('skill', '')).strip()} ({float(item.get('score', 0.0)):.2f})"
            for item in start_recs[:4]
            if str(item.get("skill", "")).strip()
        ]
        if not start_labels:
            return []

        summary_lines = [
            "Use the query-conditioned workflow memory graph as a soft generation prior.",
            f"Preferred start tools: {', '.join(start_labels)}.",
            "After selecting a tool, prefer semantically valid downstream neighbors suggested by this graph before unrelated tools.",
        ]

        for start in start_recs[:3]:
            start_skill = str(start.get("skill", "")).strip()
            if not start_skill:
                continue
            next_recs = recommend_next(
                user_requirement,
                start_skill,
                top_k=3,
                visited_skills={start_skill},
            )
            next_labels = [
                f"{str(item.get('skill', '')).strip()} ({float(item.get('score', 0.0)):.2f})"
                for item in next_recs
                if str(item.get("skill", "")).strip()
            ]
            if next_labels:
                summary_lines.append(
                    f"If you choose {start_skill}, likely next tools are {', '.join(next_labels)}."
                )

        specs.append(
            {
                "name": "memory_graph_guided",
                "hint": "\n".join(summary_lines),
                "temperature": 0.05,
            }
        )

        for idx, start in enumerate(start_recs[:3]):
            start_skill = str(start.get("skill", "")).strip()
            if not start_skill:
                continue
            visited = {start_skill}
            next_recs = recommend_next(
                user_requirement,
                start_skill,
                top_k=3,
                visited_skills=visited,
            )
            branch_lines = [
                f"Try a valid workflow that starts from {start_skill} if it is semantically compatible with the user request.",
            ]
            if next_recs:
                next_labels = [
                    f"{str(item.get('skill', '')).strip()} ({float(item.get('score', 0.0)):.2f})"
                    for item in next_recs
                    if str(item.get("skill", "")).strip()
                ]
                if next_labels:
                    branch_lines.append(
                        f"Preferred downstream neighbors after {start_skill}: {', '.join(next_labels)}."
                    )
                first_next_skill = str(next_recs[0].get("skill", "")).strip()
                if first_next_skill:
                    visited.add(first_next_skill)
                    follow_recs = recommend_next(
                        user_requirement,
                        first_next_skill,
                        top_k=2,
                        visited_skills=visited,
                    )
                    follow_labels = [
                        f"{str(item.get('skill', '')).strip()} ({float(item.get('score', 0.0)):.2f})"
                        for item in follow_recs
                        if str(item.get("skill", "")).strip()
                    ]
                    if follow_labels:
                        branch_lines.append(
                            f"Likely continuation after {first_next_skill}: {', '.join(follow_labels)}."
                        )
            branch_lines.append(
                "Use these graph priors as soft hints only. Follow the user request and skill schemas when they conflict."
            )
            specs.append(
                {
                    "name": f"memory_graph_start_{self._strategy_slug(start_skill)}",
                    "hint": "\n".join(branch_lines),
                    "temperature": 0.12 + idx * 0.08,
                }
            )

            for edge_idx, next_item in enumerate(next_recs[:2]):
                next_skill = str(next_item.get("skill", "")).strip()
                if not next_skill:
                    continue
                edge_visited = {start_skill, next_skill}
                edge_lines = [
                    f"Explore a branch that begins with the graph prior {start_skill} -> {next_skill} if it fits the request.",
                    f"Treat {start_skill} as one possible start, not the only valid start. If it conflicts with clause coverage or skill schemas, abandon this branch.",
                ]
                follow_recs = recommend_next(
                    user_requirement,
                    next_skill,
                    top_k=2,
                    visited_skills=edge_visited,
                )
                follow_labels = [
                    f"{str(item.get('skill', '')).strip()} ({float(item.get('score', 0.0)):.2f})"
                    for item in follow_recs
                    if str(item.get("skill", "")).strip()
                ]
                if follow_labels:
                    edge_lines.append(
                        f"Likely continuation after {start_skill} -> {next_skill}: {', '.join(follow_labels)}."
                    )
                edge_lines.append(
                    "Keep alternative starts available if this branch introduces unnecessary tools or misses required actions."
                )
                specs.append(
                    {
                        "name": (
                            f"memory_graph_edge_{self._strategy_slug(start_skill)}"
                            f"_{self._strategy_slug(next_skill)}"
                        ),
                        "hint": "\n".join(edge_lines),
                        "temperature": min(0.18 + idx * 0.08 + edge_idx * 0.04, 0.45),
                    }
                )

        return specs

    @staticmethod
    def _candidate_pool_target(strategy_specs: List[Dict[str, Any]], candidate_count: int) -> int:
        spec_count = len(strategy_specs)
        if spec_count <= candidate_count:
            return max(candidate_count, spec_count)
        memory_branch_specs = [
            spec
            for spec in strategy_specs
            if str(spec.get("name", "")).startswith("memory_graph_")
        ]
        if not memory_branch_specs:
            return candidate_count
        branch_target = max(candidate_count + 2, min(len(memory_branch_specs) + 1, 5))
        return min(spec_count, max(candidate_count, branch_target))

    @staticmethod
    def _compose_strategy_hint(lines: List[str]) -> str:
        cleaned = [str(line).strip() for line in lines if str(line).strip()]
        return "\n".join(cleaned)

    @staticmethod
    def _make_strategy_spec(
        *,
        family_name: str,
        variant_name: str,
        hint_lines: List[str],
        temperature: float,
    ) -> Dict[str, Any]:
        return {
            "name": str(family_name).strip(),
            "family_name": str(family_name).strip(),
            "variant_name": str(variant_name).strip(),
            "hint": PlanningMixin._compose_strategy_hint(hint_lines),
            "temperature": float(temperature),
        }

    def _build_candidate_strategy_specs(self, user_requirement: str) -> List[Dict[str, Any]]:
        del user_requirement
        candidate_prompt_mode = self._get_candidate_prompt_mode()
        if candidate_prompt_mode in {"orthogonal", "orthogonal_v2"}:
            specs: List[Dict[str, Any]] = []
            if getattr(self, "_include_original_candidate", False):
                specs.append(
                    self._make_strategy_spec(
                        family_name="original",
                        variant_name="baseline",
                        hint_lines=[],
                        temperature=float(getattr(self.llm_config, "temperature", 0.0)),
                    )
                )

            if candidate_prompt_mode == "orthogonal":
                specs.extend(
                    [
                        self._make_strategy_spec(
                            family_name="minimal",
                            variant_name="minimal",
                            hint_lines=[
                                "Prefer the shortest valid workflow.",
                                "Do not add any tool unless it is explicitly required.",
                                "If one tool can satisfy the request, use one tool.",
                                "Avoid bridge tools and optional enhancement steps.",
                            ],
                            temperature=0.0,
                        ),
                        self._make_strategy_spec(
                            family_name="action_coverage",
                            variant_name="action_coverage",
                            hint_lines=[
                                "First identify every explicit action in the user request.",
                                "Ensure each explicit action is covered by at least one tool.",
                                "Do not skip actions such as search, summarize, transcribe, denoise, combine, generate, or convert.",
                                "Do not output the action checklist; output only the workflow JSON.",
                            ],
                            temperature=0.0,
                        ),
                        self._make_strategy_spec(
                            family_name="dependency_first",
                            variant_name="dependency_first",
                            hint_lines=[
                                "Focus on correct dataflow dependencies.",
                                "For each downstream tool, choose the upstream node whose output is directly consumed.",
                                "Do not connect a tool to an earlier node only because the modality matches.",
                                "Prefer semantic dataflow continuity over superficial schema compatibility.",
                            ],
                            temperature=0.1,
                        ),
                        self._make_strategy_spec(
                            family_name="parameter_copy",
                            variant_name="parameter_copy",
                            hint_lines=[
                                "Prioritize exact parameter grounding.",
                                "Copy all user-provided filenames, topics, phrases, styles, speeds, genders, and effect names exactly.",
                                "Do not paraphrase parameter values.",
                                "Use literal user values unless the argument must be an upstream <node-i> output.",
                            ],
                            temperature=0.0,
                        ),
                        self._make_strategy_spec(
                            family_name="parallel_dag",
                            variant_name="parallel_dag",
                            hint_lines=[
                                "Preserve independent branches when the user asks for multiple outputs or parallel post-processing.",
                                "If two downstream tools consume the same upstream artifact, connect both to that artifact.",
                                "Do not force independent branches into a linear chain.",
                            ],
                            temperature=0.1,
                        ),
                    ]
                )
                return specs

            specs.extend([
                self._make_strategy_spec(
                    family_name="minimal",
                    variant_name="fewest_tools",
                    hint_lines=[
                        "Use the fewest tools possible while still satisfying the explicit request.",
                        "Collapse optional intermediate steps unless they are required for correctness.",
                        "Prefer a shorter workflow over a more descriptive workflow when both are valid.",
                    ],
                    temperature=0.0,
                ),
                self._make_strategy_spec(
                    family_name="minimal",
                    variant_name="fewest_transformations",
                    hint_lines=[
                        "Minimize the number of transformations applied to the artifact.",
                        "Avoid adding rewrites, cleanup, or conversion hops unless the user explicitly requested them.",
                        "Prefer a direct producer-to-consumer path over multi-hop reformulation.",
                    ],
                    temperature=0.05,
                ),
                self._make_strategy_spec(
                    family_name="action_coverage",
                    variant_name="strict_explicit_action_coverage",
                    hint_lines=[
                        "Enumerate every explicit user-requested action internally before planning.",
                        "Ensure each explicit action is covered by at least one tool.",
                        "Do not skip search, summarize, transcribe, denoise, combine, generate, or convert when explicitly requested.",
                    ],
                    temperature=0.05,
                ),
                self._make_strategy_spec(
                    family_name="action_coverage",
                    variant_name="step_by_step_decomposition",
                    hint_lines=[
                        "Decompose the request into sequential sub-goals before selecting tools.",
                        "Map each sub-goal to the most direct executable step.",
                        "Preserve the user-requested operation order when the request implies an order.",
                    ],
                    temperature=0.1,
                ),
                self._make_strategy_spec(
                    family_name="action_coverage",
                    variant_name="preserve_every_user_requested_operation",
                    hint_lines=[
                        "Preserve every user-requested operation, even if a shorter workflow exists.",
                        "Do not compress multiple explicit operations into one semantic shortcut when separate tools are needed to show the requested actions.",
                        "If the request asks for multiple post-processing operations, keep them explicit in the workflow.",
                    ],
                    temperature=0.12,
                ),
                self._make_strategy_spec(
                    family_name="parallel_dag",
                    variant_name="preserve_independent_branches",
                    hint_lines=[
                        "Preserve independent branches when the request implies parallel downstream use of the same artifact.",
                        "If two downstream tools can consume the same upstream output, allow them to branch instead of forcing a chain.",
                        "Favor a DAG when multiple outputs or parallel post-processing are requested.",
                    ],
                    temperature=0.1,
                ),
                self._make_strategy_spec(
                    family_name="parallel_dag",
                    variant_name="avoid_forcing_dags_into_chains",
                    hint_lines=[
                        "Do not linearize independent operations only because the modalities match.",
                        "When a branch can terminate independently, keep it independent.",
                        "Prefer topologies that preserve semantic parallelism instead of inventing unnecessary serial dependencies.",
                    ],
                    temperature=0.15,
                ),
                self._make_strategy_spec(
                    family_name="dependency_first",
                    variant_name="semantic_dependency_continuity",
                    hint_lines=[
                        "Maximize semantic dependency continuity between adjacent steps.",
                        "For each downstream tool, bind it to the upstream node whose output is directly consumed.",
                        "Do not attach a downstream tool to an earlier node only because the schema superficially fits.",
                    ],
                    temperature=0.08,
                ),
                self._make_strategy_spec(
                    family_name="parameter_copy",
                    variant_name="exact_parameter_copy",
                    hint_lines=[
                        "Copy filenames, styles, phrases, effect names, and parameter values exactly from the user request.",
                        "Do not paraphrase or normalize literal user values unless a tool requires an upstream <node-i> reference.",
                        "Preserve concrete user-provided values even when an abstract paraphrase sounds more natural.",
                    ],
                    temperature=0.0,
                ),
            ])
            return specs

        specs = [
            {
                "name": "minimal",
                "hint": "Prefer the minimal valid workflow with the fewest steps.",
                "temperature": 0.0,
            },
            {
                "name": "explicit",
                "hint": "Prefer explicit intermediate transformations and validation-friendly dependencies.",
                "temperature": 0.15,
            },
            {
                "name": "parallel",
                "hint": "Prefer structurally distinct workflows with independent parallel branches when valid.",
                "temperature": 0.25,
            },
        ]

        if getattr(self, "_include_original_candidate", False):
            specs = [
                {
                    "name": "original",
                    "hint": "",
                    "temperature": float(getattr(self.llm_config, "temperature", 0.0)),
                }
            ] + specs

        return specs

    def _search_source_structural_issues(
        self,
        compiled_nodes: List[Dict[str, Any]],
        user_requirement: str,
    ) -> List[str]:
        if not self._has_explicit_source_text(user_requirement):
            return []

        lowered = [str(node.get("task", "")).strip().lower() for node in compiled_nodes]
        if "text simplifier" not in lowered:
            return []

        issues: List[str] = []
        required_actions = set(self._match_requirement_actions(user_requirement))
        late_search_sources = {
            "text grammar checker",
            "topic generator",
            "text sentiment analysis",
            "keyword extractor",
        }
        if "summarize" not in required_actions:
            late_search_sources.add("text summarizer")
        for idx, node in enumerate(compiled_nodes):
            if lowered[idx] != "text search":
                continue
            source_idx = node.get("upstream_inputs", {}).get("arg1")
            literal_arg = node.get("args", {}).get("arg1")
            if isinstance(source_idx, int):
                if source_idx >= idx or source_idx < 0:
                    issues.append("search_invalid_upstream_reference")
                    continue
                source_task = lowered[source_idx]
                if source_task in late_search_sources:
                    issues.append(f"search_uses_late_analysis_source:{source_task}")
            elif literal_arg is not None:
                issues.append("search_uses_literal_source_despite_available_text_branch")
            else:
                issues.append("search_missing_explicit_source_binding")
        return issues

    @staticmethod
    def _argument_looks_like_url(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        return bool(re.match(r"^(?:https?://|www\.)", value.strip(), flags=re.IGNORECASE))

    def _unrequested_bridge_tool_issues(
        self,
        compiled_nodes: List[Dict[str, Any]],
        user_requirement: str,
        required_actions: Optional[Set[str]] = None,
    ) -> List[str]:
        if not compiled_nodes:
            return []

        lowered = [str(node.get("task", "")).strip().lower() for node in compiled_nodes]
        requirement_text = " ".join(str(user_requirement or "").lower().split())
        required_action_set = set(required_actions or self._match_requirement_actions(user_requirement))

        mentions_download = any(
            token in requirement_text
            for token in ("download", "downloading", "save the video", "save it locally", "save locally")
        )
        mentions_synchronization = bool(
            re.search(r"\b(sync(?:hroni[sz]\w*)?|timing|align(?:ment)?|match the visuals)\b", requirement_text)
        )
        mentions_voiceover = (
            "voiceover" in requirement_text
            or ("script" in requirement_text and "video" in requirement_text)
            or ("add it to my video" in requirement_text)
            or ("add it to the video" in requirement_text)
            or (
                re.search(r"\badd\b", requirement_text)
                and "video" in requirement_text
                and any(token in requirement_text for token in ("audio", "speech", "voice"))
            )
        )
        mentions_text_rewrite = bool(
            re.search(
                r"\b("
                r"easy[- ]to[- ]understand|simplif\w*|summar\w*|grammar|proofread|"
                r"paraphras\w*|rewrit\w*|spin\w*"
                r")\b",
                requirement_text,
            )
        )

        issues: List[str] = []
        for idx, task in enumerate(lowered):
            node = compiled_nodes[idx]
            arg1 = node.get("args", {}).get("arg1")
            upstream_arg1 = node.get("upstream_inputs", {}).get("arg1")

            if task == "text translator" and "translate" not in required_action_set:
                issues.append("unrequested_bridge_tool:text translator")
                continue

            if task == "article spinner" and not (
                mentions_text_rewrite or required_action_set.intersection({"simplify", "summarize", "grammar"})
            ):
                issues.append("unrequested_bridge_tool:article spinner")
                continue

            if task == "image downloader":
                if not mentions_download and (
                    isinstance(upstream_arg1, int) or not self._argument_looks_like_url(arg1)
                ):
                    issues.append("unrequested_bridge_tool:image downloader")
                continue

            if task == "video downloader":
                if not mentions_download and (
                    isinstance(upstream_arg1, int) or not self._argument_looks_like_url(arg1)
                ):
                    issues.append("unrequested_bridge_tool:video downloader")
                continue

            if task == "video synchronization" and not mentions_synchronization:
                issues.append("unrequested_bridge_tool:video synchronization")
                continue

            if task == "video voiceover" and not mentions_voiceover:
                issues.append("unrequested_bridge_tool:video voiceover")

        return issues

    @staticmethod
    def _verifier_issue_is_repairable(issue: str) -> bool:
        return (
            issue.startswith("missing_action:")
            or issue.startswith("validation_error:")
            or issue.startswith("search_")
            or issue.startswith("unrequested_bridge_tool:")
            or issue in {
                "topic_generated_before_retrieval",
                "grammar_applied_before_retrieval",
                "video_generated_before_waveform_branch",
            }
        )

    def _verify_candidate_workflow(
        self,
        workflow: Dict[str, Any],
        compiled_nodes: List[Dict[str, Any]],
        user_requirement: str,
    ) -> Dict[str, Any]:
        skill_names = [str(node.get("task", "")).strip() for node in compiled_nodes]
        action_coverage = self._score_requirement_action_coverage(user_requirement, skill_names)
        required_actions = [
            str(action)
            for action in action_coverage.get("required_actions", [])
            if isinstance(action, str) and action
        ]
        missing_actions = [
            str(action)
            for action in action_coverage.get("missing_actions", [])
            if isinstance(action, str) and action
        ]
        action_positions = self._workflow_first_action_positions(compiled_nodes)

        failures: List[str] = []
        warnings: List[str] = []
        validation_error: Optional[str] = None

        try:
            self._validate_prepared_workflow(workflow, compiled_nodes)
        except Exception as exc:
            validation_error = f"{type(exc).__name__}: {exc}"
            failures.append(f"validation_error:{validation_error}")

        if missing_actions:
            failures.extend(f"missing_action:{action}" for action in missing_actions)

        warnings.extend(self._search_source_structural_issues(compiled_nodes, user_requirement))
        warnings.extend(
            self._unrequested_bridge_tool_issues(
                compiled_nodes,
                user_requirement,
                required_actions=set(required_actions),
            )
        )

        retrieval_before_topic_ok = True
        if (
            "retrieval" in required_actions
            and "topic" in required_actions
            and "retrieval" in action_positions
            and "topic" in action_positions
            and action_positions["topic"] < action_positions["retrieval"]
        ):
            retrieval_before_topic_ok = False
            warnings.append("topic_generated_before_retrieval")

        grammar_after_retrieval_ok = True
        if (
            "retrieval" in required_actions
            and "grammar" in required_actions
            and "retrieval" in action_positions
            and "grammar" in action_positions
            and action_positions["grammar"] < action_positions["retrieval"]
            and not self._has_explicit_source_text(user_requirement)
        ):
            grammar_after_retrieval_ok = False
            warnings.append("grammar_applied_before_retrieval")

        video_after_waveform_ok = True
        if (
            "waveform" in required_actions
            and "video" in required_actions
            and "waveform" in action_positions
            and "video" in action_positions
            and action_positions["video"] < action_positions["waveform"]
        ):
            video_after_waveform_ok = False
            warnings.append("video_generated_before_waveform_branch")

        bridge_tool_warnings = [
            issue
            for issue in warnings
            if isinstance(issue, str) and issue.startswith("unrequested_bridge_tool:")
        ]

        hard_filter_tier = 2
        if failures:
            hard_filter_tier = 0
        elif warnings:
            hard_filter_tier = 1

        repairable = False
        if failures or warnings:
            repairable = (
                len(missing_actions) <= 1
                and len(failures) <= 2
                and len(warnings) <= 3
                and all(self._verifier_issue_is_repairable(item) for item in failures + warnings)
            )

        return {
            "hard_filter_tier": hard_filter_tier,
            "coverage_complete": not missing_actions,
            "search_source_ok": not any(issue.startswith("search_") for issue in warnings),
            "retrieval_before_topic_ok": retrieval_before_topic_ok,
            "grammar_after_retrieval_ok": grammar_after_retrieval_ok,
            "video_after_waveform_ok": video_after_waveform_ok,
            "bridge_tool_ok": not bridge_tool_warnings,
            "unrequested_bridge_tool_count": len(bridge_tool_warnings),
            "required_actions": required_actions,
            "missing_actions": missing_actions,
            "failures": failures,
            "warnings": warnings,
            "warning_count": len(warnings),
            "validation_error": validation_error,
            "verifier_pass": not failures,
            "repairable": repairable,
        }

    def _candidate_selection_meta(
        self,
        workflow: Dict[str, Any],
        compiled_nodes: List[Dict[str, Any]],
        user_requirement: str,
        score_details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        del score_details
        meta = dict(self._verify_candidate_workflow(workflow, compiled_nodes, user_requirement))
        failures = [
            item
            for item in meta.get("failures", [])
            if isinstance(item, str) and not item.startswith("validation_error:")
        ]
        warnings = [str(item) for item in meta.get("warnings", []) if isinstance(item, str)]
        meta["failures"] = failures
        meta["warning_count"] = len(warnings)
        meta["verifier_pass"] = not failures
        if failures:
            meta["hard_filter_tier"] = 0
        elif warnings:
            meta["hard_filter_tier"] = 1
        else:
            meta["hard_filter_tier"] = 2
        if failures or warnings:
            missing_actions = [
                str(item)
                for item in meta.get("missing_actions", [])
                if isinstance(item, str) and item
            ]
            meta["repairable"] = (
                len(missing_actions) <= 1
                and len(failures) <= 2
                and len(warnings) <= 3
                and all(self._verifier_issue_is_repairable(item) for item in failures + warnings)
            )
        else:
            meta["repairable"] = False
        return meta

    def recommend_graph_next_skills(
        self,
        current_skill: Optional[str],
        top_k: int = 5,
        visited_skills: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        planner = getattr(self, "_tool_graph_planner", None)
        if planner is None:
            return []

        visited_skills = visited_skills or set()
        if current_skill is None:
            starts = planner.recommend_start_tools(top_k=top_k)
            out: List[Dict[str, Any]] = []
            for item in starts:
                tool = str(item.get("tool", ""))
                alias_map = getattr(self, "_tool_graph_alias_to_skill", {})
                skill = alias_map.get(tool)
                if skill:
                    out.append(
                        {
                            "skill": skill,
                            "tool": tool,
                            "score": item.get("score", 0.0),
                            "reason": item.get("reason", ""),
                        }
                    )
            return out

        current_tool = self._graph_tool_for_skill(current_skill)
        if not current_tool:
            return []

        visited_tools = {
            self._graph_tool_for_skill(skill)
            for skill in visited_skills
            if self._graph_tool_for_skill(skill) is not None
        }
        next_tools = planner.recommend_next_tools(
            current_tool,
            visited_tools={t for t in visited_tools if t},
            top_k=top_k * 2,
        )
        out: List[Dict[str, Any]] = []
        for item in next_tools:
            tool = str(item.get("target_tool", ""))
            alias_map = getattr(self, "_tool_graph_alias_to_skill", {})
            skill = alias_map.get(tool)
            if not skill:
                continue
            out.append(
                {
                    "skill": skill,
                    "tool": tool,
                    "score": item.get("score", 0.0),
                    "edge_count": item.get("edge_count", 0),
                    "workflows": item.get("workflows", []),
                    "reason": item.get("reason", ""),
                }
            )
            if len(out) >= top_k:
                break
        return out

    def _build_candidate_record(
        self,
        workflow: Dict[str, Any],
        compiled_nodes: List[Dict[str, Any]],
        user_requirement: str,
        strategy_name: str,
        strategy_hint: str,
        sampling_temperature: float,
        family_name: Optional[str] = None,
        variant_name: Optional[str] = None,
        verification_meta: Optional[Dict[str, Any]] = None,
        repair_meta: Optional[Dict[str, Any]] = None,
        edge_grounding_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        verification_meta = verification_meta or self._verify_candidate_workflow(
            workflow,
            compiled_nodes,
            user_requirement,
        )
        score_meta = self._score_compiled_workflow(
            workflow,
            compiled_nodes,
            user_requirement=user_requirement,
        )
        return {
            "workflow": workflow,
            "score": score_meta["score"],
            "score_details": score_meta["details"],
            "verification_meta": verification_meta,
            "selection_meta": verification_meta,
            "strategy_name": strategy_name,
            "family_name": str(family_name or strategy_name).strip() or str(strategy_name).strip(),
            "variant_name": str(variant_name or strategy_name).strip() or str(strategy_name).strip(),
            "strategy_hint": strategy_hint,
            "sampling_temperature": sampling_temperature,
            "workflow_signature": self._plan_signature_from_compiled(compiled_nodes),
            "structure_signature": self._plan_structure_signature_from_compiled(compiled_nodes),
            "signature": self._plan_signature_from_compiled(compiled_nodes),
            "repair_meta": repair_meta or {"attempted": False, "applied": False},
            "edge_grounding_meta": edge_grounding_meta or {
                "mode": "none",
                "applied": False,
                "change_count": 0,
                "changes": [],
            },
        }

    def _annotate_candidate_dependency_check(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        workflow = candidate.get("workflow")
        if not isinstance(workflow, dict):
            candidate["dependency_check"] = {
                "passed": False,
                "error": "ValueError: candidate workflow must be a dict",
            }
            candidate["dependency_check_result"] = candidate["dependency_check"]
            candidate["validation_status"] = type(self)._candidate_validation_status(candidate)
            return candidate

        try:
            normalized_workflow, compiled_nodes = self._prepare_workflow(workflow)
            self._validate_prepared_workflow(normalized_workflow, compiled_nodes)
        except Exception as exc:
            candidate["dependency_check"] = {
                "passed": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            candidate["dependency_check_result"] = candidate["dependency_check"]
            candidate["validation_status"] = type(self)._candidate_validation_status(candidate)
            return candidate

        candidate["dependency_check"] = {"passed": True, "error": None}
        candidate["dependency_check_result"] = candidate["dependency_check"]
        candidate["validation_status"] = type(self)._candidate_validation_status(candidate)
        return candidate

    @staticmethod
    def _candidate_dependency_passes(candidate: Dict[str, Any]) -> bool:
        dependency_meta = candidate.get("dependency_check", {})
        return isinstance(dependency_meta, dict) and bool(dependency_meta.get("passed", False))

    @staticmethod
    def _candidate_validation_status(candidate: Dict[str, Any]) -> str:
        dependency_meta = candidate.get("dependency_check", {})
        selection_meta = candidate.get("selection_meta", {})
        dependency_pass = isinstance(dependency_meta, dict) and bool(dependency_meta.get("passed", False))
        verifier_pass = isinstance(selection_meta, dict) and bool(selection_meta.get("verifier_pass", False))
        if dependency_pass and verifier_pass:
            return "passed"
        if not dependency_pass and not verifier_pass:
            return "failed_dependency_and_verifier"
        if not dependency_pass:
            return "failed_dependency_check"
        if not verifier_pass:
            return "failed_verifier"
        return "unknown"

    @staticmethod
    def _workflow_links_for_log(workflow: Dict[str, Any]) -> List[Dict[str, str]]:
        raw_links = workflow.get("task_links", [])
        if not isinstance(raw_links, list):
            return []
        links: List[Dict[str, str]] = []
        for item in raw_links:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source", "")).strip()
            target = str(item.get("target", "")).strip()
            if not source or not target:
                continue
            links.append({"source": source, "target": target})
        return links

    async def _generate_candidate_pool_with_edge_grounding_override(
        self,
        user_requirement: str,
        candidate_count: int,
        *,
        edge_grounding_mode: str,
    ) -> List[Dict[str, Any]]:
        original_edge_grounding_mode = getattr(self, "_edge_grounding_mode", "none")
        self._edge_grounding_mode = edge_grounding_mode
        try:
            candidates = await self.generate_candidate_pool(
                user_requirement,
                candidate_count=candidate_count,
            )
        finally:
            self._edge_grounding_mode = original_edge_grounding_mode
        return [self._annotate_candidate_dependency_check(item) for item in candidates]

    async def _finalize_candidate_workflow(
        self,
        workflow: Dict[str, Any],
        user_requirement: str,
        strategy_name: str,
        strategy_hint: str,
        sampling_temperature: float,
        family_name: Optional[str] = None,
        variant_name: Optional[str] = None,
        llm_client: Any = None,
    ) -> Dict[str, Any]:
        grounded_workflow, edge_grounding_meta = self._apply_edge_grounding_mode(
            workflow,
            user_requirement=user_requirement,
        )
        normalized_workflow, compiled_nodes = self._prepare_workflow(grounded_workflow)
        if self._candidate_verifier_enabled():
            verification_meta = self._verify_candidate_workflow(
                normalized_workflow,
                compiled_nodes,
                user_requirement,
            )
            verification_meta["verifier_enabled"] = True
        else:
            verification_meta = self._default_candidate_selection_meta()
        candidate = self._build_candidate_record(
            normalized_workflow,
            compiled_nodes,
            user_requirement,
            strategy_name=strategy_name,
            strategy_hint=strategy_hint,
            sampling_temperature=sampling_temperature,
            family_name=family_name,
            variant_name=variant_name,
            verification_meta=verification_meta,
            edge_grounding_meta=edge_grounding_meta,
        )
        if not self._candidate_repair_enabled() or not verification_meta.get("repairable"):
            return candidate

        repair_meta: Dict[str, Any] = {"attempted": True, "applied": False, "source": "llm_verifier"}
        candidate["repair_meta"] = repair_meta
        try:
            repaired_workflow = await self._repair_candidate_with_llm(
                user_requirement,
                normalized_workflow,
                verification_meta,
                llm_client=llm_client,
            )
            grounded_repaired_workflow, repaired_edge_grounding_meta = self._apply_edge_grounding_mode(
                repaired_workflow,
                user_requirement=user_requirement,
            )
            repaired_normalized, repaired_compiled = self._prepare_workflow(grounded_repaired_workflow)
            repaired_verification = self._verify_candidate_workflow(
                repaired_normalized,
                repaired_compiled,
                user_requirement,
            )
            repaired_candidate = self._build_candidate_record(
                repaired_normalized,
                repaired_compiled,
                user_requirement,
                strategy_name=strategy_name,
                strategy_hint=strategy_hint,
                sampling_temperature=sampling_temperature,
                family_name=family_name,
                variant_name=variant_name,
                verification_meta=repaired_verification,
                repair_meta={"attempted": True, "applied": True, "source": "llm_verifier"},
                edge_grounding_meta=repaired_edge_grounding_meta,
            )
        except Exception as exc:
            repair_meta["error"] = f"{type(exc).__name__}: {exc}"
            return candidate

        if type(self)._candidate_sort_key(repaired_candidate) > type(self)._candidate_sort_key(candidate):
            return repaired_candidate

        repair_meta["reason"] = "repair_not_better"
        return candidate

    async def _generate_candidate_from_spec(
        self,
        user_requirement: str,
        spec: Dict[str, Any],
        *,
        round_idx: int = 0,
        allow_repair: bool = True,
    ) -> Dict[str, Any]:
        strategy_name = str(spec.get("name", "")).strip()
        family_name = str(spec.get("family_name", strategy_name)).strip() or strategy_name
        variant_name = str(spec.get("variant_name", strategy_name)).strip() or strategy_name
        hint = str(spec.get("hint", "")).strip()
        temperature = self._candidate_temperature_for_spec(spec, round_idx)
        llm_client = self._get_candidate_llm(temperature)
        workflow = await self._plan_with_client(
            user_requirement,
            strategy_hint=hint,
            llm_client=llm_client,
        )
        if allow_repair:
            candidate = await self._finalize_candidate_workflow(
                workflow,
                user_requirement=user_requirement,
                strategy_name=strategy_name,
                strategy_hint=hint,
                sampling_temperature=temperature,
                family_name=family_name,
                variant_name=variant_name,
                llm_client=llm_client,
            )
        else:
            grounded_workflow, edge_grounding_meta = self._apply_edge_grounding_mode(
                workflow,
                user_requirement=user_requirement,
            )
            normalized_workflow, compiled_nodes = self._prepare_workflow(grounded_workflow)
            if self._candidate_verifier_enabled():
                verification_meta = self._verify_candidate_workflow(
                    normalized_workflow,
                    compiled_nodes,
                    user_requirement,
                )
                verification_meta["verifier_enabled"] = True
            else:
                verification_meta = self._default_candidate_selection_meta()
            candidate = self._build_candidate_record(
                normalized_workflow,
                compiled_nodes,
                user_requirement,
                strategy_name=strategy_name,
                strategy_hint=hint,
                sampling_temperature=temperature,
                family_name=family_name,
                variant_name=variant_name,
                verification_meta=verification_meta,
                edge_grounding_meta=edge_grounding_meta,
            )
        return self._annotate_candidate_dependency_check(candidate)

    async def _generate_candidate_pool_without_original(
        self,
        user_requirement: str,
        candidate_count: int,
    ) -> List[Dict[str, Any]]:
        original_flag = bool(getattr(self, "_include_original_candidate", False))
        self._include_original_candidate = False
        try:
            candidates = await self.generate_candidate_pool(
                user_requirement,
                candidate_count=candidate_count,
            )
        finally:
            self._include_original_candidate = original_flag
        return [self._annotate_candidate_dependency_check(item) for item in candidates]

    async def _generate_all_strategy_family_candidates(
        self,
        user_requirement: str,
    ) -> List[Dict[str, Any]]:
        strategy_specs = self._build_candidate_strategy_specs(user_requirement)
        if not strategy_specs:
            raise ValueError("no candidate strategy specs configured")
        if bool(getattr(self, "_include_original_candidate", False)):
            has_original_spec = any(
                str(spec.get("family_name", spec.get("name", ""))).strip() == "original"
                for spec in strategy_specs
            )
            if not has_original_spec:
                raise ValueError(
                    "include_original_candidate=True but strategy specs do not contain an original family"
                )

        candidates: List[Dict[str, Any]] = []
        family_retry_limit = 3
        selection_mode = str(getattr(self, "_candidate_selection_mode", "rerank")).strip().lower()

        for spec_idx, spec in enumerate(strategy_specs):
            strategy_name = str(spec.get("name", "")).strip() or f"family_{spec_idx}"
            variant_name = str(spec.get("variant_name", strategy_name)).strip() or strategy_name
            strategy_label = f"{strategy_name}:{variant_name}"
            last_error: Optional[str] = None
            candidate: Optional[Dict[str, Any]] = None
            allow_repair = not (
                selection_mode == "original_first_fallback" and strategy_name == "original"
            )

            for round_idx in range(family_retry_limit):
                try:
                    candidate = await self._generate_candidate_from_spec(
                        user_requirement,
                        spec,
                        round_idx=round_idx,
                        allow_repair=allow_repair,
                    )
                    break
                except Exception as exc:
                    last_error = (
                        f"strategy={strategy_label}, round={round_idx + 1}, "
                        f"error={type(exc).__name__}: {exc}"
                    )

            if candidate is None:
                raise ValueError(
                    f"failed to generate candidate family '{strategy_label}' after "
                    f"{family_retry_limit} attempts"
                    + (f"; last_error={last_error}" if last_error else "")
                )

            candidate["id"] = len(candidates) + 1
            candidate["generation_index"] = spec_idx
            candidates.append(candidate)

        if bool(getattr(self, "_include_original_candidate", False)):
            has_original_candidate = any(
                str(item.get("family_name", item.get("strategy_name", ""))).strip() == "original"
                for item in candidates
            )
            if not has_original_candidate:
                raise ValueError(
                    "include_original_candidate=True but generated candidate pool does not contain an original family"
                )

        return candidates

    @staticmethod
    def _original_first_fallback_candidate_key(
        item: Dict[str, Any]
    ) -> Tuple[int, int, int, Tuple[int, int, int, int, int, int, int, float, float, float, float, float, float, float, float, int, int, int]]:
        dependency_meta = item.get("dependency_check")
        if not isinstance(dependency_meta, dict):
            dependency_meta = {}
        selection_meta = item.get("selection_meta")
        if not isinstance(selection_meta, dict):
            selection_meta = {}
        dependency_pass = bool(dependency_meta.get("passed", False))
        verifier_pass = bool(selection_meta.get("verifier_pass", False))
        hard_filter_tier = int(selection_meta.get("hard_filter_tier", 0))
        return (
            int(dependency_pass and verifier_pass),
            int(dependency_pass),
            hard_filter_tier,
            PlanningMixin._rerank_candidate_key(item),
        )

    def _select_best_original_first_fallback_candidate(
        self,
        candidates: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return max(candidates, key=type(self)._original_first_fallback_candidate_key)

    def _select_original_first_fallback_from_candidates(
        self,
        candidate_plans: List[Dict[str, Any]],
    ) -> Tuple[Dict[str, Any], str]:
        if not candidate_plans:
            raise ValueError("no candidate plans generated")

        original_candidate = next(
            (
                item
                for item in candidate_plans
                if str(item.get("strategy_name", "")).strip() == "original"
            ),
            None,
        )
        if original_candidate is not None and self._candidate_dependency_passes(original_candidate):
            return original_candidate, "original_dependency_pass"

        fallback_candidates = [
            item
            for item in candidate_plans
            if item is not original_candidate
        ]
        if fallback_candidates:
            selected = self._select_best_original_first_fallback_candidate(fallback_candidates)
            selected_meta = selected.get("selection_meta", {})
            dependency_meta = selected.get("dependency_check", {})
            if (
                isinstance(dependency_meta, dict)
                and bool(dependency_meta.get("passed", False))
                and isinstance(selected_meta, dict)
                and bool(selected_meta.get("verifier_pass", False))
            ):
                return selected, "fallback_verifier_pass"
            if isinstance(dependency_meta, dict) and bool(dependency_meta.get("passed", False)):
                return selected, "fallback_dependency_pass"
            return selected, "fallback_best_effort"

        return self._select_best_original_first_fallback_candidate(candidate_plans), "fallback_best_effort"

    async def plan_candidates_original_dependency_filter_first_valid(
        self,
        user_requirement: str,
        candidate_count: int = 3,
    ) -> Dict[str, Any]:
        if candidate_count < 1:
            raise ValueError("candidate_count must be >= 1")

        candidates = await self.generate_candidate_pool(
            user_requirement,
            candidate_count=candidate_count,
        )
        annotated_candidates: List[Dict[str, Any]] = [
            self._annotate_candidate_dependency_check(item)
            for item in candidates
        ]

        selected: Optional[Dict[str, Any]] = None
        selection_route = "no_dependency_valid_candidate"
        for idx, item in enumerate(annotated_candidates):
            dependency_meta = item.get("dependency_check", {})
            if not isinstance(dependency_meta, dict) or not bool(dependency_meta.get("passed", False)):
                continue
            selected = item
            if idx == 0 and str(item.get("strategy_name", "")).strip() == "original":
                selection_route = "original_dependency_pass"
            else:
                selection_route = "first_dependency_valid_candidate"
            break

        if selected is None:
            raise ValueError("no dependency-valid candidate generated")

        for idx, item in enumerate(annotated_candidates, start=1):
            item["id"] = idx
            item.setdefault("generation_index", idx - 1)

        return {
            "candidates": annotated_candidates,
            "selected": selected,
            "selection_route": selection_route,
            "generation_errors": [],
        }

    async def plan_candidates_collect_all_then_original(
        self,
        user_requirement: str,
        candidate_count: int = 3,
    ) -> Dict[str, Any]:
        if candidate_count < 1:
            raise ValueError("candidate_count must be >= 1")

        candidate_plans = await self.generate_candidate_pool(
            user_requirement,
            candidate_count=candidate_count,
        )
        selected, selection_route = self._select_original_first_fallback_from_candidates(candidate_plans)
        return {
            "candidates": candidate_plans,
            "selected": selected,
            "selection_route": selection_route,
            "generation_errors": [],
        }

    async def plan_candidates_original_first_fallback(
        self,
        user_requirement: str,
        candidate_count: int = 3,
    ) -> Dict[str, Any]:
        if candidate_count < 1:
            raise ValueError("candidate_count must be >= 1")

        if (
            self._force_generate_all_candidate_families_enabled()
            or self._disable_early_stop_enabled()
        ):
            candidate_plans = await self.generate_candidate_pool(
                user_requirement,
                candidate_count=candidate_count,
            )
            selected, selection_route = self._select_original_first_fallback_from_candidates(candidate_plans)
            return {
                "candidates": candidate_plans,
                "selected": selected,
                "selection_route": selection_route,
                "generation_errors": [],
            }

        original_spec = {
            "name": "original",
            "hint": "",
            "temperature": float(getattr(self.llm_config, "temperature", 0.0)),
        }
        candidate_plans: List[Dict[str, Any]] = []
        selected: Optional[Dict[str, Any]] = None
        selection_route = "fallback"
        generation_errors: List[str] = []
        signature_to_index: Dict[str, int] = {}
        original_candidate: Optional[Dict[str, Any]] = None

        def _upsert_candidate(candidate: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
            signature = self._plan_signature(candidate["workflow"])
            existing_idx = signature_to_index.get(signature)
            if existing_idx is not None:
                existing = candidate_plans[existing_idx]
                if type(self)._original_first_fallback_candidate_key(candidate) > type(self)._original_first_fallback_candidate_key(existing):
                    candidate_plans[existing_idx] = candidate
                    return candidate, True
                return existing, False
            signature_to_index[signature] = len(candidate_plans)
            candidate_plans.append(candidate)
            return candidate, True

        try:
            original_candidate = await self._generate_candidate_from_spec(
                user_requirement,
                original_spec,
                allow_repair=False,
            )
            original_candidate, _ = _upsert_candidate(original_candidate)
        except Exception as exc:
            generation_errors.append(f"original={type(exc).__name__}: {exc}")

        if original_candidate is not None:
            dependency_meta = original_candidate.get("dependency_check", {})
            if isinstance(dependency_meta, dict) and bool(dependency_meta.get("passed", False)):
                selected = original_candidate
                selection_route = "original_dependency_pass"

        if selected is None and original_candidate is not None and self._candidate_repair_enabled():
            try:
                repaired_original = await self._finalize_candidate_workflow(
                    original_candidate["workflow"],
                    user_requirement=user_requirement,
                    strategy_name="original_repair",
                    strategy_hint="Repair the original candidate only if dependency or verifier issues are found.",
                    sampling_temperature=float(getattr(self.llm_config, "temperature", 0.0)),
                    family_name=str(original_candidate.get("family_name", "original")).strip() or "original",
                    variant_name=str(original_candidate.get("variant_name", "baseline")).strip() or "baseline",
                    llm_client=self.llm,
                )
                repaired_original = self._annotate_candidate_dependency_check(repaired_original)
                repaired_original, _ = _upsert_candidate(repaired_original)
                repaired_dependency = repaired_original.get("dependency_check", {})
                repaired_selection = repaired_original.get("selection_meta", {})
                if (
                    isinstance(repaired_dependency, dict)
                    and bool(repaired_dependency.get("passed", False))
                    and isinstance(repaired_selection, dict)
                    and bool(repaired_selection.get("verifier_pass", False))
                ):
                    selected = repaired_original
                    selection_route = "original_repair_pass"
            except Exception as exc:
                generation_errors.append(f"original_repair={type(exc).__name__}: {exc}")

        fallback_candidates: List[Dict[str, Any]] = []
        if selected is None:
            try:
                fallback_pool = await self._generate_candidate_pool_without_original(
                    user_requirement,
                    candidate_count=candidate_count,
                )
                for item in fallback_pool:
                    stored_item, adopted_new = _upsert_candidate(item)
                    if adopted_new:
                        fallback_candidates.append(stored_item)
            except Exception as exc:
                generation_errors.append(f"fallback_pool={type(exc).__name__}: {exc}")

        if selected is None and fallback_candidates:
            selected = self._select_best_original_first_fallback_candidate(fallback_candidates)
            selected_meta = selected.get("selection_meta", {})
            dependency_meta = selected.get("dependency_check", {})
            if (
                isinstance(dependency_meta, dict)
                and bool(dependency_meta.get("passed", False))
                and isinstance(selected_meta, dict)
                and bool(selected_meta.get("verifier_pass", False))
            ):
                selection_route = "fallback_verifier_pass"
            elif isinstance(dependency_meta, dict) and bool(dependency_meta.get("passed", False)):
                selection_route = "fallback_dependency_pass"
            else:
                selection_route = "fallback_best_effort"

        if selected is None and candidate_plans:
            selected = self._select_best_original_first_fallback_candidate(candidate_plans)
            selection_route = "fallback_best_effort"

        if selected is None:
            if generation_errors:
                raise ValueError("no candidate plans generated; errors=" + " | ".join(generation_errors))
            raise ValueError("no candidate plans generated")

        for idx, item in enumerate(candidate_plans, start=1):
            item["id"] = idx
            item.setdefault("generation_index", idx - 1)

        return {
            "candidates": candidate_plans,
            "selected": selected,
            "selection_route": selection_route,
            "generation_errors": generation_errors,
        }

    async def plan_candidates_structure_aware(
        self,
        user_requirement: str,
        candidate_count: int = 3,
    ) -> Dict[str, Any]:
        if candidate_count < 1:
            raise ValueError("candidate_count must be >= 1")

        semantic_grounding_mode = self._resolve_edge_grounding_mode()
        if semantic_grounding_mode not in {
            "semantic_edge_scoring",
            "semantic_edge_scoring_h2a",
            "semantic_edge_scoring_h2b",
        }:
            semantic_grounding_mode = "semantic_edge_scoring"

        candidate_plans = await self._generate_candidate_pool_with_edge_grounding_override(
            user_requirement,
            candidate_count=candidate_count,
            edge_grounding_mode="none",
        )
        if not candidate_plans:
            raise ValueError("no candidate plans generated")

        original_index = 0
        for idx, item in enumerate(candidate_plans):
            if str(item.get("strategy_name", "")).strip() == "original":
                original_index = idx
                break

        original_candidate = candidate_plans[original_index]
        original_workflow = original_candidate.get("workflow", {})
        if not isinstance(original_workflow, dict):
            raise ValueError("original candidate workflow must be a dict")

        detected_structure = self._detect_workflow_structure(original_workflow)
        original_links = self._workflow_links_for_log(original_workflow)
        structure_aware_meta: Dict[str, Any] = {
            "detected_structure": detected_structure,
            "grounding_applied": False,
            "fallback_used": False,
            "original_links": original_links,
            "grounded_links": list(original_links),
        }

        selected = original_candidate
        selection_route = f"structure_aware_{detected_structure}_original"

        if detected_structure == "chain":
            if not self._candidate_dependency_passes(original_candidate):
                fallback_candidate = next(
                    (item for item in candidate_plans if self._candidate_dependency_passes(item)),
                    None,
                )
                if fallback_candidate is not None:
                    selected = fallback_candidate
                    structure_aware_meta["fallback_used"] = True
                    structure_aware_meta["grounded_links"] = self._workflow_links_for_log(
                        fallback_candidate.get("workflow", {})
                        if isinstance(fallback_candidate.get("workflow"), dict)
                        else {}
                    )
                    selection_route = "structure_aware_chain_first_dependency_valid_candidate"
                else:
                    selection_route = "structure_aware_chain_original_invalid"
        elif detected_structure == "dag":
            grounded_workflow: Optional[Dict[str, Any]] = None
            grounding_meta: Optional[Dict[str, Any]] = None
            try:
                grounded_workflow, grounding_meta = self._apply_specific_edge_grounding_mode(
                    original_workflow,
                    user_requirement=user_requirement,
                    mode=semantic_grounding_mode,
                )
                structure_aware_meta["grounded_links"] = self._workflow_links_for_log(grounded_workflow)
                normalized_grounded, grounded_compiled = self._prepare_workflow(grounded_workflow)
                self._validate_prepared_workflow(normalized_grounded, grounded_compiled)
                if self._candidate_verifier_enabled():
                    verification_meta = self._verify_candidate_workflow(
                        normalized_grounded,
                        grounded_compiled,
                        user_requirement,
                    )
                    verification_meta["verifier_enabled"] = True
                else:
                    verification_meta = self._default_candidate_selection_meta()
                grounded_candidate = self._build_candidate_record(
                    normalized_grounded,
                    grounded_compiled,
                    user_requirement,
                    strategy_name=str(original_candidate.get("strategy_name", "original")),
                    strategy_hint=str(original_candidate.get("strategy_hint", "")),
                    sampling_temperature=float(original_candidate.get("sampling_temperature", 0.0)),
                    family_name=str(original_candidate.get("family_name", "original")),
                    variant_name=str(original_candidate.get("variant_name", "baseline")),
                    verification_meta=verification_meta,
                    repair_meta={"attempted": False, "applied": False},
                    edge_grounding_meta=grounding_meta,
                )
                grounded_candidate = self._annotate_candidate_dependency_check(grounded_candidate)
                candidate_plans[original_index] = grounded_candidate
                selected = grounded_candidate
                structure_aware_meta["grounding_applied"] = bool(
                    isinstance(grounding_meta, dict) and grounding_meta.get("applied", False)
                )
                selection_route = "structure_aware_dag_semantic_grounding"
            except Exception as exc:
                structure_aware_meta["fallback_used"] = True
                structure_aware_meta["grounding_error"] = f"{type(exc).__name__}: {exc}"
                if grounded_workflow is not None:
                    structure_aware_meta["grounded_links"] = self._workflow_links_for_log(grounded_workflow)
                selection_route = "structure_aware_dag_grounding_fallback"

        for idx, item in enumerate(candidate_plans, start=1):
            item["id"] = idx
            item.setdefault("generation_index", idx - 1)

        selected["structure_aware_meta"] = dict(structure_aware_meta)
        return {
            "candidates": candidate_plans,
            "selected": selected,
            "selection_route": selection_route,
            "generation_errors": [],
            "structure_aware_meta": structure_aware_meta,
        }

    async def generate_candidate_pool(
        self,
        user_requirement: str,
        candidate_count: int = 3,
    ) -> List[Dict[str, Any]]:
        if candidate_count < 1:
            raise ValueError("candidate_count must be >= 1")

        if self._force_generate_all_candidate_families_enabled():
            return await self._generate_all_strategy_family_candidates(user_requirement)

        strategy_specs = self._build_candidate_strategy_specs(user_requirement)
        pool_target = self._candidate_pool_target(strategy_specs, candidate_count)

        candidates: List[Dict[str, Any]] = []
        generation_errors: List[str] = []
        seen_signatures: set[str] = set()
        max_attempts = max(pool_target * 4, len(strategy_specs))
        for i in range(max_attempts):
            spec = strategy_specs[i % len(strategy_specs)]
            round_idx = i // len(strategy_specs)
            strategy_name = str(spec.get("name", "")).strip()
            family_name = str(spec.get("family_name", strategy_name)).strip() or strategy_name
            variant_name = str(spec.get("variant_name", strategy_name)).strip() or strategy_name
            hint = str(spec.get("hint", "")).strip()
            temperature = self._candidate_temperature_for_spec(spec, round_idx)
            llm_client = self._get_candidate_llm(temperature)

            if seen_signatures:
                hint = (
                    f"{hint}\n"
                    "Produce a structurally distinct valid workflow from earlier candidates if another valid option exists. "
                    "Change task ordering, branch structure, or upstream bindings rather than repeating the same plan."
                ).strip()

            try:
                workflow = await self._plan_with_client(
                    user_requirement,
                    strategy_hint=hint,
                    llm_client=llm_client,
                )
            except Exception as exc:
                generation_errors.append(
                    f"attempt={i + 1}, temperature={temperature:.2f}, error={type(exc).__name__}: {exc}"
                )
                continue
            try:
                candidate = await self._finalize_candidate_workflow(
                    workflow,
                    user_requirement=user_requirement,
                    strategy_name=strategy_name,
                    strategy_hint=hint,
                    sampling_temperature=temperature,
                    family_name=family_name,
                    variant_name=variant_name,
                    llm_client=llm_client,
                )
            except Exception as exc:
                generation_errors.append(
                    f"attempt={i + 1}, temperature={temperature:.2f}, finalize_error={type(exc).__name__}: {exc}"
                )
                continue

            signature = self._plan_signature(candidate["workflow"])
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            candidate["id"] = len(candidates) + 1
            candidate["generation_index"] = len(candidates)
            candidates.append(candidate)
            if len(candidates) >= pool_target:
                break

        if not candidates:
            if generation_errors:
                raise ValueError("no valid candidate plans generated; errors=" + " | ".join(generation_errors))
            raise ValueError("no valid candidate plans generated")
        return candidates

    async def plan_candidates(self, user_requirement: str, candidate_count: int = 3) -> List[Dict[str, Any]]:
        candidates = await self.generate_candidate_pool(
            user_requirement,
            candidate_count=candidate_count,
        )
        ranked = sorted(candidates, key=type(self)._candidate_sort_key, reverse=True)
        selected = ranked[: max(candidate_count, 0)]
        for idx, item in enumerate(selected, start=1):
            item["id"] = idx
        return selected

    @staticmethod
    def _rerank_candidate_key(
        item: Dict[str, Any]
    ) -> Tuple[int, int, int, int, int, int, int, float, float, float, float, float, float, float, float, int, int, int]:
        details = item.get("score_details")
        if not isinstance(details, dict):
            details = {}
        selection_meta = item.get("selection_meta")
        if not isinstance(selection_meta, dict):
            selection_meta = {}
        missing_actions = details.get("missing_actions", [])
        missing_count = len(missing_actions) if isinstance(missing_actions, list) else 0
        warning_count = int(selection_meta.get("warning_count", 0))
        return (
            int(selection_meta.get("hard_filter_tier", 1 if missing_count == 0 else 0)),
            int(selection_meta.get("coverage_complete", missing_count == 0)),
            int(selection_meta.get("bridge_tool_ok", True)),
            -int(selection_meta.get("unrequested_bridge_tool_count", 0)),
            int(selection_meta.get("search_source_ok", True)),
            int(selection_meta.get("retrieval_before_topic_ok", True)),
            int(selection_meta.get("video_after_waveform_ok", True)),
            float(item.get("score", float("-inf"))),
            float(details.get("action_coverage_bonus", 0.0)) + float(details.get("action_coverage_penalty", 0.0)),
            float(details.get("text_preference_bonus", 0.0)) + float(details.get("text_preference_penalty", 0.0)),
            float(details.get("graph_transition_bonus", 0.0)) + float(details.get("graph_transition_penalty", 0.0)),
            float(details.get("name_modality_bonus", 0.0)) + float(details.get("name_modality_penalty", 0.0)),
            float(details.get("transition_bonus", 0.0)) + float(details.get("transition_penalty", 0.0)),
            float(details.get("redundancy_penalty", 0.0)),
            float(details.get("length_penalty", 0.0)),
            -warning_count,
            -missing_count,
            -int(item.get("id", 0)),
        )

    @staticmethod
    def _candidate_sort_key(
        item: Dict[str, Any]
    ) -> Tuple[int, int, int, int, int, int, int, float, float, float, float, float, float, float, float, int, int, int]:
        return PlanningMixin._rerank_candidate_key(item)

    def _select_best_candidate(self, candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        return max(candidates, key=type(self)._rerank_candidate_key)
