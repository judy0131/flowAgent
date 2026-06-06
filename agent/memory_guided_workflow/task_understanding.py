import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List

try:
    from .llm_client import OpenAICompatibleLLMClient
    from .models import (
        PlanningMemoryState,
        TaskStep,
        TaskUnderstandingResult,
        UserRequest,
        WorkflowDAG,
    )
    from .utils import coerce_list, extract_json_object
except ImportError:
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent.memory_guided_workflow.llm_client import OpenAICompatibleLLMClient
    from agent.memory_guided_workflow.models import (
        PlanningMemoryState,
        TaskStep,
        TaskUnderstandingResult,
        UserRequest,
        WorkflowDAG,
    )
    from agent.memory_guided_workflow.utils import coerce_list, extract_json_object


class TaskUnderstanding:
    """Convert a natural-language request into MIWP initial planning state."""

    def __init__(
        self,
        llm_config_path: Any = None,
        llm_config: Dict[str, Any] | None = None,
        llm_profile: str | None = None,
        llm_client: OpenAICompatibleLLMClient | None = None,
    ):
        self.llm_client = llm_client or OpenAICompatibleLLMClient(
            llm_config_path=llm_config_path,
            llm_config=llm_config,
            llm_profile=llm_profile,
        )

    def parse(self, request: UserRequest) -> TaskUnderstandingResult:
        try:
            raw_text = self._call_llm(request)
            raw_payload = extract_json_object(raw_text)
            return self._normalize_result(request, raw_payload)
        except Exception as exc:
            return self._fallback_result(request, exc)

    def initialize_memory(self, result: TaskUnderstandingResult) -> PlanningMemoryState:
        return PlanningMemoryState(
            completed_task_ids=[],
            remaining_task_ids=[step.task_id for step in result.steps],
            selected_tool_ids=[],
            workflow_dag=WorkflowDAG(),
            current_step=0,
        )

    def _build_prompt(self, request: UserRequest) -> str:
        return f"""
    You are a task decomposition engine for workflow planning.

    Your job is to decompose a user request into a set of atomic user-level tasks.

    The output of this stage will be consumed by a workflow planner.

    Therefore, your task is NOT to generate a workflow.

    Rules:
    1. Decompose the request into atomic user-level tasks.
    2. Preserve user intent.
    3. Prefer verbs explicitly mentioned in the user request.
    4. Do not replace user verbs with tool names.
    5. Do not introduce actions that are not explicitly stated or strongly implied.
    6. Do not infer intermediate implementation tasks.
    7. Do not generate tools.
    8. Do not generate workflow nodes.
    9. Do not generate dependencies.
    10. Do not generate DAG edges.
    11. Do not generate execution plans.
    12. Do not generate explanations.
    13. Keep task descriptions concise.
    14. Each task should represent a user-level operation.
    15. Multiple tasks may later become sequential, parallel, fork, or merge structures, but this stage must NOT determine workflow topology.
    16. For each task, include referenced_literals.
    17. referenced_literals must copy exact user-provided files, URLs, quoted text, or explicit parameter values required by this task.
    18. Do not paraphrase referenced_literals.
    19. If a task consumes the output of a previous task, leave referenced_literals empty unless the original literal is still directly required.
    20. Do not put vague references like "complex sentence", "the image", or "the audio" in referenced_literals; copy the actual user-provided literal.
    21. Return JSON only.

    Output JSON schema:
    {{
      "tasks": [
        {{
          "task_id": "t1",
          "description": "...",
          "referenced_literals": []
        }}
      ]
    }}

    Example 1

    User:    Translate this PDF into English.

    Output:
    {{
      "tasks": [
        {{
          "task_id": "t1",
          "description": "Translate this PDF into English.",
          "referenced_literals": []
        }}
      ]
    }}

    Example 2

    User:  Download an image, extract the text from it, and translate the text into French.

    Output:
    {{
      "tasks": [
        {{
          "task_id": "t1",
          "description": "Download an image.",
          "referenced_literals": []
        }},
        {{
          "task_id": "t2",
          "description": "Extract the text from the image.",
          "referenced_literals": []
        }},
        {{
          "task_id": "t3",
          "description": "Translate the text into French.",
          "referenced_literals": []
        }}
      ]
    }}

    Example 3

    User:
    Find climate datasets and hydrology datasets for the Yellow River Basin, then generate a report.

    Output:
    {{
      "tasks": [
        {{
          "task_id": "t1",
          "description": "Find climate datasets for the Yellow River Basin.",
          "referenced_literals": []
        }},
        {{
          "task_id": "t2",
          "description": "Find hydrology datasets for the Yellow River Basin.",
          "referenced_literals": []
        }},
        {{
          "task_id": "t3",
          "description": "Generate a report.",
          "referenced_literals": []
        }}
      ]
    }}

    Example 4

    User:
    I have a complex sentence: 'The grandiose edifice exhibiting an eclectic mixture of architectural designs evokes a sense of awe and admiration among the beholders.' I would like it to be paraphrased, simplified, and then find an image related to the simplified sentence.

    Output:
    {{
      "tasks": [
        {{
          "task_id": "t1",
          "description": "Paraphrase the complex sentence.",
          "referenced_literals": [
            "The grandiose edifice exhibiting an eclectic mixture of architectural designs evokes a sense of awe and admiration among the beholders."
          ]
        }},
        {{
          "task_id": "t2",
          "description": "Simplify the paraphrased sentence.",
          "referenced_literals": []
        }},
        {{
          "task_id": "t3",
          "description": "Find an image related to the simplified sentence.",
          "referenced_literals": []
        }}
      ]
    }}

    Now decompose the following user request.

    User:
    {request.text}

    Return JSON only.
    """.strip()

    def _call_llm(self, request: UserRequest) -> str:
        return self.llm_client.chat(
            messages=[
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": self._build_prompt(request)},
            ]
        )

    def _normalize_result(self, request: UserRequest, raw_payload: Dict[str, Any]) -> TaskUnderstandingResult:
        raw_steps = raw_payload.get("steps")
        if raw_steps is None:
            raw_steps = raw_payload.get("tasks")
        steps = self._normalize_steps(raw_steps)
        if not steps:
            steps = [self._fallback_step(request)]
        steps = self._fill_missing_referenced_literals(request.text, steps)

        return TaskUnderstandingResult(
            request=request,
            steps=steps,
            raw_llm_output=raw_payload,
        )

    def _normalize_steps(self, raw_steps: Any) -> List[TaskStep]:
        steps: List[TaskStep] = []
        for idx, item in enumerate(coerce_list(raw_steps)):
            if not isinstance(item, dict):
                description = str(item).strip()
                if description:
                    steps.append(
                        TaskStep(
                            task_id=f"s{idx + 1}",
                            description=description,
                            priority=idx + 1,
                            referenced_literals=[],
                        )
                    )
                continue

            description = str(item.get("description", "")).strip()
            if not description:
                continue

            steps.append(
                TaskStep(
                    task_id=str(item.get("task_id") or item.get("step_id") or item.get("id") or f"s{idx + 1}"),
                    description=description,
                    priority=float(item.get("priority", idx + 1) or idx + 1),
                    referenced_literals=_clean_referenced_literals(item.get("referenced_literals", [])),
                    metadata=dict(item.get("metadata", {}) or {}),
                )
            )
        return steps

    def _fallback_result(self, request: UserRequest, error: Exception) -> TaskUnderstandingResult:
        return TaskUnderstandingResult(
            request=request,
            steps=[self._fallback_step(request)],
            raw_llm_output={"fallback_reason": str(error)},
        )

    @staticmethod
    def _fallback_step(request: UserRequest) -> TaskStep:
        return TaskStep(
            task_id="s1",
            description=request.text,
            priority=1,
            referenced_literals=_extract_request_literals(request.text),
        )

    @staticmethod
    def _fill_missing_referenced_literals(request_text: str, steps: List[TaskStep]) -> List[TaskStep]:
        if not steps:
            return steps
        if any(step.referenced_literals for step in steps):
            return steps

        request_literals = _extract_request_literals(request_text)
        if len(request_literals) != 1:
            return steps

        steps[0].referenced_literals = list(request_literals)
        return steps


