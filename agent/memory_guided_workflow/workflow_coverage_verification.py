from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

try:
    from .llm_client import OpenAICompatibleLLMClient
    from .models import TaskStep, to_plain_dict
    from .utils import coerce_list, extract_json_object
except ImportError:
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent.memory_guided_workflow.llm_client import OpenAICompatibleLLMClient
    from agent.memory_guided_workflow.models import TaskStep, to_plain_dict
    from agent.memory_guided_workflow.utils import coerce_list, extract_json_object


def compute_connected_components(workflow: Dict[str, Any]) -> Dict[str, Any]:
    """Compute weakly connected components for a workflow DAG dictionary."""
    if not isinstance(workflow, dict):
        return {"component_count": 0, "components": []}

    nodes = [
        node
        for node in workflow.get("nodes", [])
        if isinstance(node, dict) and str(node.get("node_id", "")).strip()
    ]
    node_ids = [str(node.get("node_id", "")).strip() for node in nodes]
    if not node_ids:
        return {"component_count": 0, "components": []}

    adjacency = {node_id: set() for node_id in node_ids}
    for edge in workflow.get("edges", []):
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source_node_id", "")).strip()
        target = str(edge.get("target_node_id", "")).strip()
        if source not in adjacency or target not in adjacency:
            continue
        adjacency[source].add(target)
        adjacency[target].add(source)

    seen = set()
    components: List[List[str]] = []
    for node_id in node_ids:
        if node_id in seen:
            continue
        component: List[str] = []
        stack = [node_id]
        seen.add(node_id)
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbor in adjacency[current]:
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                stack.append(neighbor)
        components.append(component)

    return {"component_count": len(components), "components": components}


class WorkflowCoverageVerifier:
    """LLM-based workflow coverage verifier and repair-task generator."""

    def __init__(
        self,
        llm_client: Any = None,
        model: Optional[str] = None,
        temperature: float = 0.0,
    ):
        self.llm_client = llm_client
        self.model = model
        self.temperature = temperature

    def verify(
        self,
        user_request: str,
        workflow: Dict[str, Any],
        component_info: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Verify coverage and return a normalized coverage report."""
        component_info = component_info or compute_connected_components(workflow)
        context = {
            "user_request": str(user_request or ""),
            "workflow": to_plain_dict(workflow),
            "component_count": component_info["component_count"],
            "components": component_info.get("components", []),
        }

        try:
            raw_text = self._call_llm(self.build_prompt(context))
            payload = extract_json_object(raw_text)
            report = self._normalize_report(payload)
            report["component_count"] = component_info["component_count"]
            report["components"] = component_info.get("components", [])
            report["raw_llm_output"] = payload
            return report
        except Exception as exc:
            return {
                "component_count": component_info["component_count"],
                "components": component_info.get("components", []),
                "is_fully_covered": True,
                "missing_requirements": [],
                "repair_tasks": [],
                "metadata": {"verification_error": str(exc)},
            }

    def build_prompt(self, context: Dict[str, Any]) -> str:
        context_json = json.dumps(context, ensure_ascii=False, indent=2)
        return f"""
You are a workflow coverage verifier.

Determine whether the current workflow fully satisfies the user request.

Important:

- Do not assume the initial task decomposition is complete.
- Verify that every requested operation is represented in the workflow.
- Consider transformations, analysis, retrieval, generation, synchronization, combination, and composition operations.
- The workflow may contain multiple independent branches.
- component_count is only a structural hint.
- Do not decide repair solely based on component_count.
- Multiple branches may be valid if the user requests multiple independent outputs.
- A single connected workflow may still be incomplete.
- If the workflow has multiple connected components, check whether the user request requires these branches to be combined, synchronized, merged, or composed into a final artifact.
- Only generate a repair task when an explicit executable operation is missing.

Repair task constraints:
- repair_tasks must be executable tasks that can map to a tool.
- Do not generate presentation tasks such as "provide the result", "return the output", "show the result", or "output the final answer".
- Do not generate repair tasks for missing literal parameters.
- Do not generate repair tasks that duplicate an operation already represented in the workflow.
- Do not generate repair tasks for independent outputs that do not need to be merged.
- Preserve exact referenced_literals needed by a repair task.

Return JSON only:

{{
  "is_fully_covered": true,
  "missing_requirements": [],
  "repair_tasks": []
}}

or

{{
  "is_fully_covered": false,
  "missing_requirements": [
    "..."
  ],
  "repair_tasks": [
    {{
      "description": "...",
      "referenced_literals": []
    }}
  ]
}}

Coverage context JSON:
{context_json}
""".strip()

    def _call_llm(self, prompt: str) -> str:
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
                return self.llm_client.generate(prompt)

        if hasattr(self.llm_client, "chat"):
            messages = [
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": prompt},
            ]
            return self.llm_client.chat(messages=messages)

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

    def _normalize_report(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        is_fully_covered = bool(payload.get("is_fully_covered", False))
        missing_requirements = [
            str(item).strip()
            for item in coerce_list(payload.get("missing_requirements", []))
            if str(item).strip()
        ]
        repair_tasks = [
            task.to_dict()
            for task in self._normalize_repair_tasks(payload.get("repair_tasks", []))
        ]
        if is_fully_covered:
            missing_requirements = []
            repair_tasks = []

        return {
            "is_fully_covered": is_fully_covered,
            "missing_requirements": missing_requirements,
            "repair_tasks": repair_tasks,
        }

    def _normalize_repair_tasks(self, payload: Any) -> List[TaskStep]:
        tasks: List[TaskStep] = []
        for index, item in enumerate(coerce_list(payload), start=1):
            if isinstance(item, dict):
                description = str(item.get("description", "") or "").strip()
                referenced_literals = [
                    str(value).strip()
                    for value in coerce_list(item.get("referenced_literals", []))
                    if str(value).strip()
                ]
            else:
                description = str(item or "").strip()
                referenced_literals = []

            if not description or not _is_executable_repair_description(description):
                continue
            tasks.append(
                TaskStep(
                    task_id=f"repair_{index}",
                    description=description,
                    referenced_literals=referenced_literals,
                )
            )
        return tasks


def _is_executable_repair_description(description: str) -> bool:
    text = str(description or "").strip().lower()
    if not text:
        return False
    blocked_prefixes = (
        "provide the result",
        "provide the output",
        "return the result",
        "return the output",
        "show the result",
        "show the output",
        "output the final",
        "display the result",
        "present the result",
        "provide the missing",
        "provide missing",
        "add the missing",
        "fill the missing",
    )
    if text.startswith(blocked_prefixes):
        return False
    blocked_phrases = (
        "missing argument",
        "missing parameter",
        "missing input",
    )
    return not any(phrase in text for phrase in blocked_phrases)
