from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


OPERATIONS = {
    "retrieval",
    "extraction",
    "transformation",
    "generation",
    "modification",
    "composition",
    "analysis",
}

ARTIFACT_TYPE_ALIASES = {
    "image": "image",
    "video": "video",
    "audio": "audio",
    "text": "text",
    "url": "url",
}

TRANSFORMATION_MARKERS = (
    "summarizer",
    "translator",
    "grammar checker",
    "simplifier",
    "expander",
    "paraphraser",
    "article spinner",
)

ANALYSIS_MARKERS = (
    "sentiment analysis",
    "keyword extractor",
    "topic generator",
)

MODIFICATION_MARKERS = (
    "noise reduction",
    "effects",
    "voice changer",
    "colorizer",
    "style transfer",
    "stabilizer",
    "speed changer",
    "synchronization",
    "voiceover",
)

EXTRACTION_TOOLS = {
    "image-to-text",
    "audio-to-text",
    "video-to-text",
    "video-to-audio",
    "video-to-image",
}

GENERATION_TOOLS = {
    "text-to-image",
    "text-to-video",
    "text-to-audio",
}

ACTION_BY_TOOL = {
    "image search": "search image",
    "image search (by image)": "search image by image",
    "video search": "search video",
    "text search": "search text",
    "image downloader": "download image",
    "video downloader": "download video",
    "audio downloader": "download audio",
    "text downloader": "download text",
    "url extractor": "extract url",
    "image-to-text": "extract text from image",
    "audio-to-text": "extract text from audio",
    "video-to-text": "extract text from video",
    "video-to-audio": "extract audio from video",
    "video-to-image": "extract image from video",
    "text-to-image": "generate image from text",
    "text-to-video": "generate video from text",
    "text-to-audio": "generate audio from text",
    "audio-to-image": "generate image from audio",
    "text summarizer": "summarize text",
    "text translator": "translate text",
    "text grammar checker": "check text grammar",
    "text simplifier": "simplify text",
    "text expander": "expand text",
    "text paraphraser": "paraphrase text",
    "article spinner": "rewrite article",
    "text sentiment analysis": "analyze text sentiment",
    "keyword extractor": "extract keywords",
    "topic generator": "generate topics from text",
    "audio noise reduction": "reduce audio noise",
    "audio effects": "apply audio effects",
    "voice changer": "change voice",
    "image colorizer": "colorize image",
    "image style transfer": "apply image style transfer",
    "video stabilizer": "stabilize video",
    "video speed changer": "change video speed",
    "video synchronization": "synchronize video with audio",
    "video voiceover": "add video voiceover",
    "audio splicer": "combine audio files",
    "image stitcher": "stitch images",
    "image-to-video": "compose video from images",
}


def normalize_artifact_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return ARTIFACT_TYPE_ALIASES.get(text, text)


