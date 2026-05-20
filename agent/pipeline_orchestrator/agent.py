import asyncio
import json
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from zoneinfo import available_timezones

from pydantic import Field, create_model

from agent.tool_graph_planner import ToolGraphPlanner
from .actions import ACTION_CANONICAL_ORDER, _infer_skill_action_tags
from .models import LLMRuntimeConfig, SkillMetadata
from .paths import AGENT_ROOT, DEFAULT_SKILLS_ROOT, REPO_ROOT, TOOL_GRAPH_EDGES_PATH
from .planning_mixin import PlanningMixin
from .retrieval import WorkflowMemoryRetriever, format_workflow_memory_prompt_block
from .serialization import _safe_json_dumps
from .skill_registry import SkillRegistry
from .workflow_mixin import WorkflowMixin
from .workflow_memory import WorkflowMemoryIndex


class PipelineOrchestratorAgent(PlanningMixin, WorkflowMixin):
    @staticmethod
    def _default_llm_profiles() -> Dict[str, Dict[str, Any]]:
        return {
            "qwen-max": {
                "provider": "tongyi",
                "model_name": "qwen-max",
                "api_key_envs": ["DASHSCOPE_API_KEY", "TONGYI_API_KEY"],
            },
            "qwen-plus": {
                "provider": "tongyi",
                "model_name": "qwen-plus",
                "api_key_envs": ["DASHSCOPE_API_KEY", "TONGYI_API_KEY"],
            },
            "gpt4": {
                "provider": "openai",
                "model_name": "gpt-4o",
                "api_key_envs": ["OPENAI_API_KEY"],
                "base_url_env": "OPENAI_BASE_URL",
            },
            "gpt-4o": {
                "provider": "openai",
                "model_name": "gpt-4o",
                "api_key_envs": ["OPENAI_API_KEY"],
                "base_url_env": "OPENAI_BASE_URL",
            },
            "gpt-4o-mini": {
                "provider": "openai",
                "model_name": "gpt-4o-mini",
                "api_key_envs": ["OPENAI_API_KEY"],
                "base_url_env": "OPENAI_BASE_URL",
            },
            "gpt-4.1": {
                "provider": "openai",
                "model_name": "gpt-4.1",
                "api_key_envs": ["OPENAI_API_KEY"],
                "base_url_env": "OPENAI_BASE_URL",
            },
            "gemini-flash": {
                "provider": "gemini",
                "model_name": "gemini-2.5-flash",
                "api_key_envs": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
            },
            "gemini-2.5-flash": {
                "provider": "gemini",
                "model_name": "gemini-2.5-flash",
                "api_key_envs": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
            },
            "gemini-pro": {
                "provider": "gemini",
                "model_name": "gemini-2.5-pro",
                "api_key_envs": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
            },
            "gemini-2.5-pro": {
                "provider": "gemini",
                "model_name": "gemini-2.5-pro",
                "api_key_envs": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
            },
        }

    @staticmethod
    def _resolve_llm_config_path(raw_path: Any) -> Path:
        raw_text = str(raw_path).strip()
        if not raw_text:
            raise ValueError("llm_config_path must be non-empty when provided")

        path = Path(raw_text).expanduser()
        if path.is_absolute():
            resolved = path.resolve()
            if not resolved.exists():
                raise FileNotFoundError(f"llm config file not found: {resolved}")
            return resolved

        candidates = [
            (Path.cwd() / path).resolve(),
            (REPO_ROOT / path).resolve(),
            (AGENT_ROOT / path).resolve(),
        ]

        seen: Set[str] = set()
        unique_candidates: List[Path] = []
        for candidate in candidates:
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            unique_candidates.append(candidate)

        for candidate in unique_candidates:
            if candidate.exists():
                return candidate

        attempted = "\n".join(f"- {candidate}" for candidate in unique_candidates)
        raise FileNotFoundError(
            "llm config file not found.\n"
            f"Given: {raw_text}\n"
            f"Tried:\n{attempted}"
        )

    @staticmethod
    def _coerce_str_list(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value).strip()
        if not text:
            return []
        return [text]

    @classmethod
    def _load_llm_config_payload(
        cls,
        llm_config: Optional[Dict[str, Any]] = None,
        llm_config_path: Optional[Any] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        raw_path = llm_config_path or os.getenv("PIPELINE_ORCHESTRATOR_LLM_CONFIG")
        if raw_path:
            path = cls._resolve_llm_config_path(raw_path)
            payload = json.loads(path.read_text(encoding="utf-8"))

        if llm_config is not None:
            if not isinstance(llm_config, dict):
                raise ValueError("llm_config must be a dict when provided")
            payload = dict(llm_config)

        return payload

    @classmethod
    def _resolve_llm_runtime_config(
        cls,
        model_name: str = "qwen-max",
        provider: str = "tongyi",
        llm_profile: Optional[str] = None,
        llm_config: Optional[Dict[str, Any]] = None,
        llm_config_path: Optional[Any] = None,
    ) -> LLMRuntimeConfig:
        payload = cls._load_llm_config_payload(llm_config=llm_config, llm_config_path=llm_config_path)
        profiles = cls._default_llm_profiles()

        selected_profile = llm_profile or os.getenv("PIPELINE_ORCHESTRATOR_LLM_PROFILE")
        direct_payload: Dict[str, Any] = {}

        if payload:
            raw_profiles = payload.get("profiles")
            if isinstance(raw_profiles, dict):
                for key, value in raw_profiles.items():
                    if isinstance(value, dict):
                        profiles[str(key)] = dict(value)
                if not selected_profile:
                    selected_profile = str(
                        payload.get("default_profile")
                        or payload.get("default")
                        or payload.get("profile")
                        or ""
                    ).strip() or None
            elif isinstance(payload, dict):
                direct_payload = dict(payload)

        if selected_profile:
            profile_payload = profiles.get(selected_profile)
            if profile_payload is None:
                raise ValueError(f"unknown llm profile: {selected_profile}")
            direct_payload = dict(profile_payload)

        if not direct_payload:
            direct_payload = {
                "provider": provider,
                "model_name": model_name,
            }

        resolved_provider = str(direct_payload.get("provider", provider or "tongyi")).strip().lower()
        resolved_model_name = str(
            direct_payload.get("model_name")
            or direct_payload.get("model")
            or model_name
        ).strip()
        if not resolved_model_name:
            raise ValueError("llm model_name must be non-empty")

        if resolved_provider == "openai":
            default_envs = ["OPENAI_API_KEY"]
            default_base_url_env = "OPENAI_BASE_URL"
        elif resolved_provider == "tongyi":
            default_envs = ["DASHSCOPE_API_KEY", "TONGYI_API_KEY"]
            default_base_url_env = None
        elif resolved_provider == "gemini":
            default_envs = ["GEMINI_API_KEY", "GOOGLE_API_KEY"]
            default_base_url_env = None
        else:
            raise ValueError("provider must be 'tongyi', 'openai' or 'gemini'")

        api_key_value = direct_payload.get("api_key")
        if api_key_value is None and resolved_provider == "gemini":
            api_key_value = direct_payload.get("google_api_key")

        api_key_env_value = direct_payload.get("api_key_envs") or direct_payload.get("api_key_env")
        if api_key_env_value is None and resolved_provider == "gemini":
            api_key_env_value = (
                direct_payload.get("google_api_key_envs")
                or direct_payload.get("google_api_key_env")
            )

        base_url_env_value = direct_payload.get("base_url_envs") or direct_payload.get("base_url_env")
        base_url_envs = cls._coerce_str_list(base_url_env_value)
        if not base_url_envs and default_base_url_env:
            base_url_envs = cls._coerce_str_list(default_base_url_env)

        known_keys = {
            "provider",
            "model_name",
            "model",
            "temperature",
            "api_key",
            "api_key_envs",
            "api_key_env",
            "google_api_key",
            "google_api_key_envs",
            "google_api_key_env",
            "base_url",
            "base_url_env",
            "base_url_envs",
            "profiles",
            "default_profile",
            "default",
            "profile",
        }
        extra_kwargs = {k: v for k, v in direct_payload.items() if k not in known_keys}

        return LLMRuntimeConfig(
            provider=resolved_provider,
            model_name=resolved_model_name,
            temperature=float(direct_payload.get("temperature", 0.0)),
            api_key=str(api_key_value).strip() if api_key_value else None,
            api_key_envs=cls._coerce_str_list(api_key_env_value) or default_envs,
            base_url=str(direct_payload.get("base_url")).strip() if direct_payload.get("base_url") else None,
            base_url_env=base_url_envs[0] if base_url_envs else None,
            base_url_envs=base_url_envs,
            extra_kwargs=extra_kwargs,
        )

    @staticmethod
    def _looks_like_api_key(value: str) -> bool:
        text = str(value).strip()
        return text.startswith(("sk-", "sk-proj-", "AIza"))

    @staticmethod
    def _resolve_api_key(config: LLMRuntimeConfig) -> str:
        if config.api_key:
            return config.api_key
        for env_name in config.api_key_envs:
            if PipelineOrchestratorAgent._looks_like_api_key(env_name):
                return env_name
            value = os.getenv(env_name)
            if value:
                return value
        raise ValueError(f"Set one of {config.api_key_envs} first.")

    @staticmethod
    def _resolve_env_value(env_names: List[str]) -> Optional[str]:
        for env_name in env_names:
            value = os.getenv(env_name)
            if value:
                return value
        return None

    @classmethod
    def _build_llm_client(cls, config: LLMRuntimeConfig) -> Any:
        api_key = cls._resolve_api_key(config)

        if config.provider == "openai":
            from langchain_openai import ChatOpenAI

            client_kwargs: Dict[str, Any] = {
                "model": config.model_name,
                "api_key": api_key,
                "temperature": config.temperature,
            }
            base_url = config.base_url
            if not base_url:
                base_url = cls._resolve_env_value(config.base_url_envs)
            if not base_url and config.base_url_env:
                base_url = os.getenv(config.base_url_env)
            if base_url:
                client_kwargs["base_url"] = base_url
            client_kwargs.update(config.extra_kwargs)
            return ChatOpenAI(**client_kwargs)

        if config.provider == "tongyi":
            from langchain_community.chat_models.tongyi import ChatTongyi

            client_kwargs = {
                "model_name": config.model_name,
                "api_key": api_key,
                "temperature": config.temperature,
            }
            client_kwargs.update(config.extra_kwargs)
            return ChatTongyi(**client_kwargs)

        if config.provider == "gemini":
            base_url = config.base_url
            if not base_url:
                base_url = cls._resolve_env_value(config.base_url_envs)
            if not base_url and config.base_url_env:
                base_url = os.getenv(config.base_url_env)
            if base_url:
                from langchain_openai import ChatOpenAI

                # Third-party Gemini gateways commonly expose an OpenAI-compatible
                # /v1 surface instead of Google's native Generative AI transport.
                client_kwargs = {
                    "model": config.model_name,
                    "api_key": api_key,
                    "temperature": config.temperature,
                    "base_url": base_url,
                }
                client_kwargs.update(config.extra_kwargs)
                return ChatOpenAI(**client_kwargs)

            from langchain_google_genai import ChatGoogleGenerativeAI

            client_kwargs = {
                "model": config.model_name,
                "google_api_key": api_key,
                "temperature": config.temperature,
            }
            client_kwargs.update(config.extra_kwargs)
            return ChatGoogleGenerativeAI(**client_kwargs)

        raise ValueError("provider must be 'tongyi', 'openai' or 'gemini'")

    def __init__(
        self,
        model_name: str = "qwen-max",
        skills_root: Optional[Path] = None,
        provider: str = "tongyi",
        llm_profile: Optional[str] = None,
        llm_config: Optional[Dict[str, Any]] = None,
        llm_config_path: Optional[Any] = None,
        workflow_memory_path: Optional[Any] = None,
        enable_workflow_memory: bool = True,
        enable_candidate_verifier: bool = True,
        enable_candidate_repair: bool = True,
        candidate_selection_mode: str = "rerank",
        include_original_candidate: bool = False,
        fixed_candidate_temperature: Optional[float] = None,
        edge_grounding_mode: str = "none",
        candidate_prompt_mode: str = "legacy",
        force_generate_all_candidate_families: bool = False,
        disable_early_stop: bool = False,
        enable_strict_planning_prompt: bool = False,
        enable_action_checklist: bool = False,
        enable_parameter_normalization: bool = False,
    ):
        if candidate_selection_mode not in {
            "rerank",
            "first",
            "original_first_fallback",
            "collect_all_then_original",
            "original_dependency_filter_first_valid",
            "structure_aware",
        }:
            raise ValueError(
                "candidate_selection_mode must be 'rerank', 'first', "
                "'original_first_fallback', 'collect_all_then_original', "
                "'original_dependency_filter_first_valid' or 'structure_aware'"
            )
        if edge_grounding_mode not in {
            "none",
            "nearest_valid_upstream",
            "nearest_valid",
            "nearest",
            "semantic_edge_scoring",
            "semantic",
            "semantic_edge_scorer",
            "h2",
            "semantic_edge_scoring_h2a",
            "semantic_nearest_priority",
            "h2a",
            "semantic_edge_scoring_h2b",
            "semantic_semantic_priority",
            "h2b",
        }:
            raise ValueError(
                "edge_grounding_mode must be one of: "
                "'none', 'nearest_valid_upstream', 'semantic_edge_scoring', "
                "'semantic_edge_scoring_h2a', 'semantic_edge_scoring_h2b'"
            )
        if candidate_prompt_mode not in {"legacy", "orthogonal", "orthogonal_v2"}:
            raise ValueError(
                "candidate_prompt_mode must be 'legacy', 'orthogonal' or 'orthogonal_v2'"
            )
        self.llm_config = self._resolve_llm_runtime_config(
            model_name=model_name,
            provider=provider,
            llm_profile=llm_profile,
            llm_config=llm_config,
            llm_config_path=llm_config_path,
        )
        self.llm = self._build_llm_client(self.llm_config)
        self._candidate_llm_cache: Dict[float, Any] = {round(float(self.llm_config.temperature), 2): self.llm}
        self._enable_workflow_memory = bool(enable_workflow_memory)
        self._enable_candidate_verifier = bool(enable_candidate_verifier)
        self._enable_candidate_repair = bool(enable_candidate_repair)
        self._candidate_selection_mode = candidate_selection_mode
        self._include_original_candidate = bool(include_original_candidate)
        self._fixed_candidate_temperature = (
            None if fixed_candidate_temperature is None else float(fixed_candidate_temperature)
        )
        self._edge_grounding_mode = str(edge_grounding_mode)
        self._candidate_prompt_mode = str(candidate_prompt_mode)
        self._force_generate_all_candidate_families = bool(force_generate_all_candidate_families)
        self._disable_early_stop = bool(disable_early_stop)
        self._enable_strict_planning_prompt = bool(enable_strict_planning_prompt)
        self._enable_action_checklist = bool(enable_action_checklist)
        self._enable_parameter_normalization = bool(enable_parameter_normalization)

        default_root = DEFAULT_SKILLS_ROOT
        self.registry = SkillRegistry(skills_root or default_root)
        if not self.registry.skills:
            raise RuntimeError(f"no skills discovered under: {self.registry.skills_root}")
        self._tool_graph_planner: Optional[ToolGraphPlanner] = None
        self._tool_graph_alias_to_skill: Dict[str, str] = {}
        self._skill_to_tool_graph_name: Dict[str, str] = {}
        self._workflow_memory: Optional[WorkflowMemoryIndex] = None
        self._workflow_retriever: Optional[WorkflowMemoryRetriever] = None
        self._workflow_retrieval_cache: Dict[Tuple[str, Tuple[str, ...]], Dict[str, Any]] = {}
        self._edge_grounding_retrieval_cache: Dict[Tuple[str, Tuple[str, ...]], Dict[str, Any]] = {}
        self._load_tool_graph()
        self._load_workflow_memory(workflow_memory_path)
        self._deep_agent_import_error: Optional[str] = None
        self._preference_logger = None
        try:
            from data.preference_logger import PreferenceLogger

            self._preference_logger = PreferenceLogger()
        except Exception:
            # Keep logger optional: orchestration must not depend on training infrastructure.
            self._preference_logger = None

    @staticmethod
    def _normalize_graph_name(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", name.lower())

    def _load_tool_graph(self) -> None:
        edges_path = TOOL_GRAPH_EDGES_PATH
        if not edges_path.exists():
            return
        try:
            planner = ToolGraphPlanner.from_csv(edges_path)
        except Exception:
            return

        self._tool_graph_planner = planner
        norm_to_tools: Dict[str, List[str]] = {}
        for tool in sorted(planner.tools):
            norm = self._normalize_graph_name(tool)
            norm_to_tools.setdefault(norm, []).append(tool)

        for skill_name in self.registry.skills.keys():
            norm_skill = self._normalize_graph_name(skill_name)
            matched_tools = norm_to_tools.get(norm_skill, [])
            if len(matched_tools) == 1:
                tool = matched_tools[0]
                self._skill_to_tool_graph_name[skill_name] = tool
                self._tool_graph_alias_to_skill[tool] = skill_name

    def _graph_tool_for_skill(self, skill_name: str) -> Optional[str]:
        mapping = getattr(self, "_skill_to_tool_graph_name", {})
        return mapping.get(skill_name)

    def _load_workflow_memory(self, workflow_memory_path: Optional[Any]) -> None:
        raw_path = workflow_memory_path or os.getenv("PIPELINE_ORCHESTRATOR_WORKFLOW_MEMORY")
        if not raw_path:
            return
        path = self._resolve_llm_config_path(raw_path)
        memory = WorkflowMemoryIndex.from_json(path)
        self._workflow_memory = memory
        self._workflow_retriever = WorkflowMemoryRetriever(memory)

    def _get_workflow_memory_context(self, user_requirement: str) -> Dict[str, Any]:
        if not getattr(self, "_enable_workflow_memory", True):
            return {}
        retriever = getattr(self, "_workflow_retriever", None)
        if retriever is None:
            return {}
        requirement = str(user_requirement or "").strip()
        if not requirement:
            return {}

        query_actions = tuple(self._match_requirement_actions(requirement))
        cache = getattr(self, "_workflow_retrieval_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._workflow_retrieval_cache = cache
        cache_key = (requirement, query_actions)
        if cache_key not in cache:
            cache[cache_key] = retriever.retrieve(requirement, detected_actions=query_actions)
        return cache[cache_key]

    def _format_workflow_memory_prompt_block(self, user_requirement: str) -> str:
        return format_workflow_memory_prompt_block(self._get_workflow_memory_context(user_requirement))

    def recommend_memory_start_skills(
        self,
        user_requirement: str,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        if not getattr(self, "_enable_workflow_memory", True):
            return []
        retriever = getattr(self, "_workflow_retriever", None)
        requirement = str(user_requirement or "").strip()
        if retriever is None or not requirement:
            return []
        return retriever.recommend_start_tools(
            requirement,
            detected_actions=self._match_requirement_actions(requirement),
            top_k=top_k,
        )

    def recommend_memory_next_skills(
        self,
        user_requirement: str,
        current_skill: Optional[str],
        top_k: int = 5,
        visited_skills: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        if not getattr(self, "_enable_workflow_memory", True):
            return []
        retriever = getattr(self, "_workflow_retriever", None)
        requirement = str(user_requirement or "").strip()
        current = str(current_skill or "").strip()
        if retriever is None or not requirement:
            return []
        return retriever.recommend_next_tools(
            requirement,
            current,
            detected_actions=self._match_requirement_actions(requirement),
            visited_tools={str(skill).strip() for skill in (visited_skills or set()) if str(skill).strip()},
            top_k=top_k,
        )

    def _graph_score_compiled(self, compiled_nodes: List[Dict[str, Any]]) -> Dict[str, float]:
        planner = getattr(self, "_tool_graph_planner", None)
        if planner is None:
            return {"graph_transition_bonus": 0.0, "graph_transition_penalty": 0.0}

        graph_bonus = 0.0
        graph_penalty = 0.0
        mapped_tools: List[Optional[str]] = []
        for node in compiled_nodes:
            skill_name = str(node.get("task", ""))
            mapped_tools.append(self._graph_tool_for_skill(skill_name))

        for idx in range(1, len(mapped_tools)):
            prev_tool = mapped_tools[idx - 1]
            cur_tool = mapped_tools[idx]
            if not prev_tool or not cur_tool:
                continue
            explain = planner.explain_transition(prev_tool, cur_tool)
            if explain.get("valid"):
                count = int(explain.get("count", 0))
                graph_bonus += min(float(count), 5.0)
            else:
                graph_penalty -= 4.0
        return {
            "graph_transition_bonus": graph_bonus,
            "graph_transition_penalty": graph_penalty,
        }

    def _graph_score_plan(self, workflow: Dict[str, Any]) -> Dict[str, float]:
        _, compiled_nodes = self._prepare_workflow(workflow)
        return self._graph_score_compiled(compiled_nodes)

    @staticmethod
    def _match_requirement_actions(user_requirement: str) -> List[str]:
        text = " ".join(str(user_requirement or "").lower().split())
        actions: List[str] = []

        if (
            re.search(r"\b(find|search|look up|lookup|browse|get|collect|research)\b", text)
            and re.search(r"\b(information|info|content|details|materials?)\b", text)
        ) or re.search(r"\bsearch for information\b", text) or (
            re.search(r"\b(find|search|look up|lookup|browse|research)\b", text)
            and re.search(r"\b(on the internet|online|on the web|web)\b", text)
        ):
            actions.append("retrieval")

        if "easy-to-understand" in text or "easy to understand" in text or "simplif" in text:
            actions.append("simplify")

        if re.search(r"\b(summar(?:ize|y|ies|ized|izing)|main ideas?)\b", text):
            actions.append("summarize")

        if re.search(r"\b(sentiment|emotional tone|tone analysis|tone classification)\b", text):
            actions.append("sentiment")

        if re.search(r"\b(keywords?|key words?|key phrases?|important keywords?|important terms?)\b", text):
            actions.append("keywords")

        if "grammar" in text or "grammatically correct" in text or "proofread" in text:
            actions.append("grammar")

        if (
            "related topic" in text
            or "topic ideas" in text
            or "sub-topic" in text
            or "subtopic" in text
            or "sub-topics" in text
            or "brainstorm" in text
            or "brainstorming" in text
            or "vague idea" in text
            or "idea of what i want to write" in text
        ):
            actions.append("topic")

        if re.search(r"\b(create|generate|produce|draw)\b", text) and re.search(
            r"\b(image|illustration|illustrative|picture|artwork|visual)\b",
            text,
        ):
            actions.append("image")

        if (
            re.search(r"\b(background noise|noise)\b", text)
            and re.search(r"\b(remove|reduce|clean|denois|noise reduction)\b", text)
        ) or "noise reduction" in text:
            actions.append("denoise")

        if (
            "modify the voice" in text
            or "voice changer" in text
            or "voice change" in text
            or ("sound like" in text and "voice" in text)
            or re.search(r"\b(female voice|male voice)\b", text)
        ):
            actions.append("voice_change")

        if re.search(r"\b(transcrib|speech[- ]to[- ]text|audio[- ]to[- ]text|caption)\w*\b", text):
            actions.append("transcribe")
        elif (
            "content of the speech" in text
            or "speech content" in text
            or ("based on the content" in text and "speech" in text)
        ):
            actions.append("transcribe")

        if re.search(
            r"\btranslat\w*\b|\binto\s+(english|spanish|chinese|french|german|japanese|korean)\b",
            text,
        ):
            actions.append("translate")

        if re.search(r"\b(combine|merge|mix|splice|synchroniz)\w*\b", text):
            actions.append("combine")

        if re.search(r"\b(reverb|echo|audio effects?|sound effects?|enhance with some effects)\b", text):
            actions.append("audio_effect")

        if re.search(r"\b(waveform|spectrogram)\b", text):
            actions.append("waveform")

        if re.search(r"\b(slideshow video|create a slideshow video|generate a video|create a video)\b", text):
            actions.append("video")

        return actions

    @staticmethod
    def _action_prompt_text(action: str) -> str:
        mapping = {
            "retrieval": "Include a retrieval/search step for information about the topic.",
            "simplify": "Include a simplification step for easy-to-understand text if the request asks for it.",
            "summarize": "Include a summarization step when the request asks for a summary or the main ideas.",
            "sentiment": "Include a sentiment-analysis step when the request asks for sentiment or tone.",
            "keywords": "Include a keyword-extraction step when the request asks for important keywords or key phrases.",
            "grammar": "Include a grammar-checking or proofreading step.",
            "topic": "Include a topic-idea or sub-topic generation step.",
            "image": "Include an image-generation step representing the topic or processed text.",
            "denoise": "Include a noise-reduction step when the request asks to remove or reduce background noise.",
            "voice_change": "Include a voice-changing step when the request asks to modify the voice or change gender/tone.",
            "transcribe": "Include a transcription step when the request asks for speech or audio to be converted to text.",
            "translate": "Include a translation step when the request asks to convert content into another language.",
            "combine": "Include an explicit combine/merge/splice step when the request asks to combine multiple media inputs.",
            "audio_effect": "Include an audio-effects step when the request asks for reverb, echo, or audio enhancement.",
            "waveform": "Include an audio-visualization step when the request asks for a waveform or spectrogram image.",
            "video": "Include a video-generation step when the request asks for a slideshow video or generated video output.",
        }
        return mapping.get(action, action)

    def _action_tags_for_skill_name(self, skill_name: str) -> Set[str]:
        meta = self.registry.get(skill_name)
        action_tags = getattr(meta, "action_tags", None) if meta is not None else None
        if action_tags:
            return set(action_tags)
        return set(_infer_skill_action_tags(skill_name))

    def _workflow_action_tags(self, skill_names: List[str]) -> Set[str]:
        tags: Set[str] = set()
        for skill_name in skill_names:
            tags.update(self._action_tags_for_skill_name(str(skill_name)))
        return tags

    def _workflow_covers_action(self, skill_names: List[str], action: str) -> bool:
        return action in self._workflow_action_tags(skill_names)

    @staticmethod
    def _normalize_skill_name_for_inference(skill_name: str) -> str:
        return re.sub(r"[_\-]+", " ", str(skill_name).strip().lower())

    @classmethod
    def _infer_skill_name_modalities(cls, skill_name: str) -> Dict[str, Optional[str]]:
        text = cls._normalize_skill_name_for_inference(skill_name)
        modality_pattern = r"(audio|video|image|text)"

        conversion_match = re.search(
            rf"\b(?P<input>{modality_pattern})\b\s+(?:to|2)\s+\b(?P<output>{modality_pattern})\b",
            text,
        )
        if conversion_match:
            return {
                "input": conversion_match.group("input"),
                "output": conversion_match.group("output"),
            }

        found_modalities = re.findall(rf"\b{modality_pattern}\b", text)
        unique_modalities: List[str] = []
        for modality in found_modalities:
            if modality not in unique_modalities:
                unique_modalities.append(modality)

        if len(unique_modalities) == 1:
            modality = unique_modalities[0]
            return {"input": modality, "output": modality}

        return {"input": None, "output": None}

    @classmethod
    def _infer_name_based_link_compatibility(
        cls,
        source_skill: str,
        target_skill: str,
    ) -> Optional[bool]:
        source_modalities = cls._infer_skill_name_modalities(source_skill)
        target_modalities = cls._infer_skill_name_modalities(target_skill)
        source_output = source_modalities.get("output")
        target_input = target_modalities.get("input")
        if not source_output or not target_input:
            return None
        return str(source_output) == str(target_input)

    def _score_requirement_action_coverage(self, user_requirement: str, skill_names: List[str]) -> Dict[str, Any]:
        actions = self._match_requirement_actions(user_requirement)
        action_weights: Dict[str, Tuple[float, float]] = {
            "retrieval": (10.0, 24.0),
            "simplify": (6.0, 10.0),
            "summarize": (8.0, 14.0),
            "sentiment": (8.0, 14.0),
            "keywords": (8.0, 14.0),
            "grammar": (8.0, 18.0),
            "topic": (8.0, 16.0),
            "image": (8.0, 16.0),
            "denoise": (8.0, 14.0),
            "voice_change": (8.0, 14.0),
            "transcribe": (8.0, 16.0),
            "translate": (6.0, 12.0),
            "combine": (8.0, 16.0),
            "audio_effect": (6.0, 12.0),
            "waveform": (6.0, 12.0),
            "video": (8.0, 16.0),
        }

        bonus = 0.0
        penalty = 0.0
        covered: List[str] = []
        missing: List[str] = []
        for action in actions:
            presence_bonus, missing_penalty = action_weights.get(action, (4.0, 8.0))
            if self._workflow_covers_action(skill_names, action):
                bonus += presence_bonus
                covered.append(action)
            else:
                penalty -= missing_penalty
                missing.append(action)

        return {
            "bonus": bonus,
            "penalty": penalty,
            "required_actions": actions,
            "covered_actions": covered,
            "missing_actions": missing,
        }

    def _workflow_first_action_positions(self, compiled_nodes: List[Dict[str, Any]]) -> Dict[str, int]:
        positions: Dict[str, int] = {}
        for idx, node in enumerate(compiled_nodes):
            skill_name = str(node.get("task", "")).strip()
            for action in self._action_tags_for_skill_name(skill_name):
                positions.setdefault(action, idx)
        return positions



    def _run_skill_for_deep_agent(self, skill_name: str, args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        skill = self.registry.get(skill_name)
        if skill is None:
            raise ValueError(f"unknown skill: {skill_name}")

        missing = self._validate_step_args(skill, args)
        if missing:
            raise ValueError(f"missing args for {skill_name}: {missing}")

        executed_skills: List[str] = ctx.setdefault("executed_skills", [])

        if skill.depends_on_all:
            missed_all = [s for s in skill.depends_on_all if s not in executed_skills]
            if missed_all:
                raise ValueError(
                    f"dependency not satisfied for {skill_name}: missing depends_on_all={missed_all}"
                )
        if skill.depends_on_any and not any(s in executed_skills for s in skill.depends_on_any):
            # Fallback: if caller provides an explicit upstream artifact reference, allow this step.
            # This keeps dependency checks strict by default while supporting external/resumed inputs.
            artifacts = ctx.setdefault("artifacts", {})
            source_ref = args.get("source_ref")
            source_ready = isinstance(source_ref, str) and (
                source_ref.strip() == "external_input" or source_ref in artifacts
            )
            if not source_ready:
                raise ValueError(
                    f"dependency not satisfied for {skill_name}: requires one of depends_on_any={skill.depends_on_any}"
                )

        pkg = self.registry.load_skill(skill_name)
        return pkg.run(args, ctx)

    def _build_deep_agent_tools(self, ctx: Dict[str, Any]) -> List[Any]:
        from langchain_core.tools import StructuredTool

        tools: List[Any] = []
        for skill in self.registry.skills.values():
            field_defs: Dict[str, Any] = {}
            for key, desc in skill.input_schema.items():
                field_defs[key] = (Any, Field(default=..., description=desc))
            args_model = create_model(f"{skill.name.title()}Args", **field_defs)

            def _run(_skill_name: str = skill.name, **kwargs: Any) -> Dict[str, Any]:
                trace = ctx.setdefault("trace", [])
                item: Dict[str, Any] = {"skill": _skill_name, "args": kwargs}
                try:
                    output = self._run_skill_for_deep_agent(_skill_name, kwargs, ctx)
                    item["ok"] = True
                    item["output"] = output
                    ctx.setdefault("executed_skills", []).append(_skill_name)
                    trace.append(item)
                    return output
                except Exception as e:
                    item["ok"] = False
                    item["error"] = str(e)
                    trace.append(item)
                    raise

            tools.append(
                StructuredTool.from_function(
                    func=_run,
                    name=skill.name,
                    description=(
                        f"{skill.description}. "
                        f"depends_on_all={skill.depends_on_all}; depends_on_any={skill.depends_on_any}"
                    ),
                    args_schema=args_model,
                )
            )
        return tools

    async def _run_with_deep_agent(self, user_requirement: str, create_deep_agent=None) -> Dict[str, Any]:
        try:
            from deepagents import create_deep_agent as imported_create_deep_agent
        except ModuleNotFoundError as e:
            raise RuntimeError(
                "Missing optional dependency 'deepagents'. Install it with: python -m pip install deepagents"
            ) from e
        from langchain_core.messages import HumanMessage
        create_deep_agent = create_deep_agent or imported_create_deep_agent

        runtime_ctx: Dict[str, Any] = {"trace": [], "executed_skills": []}
        tools = self._build_deep_agent_tools(runtime_ctx)

        instructions = (
            "You are a pipeline orchestrator.\n"
            "Use available tools to satisfy the user requirement.\n"
            "Always respect skill dependencies.\n"
            "Use concise Chinese for the final answer.\n"
            "If a tool fails, explain failure and suggest next action.\n"
        )
        agent = create_deep_agent(
            model=self.llm,
            tools=tools,
            instructions=instructions,
        )

        result = await agent.ainvoke({"messages": [HumanMessage(content=user_requirement)]})
        if isinstance(result, dict) and isinstance(result.get("messages"), list) and result["messages"]:
            final_message = result["messages"][-1]
            summary = self._extract_text(getattr(final_message, "content", ""))
        else:
            summary = self._extract_text(result)

        execution = self._build_execution_from_ctx(runtime_ctx)
        plan = [
            {
                "id": idx + 1,
                "skill": step.get("skill"),
                "args": step.get("args", {}),
                "reason": "planned and executed by Deep Agents",
            }
            for idx, step in enumerate(runtime_ctx.get("trace", []))
        ]
        workflow = self._build_workflow_view(plan)
        return {"plan": workflow, "workflow": workflow, "execution": execution, "summary": summary}


    async def summarize(self, requirement: str, execution: Dict[str, Any]) -> str:
        from langchain_core.messages import HumanMessage

        execution_json = _safe_json_dumps(execution)

        prompt = f"""
Summarize pipeline execution in Chinese:
- success/failure
- key output of each step
- concrete next action if failed

Requirement:
{requirement}

Execution JSON:
{execution_json}
"""
        resp = await self.llm.ainvoke([HumanMessage(content=prompt)])
        return resp.content or ""

    async def run(
        self,
        user_requirement: str,
        planning_mode: str = "single",
        execution_mode: str = "best",
        candidate_count: int = 3,
        include_summary: bool = True,
    ) -> Dict[str, Any]:
        if planning_mode not in {"single", "multi"}:
            raise ValueError("planning_mode must be 'single' or 'multi'")
        if execution_mode not in {"best", "all"}:
            raise ValueError("execution_mode must be 'best' or 'all'")

        if planning_mode == "multi":
            selection_mode = getattr(self, "_candidate_selection_mode", "rerank")
            if selection_mode == "original_dependency_filter_first_valid":
                selection_state = await self.plan_candidates_original_dependency_filter_first_valid(
                    user_requirement,
                    candidate_count=candidate_count,
                )
                candidates = selection_state["candidates"]
                selected = selection_state["selected"]
                selection_route = str(selection_state.get("selection_route", "first_dependency_valid_candidate"))
            elif selection_mode == "original_first_fallback":
                selection_state = await self.plan_candidates_original_first_fallback(
                    user_requirement,
                    candidate_count=candidate_count,
                )
                candidates = selection_state["candidates"]
                selected = selection_state["selected"]
                selection_route = str(selection_state.get("selection_route", "fallback"))
            elif selection_mode == "collect_all_then_original":
                selection_state = await self.plan_candidates_collect_all_then_original(
                    user_requirement,
                    candidate_count=candidate_count,
                )
                candidates = selection_state["candidates"]
                selected = selection_state["selected"]
                selection_route = str(
                    selection_state.get("selection_route", "original_dependency_pass")
                )
            elif selection_mode == "structure_aware":
                selection_state = await self.plan_candidates_structure_aware(
                    user_requirement,
                    candidate_count=candidate_count,
                )
                candidates = selection_state["candidates"]
                selected = selection_state["selected"]
                selection_route = str(selection_state.get("selection_route", "structure_aware"))
            elif selection_mode == "first":
                candidates = await self.generate_candidate_pool(
                    user_requirement,
                    candidate_count=candidate_count,
                )
                selected = candidates[0]
                selection_route = "first"
            else:
                candidates = await self.plan_candidates(user_requirement, candidate_count=candidate_count)
                selected = self._select_best_candidate(candidates)
                selection_route = "rerank"
            print("\n=== selected Plan ===")
            print(_safe_json_dumps(selected, ensure_ascii=True))

            if execution_mode == "all":
                executions: List[Dict[str, Any]] = []
                for item in candidates:
                    execution = await self.execute_plan(item["workflow"])
                    executions.append({"plan_id": item["id"], "execution": execution})
                if self._preference_logger is not None:
                    try:
                        matched = [e for e in executions if e.get("plan_id") == selected["id"]]
                        selected_execution = matched[0]["execution"] if matched else None
                        self._preference_logger.log_candidates(
                            prompt=user_requirement,
                            candidates=candidates,
                            selected_plan_id=selected["id"],
                            execution_mode=execution_mode,
                            selected_execution=selected_execution,
                        )
                    except Exception:
                        pass
                summary = (
                    await self.summarize(user_requirement, executions[0]["execution"])
                    if include_summary
                    else ""
                )
                return {
                    "candidate_plans": candidates,
                    "selected_plan_id": selected["id"],
                    "selected_plan": selected["workflow"],
                    "workflow": selected["workflow"],
                    "selection_route": selection_route,
                    "structure_aware_meta": selection_state.get("structure_aware_meta") if selection_mode == "structure_aware" else None,
                    "executions": executions,
                    "summary": summary,
                }

            execution = await self.execute_plan(selected["workflow"])
            if self._preference_logger is not None:
                try:
                    self._preference_logger.log_candidates(
                        prompt=user_requirement,
                        candidates=candidates,
                        selected_plan_id=selected["id"],
                        execution_mode=execution_mode,
                        selected_execution=execution,
                    )
                except Exception:
                    pass
            summary = await self.summarize(user_requirement, execution) if include_summary else ""
            return {
                "candidate_plans": candidates,
                "selected_plan_id": selected["id"],
                "selected_plan": selected["workflow"],
                "workflow": selected["workflow"],
                "selection_route": selection_route,
                "structure_aware_meta": selection_state.get("structure_aware_meta") if selection_mode == "structure_aware" else None,
                "execution": execution,
                "summary": summary,
            }

        try:
            return await self._run_with_deep_agent(user_requirement)
        except Exception as e:
            self._deep_agent_import_error = str(e)

        workflow = await self.plan(user_requirement)
        self.validate_plan(workflow)
        execution = await self.execute_plan(workflow)
        summary = await self.summarize(user_requirement, execution) if include_summary else ""
        return {"plan": workflow, "workflow": workflow, "execution": execution, "summary": summary}


async def main() -> None:
    agent = PipelineOrchestratorAgent(model_name="qwen-max")
    #agent = PipelineOrchestratorAgent(
    #    llm_config_path="configs/pipeline_openai.json"
    #)
    print("Pipeline Orchestrator started. Input 'exit' to quit.")
    while True:
        text = input("\nRequirement> ").strip()
        if text.lower() in {"exit", "quit"}:
            break
        try:
            result = await agent.run(text)
            plan_payload = result.get("plan", result.get("selected_plan", result.get("candidate_plans", [])))
            execution_payload = result.get("execution", result.get("executions", []))

            print("\n=== Plan ===")
            print(_safe_json_dumps(plan_payload, ensure_ascii=True))
            print("\n=== Execution ===")
            print(_safe_json_dumps(execution_payload, ensure_ascii=True))
            print("\n=== Summary ===")
            print(result["summary"])
        except Exception as e:
            print(f"[ERROR] {e}")


if __name__ == "__main__":
    asyncio.run(main())
