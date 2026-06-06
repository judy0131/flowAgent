from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

try:
    from .llm_client import OpenAICompatibleLLMClient
    from .models import (
        PlannerDecision,
        PlanningCandidate,
        PlanningMemoryState,
        PredecessorCandidate,
        TaskStep,
        ToolCandidate,
        ToolRetrievalResult,
        WorkflowDAG,
        WorkflowEdge,
        WorkflowNode,
        to_plain_dict,
    )
except ImportError:
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent.memory_guided_workflow.llm_client import OpenAICompatibleLLMClient
    from agent.memory_guided_workflow.models import (
        PlannerDecision,
        PlanningCandidate,
        PlanningMemoryState,
        PredecessorCandidate,
        TaskStep,
        ToolCandidate,
        ToolRetrievalResult,
        WorkflowDAG,
        WorkflowEdge,
        WorkflowNode,
        to_plain_dict,
    )


def complete_taskbench_arguments(
    task: Optional[TaskStep] = None,
    tool_name: str = "",
    predecessor_node_ids: Optional[List[str]] = None,
    workflow_nodes: Optional[List[WorkflowNode]] = None,
    llm_arguments: Optional[List[Any]] = None,
    task_description: Optional[str] = None,
) -> List[str]:
    """Complete TaskBench-compatible arguments after planning is fixed.

    This does not select tools or edges. It only converts selected predecessor
    nodes into ``<node-i>`` references and appends literals preserved by task
    understanding. LLM literal arguments are appended only as an optional
    fallback for missing tool inputs.
    """
    if task is None:
        task = TaskStep(
            task_id="",
            description=str(task_description or ""),
            referenced_literals=[],
        )
    elif not isinstance(task, TaskStep):
        task = TaskStep(
            task_id="",
            description=str(task),
            referenced_literals=[],
        )
    _ = tool_name
    predecessor_node_ids = predecessor_node_ids or []
    workflow_nodes = workflow_nodes or []
    node_index_by_id = {
        node.node_id: index
        for index, node in enumerate(workflow_nodes)
    }

    arguments: List[str] = []
    for predecessor_node_id in predecessor_node_ids:
        if predecessor_node_id in node_index_by_id:
            arguments.append(f"<node-{node_index_by_id[predecessor_node_id]}>")

    arguments.extend(_clean_llm_literal_arguments(getattr(task, "referenced_literals", []) or []))
    arguments.extend(_clean_llm_literal_arguments(llm_arguments or []))
    return _dedupe_preserve_order(arguments)


