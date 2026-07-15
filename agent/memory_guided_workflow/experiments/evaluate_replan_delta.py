from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Mapping


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.memory_guided_workflow.experiments.intent_audited_replan_edge_repair import (
    evaluate_rows,
    latest_rows_by_id,
    read_json_records,
)


DATASET_DEFAULTS: Dict[str, Dict[str, str]] = {
    "multimedia": {
        "sample_file": "taskbench/data_multimedia/multimedia_test_data.json",
        "gold_file": "taskbench/data_multimedia/data.json",
        "tool_desc": "taskbench/data_multimedia/tool_desc.json",
        "prediction_dir": "taskbench/data_multimedia/predictions_use_demos_2_reformat_by_self",
        "replan_dir": "taskbench/data_multimedia/replan_reformat_by_self",
        "replan_prefix": "data_multimedia",
    },
    "huggingface": {
        "sample_file": "taskbench/data_huggingface/huggingface_test_data.json",
        "gold_file": "taskbench/data_huggingface/data.json",
        "tool_desc": "taskbench/data_huggingface/tool_desc.json",
        "prediction_dir": "taskbench/data_huggingface/predictions_use_demos_2_reformat_by_self",
        "replan_dir": "taskbench/data_huggingface/replan_reformat_by_self",
        "replan_prefix": "data_huggingface",
    },
    "dailylife": {
        "sample_file": "taskbench/data_dailylifeapis/dailylife_test_data.json",
        "gold_file": "taskbench/data_dailylifeapis/data.json",
        "tool_desc": "taskbench/data_dailylifeapis/tool_desc.json",
        "prediction_dir": "taskbench/data_dailylifeapis/predictions_use_demos_2_reformat_by_self",
        "replan_dir": "taskbench/data_dailylifeapis/replan_reformat_by_self",
        "replan_prefix": "data_dailylifeapis",
    },
}


METRIC_KEYS = {
    "n_f1": "node_micro_f1",
    "e_f1": "edge_micro_f1",
    "ned": "ned",
}


def main() -> int:
    args = parse_args()
    defaults = DATASET_DEFAULTS[args.dataset]

    sample_file = resolve_path(args.sample_file or defaults["sample_file"])
    gold_file = resolve_path(args.gold_file or defaults["gold_file"])
    tool_desc = resolve_path(args.tool_desc or defaults["tool_desc"])
    before_file = resolve_path(args.before_file) if args.before_file else infer_before_file(args.model, defaults)
    after_file = resolve_path(args.after_file) if args.after_file else infer_after_file(args.model, defaults)

    before_rows = latest_rows_by_id(read_json_records(before_file))
    after_rows = latest_rows_by_id(read_json_records(after_file))

    before = evaluate_rows(
        sample_file=sample_file,
        gold_file=gold_file,
        prediction_rows=before_rows,
        tool_desc_file=tool_desc,
    )
    after = evaluate_rows(
        sample_file=sample_file,
        gold_file=gold_file,
        prediction_rows=after_rows,
        tool_desc_file=tool_desc,
    )
    report = build_report(
        dataset=args.dataset,
        model=args.model,
        sample_file=sample_file,
        gold_file=gold_file,
        tool_desc=tool_desc,
        before_file=before_file,
        after_file=after_file,
        before=before,
        after=after,
    )

    print_report(report)
    if args.output:
        output = resolve_path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved_json={output}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare pre/post replan N-F1, E-F1, and NED on a TaskBench sample set."
    )
    parser.add_argument("--dataset", choices=sorted(DATASET_DEFAULTS), required=True)
    parser.add_argument("--model", default="qwen3-14b")
    parser.add_argument("--sample-file", default=None)
    parser.add_argument("--gold-file", default=None)
    parser.add_argument("--tool-desc", default=None)
    parser.add_argument("--before-file", default=None, help="Original prediction JSON/JSONL. Auto-discovered if omitted.")
    parser.add_argument("--after-file", default=None, help="Replan prediction JSON/JSONL. Auto-discovered if omitted.")
    parser.add_argument("--output", default=None, help="Optional JSON report path.")
    return parser.parse_args()


