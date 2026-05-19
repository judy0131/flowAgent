import argparse
import asyncio
import importlib.util
import sys
from pathlib import Path


_UNSET = object()
DEFAULT_CASE_IDS_FILE = "taskbench/data_multimedia/rollback_experiments/20260515_172328/case_ids.txt"
DEFAULT_PREDICTION_DIR = "predictions_pipeline_agent_grounding_fix_E"
DEFAULT_SKILLS_ROOT = "skills_multimedia"


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
    _update_action(parser, "model_name", default="gpt-4.1")
    _update_action(parser, "planning_mode", default="multi")
    _update_action(
        parser,
        "llm_config_path",
        default="configs/openai.json",
        help_text="Path to JSON config containing OpenAI LLM settings. Defaults to gpt-5.4-xhigh.",
    )
    _update_action(
        parser,
        "workflow_memory_path",
        default="taskbench/data_multimedia/workflow_memory_fold0_excluded.json",
        help_text="Default workflow memory for OpenAI runner.",
    )
    parser.set_defaults(
        prediction_dir=DEFAULT_PREDICTION_DIR,
        skills_root=DEFAULT_SKILLS_ROOT,
        case_ids_file=DEFAULT_CASE_IDS_FILE,
        planning_mode="multi",
        candidate_count=3,
        candidate_selection_mode="original_first_fallback",
        include_original_candidate=True,
        enable_candidate_verifier=True,
        enable_candidate_repair=True,
        enable_workflow_memory=False,
        fixed_candidate_temperature=0.0,
        resume=False,
    )
    return parser


if __name__ == "__main__":
    cli_args = build_parser().parse_args(sys.argv[1:])
    asyncio.run(_run(cli_args))