class IncrementalPlanner:
    """LLM-based incremental planner for MIWP.

    The planner processes one TaskStep at a time. For each task it retrieves
    candidate tools, exposes the current workflow DAG plus transition priors to
    an LLM, validates the LLM decision, then appends one node and zero or more
    predecessor edges.
    """

    def __init__(
        self,
        tool_knowledge: Any,
        tool_transition_graph: Any,
        llm_client: Any = None,
        model: Optional[str] = None,
        temperature: float = 0.0,
        top_k: int = 5,
    ):
        self.tool_knowledge = tool_knowledge
        self.tool_transition_graph = tool_transition_graph
        self.llm_client = llm_client
        self.model = model
        self.temperature = temperature
        self.top_k = max(int(top_k or 5), 5)
        self.min_candidate_k = self.top_k
        self.max_candidate_k = max(self.min_candidate_k, 10)
        self.score_gap_threshold = 0.03
        self.debug_history: List[Dict[str, Any]] = []
        self._last_candidate_generation_debug: Dict[str, Any] = {}

    def build_planning_candidates(
        self,
        task: TaskStep,
        memory: Any,
    ) -> List[PlanningCandidate]:
        """Retrieve tools and attach possible predecessor nodes with priors."""
        tool_candidates = self._retrieve_tool_candidates(task.description)
        self._last_candidate_generation_debug = {
            "candidate_pool_policy": {
                "min_candidate_k": self.min_candidate_k,
                "max_candidate_k": self.max_candidate_k,
                "score_gap_threshold": self.score_gap_threshold,
                "selected_count": len(tool_candidates),
            },
            "candidate_tools": [_candidate_debug(candidate) for candidate in tool_candidates],
        }
        workflow = _get_workflow_dag(memory)

        candidates: List[PlanningCandidate] = []
        for tool in tool_candidates:
            candidate_input_types = self._get_tool_input_types(tool.tool_id)
            candidate_output_types = self._get_tool_output_types(tool.tool_id)
            predecessor_candidates: List[PredecessorCandidate] = []
            for node in workflow.nodes:
                transition_probability = self._get_transition_probability(node.tool_id, tool.tool_id)
                predecessor_output_types = self._get_node_output_types(node)
                type_compatible = _are_types_compatible(
                    predecessor_output_types,
                    candidate_input_types,
                )
                predecessor_candidates.append(
                    PredecessorCandidate(
                        node_id=node.node_id,
                        tool_id=node.tool_id,
                        tool_name=node.tool_name,
                        task_id=node.task_id,
                        task_description=node.task_description,
                        transition_probability=transition_probability,
                        metadata={
                            "output_types": list(predecessor_output_types),
                            "candidate_input_types": list(candidate_input_types),
                            "type_compatible": type_compatible,
                        },
                    )
                )

            candidates.append(
                PlanningCandidate(
                    task_id=task.task_id,
                    task_description=task.description,
                    tool_id=tool.tool_id,
                    tool_name=tool.name,
                    retrieval_score=float(tool.retrieval_score),
                    predecessor_candidates=predecessor_candidates,
                    metadata={
                        **dict(tool.metadata),
                        "intent": str(tool.metadata.get("intent", "unknown") or "unknown"),
                        "input_types": list(candidate_input_types),
                        "output_types": list(candidate_output_types),
                    },
                )
            )

        return candidates

    def _retrieve_tool_candidates(self, query: str) -> List[ToolCandidate]:
        retrieval = self.tool_knowledge.retrieve_tools(
            query=query,
            top_k=self.max_candidate_k,
        )
        tool_candidates: List[ToolCandidate] = []
        selected_candidates = self._select_dynamic_candidate_pool(retrieval.candidates)
        for candidate in selected_candidates:
            metadata = dict(candidate.metadata)
            intent = _candidate_intent(candidate)
            metadata["retrieval_score"] = float(candidate.retrieval_score)
            metadata["intent"] = intent
            metadata["input_types"] = self._get_tool_input_types(candidate.tool_id)
            metadata["output_types"] = self._get_tool_output_types(candidate.tool_id)
            tool_candidates.append(
                ToolCandidate(
                    tool_id=candidate.tool_id,
                    name=candidate.name,
                    retrieval_score=float(candidate.retrieval_score),
                    intent=intent,
                    metadata=metadata,
                )
            )
        return tool_candidates

    def _select_dynamic_candidate_pool(
        self,
        candidates: List[ToolCandidate],
    ) -> List[ToolCandidate]:
        if len(candidates) <= self.min_candidate_k:
            return list(candidates)

        selected = list(candidates[: self.min_candidate_k])
        base_score = float(candidates[self.min_candidate_k - 1].retrieval_score)
        for candidate in candidates[self.min_candidate_k : self.max_candidate_k]:
            score = float(candidate.retrieval_score)
            if base_score - score <= self.score_gap_threshold:
                selected.append(candidate)
                continue
            break
        return selected

    def build_workflow_context(self, memory: Any) -> Dict[str, Any]:
        """Build a complete JSON-friendly view of the workflow DAG."""
        workflow = _get_workflow_dag(memory)
        return {
            "nodes": [
                {
                    "node_id": node.node_id,
                    "task_id": node.task_id,
                    "task_description": node.task_description,
                    "tool_id": node.tool_id,
                    "tool_name": node.tool_name,
                    "output_types": self._get_node_output_types(node),
                    "metadata": to_plain_dict(node.metadata),
                }
                for node in workflow.nodes
            ],
            "edges": [
                {
                    "source_node_id": edge.source_node_id,
                    "target_node_id": edge.target_node_id,
                    "edge_type": edge.edge_type,
                    "metadata": to_plain_dict(edge.metadata),
                }
                for edge in workflow.edges
            ],
        }

    def build_planning_context(
        self,
        task: TaskStep,
        candidates: List[PlanningCandidate],
        memory: Any,
    ) -> Dict[str, Any]:
        """Build the full planning context passed to the LLM."""
        return {
            "current_task": {
                "task_id": task.task_id,
                "description": task.description,
                "referenced_literals": list(getattr(task, "referenced_literals", []) or []),
            },
            "workflow_so_far": self.build_workflow_context(memory),
            "candidate_generation_debug": to_plain_dict(self._last_candidate_generation_debug),
            "candidate_tools": [
                {
                    "tool_id": candidate.tool_id,
                    "tool_name": candidate.tool_name,
                    "retrieval_score": candidate.retrieval_score,
                    "intent": str(candidate.metadata.get("intent", "unknown") or "unknown"),
                    "input_types": list(candidate.metadata.get("input_types", []) or []),
                    "predecessor_candidates": [
                        {
                            "node_id": predecessor.node_id,
                            "tool_id": predecessor.tool_id,
                            "tool_name": predecessor.tool_name,
                            "task_id": predecessor.task_id,
                            "task_description": predecessor.task_description,
                            "transition_probability": predecessor.transition_probability,
                            "output_types": list(predecessor.metadata.get("output_types", []) or []),
                            "type_compatible": bool(predecessor.metadata.get("type_compatible", False)),
                        }
                        for predecessor in candidate.predecessor_candidates
                    ],
                }
                for candidate in candidates
            ],
        }

    def build_prompt(self, context: Dict[str, Any]) -> str:
        """Build the LLM prompt for a single incremental planning step."""
        context_json = json.dumps(context, ensure_ascii=False, indent=2)
        return f"""
You are an incremental workflow planner.

Your task is to expand the current workflow DAG by exactly one node.

You are given:
1. current_task
2. workflow_so_far, including nodes and edges
3. candidate_tools
4. predecessor_candidates with transition_probability

You must decide:
1. selected_tool_id
2. predecessor_node_ids

Rules:
- Select exactly one selected_tool_id from candidate_tools.
- predecessor_node_ids must be selected only from workflow_so_far.nodes.
- You may select zero predecessor nodes if the current task is independent or starts a new branch.
- You may select one predecessor node for a chain.
- You may select multiple predecessor nodes for a merge.
- Do not invent tools.
- Do not invent nodes.
- Do not output text outside JSON.

Tool selection:
- Candidate tools are suggestions retrieved from semantic matching.
- Select the tool that best satisfies the current task semantics and workflow context.
- Prefer tools whose intent matches the requested action in current_task.
- For example, if the task says find/search/provide related media, prefer intent=search.
- If the task says generate/create/draw/render, prefer intent=generate/create.
- If input/output types are both plausible, action intent should break ties.
- Do not select a tool solely because it has the highest retrieval score.
- Retrieval scores are supporting evidence only.

Predecessor selection:
- First determine which previous node(s) provide the information, artifact, or result required by the current task.
- Prefer predecessors that create a coherent workflow structure and satisfy the semantic dependency of the current task.
- A predecessor should normally provide an artifact compatible with at least one required input type of the selected tool.
- Do not select predecessors whose outputs cannot satisfy any required input type.
- Use transition_probability  as supporting evidence when multiple predecessor choices are semantically plausible.
- Do not select a predecessor solely because it has the highest transition_probability.
- Avoid unnecessary edges and avoid connecting to unrelated nodes.
- For DAG workflows, fork is allowed when multiple downstream tasks consume the same output.
- For DAG workflows, merge is allowed when the current task requires outputs from multiple previous nodes.

Return JSON only:

{{
  "selected_tool_id": "...",
  "predecessor_node_ids": ["..."],
  "reason": "..."
}}

Planning context JSON:
{context_json}
""".strip()

    def decide_with_llm(self, context: Dict[str, Any]) -> PlannerDecision:
        """Call the configured LLM and parse a PlannerDecision."""
        task = _task_from_context(context)
        candidates = _planning_candidates_from_context(context)
        workflow_context = context.get("workflow_so_far", {})

        try:
            prompt = self.build_prompt(context)
            raw_text = self._call_llm(prompt)
            payload = self._extract_json(raw_text)
            predecessor_node_ids = payload.get("predecessor_node_ids", [])
            if not isinstance(predecessor_node_ids, list):
                predecessor_node_ids = []

            return PlannerDecision(
                task_id=task.task_id,
                selected_tool_id=str(payload.get("selected_tool_id", "")).strip(),
                predecessor_node_ids=[
                    str(node_id).strip()
                    for node_id in predecessor_node_ids
                    if str(node_id).strip()
                ],
                reason=str(payload.get("reason", "") or ""),
                raw_llm_output=payload,
                metadata={"raw_llm_text": raw_text},
            )
        except Exception as exc:
            decision = self.fallback_decision(task, candidates, workflow_context)
            decision.metadata["fallback_reason"] = str(exc)
            return decision

    def validate_decision(
        self,
        decision: PlannerDecision,
        candidates: List[PlanningCandidate],
        workflow_context: Dict[str, Any],
    ) -> PlannerDecision:
        """Ensure the LLM selected only legal tools and predecessor nodes."""
        candidate_tool_ids = {candidate.tool_id for candidate in candidates}
        task = _task_from_candidates_or_decision(candidates, decision)

        if not decision.selected_tool_id or decision.selected_tool_id not in candidate_tool_ids:
            fallback = self.fallback_decision(task, candidates, workflow_context)
            fallback.metadata["validation_error"] = "selected_tool_id is not in candidate_tools"
            return fallback

        valid_node_ids = {
            str(node.get("node_id", ""))
            for node in workflow_context.get("nodes", [])
            if isinstance(node, dict)
        }

        cleaned_predecessors: List[str] = []
        seen = set()
        for node_id in decision.predecessor_node_ids:
            if node_id not in valid_node_ids or node_id in seen:
                continue
            cleaned_predecessors.append(node_id)
            seen.add(node_id)

        decision.predecessor_node_ids = cleaned_predecessors
        self.validate_predecessors(decision, candidates)
        return decision

    def validate_predecessors(
        self,
        decision: PlannerDecision,
        candidates: List[PlanningCandidate],
    ) -> PlannerDecision:
        """Remove selected predecessors that cannot provide compatible types."""
        selected_candidate = _find_candidate(candidates, decision.selected_tool_id)
        if selected_candidate is None:
            return decision

        compatible_by_node_id = {
            predecessor.node_id: bool(predecessor.metadata.get("type_compatible", False))
            for predecessor in selected_candidate.predecessor_candidates
        }
        kept: List[str] = []
        removed: List[str] = []
        for node_id in decision.predecessor_node_ids:
            if compatible_by_node_id.get(node_id, False):
                kept.append(node_id)
            else:
                removed.append(node_id)

        decision.predecessor_node_ids = kept
        if removed:
            decision.metadata["removed_incompatible_predecessors"] = removed
        return decision

    def fallback_decision(
        self,
        task: TaskStep,
        candidates: List[PlanningCandidate],
        workflow_context: Dict[str, Any],
    ) -> PlannerDecision:
        """Deterministic fallback when LLM output is invalid or unavailable."""
        if not candidates:
            return PlannerDecision(
                task_id=task.task_id,
                selected_tool_id="",
                predecessor_node_ids=[],
                reason="fallback: no candidate tools available",
                metadata={"fallback": True},
            )

        selected = max(candidates, key=lambda item: item.retrieval_score)
        predecessor_node_ids: List[str] = []
        if selected.predecessor_candidates:
            best_predecessor = max(
                selected.predecessor_candidates,
                key=lambda item: item.transition_probability,
            )
            if best_predecessor.transition_probability > 0:
                predecessor_node_ids = [best_predecessor.node_id]

        return PlannerDecision(
            task_id=task.task_id,
            selected_tool_id=selected.tool_id,
            predecessor_node_ids=predecessor_node_ids,
            reason="fallback: selected highest retrieval score and best positive transition predecessor",
            metadata={"fallback": True},
        )

    def complete_literal_arguments_if_needed(
        self,
        task: TaskStep,
        selected_candidate: PlanningCandidate,
        reference_arguments: List[str],
    ) -> tuple[List[str], Dict[str, Any]]:
        """Complete literal arguments only when tool input count is not met."""
        input_types = self._get_tool_input_types(selected_candidate.tool_id)
        required_count = len(input_types)
        current_count = len(reference_arguments)
        missing_count = max(required_count - current_count, 0)
        completion: Dict[str, Any] = {
            "input_types": list(input_types),
            "required_count": required_count,
            "reference_argument_count": current_count,
            "missing_count": missing_count,
            "literal_arguments": [],
            "completed_by": "not_needed" if missing_count == 0 else "llm",
        }

        if missing_count <= 0:
            return list(reference_arguments), completion

        literal_arguments, llm_metadata = self.complete_literal_arguments_with_llm(
            task=task,
            selected_candidate=selected_candidate,
            reference_arguments=reference_arguments,
            input_types=input_types,
            missing_count=missing_count,
        )
        literal_arguments = literal_arguments[:missing_count]
        final_arguments = _dedupe_preserve_order(
            list(reference_arguments) + list(literal_arguments)
        )

        completion["literal_arguments"] = list(literal_arguments)
        completion["final_count"] = len(final_arguments)
        completion["llm"] = llm_metadata
        if len(final_arguments) < required_count:
            completion["warning"] = "argument_count_below_tool_input_count"

        return final_arguments, completion

    def complete_literal_arguments_with_llm(
        self,
        task: TaskStep,
        selected_candidate: PlanningCandidate,
        reference_arguments: List[str],
        input_types: List[str],
        missing_count: int,
    ) -> tuple[List[str], Dict[str, Any]]:
        """Ask the LLM for missing literal arguments without changing the DAG."""
        prompt = self.build_literal_argument_prompt(
            task=task,
            selected_candidate=selected_candidate,
            reference_arguments=reference_arguments,
            input_types=input_types,
            missing_count=missing_count,
        )
        try:
            raw_text = self._call_llm(prompt)
            payload = self._extract_json(raw_text)
            return _extract_llm_literal_arguments(payload), {
                "raw_llm_text": raw_text,
                "raw_llm_output": payload,
            }
        except Exception as exc:
            return [], {"fallback_reason": str(exc)}

    def build_literal_argument_prompt(
        self,
        task: TaskStep,
        selected_candidate: PlanningCandidate,
        reference_arguments: List[str],
        input_types: List[str],
        missing_count: int,
    ) -> str:
        """Build a focused prompt for filling only missing literal arguments."""
        context = {
            "current_task": {
                "task_id": task.task_id,
                "description": task.description,
            },
            "selected_tool": {
                "tool_id": selected_candidate.tool_id,
                "tool_name": selected_candidate.tool_name,
                "input_types": list(input_types),
            },
            "existing_arguments": list(reference_arguments),
            "missing_literal_argument_count": missing_count,
        }
        context_json = json.dumps(context, ensure_ascii=False, indent=2)
        return f"""
You complete missing TaskBench literal arguments for one already-planned workflow node.

The selected tool and DAG predecessors are already fixed. Do not change them.

Rules:
- existing_arguments already contains predecessor references such as <node-0>.
- Return only literal arguments explicitly present in current_task, such as file names, URLs, quoted effect names, colors, languages, or numeric settings.
- Do not return predecessor references like <node-i>.
- Return at most missing_literal_argument_count items.
- If no explicit literal argument is present, return an empty list.
- Do not output text outside JSON.

Return JSON only:

{{
  "literal_arguments": ["..."]
}}

Literal argument context JSON:
{context_json}
""".strip()

    def _get_tool_input_types(self, tool_id: str) -> List[str]:
        if hasattr(self.tool_knowledge, "get_tool"):
            tool = self.tool_knowledge.get_tool(tool_id)
            if tool is not None and hasattr(tool, "input_types"):
                return [str(item) for item in getattr(tool, "input_types", []) if str(item).strip()]
        return []

    def _get_transition_probability(self, source_tool_id: str, target_tool_id: str) -> float:
        if hasattr(self.tool_transition_graph, "get_transition_probability"):
            return float(
                self.tool_transition_graph.get_transition_probability(
                    source_tool_id,
                    target_tool_id,
                )
                or 0.0
            )
        return 0.0

    def _get_tool_output_types(self, tool_id: str) -> List[str]:
        if hasattr(self.tool_knowledge, "get_tool"):
            tool = self.tool_knowledge.get_tool(tool_id)
            if tool is not None and hasattr(tool, "output_types"):
                return [str(item) for item in getattr(tool, "output_types", []) if str(item).strip()]
        return []

    def _get_node_output_types(self, node: WorkflowNode) -> List[str]:
        metadata = getattr(node, "metadata", {}) or {}
        if isinstance(metadata, dict):
            output_types = metadata.get("output_types", [])
            if output_types:
                return [str(item) for item in output_types if str(item).strip()]
        return self._get_tool_output_types(node.tool_id)

    def apply_decision(
        self,
        task: TaskStep,
        decision: PlannerDecision,
        candidates: List[PlanningCandidate],
        memory: Any,
    ) -> List[str]:
        """Append the selected workflow node and predecessor edges to memory."""
        selected_candidate = _find_candidate(candidates, decision.selected_tool_id)
        if selected_candidate is None:
            raise ValueError(f"selected tool is not in candidates: {decision.selected_tool_id}")

        workflow = _get_workflow_dag(memory)
        reference_arguments = complete_taskbench_arguments(
            task=task,
            tool_name=selected_candidate.tool_name,
            predecessor_node_ids=decision.predecessor_node_ids,
            workflow_nodes=workflow.nodes,
        )
        final_arguments, argument_completion = self.complete_literal_arguments_if_needed(
            task=task,
            selected_candidate=selected_candidate,
            reference_arguments=reference_arguments,
        )

        node = WorkflowNode(
            node_id=f"n_{task.task_id}",
            task_id=task.task_id,
            task_description=task.description,
            tool_id=selected_candidate.tool_id,
            tool_name=selected_candidate.tool_name,
            metadata={
                "selected_by": "llm_incremental_planner",
                "retrieval_score": selected_candidate.retrieval_score,
                "reason": decision.reason,
                "arguments": list(final_arguments),
                "argument_completion": argument_completion,
                "input_types": list(selected_candidate.metadata.get("input_types", []) or []),
                "output_types": list(selected_candidate.metadata.get("output_types", []) or []),
            },
        )
        if hasattr(node, "arguments"):
            setattr(node, "arguments", list(final_arguments))

        transition_by_node_id = {
            predecessor.node_id: predecessor.transition_probability
            for predecessor in selected_candidate.predecessor_candidates
        }
        edges = [
            WorkflowEdge(
                source_node_id=predecessor_node_id,
                target_node_id=node.node_id,
                edge_type="tool_transition",
                metadata={
                    "selected_by": "llm_incremental_planner",
                    "transition_probability": transition_by_node_id.get(predecessor_node_id, 0.0),
                },
            )
            for predecessor_node_id in decision.predecessor_node_ids
        ]

        action = {
            "tool_id": selected_candidate.tool_id,
            "tool_name": selected_candidate.tool_name,
            "retrieval_score": selected_candidate.retrieval_score,
            "predecessor_node_ids": list(decision.predecessor_node_ids),
            "arguments": list(final_arguments),
            "argument_completion": argument_completion,
            "input_types": list(selected_candidate.metadata.get("input_types", []) or []),
            "output_types": list(selected_candidate.metadata.get("output_types", []) or []),
            "reason": decision.reason,
        }

        if hasattr(memory, "apply_selected_action"):
            memory.apply_selected_action(
                task_id=task.task_id,
                action=action,
                node=node,
                edges=edges,
            )
            return list(final_arguments)

        _apply_to_memory_state(memory, task.task_id, action, node, edges)
        return list(final_arguments)

    def plan_next(
        self,
        task: TaskStep,
        memory: Any,
    ) -> PlannerDecision:
        """Plan and commit one task."""
        candidates = self.build_planning_candidates(task, memory)
        context = self.build_planning_context(task, candidates, memory)
        decision = self.decide_with_llm(context)
        decision = self.validate_decision(
            decision=decision,
            candidates=candidates,
            workflow_context=context["workflow_so_far"],
        )
        final_arguments = self.apply_decision(task, decision, candidates, memory)
        committed_node = _find_workflow_node(
            _get_workflow_dag(memory),
            f"n_{task.task_id}",
        )
        argument_completion = (
            committed_node.metadata.get("argument_completion", {})
            if committed_node is not None and isinstance(committed_node.metadata, dict)
            else {}
        )

        self.debug_history.append(
            {
                "planning_context": context,
                "decision": decision.to_dict(),
                "final_arguments": list(final_arguments),
                "argument_completion": to_plain_dict(argument_completion),
            }
        )

        return decision

    def plan(
        self,
        tasks: List[TaskStep],
        memory: Any,
    ) -> WorkflowDAG:
        """Plan all tasks incrementally and return the final workflow DAG."""
        for task in tasks:
            self.plan_next(task, memory)
        return _get_workflow_dag(memory)

    def _call_llm(self, prompt: str) -> str:
        """Call the configured LLM client without importing provider SDKs here."""
        if self.llm_client is None:
            self.llm_client = OpenAICompatibleLLMClient()

        if hasattr(self.llm_client, "generate"):
            try:
                return self.llm_client.generate(
                    prompt=prompt,
                    model=self.model,
                    temperature=self.temperature,
                )
            except TypeError:
                try:
                    return self.llm_client.generate(
                        prompt=prompt,
                        temperature=self.temperature,
                    )
                except TypeError:
                    return self.llm_client.generate(prompt)

        if hasattr(self.llm_client, "chat"):
            messages = [
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": prompt},
            ]
            try:
                return self.llm_client.chat(messages=messages)
            except TypeError:
                try:
                    return self.llm_client.chat(
                        prompt=prompt,
                        temperature=self.temperature,
                    )
                except TypeError:
                    return self.llm_client.chat(prompt)

        if hasattr(self.llm_client, "complete"):
            try:
                return self.llm_client.complete(
                    prompt=prompt,
                    model=self.model,
                    temperature=self.temperature,
                )
            except TypeError:
                return self.llm_client.complete(prompt)

        raise TypeError("llm_client must provide generate(), chat(), or complete()")

    @staticmethod
    def _extract_json(text: str) -> Dict[str, Any]:
        """Extract a JSON object from plain text, fenced JSON, or mixed output."""
        raw_text = str(text or "").strip()
        if not raw_text:
            raise ValueError("empty LLM response")

        try:
            payload = json.loads(raw_text)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass

        for block in re.findall(r"```(?:json)?\s*(.*?)```", raw_text, flags=re.DOTALL | re.IGNORECASE):
            try:
                payload = json.loads(block.strip())
                if isinstance(payload, dict):
                    return payload
            except json.JSONDecodeError:
                continue

        object_text = _find_first_json_object(raw_text)
        if object_text:
            payload = json.loads(object_text)
            if isinstance(payload, dict):
                return payload

        raise ValueError("no JSON object found in LLM response")


