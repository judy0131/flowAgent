import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKFLOWHUB_DIR = ROOT / "data" / "workflowhub"
DEFAULT_SKILLS_DIR = ROOT / "skills" / "workflowhub"


EXECUTOR_TEMPLATE = """from typing import Any, Dict


def run(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    artifacts = ctx.setdefault("artifacts", {{}})
    source_ref = str(args.get("source_ref", "external_input"))
    output_key = str(args.get("output_key", "{default_output_key}"))
    operation = "{operation}"
    tool_id = "{tool_id}"

    source = artifacts.get(source_ref, source_ref)
    produced = {{
        "operation": operation,
        "tool_id": tool_id,
        "source_ref": source_ref,
        "source": source,
        "output_key": output_key,
        "status": "simulated_ok",
    }}
    artifacts[output_key] = produced
    return produced
"""


def _slugify(text: str, max_len: int = 72) -> str:
    value = text.strip().lower().replace("+", " plus ")
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    value = re.sub(r"_+", "_", value)
    return value[:max_len] or "workflowhub_step"


def _strip_step_prefix(step_name: str) -> str:
    return re.sub(r"^\d+_", "", step_name).strip()


def _extract_urls(text: str) -> List[str]:
    urls = re.findall(r"https?://[^\s)]+", text or "")
    cleaned: List[str] = []
    for url in urls:
        url = url.rstrip(".,;")
        if url not in cleaned:
            cleaned.append(url)
    return cleaned


def _summarize_doc(text: str, max_len: int = 200) -> str:
    compact = " ".join((text or "").split())
    if not compact:
        return "Generated from WorkflowHub CWL step definitions."
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 3].rstrip() + "..."


def _entity_value(entity: Dict[str, Any], key: str) -> Optional[str]:
    value = entity.get(key)
    if isinstance(value, str):
        return value.strip() or None
    return None


def _load_cwl(cwl_path: Path) -> Dict[str, Any]:
    with cwl_path.open("r", encoding="utf-8") as f:
        payload = yaml.safe_load(f) or {}
    return payload if isinstance(payload, dict) else {}


def _load_rocrate_meta(crate_dir: Path) -> Dict[str, str]:
    meta_path = crate_dir / "ro-crate-metadata.json"
    if not meta_path.exists():
        return {}
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}

    graph = payload.get("@graph", [])
    if not isinstance(graph, list):
        return {}

    dataset = next(
        (item for item in graph if isinstance(item, dict) and item.get("@id") == "./"),
        {},
    )
    workflow_entity = next(
        (
            item
            for item in graph
            if isinstance(item, dict)
            and "ComputationalWorkflow" in (item.get("@type") if isinstance(item.get("@type"), list) else [])
        ),
        {},
    )

    return {
        "workflow_name": _entity_value(workflow_entity, "name")
        or _entity_value(dataset, "name")
        or crate_dir.name,
        "workflow_description": _entity_value(workflow_entity, "description")
        or _entity_value(dataset, "description")
        or "",
        "workflow_url": _entity_value(workflow_entity, "url")
        or _entity_value(dataset, "identifier")
        or _entity_value(dataset, "url")
        or "",
        "workflow_license": _entity_value(workflow_entity, "license")
        or _entity_value(dataset, "license")
        or "",
        "workflow_keywords": _entity_value(workflow_entity, "keywords") or "",
    }


def _extract_dep_step_names(step_inputs: Dict[str, Any]) -> Set[str]:
    deps: Set[str] = set()
    for value in step_inputs.values():
        values: Iterable[Any] = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str) and "/" in item:
                deps.add(item.split("/", 1)[0])
    return deps


