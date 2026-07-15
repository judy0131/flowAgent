from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.memory_guided_workflow.operation_vocabulary import (  # noqa: E402
    build_operation_vocabulary,
    render_ascii_tree,
    write_vocabulary_artifacts,
)


DEFAULT_TOOL_DESC = ROOT / "taskbench" / "data_multimedia" / "tool_desc.json"
DEFAULT_OUTPUT_DIR = ROOT / "agent" / "memory_guided_workflow" / "outputs"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build an intent -> operation -> candidate tools vocabulary from tool_desc.json."
    )
    parser.add_argument(
        "--tool-desc",
        default=str(DEFAULT_TOOL_DESC),
        help="Path to TaskBench-style tool_desc.json.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory for generated JSON, Mermaid, and Markdown files.",
    )
    parser.add_argument(
        "--prefix",
        default="operation_vocabulary",
        help="Output file prefix.",
    )
    parser.add_argument(
        "--print-tree",
        action="store_true",
        help="Print the ASCII tree to stdout.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    vocabulary = build_operation_vocabulary(args.tool_desc)
    paths = write_vocabulary_artifacts(
        vocabulary=vocabulary,
        output_json=output_dir / f"{args.prefix}.json",
        output_markdown=output_dir / f"{args.prefix}_tree.md",
        output_mermaid=output_dir / f"{args.prefix}_tree.mmd",
    )

    metadata = vocabulary["metadata"]
    print(
        json.dumps(
            {
                "tool_desc": str(Path(args.tool_desc)),
                "intent_count": metadata["intent_count"],
                "operation_count": metadata["operation_count"],
                "tool_count": metadata["tool_count"],
                "outputs": {key: str(value) for key, value in paths.items()},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.print_tree:
        print()
        print(render_ascii_tree(vocabulary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