def _get_workflow_dag(memory: Any) -> WorkflowDAG:
    if hasattr(memory, "get_workflow_dag"):
        return memory.get_workflow_dag()
    if isinstance(memory, PlanningMemoryState):
        return memory.workflow_dag
    if hasattr(memory, "state") and isinstance(memory.state, PlanningMemoryState):
        return memory.state.workflow_dag
    if hasattr(memory, "workflow_dag"):
        return memory.workflow_dag
    raise TypeError("memory must expose a WorkflowDAG")


def _find_workflow_node(workflow: WorkflowDAG, node_id: str) -> Optional[WorkflowNode]:
    if hasattr(workflow, "get_node"):
        return workflow.get_node(node_id)
    for node in getattr(workflow, "nodes", []):
        if getattr(node, "node_id", None) == node_id:
            return node
    return None


def _tool_intent(tool: Any) -> str:
    direct_intent = str(getattr(tool, "intent", "") or "").strip()
    if direct_intent and direct_intent != "unknown":
        return direct_intent
    metadata = getattr(tool, "metadata", {}) or {}
    if isinstance(metadata, dict):
        intent = str(metadata.get("intent", "") or "").strip()
        if intent:
            return intent
    return "unknown"


def _candidate_intent(candidate: ToolCandidate) -> str:
    direct_intent = str(getattr(candidate, "intent", "") or "").strip()
    if direct_intent and direct_intent != "unknown":
        return direct_intent
    metadata = candidate.metadata or {}
    intent = str(metadata.get("intent", "") or "").strip()
    if intent:
        return intent
    return "unknown"


