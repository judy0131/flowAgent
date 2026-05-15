import importlib.util
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .actions import _infer_skill_action_tags
from .models import SkillMetadata, SkillPackage


class SkillRegistry:
    def __init__(self, skills_root: Path):
        self.skills_root = skills_root
        self._meta_by_name: Dict[str, SkillMetadata] = {}
        self._loaded_by_name: Dict[str, SkillPackage] = {}
        self._discover()

    def _discover(self) -> None:
        if not self.skills_root.exists():
            return
        for skill_json in self.skills_root.glob("*/skill.json"):
            skill_dir = skill_json.parent
            with skill_json.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            meta = SkillMetadata(
                name=payload["name"],
                description=payload["description"],
                input_schema=payload.get("input_schema", {}),
                executor=payload.get("executor", "executor.py:run"),
                depends_on_all=payload.get("depends_on_all", []),
                depends_on_any=payload.get("depends_on_any", []),
                action_tags=payload.get(
                    "action_tags",
                    _infer_skill_action_tags(
                        payload["name"],
                        payload.get("description", ""),
                        payload.get("input_schema", {}),
                    ),
                ),
            )
            self._meta_by_name[meta.name] = meta

    @property
    def skills(self) -> Dict[str, SkillMetadata]:
        return self._meta_by_name

    def list_for_prompt(self) -> str:
        lines: List[str] = []
        for skill in self._meta_by_name.values():
            schema = json.dumps(skill.input_schema, ensure_ascii=False)
            lines.append(
                f"- {skill.name}: {skill.description}; input_schema={schema}; "
                f"depends_on_all={skill.depends_on_all}; depends_on_any={skill.depends_on_any}; "
                f"action_tags={skill.action_tags}"
            )
        return "\n".join(lines)

    def get(self, name: str) -> Optional[SkillMetadata]:
        return self._meta_by_name.get(name)

    def load_skill(self, name: str) -> SkillPackage:
        if name in self._loaded_by_name:
            return self._loaded_by_name[name]

        meta = self.get(name)
        if meta is None:
            raise KeyError(f"skill not found: {name}")

        skill_dir = self.skills_root / name
        skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        run_fn = self._load_executor(skill_dir, meta.executor)
        pkg = SkillPackage(metadata=meta, skill_dir=skill_dir, markdown=skill_md, run=run_fn)
        self._loaded_by_name[name] = pkg
        return pkg

    @staticmethod
    def _load_executor(skill_dir: Path, executor_ref: str) -> Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]:
        file_name, fn_name = executor_ref.split(":", 1)
        file_path = skill_dir / file_name
        module_name = f"skill_{skill_dir.name}_executor"
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load executor module: {file_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        run_fn = getattr(module, fn_name, None)
        if run_fn is None:
            raise RuntimeError(f"executor function not found: {file_path}:{fn_name}")
        return run_fn
