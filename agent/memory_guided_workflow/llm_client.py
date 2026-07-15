import json
import os
from pathlib import Path
from typing import Any, Dict, List

from .utils import coerce_str_list


class OpenAICompatibleLLMClient:
    """Load LLM config and call an OpenAI-compatible chat completion API."""

    _package_root = Path(__file__).resolve().parent
    _agent_root = _package_root.parent
    _repo_root = _agent_root.parent

    def __init__(
        self,
        llm_config_path: Any = None,
        llm_config: Dict[str, Any] | None = None,
        llm_profile: str | None = None,
        default_config_path: str = "configs/qwen.json",
    ):
        self.llm_config_path = llm_config_path
        self.llm_config = llm_config
        self.llm_profile = llm_profile
        self.default_config_path = default_config_path
        self._resolved_config: Dict[str, Any] | None = None
        self._chat_client: Any = None
        self._chat_client_signature: tuple[str, str | None, float, int, bool] | None = None

    def chat(self, messages: List[Dict[str, str]]) -> str:
        config = self.resolve_config()
        api_key = self.resolve_api_key(config)
        model = str(config.get("model_name") or config.get("model") or "").strip()
        if not model:
            raise ValueError("llm config must include model_name or model")

        base_url = self.resolve_base_url(config)
        timeout = float(config.get("timeout", 30))
        max_retries = int(config.get("max_retries", 0))
        trust_env = self.resolve_trust_env(config)
        client = self._get_chat_client(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            trust_env=trust_env,
        )

        request_kwargs: Dict[str, Any] = {
            "model": model,
            "temperature": float(config.get("temperature", 0)),
            "messages": messages,
        }
        extra_body = self.resolve_extra_body(config)
        if extra_body:
            request_kwargs["extra_body"] = extra_body

        response = client.chat.completions.create(**request_kwargs)
        content = response.choices[0].message.content
        if not content:
            raise ValueError("empty LLM response")
        return content

    def resolve_config(self) -> Dict[str, Any]:
        if self._resolved_config is not None:
            return dict(self._resolved_config)

        payload = self.load_config_payload()
        profiles = payload.get("profiles") if isinstance(payload.get("profiles"), dict) else None
        selected_profile = (
            self.llm_profile
            or os.getenv("TASK_UNDERSTANDING_LLM_PROFILE")
            or os.getenv("PIPELINE_ORCHESTRATOR_LLM_PROFILE")
        )

        if profiles:
            if not selected_profile:
                selected_profile = str(
                    payload.get("default_profile")
                    or payload.get("default")
                    or payload.get("profile")
                    or ""
                ).strip()
            if not selected_profile:
                raise ValueError("llm config defines profiles but no profile was selected")
            profile_payload = profiles.get(selected_profile)
            if not isinstance(profile_payload, dict):
                raise ValueError(f"unknown llm profile: {selected_profile}")
            self._resolved_config = dict(profile_payload)
            return dict(self._resolved_config)

        self._resolved_config = dict(payload)
        return dict(self._resolved_config)

    def _get_chat_client(
        self,
        api_key: str,
        base_url: str | None,
        timeout: float,
        max_retries: int,
        trust_env: bool,
    ) -> Any:
        signature = (api_key, base_url, timeout, max_retries, trust_env)
        if self._chat_client is not None and self._chat_client_signature == signature:
            return self._chat_client

        from openai import OpenAI

        client_kwargs: Dict[str, Any] = {
            "api_key": api_key,
            "timeout": timeout,
            "max_retries": max_retries,
        }
        if base_url:
            client_kwargs["base_url"] = base_url
        if not trust_env:
            import httpx

            client_kwargs["http_client"] = httpx.Client(trust_env=False, timeout=timeout)
        self._chat_client = OpenAI(**client_kwargs)
        self._chat_client_signature = signature
        return self._chat_client

    def load_config_payload(self) -> Dict[str, Any]:
        if self.llm_config is not None:
            return dict(self.llm_config)

        raw_path = (
            self.llm_config_path
            or os.getenv("TASK_UNDERSTANDING_LLM_CONFIG")
            or os.getenv("PIPELINE_ORCHESTRATOR_LLM_CONFIG")
            or self.default_config_path
        )
        path = self.resolve_config_path(raw_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"llm config must be a JSON object: {path}")
        return payload

    @classmethod
    def resolve_config_path(cls, raw_path: Any) -> Path:
        raw_text = str(raw_path).strip()
        if not raw_text:
            raise ValueError("llm_config_path must be non-empty")

        path = Path(raw_text).expanduser()
        if path.is_absolute():
            resolved = path.resolve()
            if not resolved.exists():
                raise FileNotFoundError(f"llm config file not found: {resolved}")
            return resolved

        candidates = [
            (Path.cwd() / path).resolve(),
            (cls._repo_root / path).resolve(),
            (cls._agent_root / path).resolve(),
        ]
        seen: set[str] = set()
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

    @classmethod
    def resolve_api_key(cls, config: Dict[str, Any]) -> str:
        api_key = str(config.get("api_key") or "").strip()
        if api_key:
            return api_key

        for env_name in coerce_str_list(config.get("api_key_envs") or config.get("api_key_env")):
            if cls.looks_like_api_key(env_name):
                return env_name
            value = os.getenv(env_name)
            if value:
                return value

        raise ValueError("llm config must include api_key or api_key_envs with a set environment variable")

    @classmethod
    def resolve_base_url(cls, config: Dict[str, Any]) -> str | None:
        base_url = str(config.get("base_url") or "").strip()
        if base_url:
            return base_url

        for env_name in coerce_str_list(config.get("base_url_envs") or config.get("base_url_env")):
            value = os.getenv(env_name)
            if value:
                return value
        return None

    @staticmethod
    def resolve_extra_body(config: Dict[str, Any]) -> Dict[str, Any]:
        value = config.get("extra_body")
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("llm config extra_body must be a JSON object")
        return dict(value)

    @staticmethod
    def resolve_trust_env(config: Dict[str, Any]) -> bool:
        value = config.get("trust_env", True)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off"}
        return bool(value)

    @staticmethod
    def looks_like_api_key(value: str) -> bool:
        text = str(value).strip()
        return text.startswith(("sk-", "sk-proj-", "AIza"))