def _candidate_debug(candidate: ToolCandidate) -> Dict[str, Any]:
    metadata = dict(candidate.metadata)
    return {
        "tool_id": candidate.tool_id,
        "tool_name": candidate.name,
        "retrieval_score": float(metadata.get("retrieval_score", candidate.retrieval_score) or 0.0),
        "intent": _candidate_intent(candidate),
        "input_types": list(metadata.get("input_types", []) or []),
        "output_types": list(metadata.get("output_types", []) or []),
    }


def _are_types_compatible(output_types: List[str], input_types: List[str]) -> bool:
    if not output_types or not input_types:
        return True
    normalized_outputs = {_normalize_type_name(item) for item in output_types}
    normalized_inputs = {_normalize_type_name(item) for item in input_types}
    return bool(normalized_outputs & normalized_inputs)


def _normalize_type_name(value: Any) -> str:
    return str(value or "").strip().lower()


def _apply_to_memory_state(
    memory: Any,
    task_id: str,
    action: Dict[str, Any],
    node: WorkflowNode,
    edges: List[WorkflowEdge],
) -> None:
    state = memory.state if hasattr(memory, "state") else memory
    if not isinstance(state, PlanningMemoryState):
        raise TypeError("memory must be PlanningMemoryState-like or provide apply_selected_action()")

    state.selected_action_history.append(
        {
            "task_id": task_id,
            "action": dict(action),
            "step": state.current_step,
        }
    )
    state.workflow_dag.add_node(node)
    for edge in edges:
        state.workflow_dag.add_edge(edge)

    if action.get("tool_id") and action["tool_id"] not in state.selected_tool_ids:
        state.selected_tool_ids.append(action["tool_id"])
    if task_id in state.remaining_task_ids:
        state.remaining_task_ids.remove(task_id)
    if task_id not in state.completed_task_ids:
        state.completed_task_ids.append(task_id)
    state.current_step += 1