def generate_skills(workflowhub_dir: Path, skills_dir: Path, clean: bool = False) -> List[Path]:
    cwl_files = sorted(workflowhub_dir.glob("*.crate/*.cwl"))
    if not cwl_files:
        raise FileNotFoundError(f"No CWL files found under: {workflowhub_dir}")

    if clean and skills_dir.exists():
        for item in skills_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item)

    skills_dir.mkdir(parents=True, exist_ok=True)

    skill_defs: Dict[str, Dict[str, Any]] = {}

    for cwl_file in cwl_files:
        cwl_payload = _load_cwl(cwl_file)
        steps = cwl_payload.get("steps", {}) or {}
        if not isinstance(steps, dict):
            continue

        meta = _load_rocrate_meta(cwl_file.parent)
        workflow_name = meta.get("workflow_name") or cwl_file.stem
        workflow_doc = (meta.get("workflow_description") or cwl_payload.get("doc") or "").strip()
        workflow_url = meta.get("workflow_url") or ""
        workflow_license = meta.get("workflow_license") or ""
        workflow_keyword = meta.get("workflow_keywords") or ""

        step_to_skill: Dict[str, str] = {
            step_name: _slugify(_strip_step_prefix(step_name))
            for step_name in steps.keys()
        }

        for step_name, step_data in steps.items():
            if not isinstance(step_data, dict):
                continue

            skill_name = step_to_skill[step_name]
            step_short_name = _strip_step_prefix(step_name)
            run_block = step_data.get("run", {})
            tool_id = str(run_block.get("id", "unknown_tool")) if isinstance(run_block, dict) else "unknown_tool"
            deps_raw = _extract_dep_step_names(step_data.get("in", {}))
            deps = sorted({step_to_skill[d] for d in deps_raw if d in step_to_skill})
            out_ports = step_data.get("out", [])
            default_output_key = out_ports[0] if isinstance(out_ports, list) and out_ports else f"{skill_name}_out"

            urls = set(_extract_urls(workflow_doc))
            if workflow_url:
                urls.add(workflow_url)
            url_text = ", ".join(sorted(urls)) if urls else "none"

            summary = _summarize_doc(workflow_doc)
            description = (
                f"[{workflow_name}] WorkflowHub step '{step_short_name}' generated from {tool_id}. "
                f"Context: {summary}"
            )

            spec = skill_defs.get(skill_name)
            if spec is None:
                skill_defs[skill_name] = {
                    "name": skill_name,
                    "description": description,
                    "input_schema": {"source_ref": "str", "output_key": "str"},
                    "executor": "executor.py:run",
                    "depends_on_all": [],
                    "depends_on_any": deps,
                    "allow_no_dependency": len(deps) == 0,
                    "default_output_key": default_output_key,
                    "operation": step_short_name,
                    "tool_id": tool_id,
                    "workflow_names": {workflow_name},
                    "workflow_licenses": {workflow_license} if workflow_license else set(),
                    "workflow_keywords": {workflow_keyword} if workflow_keyword else set(),
                    "source_cwls": {cwl_file.relative_to(workflowhub_dir).as_posix()},
                    "more_info_urls": urls,
                    "doc_summary": summary,
                    "url_text": url_text,
                }
            else:
                spec["allow_no_dependency"] = spec["allow_no_dependency"] or len(deps) == 0
                merged = sorted(set(spec["depends_on_any"]) | set(deps))
                spec["depends_on_any"] = [] if spec["allow_no_dependency"] else merged
                spec["workflow_names"].add(workflow_name)
                if workflow_license:
                    spec["workflow_licenses"].add(workflow_license)
                if workflow_keyword:
                    spec["workflow_keywords"].add(workflow_keyword)
                spec["source_cwls"].add(cwl_file.relative_to(workflowhub_dir).as_posix())
                spec["more_info_urls"].update(urls)
                spec["url_text"] = ", ".join(sorted(spec["more_info_urls"])) if spec["more_info_urls"] else "none"

    generated: List[Path] = []
    for skill_name, spec in sorted(skill_defs.items()):
        skill_dir = skills_dir / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)

        skill_json = {
            "name": spec["name"],
            "description": spec["description"],
            "input_schema": spec["input_schema"],
            "executor": spec["executor"],
            "depends_on_all": spec["depends_on_all"],
            "depends_on_any": spec["depends_on_any"],
            "meta": {
                "source": "workflowhub",
                "workflow_names": sorted(spec["workflow_names"]),
                "workflow_licenses": sorted(spec["workflow_licenses"]),
                "workflow_keywords": sorted(spec["workflow_keywords"]),
                "source_cwls": sorted(spec["source_cwls"]),
                "more_info_urls": sorted(spec["more_info_urls"]),
            },
        }
        (skill_dir / "skill.json").write_text(
            json.dumps(skill_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        skill_md = f"""---
name: {spec["name"]}
description: {spec["description"]}
---

# {spec["name"]}

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: {spec["depends_on_any"]}

Workflow context:
- workflow_names: {sorted(spec["workflow_names"])}
- source_cwls: {sorted(spec["source_cwls"])}
- more_info_urls: {sorted(spec["more_info_urls"])}
- doc_summary: {spec["doc_summary"]}

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
"""
        (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")

        executor = EXECUTOR_TEMPLATE.format(
            default_output_key=spec["default_output_key"],
            operation=spec["operation"],
            tool_id=spec["tool_id"],
        )
        (skill_dir / "executor.py").write_text(executor, encoding="utf-8")
        generated.append(skill_dir)

    return generated


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate legacy-style operator skills from WorkflowHub CWL workflows."
    )
    parser.add_argument(
        "--workflowhub-dir",
        default=str(DEFAULT_WORKFLOWHUB_DIR),
        help="Directory containing *.crate/*.cwl files.",
    )
    parser.add_argument(
        "--skills-dir",
        default=str(DEFAULT_SKILLS_DIR),
        help="Destination directory for generated skills.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Clean destination directory before generating skills.",
    )
    args = parser.parse_args()

    generated = generate_skills(
        workflowhub_dir=Path(args.workflowhub_dir),
        skills_dir=Path(args.skills_dir),
        clean=args.clean,
    )
    print(f"Generated {len(generated)} operator skills:")
    for p in generated:
        print(f"- {p}")


if __name__ == "__main__":
    main()
