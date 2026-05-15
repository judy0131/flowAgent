import json
import re
from dataclasses import replace
from typing import Any, Dict, List, Optional, Set, Tuple

from .serialization import _safe_json_dumps


class PlanningMixin:
    def _build_plan_prompt(self, user_requirement: str, strategy_hint: Optional[str] = None) -> str:
        strategy_line = f"\nPlanning strategy hint:\n{strategy_hint}\n" if strategy_hint else ""
        required_actions = self._match_requirement_actions(user_requirement)
        coverage_checklist = ""
        if required_actions:
            coverage_lines = "\n".join(f"- {self._action_prompt_text(action)}" for action in required_actions)
            coverage_checklist = (
                "\nDetected required actions from the user request:\n"
                f"{coverage_lines}\n"
                "All detected actions must be covered by task_nodes.\n"
            )
        output_schema = """
        {
          "task_steps": [
            "Step 1: Call Video-to-Audio with arg1=example.mp4.",
            "Step 2: Call Audio Noise Reduction with arg1=<node-0>.",
            "Step 3: Call Audio Effects with arg2=reverb, arg1=<node-1>."
          ],
          "task_nodes": [
            {
              "task": "Video-to-Audio",
              "arguments": ["example.mp4"]
            },
            {
              "task": "Audio Noise Reduction",
              "arguments": ["<node-0>"]
            },
            {
              "task": "Audio Effects",
              "arguments": ["reverb", "<node-1>"]
            }
          ],
          "task_links": [
            {
              "source": "Video-to-Audio",
              "target": "Audio Noise Reduction"
            },
            {
              "source": "Audio Noise Reduction",
              "target": "Audio Effects"
            }
          ]
        }"""

        good_examples = """
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
        }"""
        prompt = f"""
You are a data pipeline planner.

Your job is to break the user requirement into the valid executable workflow.
Use only the listed skills.
Return strict JSON only.

Output schema:
{json.dumps(output_schema, indent=2)}

Requirement decomposition rules:
- First, silently break the user requirement into action clauses before selecting tools.
- Pay close attention to punctuation and conjunctions such as commas, "and", "then", "after", "before", "finally", "with", and "using".
- Distinguish between:
  - retrieval actions: find, search, look up, browse, get information, collect information
  - transformation actions: simplify, summarize, translate, grammar check, denoise, splice, transcribe, apply effects
  - generation actions: create, generate, draw, produce an image/audio/video/text artifact
- When the request includes a literal phrase, title, topic, or theme to use as an argument, prefer the exact span copied from the user requirement instead of paraphrasing it unless the user explicitly asks to rewrite it.
- Avoid silently changing wording such as changing "impact" to "effect" when a later tool can consume the original phrase directly.
- If the request says to find or search for information about a topic, include a retrieval/search step unless the full source content is already explicitly provided in the request.
- If the request provides a seed phrase such as a topic, title, theme, or starting point text, treat it as input to an upstream step like simplification or search, not as proof that the retrieval step can be skipped.
- When the request asks for multiple outputs or multiple post-processing actions in sequence, ensure each required action is represented in task_nodes in order.


Planning rules:
- Ensure the workflow is a valid DAG.
- Use only skills from the available skills list.
- task_nodes must be in execution order.
- Every task node must contain:
  - task: exact skill name from the available skills list
  - arguments: list of argument values.
- Use references like <node-0>, <node-1>, etc. inside arguments for upstream task outputs. The index is zero-based and refers to the earlier item in task_nodes.
- task_links must match the upstream/downstream dependencies implied by the node references.
- task_steps must describe the same workflow as task_nodes.
- A valid workflow may contain exactly one task if one skill can directly satisfy the user requirement.


Dependency and wiring rules:
- Consumer tasks must reference upstream outputs using <node-i> in arguments.
- Do NOT use natural-language references like "output of step 1" inside arguments.
- Do NOT use placeholder strings such as "step1_out", "step_1_output", or similar.
- For multi-input tasks, include all required arguments explicitly in the arguments list.
- Each <node-i> reference must point to the specific earlier node whose output is directly needed by that step.
- When a later step continues processing the same user-requested artifact, keep it on that artifact branch instead of switching to a sibling branch without evidence in the user request.
- If two steps independently consume the same earlier result, connect both of them to that shared earlier node instead of chaining one through the other without evidence in the user request.
- You may use skill names as a soft heuristic to infer likely input/output modality when choosing between plausible upstream nodes.Names like X-to-Y often suggest input X and output Y, and names like Audio Effects often suggest continued processing of audio.
- Treat skill-name-based modality inference as a hint, not a hard rule.When skill-name implications conflict with the skill schema, explicit argument semantics, or the user request, follow the schema and the user request.Prefer modality-consistent wiring when multiple earlier nodes are plausible candidates.
- If Topic Generator is immediately followed by Text-to-Image, preserve that direct dependency unless the user request explicitly introduces another intervening artifact.

Branch consistency rules:
- Each workflow branch should represent a coherent transformation lineage of the same artifact or goal.
- Do not merge unrelated branches unless the downstream task explicitly requires multiple inputs.
- Do not fork branches unnecessarily if downstream outputs are identical.

Modality tracking rules:
- Track the modality of each node output implicitly throughout the workflow.
- Ensure downstream tasks consume compatible modalities.
- Avoid passing text outputs into image/audio/video processing skills unless the skill schema explicitly allows it.

Global consistency rules:
- Ensure task_nodes, task_links, and task_steps describe the same workflow structure.
- Ensure every dependency edge appearing in arguments is reflected in task_links.
- Ensure every task_link corresponds to at least one actual dependency.
- Ensure there are no orphan nodes.

Validation rules:
- Reject an empty workflow.
- A single-task workflow is valid if that one task fully satisfies the user request.
- Reject any workflow where a node reference points to itself or to a later node.
- Reject any workflow where task_links contradict task_nodes dependencies.
- Reject any workflow with cycles or unnecessary disconnected branches unless parallel independent branches are clearly required by the user request.
- Before returning the workflow, check that every explicit action clause in the user request is covered by at least one node.
- If one clause asks to find/search information and another clause asks to clean/check/transform that information, those are usually separate steps.

Final self-check before output:
- Verify that every user-requested action is covered.
- Verify that every node is necessary.
- Verify that every dependency is valid.
- Verify that the workflow is executable as written.
- Verify that no simpler valid workflow exists using the available skills.

The following are examples of valid workflows. Follow their wiring style exactly:
- use literal values directly in arguments
- use <node-i> only for upstream outputs
- keep task_nodes in execution order
- keep task_links consistent with task_nodes


Output requirements:
- Return JSON only.
- Do not wrap the JSON in markdown fences.
- Do not include any explanation outside the JSON.
- Do not include null fields.
- Do not include fields other than task_steps, task_nodes, task_links.
{coverage_checklist}

Good examples:
{json.dumps(good_examples, indent=2)}

Available skills:
{json.dumps(self.registry.list_for_prompt())}

User requirement:
{user_requirement}
{strategy_line}
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
        verification_meta: Optional[Dict[str, Any]] = None,
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
            "taskbench_prior_bonus": 0.0,
            "taskbench_prior_penalty": 0.0,
            "taskbench_prior_transition_bonus": 0.0,
            "taskbench_prior_transition_penalty": 0.0,
            "taskbench_prior_motif_bonus": 0.0,
            "taskbench_prior_start_bonus": 0.0,
            "taskbench_prior_end_bonus": 0.0,
            "memory_bonus": 0.0,
            "memory_penalty": 0.0,
            "memory_transition_bonus": 0.0,
            "memory_transition_penalty": 0.0,
            "memory_motif_bonus": 0.0,
            "memory_start_bonus": 0.0,
            "memory_end_bonus": 0.0,
            "action_coverage_score": 0.0,
            "schema_validity_score": 0.0,
            "dependency_correctness_score": 0.0,
            "modality_consistency_score": 0.0,
            "dag_validity_score": 0.0,
            "executability_score": 0.0,
            "extra_action_penalty": 0.0,
            "memory_prior_score": 0.0,
            "memory_prior_raw_score": 0.0,
            "start_prior_score": 0.0,
            "transition_prior_score": 0.0,
            "motif_match_score": 0.0,
            "end_prior_score": 0.0,
            "extra_node_count": 0,
        }
        verification = verification_meta if isinstance(verification_meta, dict) else None
        if verification is None:
            verification = self._verify_candidate_workflow(workflow, compiled_nodes, user_requirement)

        skill_names = [str(node.get("task", "")).lower() for node in compiled_nodes]
        action_coverage = self._score_requirement_action_coverage(user_requirement, skill_names)
        details["action_coverage_bonus"] = action_coverage["bonus"]
        details["action_coverage_penalty"] = action_coverage["penalty"]
        details["required_actions"] = action_coverage["required_actions"]
        details["covered_actions"] = action_coverage["covered_actions"]
        details["missing_actions"] = action_coverage["missing_actions"]
        details["action_coverage_score"] = action_coverage["bonus"] + action_coverage["penalty"]

        steps_count = len(compiled_nodes)
        if steps_count > 1:
            required_action_count = len(action_coverage["required_actions"])
            if required_action_count > 0:
                expected_step_ceiling = required_action_count + 1
                extra_steps = max(steps_count - expected_step_ceiling, 0)
                penalty = float(extra_steps * 6)
                details["extra_node_count"] = int(extra_steps)
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

        schema_failures = [
            str(item)
            for item in verification.get("schema_failures", [])
            if isinstance(item, str) and item
        ]
        dependency_failures = [
            str(item)
            for item in verification.get("dependency_failures", [])
            if isinstance(item, str) and item
        ]
        modality_failures = [
            str(item)
            for item in verification.get("modality_failures", [])
            if isinstance(item, str) and item
        ]
        modality_warnings = [
            str(item)
            for item in verification.get("modality_warnings", [])
            if isinstance(item, str) and item
        ]
        dag_failures = [
            str(item)
            for item in verification.get("dag_failures", [])
            if isinstance(item, str) and item
        ]

        schema_score = 6.0 if verification.get("schema_ok", False) else -12.0 - max(len(schema_failures) - 1, 0) * 3.0
        dependency_score = (
            5.0 if verification.get("dependency_ok", False) else -10.0 - max(len(dependency_failures) - 1, 0) * 3.0
        )
        modality_score = 5.0 if verification.get("modality_ok", False) else -12.0 - max(len(modality_failures) - 1, 0) * 4.0
        if modality_warnings:
            modality_score -= float(len(modality_warnings)) * 1.5
        dag_score = 6.0 if verification.get("dag_ok", False) else -16.0 - max(len(dag_failures) - 1, 0) * 4.0
        executability_score = schema_score + dependency_score + modality_score + dag_score
        details["schema_validity_score"] = schema_score
        details["dependency_correctness_score"] = dependency_score
        details["modality_consistency_score"] = modality_score
        details["dag_validity_score"] = dag_score
        details["executability_score"] = executability_score
        score += executability_score

        graph_meta = self._graph_score_compiled(compiled_nodes)
        details["graph_transition_bonus"] = graph_meta["graph_transition_bonus"]
        details["graph_transition_penalty"] = graph_meta["graph_transition_penalty"]
        score += graph_meta["graph_transition_bonus"] + graph_meta["graph_transition_penalty"]

        text_pref_meta = self._score_text_planning_preferences(compiled_nodes, user_requirement)
        details["text_preference_bonus"] = text_pref_meta["bonus"]
        details["text_preference_penalty"] = text_pref_meta["penalty"]
        score += text_pref_meta["bonus"] + text_pref_meta["penalty"]

        prior_meta = self._score_taskbench_prior_compiled(compiled_nodes, user_requirement)
        details["taskbench_prior_bonus"] = prior_meta["bonus"]
        details["taskbench_prior_penalty"] = prior_meta["penalty"]
        details["taskbench_prior_transition_bonus"] = prior_meta["transition_bonus"]
        details["taskbench_prior_transition_penalty"] = prior_meta["transition_penalty"]
        details["taskbench_prior_motif_bonus"] = prior_meta["motif_bonus"]
        details["taskbench_prior_start_bonus"] = prior_meta["start_bonus"]
        details["taskbench_prior_end_bonus"] = prior_meta["end_bonus"]
        # Keep the older memory_* fields populated for downstream consumers that
        # still read them while the unified TaskBench prior becomes the only
        # structure-aware scoring source.
        details["memory_bonus"] = prior_meta["bonus"]
        details["memory_penalty"] = prior_meta["penalty"]
        details["memory_transition_bonus"] = prior_meta["transition_bonus"]
        details["memory_transition_penalty"] = prior_meta["transition_penalty"]
        details["memory_motif_bonus"] = prior_meta["motif_bonus"]
        details["memory_start_bonus"] = prior_meta["start_bonus"]
        details["memory_end_bonus"] = prior_meta["end_bonus"]
        details["transition_prior_score"] = prior_meta["transition_bonus"] + prior_meta["transition_penalty"]
        details["motif_match_score"] = prior_meta["motif_bonus"]
        details["start_prior_score"] = prior_meta["start_bonus"]
        details["end_prior_score"] = prior_meta["end_bonus"]
        raw_memory_prior_score = (
            details["transition_prior_score"]
            + details["motif_match_score"]
            + details["start_prior_score"]
            + details["end_prior_score"]
        )
        details["memory_prior_raw_score"] = raw_memory_prior_score
        details["memory_prior_score"] = raw_memory_prior_score * 0.5
        score += details["memory_prior_score"]

        extra_actions = [
            str(item)
            for item in verification.get("extra_actions", [])
            if isinstance(item, str) and item
        ]
        if extra_actions:
            extra_penalty = float(len(extra_actions)) * 7.0
            details["extra_action_penalty"] = -extra_penalty
            score -= extra_penalty

        return {
            "score": round(score, 3),
            "details": {**details, "final_score": round(score, 3)},
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

    @staticmethod
    def _strategy_slug(text: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", str(text or "").strip().lower()).strip("_")
        return slug or "memory_graph"

    def _build_memory_graph_strategy_specs(self, user_requirement: str) -> List[Dict[str, Any]]:
        get_context = getattr(self, "_get_workflow_memory_context", None)
        recommend_starts = getattr(self, "recommend_memory_start_skills", None)
        recommend_next = getattr(self, "recommend_memory_next_skills", None)
        if not callable(get_context) or not callable(recommend_starts):
            return []

        context = get_context(user_requirement)
        if not isinstance(context, dict):
            return []
        start_recs = context.get("start_skills")
        if not isinstance(start_recs, list) or not start_recs:
            start_recs = recommend_starts(user_requirement, top_k=4)
        if not isinstance(start_recs, list) or not start_recs:
            return []

        transitions = context.get("transitions", [])
        motifs = context.get("motifs", [])

        specs: List[Dict[str, Any]] = []
        start_labels = [
            f"{str(item.get('skill', '')).strip()} ({float(item.get('score', 0.0)):.2f})"
            for item in start_recs[:4]
            if str(item.get("skill", "")).strip()
        ]
        if not start_labels:
            return []

        transition_labels = [
            (
                f"{str(item.get('source', '')).strip()} -> "
                f"{str(item.get('target', '')).strip()} "
                f"({float(item.get('score', 0.0)):.2f})"
            )
            for item in transitions[:4]
            if str(item.get("source", "")).strip() and str(item.get("target", "")).strip()
        ]
        motif_labels = []
        for motif in motifs[:3]:
            if not isinstance(motif, dict):
                continue
            tasks = " -> ".join(str(task).strip() for task in motif.get("tasks", []) if str(task).strip())
            if not tasks:
                continue
            motif_labels.append(
                f"{tasks} (support={int(motif.get('support', 0))})"
            )

        summary_lines = [
            "Use the query-conditioned workflow memory priors only as soft candidate-generation hints.",
            f"Preferred start tools: {', '.join(start_labels)}.",
        ]
        if transition_labels:
            summary_lines.append(f"Likely transitions for this request: {'; '.join(transition_labels)}.")
        if motif_labels:
            summary_lines.append(f"Likely reusable motifs for this request: {'; '.join(motif_labels)}.")
        summary_lines.append(
            "Do not force these priors if they conflict with the user request, required actions, or skill schemas."
        )

        specs.append(
            {
                "name": "memory_graph_guided",
                "hint": "\n".join(summary_lines),
                "temperature": 0.05,
            }
        )

        transitions_by_source: Dict[str, List[Dict[str, Any]]] = {}
        for item in transitions:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source", "")).strip()
            if not source:
                continue
            transitions_by_source.setdefault(source, []).append(item)

        for idx, start in enumerate(start_recs[:3]):
            start_skill = str(start.get("skill", "")).strip()
            if not start_skill:
                continue
            local_next_recs = transitions_by_source.get(start_skill, [])
            if not local_next_recs and callable(recommend_next):
                local_next_recs = recommend_next(
                    user_requirement,
                    start_skill,
                    top_k=3,
                    visited_skills={start_skill},
                )
            branch_lines = [
                f"Try a valid workflow that starts from {start_skill} if it is compatible with the user request.",
                "Follow user intents and required action coverage first. Memory is only a soft prior.",
            ]
            next_labels = [
                (
                    f"{str(item.get('skill', item.get('target', ''))).strip()} "
                    f"({float(item.get('score', 0.0)):.2f})"
                )
                for item in local_next_recs[:3]
                if str(item.get("skill", item.get("target", ""))).strip()
            ]
            if next_labels:
                branch_lines.append(
                    f"Preferred downstream continuation after {start_skill}: {', '.join(next_labels)}."
                )
            branch_lines.append(
                "Abandon this start if it introduces extra tools, misses required actions, or breaks schema compatibility."
            )
            specs.append(
                {
                    "name": f"memory_graph_start_{self._strategy_slug(start_skill)}",
                    "hint": "\n".join(branch_lines),
                    "temperature": 0.15 + idx * 0.05,
                }
            )

        for idx, motif in enumerate(motifs[:3]):
            if not isinstance(motif, dict):
                continue
            tasks = [str(task).strip() for task in motif.get("tasks", []) if str(task).strip()]
            if len(tasks) < 2:
                continue
            motif_text = " -> ".join(tasks)
            support = int(motif.get("support", 0))
            action_tags = [str(tag).strip() for tag in motif.get("action_tags", []) if str(tag).strip()]
            motif_lines = [
                "Try to reuse this historical motif only if it fits the current request:",
                motif_text,
            ]
            if support > 0:
                motif_lines.append(f"Historical support: {support}.")
            if action_tags:
                motif_lines.append(f"Associated action tags: {', '.join(action_tags)}.")
            motif_lines.append(
                "Do not force this motif if it introduces unrequested actions, extra conversions, or schema mismatches."
            )
            specs.append(
                {
                    "name": f"memory_graph_motif_{self._strategy_slug(tasks[0])}_{idx + 1}",
                    "hint": "\n".join(motif_lines),
                    "temperature": min(0.25 + idx * 0.05, 0.35),
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

    def _build_candidate_strategy_specs(self, user_requirement: str) -> List[Dict[str, Any]]:
        detected_actions = self._match_requirement_actions(user_requirement)
        requirement_text = " ".join(str(user_requirement or "").lower().split())
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

        if "transcribe" in detected_actions and any(
            action in detected_actions for action in {"expand", "grammar", "summarize", "sentiment", "keywords", "topic"}
        ):
            specs.insert(
                0,
                {
                    "name": "source_media_clause_coverage",
                    "hint": (
                        "If the request derives rewritten text or regenerated narration from existing audio or video content, "
                        "keep the transcription step explicit before downstream text rewriting, grammar correction, search, "
                        "analysis, or speech regeneration. Do not introduce extra video-conversion steps unless the request "
                        "explicitly provides video input or asks for a video output."
                    ),
                    "temperature": 0.1,
                },
            )
        if (
            "simplify" in detected_actions
            and any(action in detected_actions for action in {"audio_effect", "combine"})
            and re.search(r"\b(instructions?|settings?|parameters?|software tools?)\b", requirement_text)
        ):
            specs.insert(
                0,
                {
                    "name": "instruction_branch_preserving",
                    "hint": (
                        "If the request separately provides media files and asks to make textual instructions easier for tools "
                        "to understand, keep that instruction-simplification branch explicit and feed it into the effect or "
                        "combine step instead of dropping it."
                    ),
                    "temperature": 0.12,
                },
            )

        if any(action in detected_actions for action in {"summarize", "sentiment", "keywords"}):
            specs.insert(
                0,
                {
                    "name": "analysis_coverage",
                    "hint": "Preserve analysis coverage. If the request asks for summaries, sentiment, or keywords, keep each required analysis step explicit instead of collapsing them.",
                    "temperature": 0.1,
                },
            )
        if self._has_explicit_source_text(user_requirement) and "retrieval" in detected_actions:
            source_text_name = (
                "source_text_summary_search"
                if "summarize" in detected_actions
                else "source_text_simplify_search"
            )
            source_text_hint = (
                "When the request includes source text and later asks to search after summarizing, "
                "prefer searching from the summary rather than from later analysis outputs unless the request explicitly says otherwise."
                if "summarize" in detected_actions
                else
                "When the request includes source text and later asks to search, prefer searching from the simplified source text rather than from later analysis outputs unless the request explicitly says otherwise."
            )
            specs.insert(
                0,
                {
                    "name": source_text_name,
                    "hint": source_text_hint,
                    "temperature": 0.1,
                },
            )
        if "retrieval" in detected_actions:
            specs.insert(
                0,
                {
                    "name": "retrieval_guardrail",
                    "hint": "Do not omit retrieval. If the request asks to find or search for information, include an explicit search step before later topic or image generation steps.",
                    "temperature": 0.0,
                },
            )
        if "grammar" in detected_actions and "topic" in detected_actions:
            specs.insert(
                1,
                {
                    "name": "grammar_topic_clause_coverage",
                    "hint": "Preserve clause coverage. If the request asks to search, then grammar-check, then generate topics, keep those as separate sequential steps unless one tool explicitly combines them.",
                    "temperature": 0.15,
                },
            )
        if "combine" in detected_actions or "video" in detected_actions:
            specs.append(
                {
                    "name": "multimodal_branch_preserving",
                    "hint": "Prefer a multimodal plan that preserves each media branch until the explicit combine or video step, instead of collapsing branches prematurely.",
                    "temperature": 0.4,
                }
            )

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
            or issue.startswith("extra_action:")
            or issue.startswith("modality_edge:")
            or issue.startswith("unknown_skill:")
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
        structure_meta = self._analyze_compiled_workflow(compiled_nodes)

        try:
            self._validate_workflow_payload(workflow, compiled_nodes)
        except Exception as exc:
            validation_error = f"{type(exc).__name__}: {exc}"
            failures.append(f"validation_error:{validation_error}")

        failures.extend(
            str(item)
            for item in structure_meta.get("failures", [])
            if isinstance(item, str) and item
        )
        warnings.extend(
            str(item)
            for item in structure_meta.get("warnings", [])
            if isinstance(item, str) and item
        )

        if missing_actions:
            failures.extend(f"missing_action:{action}" for action in missing_actions)

        executed_actions = sorted(self._workflow_action_tags(skill_names))
        extra_actions = [
            action
            for action in executed_actions
            if action not in required_actions
            and action in {
                "retrieval",
                "transcribe",
                "simplify",
                "translate",
                "voice_change",
                "denoise",
                "combine",
                "audio_effect",
                "video",
            }
        ]
        if extra_actions:
            warnings.extend(f"extra_action:{action}" for action in extra_actions)

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
        dag_failures = [
            issue
            for issue in failures
            if isinstance(issue, str)
            and (
                issue == "empty_workflow"
                or issue == "graph_cycle"
                or issue.startswith("validation_error:")
                or issue.startswith("invalid_")
                or issue.startswith("out_of_range_reference:")
                or issue.startswith("non_earlier_reference:")
                or issue.startswith("isolated_nodes:")
                or issue.startswith("disconnected_components:")
            )
        ]

        hard_filter_tier = 2
        if failures:
            hard_filter_tier = 0
        elif warnings:
            hard_filter_tier = 1

        repairable = False
        if failures or warnings:
            repair_warnings = [item for item in warnings if not str(item).startswith("unknown_skill:")]
            repairable = (
                len(missing_actions) <= 1
                and len(failures) <= 2
                and len(repair_warnings) <= 3
                and all(self._verifier_issue_is_repairable(item) for item in failures + warnings)
            )

        return {
            "hard_filter_tier": hard_filter_tier,
            "coverage_complete": not missing_actions,
            "schema_ok": bool(structure_meta.get("schema_ok", False)),
            "dependency_ok": bool(structure_meta.get("dependency_ok", False)),
            "modality_ok": bool(structure_meta.get("modality_ok", False)),
            "dag_ok": bool(structure_meta.get("dag_ok", False)),
            "search_source_ok": not any(issue.startswith("search_") for issue in warnings),
            "retrieval_before_topic_ok": retrieval_before_topic_ok,
            "grammar_after_retrieval_ok": grammar_after_retrieval_ok,
            "video_after_waveform_ok": video_after_waveform_ok,
            "bridge_tool_ok": not bridge_tool_warnings,
            "unrequested_bridge_tool_count": len(bridge_tool_warnings),
            "required_actions": required_actions,
            "covered_actions": action_coverage.get("covered_actions", []),
            "missing_actions": missing_actions,
            "executed_actions": executed_actions,
            "extra_actions": extra_actions,
            "schema_failures": list(structure_meta.get("schema_failures", [])),
            "schema_warnings": list(structure_meta.get("schema_warnings", [])),
            "dependency_failures": list(structure_meta.get("dependency_failures", [])),
            "modality_failures": list(structure_meta.get("modality_failures", [])),
            "modality_warnings": list(structure_meta.get("modality_warnings", [])),
            "dag_failures": dag_failures,
            "failures": failures,
            "warnings": warnings,
            "warning_count": len(warnings),
            "validation_error": validation_error,
            "validation_ok": validation_error is None,
            "graph": structure_meta.get("graph", {}),
            "verifier_pass": not failures,
            "repairable": repairable,
        }

    def _candidate_selection_meta(
        self,
        workflow: Dict[str, Any],
        compiled_nodes: List[Dict[str, Any]],
        user_requirement: str,
        score_details: Optional[Dict[str, Any]] = None,
        verification_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        del score_details
        source_meta = verification_meta if isinstance(verification_meta, dict) else None
        if source_meta is None:
            source_meta = self._verify_candidate_workflow(workflow, compiled_nodes, user_requirement)
        meta = dict(source_meta)
        failures = [
            item
            for item in meta.get("failures", [])
            if isinstance(item, str) and not item.startswith("validation_error:")
        ]
        warnings = [str(item) for item in meta.get("warnings", []) if isinstance(item, str)]
        meta["failures"] = failures
        meta["warning_count"] = len(warnings)
        meta["validation_ok"] = not bool(meta.get("validation_error"))
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
            repair_warnings = [
                str(item)
                for item in warnings
                if isinstance(item, str) and not str(item).startswith("unknown_skill:")
            ]
            meta["repairable"] = (
                len(missing_actions) <= 1
                and len(failures) <= 2
                and len(repair_warnings) <= 3
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
        verification_meta: Optional[Dict[str, Any]] = None,
        repair_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        verification_meta = verification_meta or self._verify_candidate_workflow(
            workflow,
            compiled_nodes,
            user_requirement,
        )
        selection_meta = self._candidate_selection_meta(
            workflow,
            compiled_nodes,
            user_requirement,
            verification_meta=verification_meta,
        )
        score_meta = self._score_compiled_workflow(
            workflow,
            compiled_nodes,
            user_requirement=user_requirement,
            verification_meta=verification_meta,
        )
        return {
            "workflow": workflow,
            "score": score_meta["score"],
            "score_details": score_meta["details"],
            "verification_meta": verification_meta,
            "selection_meta": selection_meta,
            "strategy_name": strategy_name,
            "strategy_hint": strategy_hint,
            "sampling_temperature": sampling_temperature,
            "repair_meta": repair_meta or {"attempted": False, "applied": False},
        }

    async def _finalize_candidate_workflow(
        self,
        workflow: Dict[str, Any],
        user_requirement: str,
        strategy_name: str,
        strategy_hint: str,
        sampling_temperature: float,
        llm_client: Any = None,
    ) -> Dict[str, Any]:
        normalized_workflow, compiled_nodes = self._prepare_workflow(workflow)
        verification_meta = self._verify_candidate_workflow(
            normalized_workflow,
            compiled_nodes,
            user_requirement,
        )
        candidate = self._build_candidate_record(
            normalized_workflow,
            compiled_nodes,
            user_requirement,
            strategy_name=strategy_name,
            strategy_hint=strategy_hint,
            sampling_temperature=sampling_temperature,
            verification_meta=verification_meta,
        )
        selection_meta = candidate.get("selection_meta", {})
        repairable = bool(verification_meta.get("repairable"))
        if isinstance(selection_meta, dict):
            repairable = repairable or bool(selection_meta.get("repairable"))
        if not repairable:
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
            repaired_normalized, repaired_compiled = self._prepare_workflow(repaired_workflow)
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
                verification_meta=repaired_verification,
                repair_meta={"attempted": True, "applied": True, "source": "llm_verifier"},
            )
        except Exception as exc:
            repair_meta["error"] = f"{type(exc).__name__}: {exc}"
            return candidate

        if type(self)._candidate_sort_key(repaired_candidate) > type(self)._candidate_sort_key(candidate):
            return repaired_candidate

        repair_meta["reason"] = "repair_not_better"
        return candidate

    async def plan_candidates(self, user_requirement: str, candidate_count: int = 3) -> List[Dict[str, Any]]:
        if candidate_count < 1:
            raise ValueError("candidate_count must be >= 1")

        strategy_specs = self._build_candidate_strategy_specs(user_requirement)
        pool_target = self._candidate_pool_target(strategy_specs, candidate_count)

        candidates: List[Dict[str, Any]] = []
        generation_errors: List[str] = []
        seen_signatures: set[str] = set()
        max_attempts = max(candidate_count * 4, len(strategy_specs))
        for i in range(max_attempts):
            spec = strategy_specs[i % len(strategy_specs)]
            round_idx = i // len(strategy_specs)
            strategy_name = str(spec.get("name", "")).strip()
            hint = str(spec.get("hint", "")).strip()
            base_temperature = float(spec.get("temperature", 0.0))
            temperature = min(base_temperature + round_idx * 0.1, 0.6)
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
            candidates.append(candidate)
            if len(candidates) >= pool_target:
                break

        if not candidates:
            if generation_errors:
                raise ValueError("no valid candidate plans generated; errors=" + " | ".join(generation_errors))
            raise ValueError("no valid candidate plans generated")
        ranking_pool = [
            item
            for item in candidates
            if int((item.get("selection_meta") or {}).get("hard_filter_tier", 0)) > 0
        ]
        if not ranking_pool:
            ranking_pool = candidates
        ranked = sorted(ranking_pool, key=type(self)._candidate_sort_key, reverse=True)
        selected = ranked[: max(candidate_count, 0)]
        for idx, item in enumerate(selected, start=1):
            item["id"] = idx
        return selected

    @staticmethod
    def _rerank_candidate_key(item: Dict[str, Any]) -> Tuple[Any, ...]:
        details = item.get("score_details")
        if not isinstance(details, dict):
            details = {}
        selection_meta = item.get("selection_meta")
        if not isinstance(selection_meta, dict):
            selection_meta = {}
        missing_actions = details.get("missing_actions", [])
        missing_count = len(missing_actions) if isinstance(missing_actions, list) else 0
        warning_count = int(selection_meta.get("warning_count", 0))
        extra_actions = selection_meta.get("extra_actions", [])
        extra_action_count = len(extra_actions) if isinstance(extra_actions, list) else 0
        action_coverage_score = float(
            details.get(
                "action_coverage_score",
                float(details.get("action_coverage_bonus", 0.0)) + float(details.get("action_coverage_penalty", 0.0)),
            )
        )
        executability_score = float(
            details.get(
                "executability_score",
                float(details.get("schema_validity_score", 0.0))
                + float(details.get("dependency_correctness_score", 0.0))
                + float(details.get("modality_consistency_score", 0.0))
                + float(details.get("dag_validity_score", 0.0)),
            )
        )
        memory_prior_score = float(
            details.get(
                "memory_prior_score",
                float(details.get("taskbench_prior_bonus", 0.0)) + float(details.get("taskbench_prior_penalty", 0.0)),
            )
        )
        return (
            int(selection_meta.get("hard_filter_tier", 1 if missing_count == 0 else 0)),
            int(selection_meta.get("coverage_complete", missing_count == 0)),
            int(selection_meta.get("validation_ok", True)),
            int(selection_meta.get("dag_ok", True)),
            int(selection_meta.get("schema_ok", True)),
            int(selection_meta.get("dependency_ok", True)),
            int(selection_meta.get("modality_ok", True)),
            int(selection_meta.get("bridge_tool_ok", True)),
            -int(selection_meta.get("unrequested_bridge_tool_count", 0)),
            int(selection_meta.get("search_source_ok", True)),
            int(selection_meta.get("retrieval_before_topic_ok", True)),
            int(selection_meta.get("grammar_after_retrieval_ok", True)),
            int(selection_meta.get("video_after_waveform_ok", True)),
            -extra_action_count,
            float(item.get("score", float("-inf"))),
            action_coverage_score,
            executability_score,
            memory_prior_score,
            float(details.get("text_preference_bonus", 0.0)) + float(details.get("text_preference_penalty", 0.0)),
            float(details.get("name_modality_bonus", 0.0)) + float(details.get("name_modality_penalty", 0.0)),
            float(details.get("transition_bonus", 0.0)) + float(details.get("transition_penalty", 0.0)),
            float(details.get("redundancy_penalty", 0.0)),
            float(details.get("extra_action_penalty", 0.0)),
            float(details.get("length_penalty", 0.0)),
            -warning_count,
            -missing_count,
            -int(item.get("id", 0)),
        )

    @staticmethod
    def _candidate_sort_key(item: Dict[str, Any]) -> Tuple[Any, ...]:
        return PlanningMixin._rerank_candidate_key(item)

    def _select_best_candidate(self, candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        return max(candidates, key=type(self)._rerank_candidate_key)