def _task_from_context(context: Dict[str, Any]) -> TaskStep:
    current_task = context.get("current_task", {})
    return TaskStep(
        task_id=str(current_task.get("task_id", "")),
        description=str(current_task.get("description", "")),
        referenced_literals=list(current_task.get("referenced_literals", []) or []),
    )


def _planning_candidates_from_context(context: Dict[str, Any]) -> List[PlanningCandidate]:
    task = _task_from_context(context)
    candidates: List[PlanningCandidate] = []
    for item in context.get("candidate_tools", []):
        if not isinstance(item, dict):
            continue
        predecessor_candidates = [
            PredecessorCandidate(
                node_id=str(predecessor.get("node_id", "")),
                tool_id=str(predecessor.get("tool_id", "")),
                tool_name=str(predecessor.get("tool_name", "")),
                task_id=str(predecessor.get("task_id", "")),
                task_description=str(predecessor.get("task_description", "")),
                transition_probability=float(predecessor.get("transition_probability", 0.0) or 0.0),
                metadata={
                    "output_types": list(predecessor.get("output_types", []) or []),
                    "type_compatible": bool(predecessor.get("type_compatible", False)),
                },
            )
            for predecessor in item.get("predecessor_candidates", [])
            if isinstance(predecessor, dict)
        ]
        candidates.append(
            PlanningCandidate(
                task_id=task.task_id,
                task_description=task.description,
                tool_id=str(item.get("tool_id", "")),
                tool_name=str(item.get("tool_name", "")),
                retrieval_score=float(item.get("retrieval_score", 0.0) or 0.0),
                predecessor_candidates=predecessor_candidates,
                metadata={
                    "input_types": list(item.get("input_types", []) or []),
                    "intent": str(item.get("intent", "unknown") or "unknown"),
                },
            )
        )
    return candidates


