from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[3]


METRIC_ALIASES = {
    "n_f1": ("node_micro_f1_no_matching", "node_micro_f1", "n_f1"),
    "e_f1": ("link_binary_f1", "edge_micro_f1", "e_f1"),
    "ned": ("edit_distance", "ned"),
}


def main() -> int:
    args = parse_args()
    files = collect_metric_files(args.paths)
    if not files:
        raise FileNotFoundError("no metric JSON files found")

    rows = []
    for path in files:
        row = extract_metric_row(path, section=args.section)
        if row is not None:
            rows.append(row)

    if not rows:
        raise ValueError(f"no rows with section={args.section!r} and N-F1/E-F1/NED metrics found")

    print_table(rows, raw=args.raw)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print a compact N-F1/E-F1/NED table from TaskBench evaluate metric JSON files."
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="Metric JSON files or directories containing metric JSON files.",
    )
    parser.add_argument(
        "--section",
        default="overall_overall",
        help="Metric section to extract. Default: overall_overall.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print raw 0-1 values instead of percentage values.",
    )
    return parser.parse_args()


def collect_metric_files(paths: Sequence[str]) -> List[Path]:
    files: List[Path] = []
    seen = set()
    for raw in paths:
        path = resolve_path(raw)
        candidates = sorted(path.glob("*.json")) if path.is_dir() else [path]
        for candidate in candidates:
            if not candidate.is_file() or candidate.suffix.lower() != ".json":
                continue
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(resolved)
    return files


def extract_metric_row(path: Path, section: str) -> Dict[str, Any] | None:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        return None
    metrics = payload.get(section)
    if not isinstance(metrics, Mapping):
        metrics = payload
    values = {
        "n_f1": first_number(metrics, METRIC_ALIASES["n_f1"]),
        "e_f1": first_number(metrics, METRIC_ALIASES["e_f1"]),
        "ned": first_number(metrics, METRIC_ALIASES["ned"]),
    }
    if any(value is None for value in values.values()):
        return None
    return {
        "file": path.stem,
        **values,
    }


def first_number(metrics: Mapping[str, Any], keys: Iterable[str]) -> float | None:
    for key in keys:
        value = metrics.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def print_table(rows: Sequence[Mapping[str, Any]], raw: bool) -> None:
    headers = ["file", "N-F1", "E-F1", "NED"]
    display_rows = []
    for row in rows:
        display_rows.append(
            [
                str(row["file"]),
                format_metric(row["n_f1"], raw=raw),
                format_metric(row["e_f1"], raw=raw),
                format_metric(row["ned"], raw=raw),
            ]
        )
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in display_rows))
        for index in range(len(headers))
    ]
    print(" | ".join(headers[index].ljust(widths[index]) for index in range(len(headers))))
    print("-+-".join("-" * width for width in widths))
    for row in display_rows:
        print(" | ".join(row[index].ljust(widths[index]) for index in range(len(row))))


def format_metric(value: Any, raw: bool) -> str:
    number = float(value)
    return f"{number:.6f}" if raw else f"{number * 100:.2f}"


def resolve_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (REPO_ROOT / path).resolve()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
