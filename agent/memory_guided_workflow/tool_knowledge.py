from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import numpy as np

try:
    from .models import ToolCandidate, ToolRetrievalResult, ToolSpec
except ImportError:
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent.memory_guided_workflow.models import ToolCandidate, ToolRetrievalResult, ToolSpec


DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class ToolKnowledge:
    """Tool repository and embedding-based Top-K candidate retrieval.

    ToolKnowledge is intentionally narrow: it maps a task description to
    candidate tools. It does not read transition graphs, manage planning memory,
    generate workflow nodes or edges, verify workflows, or perform reranking.
    """

    def __init__(
        self,
        tool_desc_path: str,
        embedding_model: Optional[str] = None,
        build_index_on_init: bool = True,
    ):
        self.tool_desc_path = tool_desc_path
        self.embedding_model_name = embedding_model or DEFAULT_EMBEDDING_MODEL
        self.tools: Dict[str, ToolSpec] = {}
        self.tool_embeddings: Dict[str, np.ndarray] = {}
        self._embedding_model: Any = None

        self.load_tools()
        if build_index_on_init:
            self.build_index()

    def load_tools(self) -> Dict[str, ToolSpec]:
        """Load ToolSpec objects from tool_desc.json."""
        payload = _read_json(self.tool_desc_path)
        raw_nodes = payload.get("nodes", []) if isinstance(payload, Mapping) else []

        tools: Dict[str, ToolSpec] = {}
        for raw_node in raw_nodes:
            if not isinstance(raw_node, Mapping):
                continue

            tool_id = str(raw_node.get("id", "")).strip()
            if not tool_id:
                continue

            description = str(raw_node.get("desc", "") or "")
            intent = str(raw_node.get("intent") or "").strip() or "unknown"
            tools[tool_id] = ToolSpec(
                tool_id=tool_id,
                name=tool_id,
                description=description,
                input_types=_coerce_str_list(raw_node.get("input-type")),
                output_types=_coerce_str_list(raw_node.get("output-type")),
                metadata={
                    "raw": dict(raw_node),
                    "intent": intent,
                },
            )

        self.tools = tools
        return dict(self.tools)

    def build_index(self) -> None:
        """Build the in-memory embedding index for all tools."""
        self.tool_embeddings = {}
        if not self.tools:
            return

        model = self._get_embedding_model()
        tool_ids = list(self.tools.keys())
        texts = [self._build_tool_text(self.tools[tool_id]) for tool_id in tool_ids]
        embeddings = model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        matrix = np.asarray(embeddings, dtype=float)

        for tool_id, embedding in zip(tool_ids, matrix):
            self.tool_embeddings[tool_id] = np.asarray(embedding, dtype=float)

    def warmup_embedding_model(self) -> None:
        """Load the embedding model into memory without changing the index."""
        self._get_embedding_model()

    def retrieve_tools(
        self,
        query: str,
        top_k: int = 5,
    ) -> ToolRetrievalResult:
        """Retrieve Top-K candidate tools for a task description."""
        query_text = str(query or "").strip()
        if top_k <= 0 or not query_text or not self.tools:
            return ToolRetrievalResult(task_id="", query=query_text, candidates=[])

        if not self.tool_embeddings:
            self.build_index()

        query_embedding = np.asarray(
            self._get_embedding_model().encode(query_text, convert_to_numpy=True, show_progress_bar=False),
            dtype=float,
        )

        scored: List[ToolCandidate] = []
        for tool_id, tool in self.tools.items():
            embedding = self.tool_embeddings.get(tool_id)
            if embedding is None:
                continue
            score = _cosine_similarity(query_embedding, embedding)
            scored.append(
                ToolCandidate(
                    tool_id=tool.tool_id,
                    name=tool.name,
                    retrieval_score=score,
                    intent=_tool_intent(tool),
                    metadata={
                        "tool_text": self._build_tool_text(tool),
                        "intent": _tool_intent(tool),
                    },
                )
            )

        scored.sort(key=lambda candidate: candidate.retrieval_score, reverse=True)
        return ToolRetrievalResult(
            task_id="",
            query=query_text,
            candidates=scored[:top_k],
        )

    def get_tool(self, tool_id: str) -> Optional[ToolSpec]:
        """Return a tool by id."""
        return self.tools.get(str(tool_id).strip())

    def get_all_tools(self) -> List[ToolSpec]:
        """Return all loaded tools."""
        return list(self.tools.values())

    def get_tool_count(self) -> int:
        """Return the number of loaded tools."""
        return len(self.tools)

    def save_index(self, path: str) -> None:
        """Save tools and embeddings to a JSON index file."""
        payload = {
            "tool_desc_path": self.tool_desc_path,
            "embedding_model": self.embedding_model_name,
            "tools": {
                tool_id: _tool_to_dict(tool)
                for tool_id, tool in self.tools.items()
            },
            "tool_embeddings": {
                tool_id: embedding.tolist()
                for tool_id, embedding in self.tool_embeddings.items()
            },
        }
        with open(path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)

    def load_index(self, path: str) -> None:
        """Load tools and embeddings from a saved JSON index file."""
        payload = _read_json(path)
        self.embedding_model_name = str(payload.get("embedding_model") or self.embedding_model_name)
        tool_desc_tools = dict(self.tools)

        raw_tools = payload.get("tools", {})
        if isinstance(raw_tools, Mapping):
            self.tools = {
                str(tool_id): _tool_from_dict(tool_payload)
                for tool_id, tool_payload in raw_tools.items()
                if isinstance(tool_payload, Mapping)
            }
            for tool_id, tool in self.tools.items():
                current_intent = str(tool.metadata.get("intent", "") or "").strip()
                if current_intent and current_intent != "unknown":
                    continue
                desc_tool = tool_desc_tools.get(tool_id)
                if desc_tool is None:
                    continue
                desc_intent = str(desc_tool.metadata.get("intent", "") or "").strip()
                if desc_intent:
                    tool.metadata["intent"] = desc_intent

        raw_embeddings = payload.get("tool_embeddings", {})
        if isinstance(raw_embeddings, Mapping):
            self.tool_embeddings = {
                str(tool_id): np.asarray(values, dtype=float)
                for tool_id, values in raw_embeddings.items()
            }

    @staticmethod
    def _build_tool_text(tool: ToolSpec) -> str:
        parts = [
            tool.name,
            tool.description,
            f"intent={_tool_intent(tool)}",
            f"input={' '.join(tool.input_types)}" if tool.input_types else "",
            f"output={' '.join(tool.output_types)}" if tool.output_types else "",
        ]
        return "\n".join(part for part in parts if part)

    def _get_embedding_model(self) -> Any:
        if self._embedding_model is not None:
            return self._embedding_model

        try:
            from sentence_transformers import SentenceTransformer
        except Exception as exc:
            raise RuntimeError(
                "sentence-transformers is required for ToolKnowledge embedding retrieval. "
                "Install it with: pip install sentence-transformers"
            ) from exc

        self._embedding_model = SentenceTransformer(self.embedding_model_name)
        return self._embedding_model