def _task_from_candidates_or_decision(
    candidates: List[PlanningCandidate],
    decision: PlannerDecision,
) -> TaskStep:
    if candidates:
        return TaskStep(
            task_id=candidates[0].task_id,
            description=candidates[0].task_description,
        )
    return TaskStep(task_id=decision.task_id, description="")


def _find_candidate(
    candidates: List[PlanningCandidate],
    tool_id: str,
) -> Optional[PlanningCandidate]:
    for candidate in candidates:
        if candidate.tool_id == tool_id:
            return candidate
    return None


def _find_first_json_object(text: str) -> str:
    start = text.find("{")
    if start < 0:
        return ""

    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == "\"":
                in_string = False
            continue

        if char == "\"":
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


def _extract_llm_literal_arguments(payload: Dict[str, Any]) -> List[str]:
    if not isinstance(payload, dict):
        return []

    raw_arguments = payload.get("literal_arguments")
    if raw_arguments is None:
        raw_arguments = payload.get("arguments")
    if raw_arguments is None:
        raw_arguments = payload.get("taskbench_arguments")

    if raw_arguments is None:
        return []
    if isinstance(raw_arguments, str):
        raw_arguments = [raw_arguments]
    if not isinstance(raw_arguments, list):
        return []

    return _clean_llm_literal_arguments(raw_arguments)


