import argparse
import asyncio
import importlib.util
import sys
from pathlib import Path


_UNSET = object()


def _load_base_module():
    base_path = Path(__file__).with_name("run_with_pipeline_agent_base.py")
    spec = importlib.util.spec_from_file_location("run_with_pipeline_agent_base", base_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load base pipeline agent module: {base_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _update_action(
    parser: argparse.ArgumentParser,
    dest: str,
    *,
    default=_UNSET,
    choices=_UNSET,
    help_text=_UNSET,
) -> None:
    for action in parser._actions:
        if action.dest != dest:
            continue
        if default is not _UNSET:
            action.default = default
        if choices is not _UNSET:
            action.choices = choices
        if help_text is not _UNSET:
            action.help = help_text
        return
    raise ValueError(f"parser action not found: {dest}")


_BASE = _load_base_module()

_run = _BASE._run
_resolve_existing_file = _BASE._resolve_existing_file
_convert_plan_to_taskbench_result = _BASE._convert_plan_to_taskbench_result
_build_prediction_record = _BASE._build_prediction_record


def build_parser() -> argparse.ArgumentParser:
    parser = _BASE.build_parser()
    parser.description = "Run TaskBench inference with PipelineOrchestratorAgent using OpenAI."
    _update_action(parser, "provider", default="openai", choices=["openai", "tongyi", "gemini"])
    _update_action(parser, "model_name", default="gpt-5.4-xhigh")
    _update_action(parser, "planning_mode", default="multi")
    _update_action(
        parser,
        "llm_config_path",
        default="configs/openai_gpt54_xhigh.json",
        help_text="Path to JSON config containing OpenAI LLM settings. Defaults to gpt-5.4-xhigh.",
    )
    _update_action(
        parser,
        "workflow_memory_path",
        default="taskbench/data_multimedia/workflow_memory_fold0_excluded.json",
        help_text="Default workflow memory for OpenAI runner.",
    )
    return parser


if __name__ == "__main__":
    cli_args = build_parser().parse_args(["--limit", "20", *sys.argv[1:]])
    if not cli_args.skills_root:
        cli_args.skills_root = "skills_multimedia"
    asyncio.run(_run(cli_args))