def _read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def _coerce_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _cosine_similarity(left: np.ndarray, right: np.ndarray) -> float:
    left_norm = float(np.linalg.norm(left))
    right_norm = float(np.linalg.norm(right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return float(np.dot(left, right) / (left_norm * right_norm))


def _tool_to_dict(tool: ToolSpec) -> Dict[str, Any]:
    return {
        "tool_id": tool.tool_id,
        "name": tool.name,
        "description": tool.description,
        "input_types": list(tool.input_types),
        "output_types": list(tool.output_types),
        "metadata": dict(tool.metadata),
    }


def _tool_from_dict(payload: Mapping[str, Any]) -> ToolSpec:
    tool_id = str(payload.get("tool_id") or payload.get("name") or "").strip()
    description = str(payload.get("description", "") or "")
    metadata = dict(payload.get("metadata", {}) or {})
    metadata.setdefault("intent", "unknown")
    return ToolSpec(
        tool_id=tool_id,
        name=str(payload.get("name") or tool_id),
        description=description,
        input_types=_coerce_str_list(payload.get("input_types")),
        output_types=_coerce_str_list(payload.get("output_types")),
        metadata=metadata,
    )


def _tool_intent(tool: ToolSpec) -> str:
    intent = str(tool.metadata.get("intent", "") or "").strip()
    if intent:
        return intent
    return "unknown"


def _default_tool_desc_path() -> str:
    repo_root = Path(__file__).resolve().parents[2]
    return str(repo_root / "taskbench" / "data_multimedia" / "tool_desc.json")


def _main() -> None:
    parser = argparse.ArgumentParser(description="Run ToolKnowledge embedding retrieval.")
    parser.add_argument(
        "--tool_desc",
        default=_default_tool_desc_path(),
        help="Path to tool_desc.json.",
    )
    parser.add_argument(
        "--query",
        default="Extract the text from the image.",
        help="Task description query.",
    )
    parser.add_argument("--top-k", type=int, default=5, help="Number of candidate tools.")
    parser.add_argument(
        "--embedding-model",
        default=None,
        help=f"sentence-transformers model name. Default: {DEFAULT_EMBEDDING_MODEL}",
    )
    parser.add_argument("--save-index", default=None, help="Optional path to save embedding index JSON.")
    parser.add_argument("--load-index", default=None, help="Optional path to load embedding index JSON.")
    args = parser.parse_args()

    knowledge = ToolKnowledge(
        tool_desc_path=args.tool_desc,
        embedding_model=args.embedding_model,
        build_index_on_init=not bool(args.load_index),
    )
    if args.load_index:
        knowledge.load_index(args.load_index)

    result = knowledge.retrieve_tools(args.query, top_k=args.top_k)

    print(f"tool_count={knowledge.get_tool_count()}")
    print(f"query: {result.query}")
    for candidate in result.candidates:
        print(f"{candidate.name}\nscore={candidate.retrieval_score:.4f}")

    if args.save_index:
        knowledge.save_index(args.save_index)
        print(f"saved_index={args.save_index}")


if __name__ == "__main__":
    _main()