def _clean_llm_literal_arguments(values: List[Any]) -> List[str]:
    return _dedupe_preserve_order(
        value
        for value in values
        if _is_valid_llm_literal_argument(value)
    )


def _is_valid_llm_literal_argument(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    normalized = text.lower()
    if normalized.startswith("<node-") and normalized.endswith(">"):
        return False
    if normalized.startswith("step ") or normalized.startswith("step"):
        return False
    return True


def _dedupe_preserve_order(values: Any) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


class _DemoPlanningMemory:
    def __init__(self, tasks: List[TaskStep]):
        self.state = PlanningMemoryState(
            remaining_task_ids=[task.task_id for task in tasks],
            workflow_dag=WorkflowDAG(),
        )

    def get_workflow_dag(self) -> WorkflowDAG:
        return self.state.workflow_dag

    def apply_selected_action(
        self,
        task_id: str,
        action: Dict[str, Any],
        node: WorkflowNode,
        edges: List[WorkflowEdge],
    ) -> None:
        _apply_to_memory_state(self.state, task_id, action, node, edges)


class _FakeToolKnowledge:
    def __init__(self):
        self._candidates = {
            "t1": [
                ToolCandidate("Image Downloader", "Image Downloader", 0.90),
            ],
            "t2": [
                ToolCandidate("Image-to-Text", "Image-to-Text", 0.92),
                ToolCandidate("Keyword Extractor", "Keyword Extractor", 0.50),
            ],
            "t3": [
                ToolCandidate("Text Translator", "Text Translator", 0.88),
                ToolCandidate("Text Summarizer", "Text Summarizer", 0.40),
            ],
        }

    def retrieve_tools(self, query: str, top_k: int = 5) -> ToolRetrievalResult:
        task_id = "t1"
        if "extract" in query.lower():
            task_id = "t2"
        elif "translate" in query.lower():
            task_id = "t3"
        return ToolRetrievalResult(
            task_id=task_id,
            query=query,
            candidates=self._candidates[task_id][:top_k],
        )


class _FakeToolTransitionGraph:
    def __init__(self):
        self._probabilities = {
            ("Image Downloader", "Image-to-Text"): 0.8,
            ("Image-to-Text", "Text Translator"): 0.9,
            ("Image Downloader", "Keyword Extractor"): 0.1,
            ("Image-to-Text", "Text Summarizer"): 0.2,
        }

    def get_transition_probability(self, source_tool_id: str, target_tool_id: str) -> float:
        return self._probabilities.get((source_tool_id, target_tool_id), 0.0)


class _FakeLLMClient:
    def chat(self, messages: List[Dict[str, str]]) -> str:
        prompt = messages[-1]["content"]
        context = IncrementalPlanner._extract_json(prompt[prompt.rfind("Planning context JSON:") :])
        task_id = context["current_task"]["task_id"]
        if task_id == "t1":
            return json.dumps(
                {
                    "selected_tool_id": "Image Downloader",
                    "predecessor_node_ids": [],
                    "literal_arguments": [],
                    "reason": "Download starts the workflow.",
                }
            )
        if task_id == "t2":
            return json.dumps(
                {
                    "selected_tool_id": "Image-to-Text",
                    "predecessor_node_ids": ["n_t1"],
                    "literal_arguments": [],
                    "reason": "Text extraction depends on the downloaded image.",
                }
            )
        return json.dumps(
            {
                "selected_tool_id": "Text Translator",
                "predecessor_node_ids": ["n_t2"],
                "literal_arguments": ["French"],
                "reason": "Translation depends on the extracted text.",
            }
        )


def _main() -> None:
    tasks = [
        TaskStep(task_id="t1", description="Download an image."),
        TaskStep(task_id="t2", description="Extract the text from the image."),
        TaskStep(task_id="t3", description="Translate the text into French."),
    ]
    memory = _DemoPlanningMemory(tasks)
    planner = IncrementalPlanner(
        tool_knowledge=_FakeToolKnowledge(),
        tool_transition_graph=_FakeToolTransitionGraph(),
        llm_client=_FakeLLMClient(),
        top_k=5,
    )

    final_dag = planner.plan(tasks, memory)

    print("Planning trace:")
    for index, item in enumerate(planner.debug_history, start=1):
        print(f"\nStep {index} planning context:")
        print(json.dumps(item["planning_context"], ensure_ascii=False, indent=2))
        print("LLM decision:")
        print(json.dumps(item["decision"], ensure_ascii=False, indent=2))

    print("\nFinal DAG:")
    print(json.dumps(final_dag.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