def _clean_referenced_literals(value: Any) -> List[str]:
    result: List[str] = []
    seen = set()
    for item in coerce_list(value):
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _extract_request_literals(text: str) -> List[str]:
    raw_text = str(text or "")
    literals: List[str] = []

    for pattern in (r"'([^'\n]{2,})'", r'"([^"\n]{2,})"'):
        literals.extend(match.group(1).strip() for match in re.finditer(pattern, raw_text))

    literals.extend(re.findall(r"https?://[^\s,;]+", raw_text))
    literals.extend(
        re.findall(
            r"\b[\w.-]+\.(?:mp4|mov|avi|mkv|wav|mp3|flac|jpg|jpeg|png|gif|txt|pdf|csv|json|doc|docx)\b",
            raw_text,
            flags=re.IGNORECASE,
        )
    )

    return _clean_referenced_literals(literals)

def _main() -> int:
    cli = argparse.ArgumentParser(
        description="Run TaskUnderstanding and retrieve candidate tools for each task."
    )
    cli.add_argument(
        "--request",
        default="Download an image, extract the text from it, and translate the text into French.",
        help="Natural-language user request.",
    )
    cli.add_argument(
        "--llm-config",
        default="configs/qwen.json",
        help="LLM config path for TaskUnderstanding.",
    )
    cli.add_argument(
        "--llm-profile",
        default=None,
        help="Optional LLM profile name when the config defines profiles.",
    )
    cli.add_argument(
        "--tool-desc",
        default=str(Path(__file__).resolve().parents[2] / "taskbench" / "data_multimedia" / "tool_desc.json"),
        help="Path to tool_desc.json.",
    )
    cli.add_argument(
        "--embedding-model",
        default=None,
        help="Optional sentence-transformers model name for ToolKnowledge.",
    )
    cli.add_argument("--top-k", type=int, default=5, help="Candidate tools per task.")
    args = cli.parse_args()

    from agent.memory_guided_workflow.tool_knowledge import ToolKnowledge

    understanding = TaskUnderstanding(
        llm_config_path=args.llm_config,
        llm_profile=args.llm_profile,
    ).parse(UserRequest(text=args.request))

    fallback_reason = understanding.raw_llm_output.get("fallback_reason")
    if fallback_reason:
        print("TaskUnderstanding failed before ToolKnowledge retrieval.")
        print(f"fallback_reason={fallback_reason}")
        return 2

    tool_knowledge = ToolKnowledge(
        tool_desc_path=args.tool_desc,
        embedding_model=args.embedding_model,
    )

    print("TaskUnderstanding:")
    print(json.dumps(understanding.to_dict(), indent=2, ensure_ascii=False))

    print("\nToolKnowledge:")
    for step in understanding.steps:
        print(f"\n[{step.step_id}] {step.description}")
        retrieval = tool_knowledge.retrieve_tools(step.description, top_k=args.top_k)
        if not retrieval.candidates:
            print("  no candidates")
            continue
        for candidate in retrieval.candidates:
            print(
                f"  {candidate.name}\t"
                f"score={candidate.retrieval_score:.4f}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