def normalize_artifact_types(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = [value]
    artifacts: List[str] = []
    for item in raw_items:
        normalized = normalize_artifact_type(item)
        if normalized:
            artifacts.append(normalized)
    return artifacts


def _normalized_tool_name(tool: str) -> str:
    return " ".join(str(tool or "").strip().lower().split())


def _hyphenated_tool_name(tool: str) -> str:
    return _normalized_tool_name(tool).replace(" ", "-")


def _contains_any(text: str, markers: Iterable[str]) -> bool:
    return any(marker in text for marker in markers)


def _parse_artifact_conversion(tool: str) -> Tuple[str, str]:
    normalized = _hyphenated_tool_name(tool)
    match = re.fullmatch(r"([a-z]+)-to-([a-z]+)", normalized)
    if not match:
        return "", ""
    return normalize_artifact_type(match.group(1)), normalize_artifact_type(match.group(2))


def _infer_operation_from_desc(desc: str) -> str:
    text = str(desc or "").strip().lower()
    if not text:
        return "transformation"
    if re.search(r"\b(search\w*|download\w*|retriev\w*|fetch\w*|url)\b", text):
        return "retrieval"
    if re.search(r"\b(extract\w*|transcrib\w*|ocr|separat\w*)\b", text):
        return "extraction"
    if re.search(r"\b(generat\w*|creat\w*|produc\w*)\b", text):
        return "generation"
    if re.search(r"\b(summariz|translate|rewrite|simpl|expand|paraphrase|grammar)\b", text):
        return "transformation"
    if re.search(r"\b(analy|sentiment|keyword|topic)\b", text):
        return "analysis"
    if re.search(r"\b(modif|apply|reduce|add|adjust|stabiliz|synchroniz|color|style|effect|voice)\b", text):
        return "modification"
    if re.search(r"\b(combine|merge|splice|stitch|slideshow|compose)\b", text):
        return "composition"
    return "transformation"


def infer_operation(
    tool: str,
    desc: str = "",
    input_artifacts: List[str] | None = None,
    output_artifacts: List[str] | None = None,
) -> Tuple[str, bool]:
    del input_artifacts, output_artifacts
    name = _normalized_tool_name(tool)
    hyphen_name = _hyphenated_tool_name(tool)

    if "search" in name or "downloader" in name or name == "url extractor":
        return "retrieval", False
    if "splicer" in name or "stitcher" in name or hyphen_name == "image-to-video":
        return "composition", False
    if hyphen_name in GENERATION_TOOLS:
        return "generation", False
    if hyphen_name in EXTRACTION_TOOLS:
        return "extraction", False
    if _contains_any(name, TRANSFORMATION_MARKERS):
        return "transformation", False
    if _contains_any(name, ANALYSIS_MARKERS):
        return "analysis", False
    if _contains_any(name, MODIFICATION_MARKERS):
        return "modification", False

    operation = _infer_operation_from_desc(desc)
    if operation not in OPERATIONS:
        operation = "transformation"
    return operation, True


def infer_action(
    tool: str,
    operation: str,
    input_artifacts: List[str] | None = None,
    output_artifacts: List[str] | None = None,
    desc: str = "",
) -> Tuple[str, bool]:
    del desc
    name = _normalized_tool_name(tool)
    if name in ACTION_BY_TOOL:
        return ACTION_BY_TOOL[name], False

    inputs = input_artifacts or []
    outputs = output_artifacts or []
    input_artifact = inputs[0] if inputs else "artifact"
    output_artifact = outputs[0] if outputs else "artifact"
    source_artifact, target_artifact = _parse_artifact_conversion(tool)
    if source_artifact and target_artifact:
        input_artifact = source_artifact
        output_artifact = target_artifact

    if "downloader" in name:
        return f"download {output_artifact}", False
    if "search" in name:
        return f"search {output_artifact}", False
    if name == "url extractor":
        return "extract url", False

    if operation == "extraction":
        return f"extract {output_artifact} from {input_artifact}", False
    if operation == "generation":
        return f"generate {output_artifact} from {input_artifact}", False
    if operation == "composition":
        if input_artifact == output_artifact:
            return f"combine {input_artifact} files", False
        return f"compose {output_artifact} from {input_artifact}", False
    if operation == "analysis":
        return f"analyze {input_artifact}", True
    if operation == "modification":
        return f"modify {input_artifact}", True
    if operation == "transformation":
        return f"transform {input_artifact}", True
    if operation == "retrieval":
        return f"retrieve {output_artifact}", True
    return f"process {input_artifact} to {output_artifact}", True


def _artifact_transition_distribution(records: List[Dict[str, Any]]) -> Dict[str, int]:
    counter: Counter[str] = Counter()
    for record in records:
        inputs = sorted(set(record.get("input_artifacts") or [])) or ["none"]
        outputs = sorted(set(record.get("output_artifacts") or [])) or ["none"]
        for input_artifact in inputs:
            for output_artifact in outputs:
                counter[f"{input_artifact}->{output_artifact}"] += 1
    return dict(sorted(counter.items()))


def build_records(tool_desc: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_nodes = tool_desc.get("nodes", [])
    if not isinstance(raw_nodes, list):
        raw_nodes = []

    records: List[Dict[str, Any]] = []
    for idx, node in enumerate(raw_nodes):
        if not isinstance(node, dict):
            node = {}
        tool = str(node.get("id") or f"unknown_tool_{idx}").strip()
        desc = str(node.get("desc") or "").strip()
        input_artifacts = normalize_artifact_types(node.get("input-type"))
        output_artifacts = normalize_artifact_types(node.get("output-type"))
        operation, operation_needs_review = infer_operation(
            tool=tool,
            desc=desc,
            input_artifacts=input_artifacts,
            output_artifacts=output_artifacts,
        )
        action, action_needs_review = infer_action(
            tool=tool,
            operation=operation,
            input_artifacts=input_artifacts,
            output_artifacts=output_artifacts,
            desc=desc,
        )
        record = {
            "tool": tool,
            "desc": desc,
            "input_artifacts": input_artifacts,
            "output_artifacts": output_artifacts,
            "operation": operation,
            "action": action,
        }
        if operation_needs_review or action_needs_review:
            record["needs_review"] = True
        records.append(record)
    return records


def build_stats(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    operation_distribution = Counter(str(record.get("operation", "")) for record in records)
    review_tools = [
        str(record.get("tool", ""))
        for record in records
        if bool(record.get("needs_review", False))
    ]
    return {
        "total_tools": len(records),
        "operation_distribution": dict(sorted(operation_distribution.items())),
        "artifact_transition_distribution": _artifact_transition_distribution(records),
        "needs_review_count": len(review_tools),
        "needs_review_tools": review_tools,
    }


def write_jsonl(path: Path, records: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _jsonl_output_path(output_path: Path) -> Path:
    if output_path.suffix.lower() == ".json":
        return output_path.with_suffix(".jsonl")
    return output_path.parent / f"{output_path.name}.jsonl"


def build_action_ontology(input_path: Path, output_path: Path) -> Dict[str, Any]:
    tool_desc = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(tool_desc, dict):
        tool_desc = {}
    records = build_records(tool_desc)
    payload = {
        "source": str(input_path),
        "stats": build_stats(records),
        "tools": records,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_jsonl(_jsonl_output_path(output_path), records)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Build action ontology from TaskBench tool_desc.json.")
    parser.add_argument("--input", required=True, type=Path, help="Path to tool_desc.json.")
    parser.add_argument("--output", required=True, type=Path, help="Path to action_ontology.json.")
    args = parser.parse_args()

    payload = build_action_ontology(args.input, args.output)
    jsonl_path = _jsonl_output_path(args.output)
    stats = payload["stats"]
    print(f"[DONE] wrote {args.output}")
    print(f"[DONE] wrote {jsonl_path}")
    print(f"[STATS] total_tools={stats['total_tools']}")
    print(f"[STATS] operation_distribution={json.dumps(stats['operation_distribution'], ensure_ascii=False)}")
    print(
        "[STATS] artifact_transition_distribution="
        f"{json.dumps(stats['artifact_transition_distribution'], ensure_ascii=False)}"
    )
    if stats["needs_review_count"]:
        print(f"[WARN] needs_review_count={stats['needs_review_count']}")


if __name__ == "__main__":
    main()