def build_report(
    *,
    dataset: str,
    model: str,
    sample_file: Path,
    gold_file: Path,
    tool_desc: Path,
    before_file: Path,
    after_file: Path,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> Dict[str, Any]:
    metrics: Dict[str, Dict[str, Any]] = {}
    for display_key, metric_key in METRIC_KEYS.items():
        before_value = float(before.get(metric_key, 0.0) or 0.0)
        after_value = float(after.get(metric_key, 0.0) or 0.0)
        metrics[display_key] = {
            "before": before_value,
            "after": after_value,
            "delta_after_minus_before": after_value - before_value,
        }

    return {
        "dataset": dataset,
        "model": model,
        "sample_file": str(sample_file),
        "gold_file": str(gold_file),
        "tool_desc": str(tool_desc),
        "before_file": str(before_file),
        "after_file": str(after_file),
        "before": compact_eval(before),
        "after": compact_eval(after),
        "metrics": metrics,
    }


def compact_eval(metrics: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "sample_ids": metrics.get("sample_ids"),
        "gold_ids": metrics.get("gold_ids"),
        "prediction_ids": metrics.get("prediction_ids"),
        "common_ids": metrics.get("common_ids"),
        "support": metrics.get("support"),
        "skipped": metrics.get("skipped"),
        "node_micro_precision": metrics.get("node_micro_precision"),
        "node_micro_recall": metrics.get("node_micro_recall"),
        "node_micro_f1": metrics.get("node_micro_f1"),
        "edge_micro_precision": metrics.get("edge_micro_precision"),
        "edge_micro_recall": metrics.get("edge_micro_recall"),
        "edge_micro_f1": metrics.get("edge_micro_f1"),
        "ned": metrics.get("ned"),
    }


def print_report(report: Mapping[str, Any]) -> None:
    print(f"dataset={report['dataset']} model={report['model']}")
    print(f"before_file={report['before_file']}")
    print(f"after_file={report['after_file']}")
    before = report["before"]
    after = report["after"]
    print(
        "coverage: "
        f"before_common={before['common_ids']} after_common={after['common_ids']} "
        f"before_support={before['support']} after_support={after['support']}"
    )
    print("")
    print("metric,before,after,delta(after-before)")
    for name in ("n_f1", "e_f1", "ned"):
        row = report["metrics"][name]
        print(
            f"{name},"
            f"{format_float(row['before'])},"
            f"{format_float(row['after'])},"
            f"{format_signed_float(row['delta_after_minus_before'])}"
        )


def infer_before_file(model: str, defaults: Mapping[str, str]) -> Path:
    prediction_dir = resolve_path(defaults["prediction_dir"])
    candidates = sorted(prediction_dir.glob(f"{model}*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"no original prediction file found: {prediction_dir}/{model}*.json")
    return candidates[0].resolve()


def infer_after_file(model: str, defaults: Mapping[str, str]) -> Path:
    replan_dir = resolve_path(defaults["replan_dir"])
    prefix = defaults["replan_prefix"]
    patterns = [
        f"{prefix}_{model}*_intent_guided_replan.jsonl",
        f"{prefix}_{model}*_intent_guided_replan.json",
        f"{prefix}_{model}*replan*final.json",
    ]
    candidates = []
    for pattern in patterns:
        candidates.extend(replan_dir.glob(pattern))
    candidates = sorted(set(candidates), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"no replan file found in {replan_dir} for model={model}")
    return candidates[0].resolve()


def resolve_path(raw: Any) -> Path:
    path = Path(str(raw)).expanduser()
    return path.resolve() if path.is_absolute() else (REPO_ROOT / path).resolve()


def format_float(value: Any) -> str:
    return f"{float(value):.6f}"


def format_signed_float(value: Any) -> str:
    numeric = float(value)
    return f"{numeric:+.6f}"


if __name__ == "__main__":
    raise SystemExit(main())
