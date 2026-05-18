from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


@dataclass
class SkillMetadata:
    name: str
    description: str
    input_schema: Dict[str, str]
    executor: str
    depends_on_all: List[str]
    depends_on_any: List[str]
    action_tags: List[str] = field(default_factory=list)
    input_types: Dict[str, List[str]] = field(default_factory=dict)
    output_types: List[str] = field(default_factory=list)


@dataclass
class SkillPackage:
    metadata: SkillMetadata
    skill_dir: Path
    markdown: str
    run: Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]


@dataclass
class LLMRuntimeConfig:
    provider: str
    model_name: str
    temperature: float = 0.0
    api_key: Optional[str] = None
    api_key_envs: List[str] = field(default_factory=list)
    base_url: Optional[str] = None
    base_url_env: Optional[str] = None
    base_url_envs: List[str] = field(default_factory=list)
    extra_kwargs: Dict[str, Any] = field(default_factory=dict)
