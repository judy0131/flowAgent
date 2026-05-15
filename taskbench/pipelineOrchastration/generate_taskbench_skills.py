import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List


def _safe_dir_name(name: str) -> str:
    # Windows-incompatible characters: <>:"/\|?*
    return re.sub(r'[<>:"/\\|?*]+', "_", name).strip()


def _build_input_schema(input_types: List[str]) -> Dict[str, str]:
    schema: Dict[str, str] = {}
    for i, t in enumerate(input_types, start=1):
        key = f"arg{i}"
        schema[key] = f"Input parameter {i}, expected type: {t}"
    if not schema:
        schema["arg1"] = "Primary input argument."
    return schema


def _skill_json_payload(skill_name: str, node: Dict[str, Any]) -> Dict[str, Any]:
    tool_name = str(node.get("id", "")).strip()
    desc = str(node.get("desc", "")).strip()
    input_types = [str(x) for x in node.get("input-type", [])]
    return {
        "name": skill_name,
        "description": desc or f"TaskBench tool adapter for {tool_name}.",
        "input_schema": _build_input_schema(input_types),
        "executor": "executor.py:run",
    }


def _executor_source() -> str:
    return """from typing import Any, Dict


def run(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    artifacts = ctx.setdefault("artifacts", {})
    trace = ctx.setdefault("taskbench_tool_trace", [])
    trace.append({"args": dict(args)})

    output_key = args.get("output_key")
    if isinstance(output_key, str) and output_key.strip():
        artifacts[output_key] = {
            "status": "ok",
            "args": dict(args),
        }

    return {"status": "ok", "echo_args": dict(args)}
"""


def _skill_md(tool_name: str, node: Dict[str, Any]) -> str:
    input_types = ", ".join(str(x) for x in node.get("input-type", [])) or "none"
    output_types = ", ".join(str(x) for x in node.get("output-type", [])) or "none"
    desc = str(node.get("desc", "")).strip()
    return (
        f"# {tool_name}\n\n"
        f"{desc}\n\n"
        f"- input types: {input_types}\n"
        f"- output types: {output_types}\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate TaskBench-compatible skills from tool_desc.json")
    parser.add_argument("--tool_desc", type=str, required=True, help="Path to TaskBench tool_desc.json")
    parser.add_argument(
        "--output_root",
        type=str,
        required=True,
        help="Directory to write generated skills (one folder per tool).",
    )
    parser.add_argument("--clean", action="store_true", default=False)
    args = parser.parse_args()

    tool_desc_path = Path(args.tool_desc).resolve()
    output_root = Path(args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    payload = json.loads(tool_desc_path.read_text(encoding="utf-8"))
    nodes = payload.get("nodes", [])
    if not isinstance(nodes, list):
        raise ValueError("tool_desc.json invalid: nodes must be a list")

    if args.clean:
        for child in output_root.iterdir():
            if child.is_dir():
                for sub in child.iterdir():
                    if sub.is_file():
                        sub.unlink()
                child.rmdir()

    count = 0
    for node in nodes:
        tool_name = str(node.get("id", "")).strip()
        if not tool_name:
            continue
        skill_dir = output_root / _safe_dir_name(tool_name)
        skill_dir.mkdir(parents=True, exist_ok=True)

        safe_name = _safe_dir_name(tool_name)
        skill_json = _skill_json_payload(safe_name, node)
        (skill_dir / "skill.json").write_text(
            json.dumps(skill_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (skill_dir / "SKILL.md").write_text(_skill_md(tool_name, node), encoding="utf-8")
        (skill_dir / "executor.py").write_text(_executor_source(), encoding="utf-8")
        count += 1

    print(f"Generated {count} skills into: {output_root}")
    print("Note: folder names are sanitized for filesystem compatibility.")


if __name__ == "__main__":
    main()
