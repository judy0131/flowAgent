# -*- coding: utf-8 -*-
"""Unified Intent Auditor -> Replan -> Edge-only Graph Repair pipeline.

This is the main experiment entrypoint for the current workflow:

1. Read an existing intent coverage table.
2. Audit the original workflow against intent coverage hints.
3. Replan rows with missing tool/intent coverage.
4. Repair workflow edges and node references with the tool graph.
5. Write final predictions and optional N-F1/E-F1/NED metrics.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import re
import sys
import threading
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence, Tuple
import zipfile
import xml.etree.ElementTree as ET


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.memory_guided_workflow.llm_client import OpenAICompatibleLLMClient
from agent.memory_guided_workflow.utils import (
    extract_json_object,
    get_keep_original_graph_repair_config,
    get_replan_prompt_rules,
    get_replan_prompt_variables,
    get_semantic_tool_families,
    get_tool_desc_intent_path,
    load_dataset_runtime_config,
)


class bg:
    _XML_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

    @staticmethod
    def read_xlsx_records(path: Path) -> List[Dict[str, Any]]:
        rows = bg._read_xlsx_rows(path)
        if not rows:
            return []
        header = rows[0]
        records: List[Dict[str, Any]] = []
        for row in rows[1:]:
            record = {header[index]: row[index] if index < len(row) else "" for index in range(len(header))}
            if any(str(value or "").strip() for value in record.values()):
                records.append(record)
        return records

    @staticmethod
    def _read_xlsx_rows(path: Path) -> List[List[str]]:
        with zipfile.ZipFile(path) as archive:
            shared_strings = bg._read_shared_strings(archive)
            sheet_name = bg._first_worksheet_name(archive)
            sheet = ET.fromstring(archive.read(sheet_name))

        rows: List[List[str]] = []
        for row in sheet.findall(".//a:sheetData/a:row", bg._XML_NS):
            values: List[str] = []
            for cell in row.findall("a:c", bg._XML_NS):
                index = bg._excel_column_index(cell.attrib.get("r", "A1"))
                while len(values) <= index:
                    values.append("")
                values[index] = bg._read_cell_text(cell, shared_strings)
            rows.append(values)
        return rows

    @staticmethod
    def _first_worksheet_name(archive: zipfile.ZipFile) -> str:
        for name in archive.namelist():
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
                return name
        raise ValueError("xlsx has no worksheet xml")

    @staticmethod
    def _read_shared_strings(archive: zipfile.ZipFile) -> List[str]:
        if "xl/sharedStrings.xml" not in archive.namelist():
            return []
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        shared: List[str] = []
        for item in root.findall("a:si", bg._XML_NS):
            shared.append("".join(text.text or "" for text in item.findall(".//a:t", bg._XML_NS)))
        return shared

    @staticmethod
    def _read_cell_text(cell: ET.Element, shared_strings: List[str]) -> str:
        cell_type = cell.attrib.get("t")
        if cell_type == "s":
            value = cell.find("a:v", bg._XML_NS)
            if value is None or value.text is None:
                return ""
            index = bg._coerce_int(value.text)
            if index is None or index < 0 or index >= len(shared_strings):
                return ""
            return shared_strings[index]
        if cell_type == "inlineStr":
            return "".join(text.text or "" for text in cell.findall(".//a:t", bg._XML_NS))
        value = cell.find("a:v", bg._XML_NS)
        return value.text if value is not None and value.text is not None else ""

    @staticmethod
    def _excel_column_index(cell_ref: str) -> int:
        letters = "".join(char for char in str(cell_ref) if char.isalpha())
        result = 0
        for char in letters:
            result = result * 26 + ord(char.upper()) - 64
        return max(result - 1, 0)

    @staticmethod
    def write_xlsx_rows(path: Path, rows: List[List[Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        width_count = max(max((len(row) for row in rows), default=0), 1)
        dim = f"A1:{bg._column_name(width_count - 1)}{max(len(rows), 1)}"
        widths = [18, 70, 42, 42, 42, 42, 42, 42, 32, 32, 52, 90, 90, 90, 90, 90, 90]
        if len(widths) < width_count:
            widths.extend([32] * (width_count - len(widths)))
        cols = "".join(
            f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
            for index, width in enumerate(widths[:width_count], start=1)
        )
        row_xml: List[str] = []
        for row_index, row in enumerate(rows, start=1):
            style = 1 if row_index == 1 else 2
            height = 24 if row_index == 1 else 90
            cells = "".join(
                bg._cell_xml(value, row_index, col_index, style)
                for col_index, value in enumerate(row)
            )
            row_xml.append(f'<row r="{row_index}" ht="{height}" customHeight="1">{cells}</row>')
        sheet_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <dimension ref="{dim}"/>
  <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <cols>{cols}</cols>
  <sheetData>{''.join(row_xml)}</sheetData>
  <autoFilter ref="{dim}"/>
</worksheet>'''
        bg._write_minimal_xlsx_package(path, sheet_xml)

    @staticmethod
    def _cell_xml(value: Any, row_index: int, col_index: int, style: int) -> str:
        ref = f"{bg._column_name(col_index)}{row_index}"
        text = "" if value is None else str(value)
        text = "".join(char for char in text if char in "\t\n\r" or ord(char) >= 32)
        escaped = html.escape(text, quote=False)
        return (
            f'<c r="{ref}" t="inlineStr" s="{style}">'
            f'<is><t xml:space="preserve">{escaped}</t></is></c>'
        )

    @staticmethod
    def _column_name(index: int) -> str:
        result = ""
        index += 1
        while index:
            index, remainder = divmod(index - 1, 26)
            result = chr(65 + remainder) + result
        return result

    @staticmethod
    def _write_minimal_xlsx_package(path: Path, sheet_xml: str) -> None:
        styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="3">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''
        content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>'''
        root_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''
        workbook_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="intent_replan" sheetId="1" r:id="rId1"/></sheets>
</workbook>'''
        workbook_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        core_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:creator>MIWP</dc:creator><cp:lastModifiedBy>MIWP</cp:lastModifiedBy><dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>
</cp:coreProperties>'''
        app_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Python stdlib</Application>
</Properties>'''
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", content_types.encode("utf-8"))
            archive.writestr("_rels/.rels", root_rels.encode("utf-8"))
            archive.writestr("xl/workbook.xml", workbook_xml.encode("utf-8"))
            archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels.encode("utf-8"))
            archive.writestr("xl/worksheets/sheet1.xml", sheet_xml.encode("utf-8"))
            archive.writestr("xl/styles.xml", styles_xml.encode("utf-8"))
            archive.writestr("docProps/core.xml", core_xml.encode("utf-8"))
            archive.writestr("docProps/app.xml", app_xml.encode("utf-8"))

    @staticmethod
    def read_jsonl_records(path: Path) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        with path.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    print(f"[WARN] skip invalid JSONL line {line_number}: {exc}")
                    continue
                if isinstance(payload, dict):
                    rows.append(payload)
        return rows

    @staticmethod
    def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    @staticmethod
    def load_transition_index(raw_path: Any) -> Dict[Tuple[str, str], Any]:
        if not raw_path:
            return {}
        path = resolve_path(raw_path)
        if not path.exists():
            raise FileNotFoundError(f"transition graph not found: {path}")
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        index: Dict[Tuple[str, str], Any] = {}
        if not isinstance(payload, dict):
            return index
        for edge in bg._coerce_list(payload.get("edges")) + bg._coerce_list(payload.get("links")):
            if isinstance(edge, dict):
                bg._add_transition_edge(index, edge)
        adjacency = payload.get("adjacency")
        if isinstance(adjacency, dict):
            for source, raw_targets in adjacency.items():
                if isinstance(raw_targets, dict):
                    for target, value in raw_targets.items():
                        index[(bg._normalize_tool_key(source), bg._normalize_tool_key(target))] = bg._extract_probability(value)
                elif isinstance(raw_targets, list):
                    for item in raw_targets:
                        if not isinstance(item, dict):
                            continue
                        target = item.get("target_tool_id") or item.get("target") or item.get("target_tool")
                        if target:
                            index[(bg._normalize_tool_key(source), bg._normalize_tool_key(target))] = bg._extract_probability(item)
        return index

    @staticmethod
    def _add_transition_edge(index: Dict[Tuple[str, str], Any], edge: Dict[str, Any]) -> None:
        source = edge.get("source_tool_id") or edge.get("source") or edge.get("from")
        target = edge.get("target_tool_id") or edge.get("target") or edge.get("to")
        if source and target:
            index[(bg._normalize_tool_key(source), bg._normalize_tool_key(target))] = bg._extract_probability(edge)

    @staticmethod
    def get_transition_probability(index: Dict[Tuple[str, str], Any], source_tool: Any, target_tool: Any) -> Any:
        if not index:
            return None
        return index.get((bg._normalize_tool_key(source_tool), bg._normalize_tool_key(target_tool)))

    @staticmethod
    def type_compatible(source_output: Any, target_input: Any) -> bool | str:
        target_types = bg.normalize_type_set(target_input)
        if not target_types:
            return "unknown"
        source_types = bg.normalize_type_set(source_output)
        if not source_types:
            return "unknown"
        if "any" in target_types or "*" in target_types:
            return True
        return bool(source_types & target_types)

    @staticmethod
    def normalize_type_set(value: Any) -> set[str]:
        return {bg._normalize_type_name(item) for item in bg._coerce_list(value) if bg._normalize_type_name(item)}

    @staticmethod
    def normalize_type_list(value: Any) -> List[str]:
        return [bg._normalize_type_name(item) for item in bg._coerce_list(value) if bg._normalize_type_name(item)]

    @staticmethod
    def consume_first_matching_slot(remaining: List[str], candidate_types: Any) -> str:
        candidates = bg.normalize_type_set(candidate_types)
        for index, slot in enumerate(list(remaining)):
            if slot in candidates or "any" in candidates or "*" in candidates:
                remaining.pop(index)
                return slot
        return ""

    @staticmethod
    def infer_literal_argument_types(argument: Any) -> List[str]:
        text = str(argument or "").strip().lower()
        if not text:
            return []
        inferred: List[str] = []
        if any(f".{ext}" in text for ext in ["jpg", "jpeg", "png", "gif", "bmp", "tiff", "svg", "ico"]):
            inferred.append("image")
        elif any(f".{ext}" in text for ext in ["mp3", "wav", "wma", "ogg", "aac", "flac", "aiff", "au"]):
            inferred.append("audio")
        elif any(f".{ext}" in text for ext in ["mp4", "avi", "mov", "flv", "wmv", "mkv", "webm", "m4v", "mpg", "mpeg"]):
            inferred.append("video")
        if re.match(r"^[a-z][a-z0-9+.-]*://", text):
            inferred.append("url")
        if not inferred:
            inferred.append("text")
        return dedupe_preserve_order(inferred)

    @staticmethod
    def _normalize_type_name(value: Any) -> str:
        return str(value or "").strip().lower().replace("_", "-")

    @staticmethod
    def _normalize_tool_key(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip().lower().replace("_", " "))

    @staticmethod
    def _extract_probability(value: Any) -> Any:
        if isinstance(value, (int, float)):
            return value
        if not isinstance(value, dict):
            return None
        for key in ["transition_probability", "probability", "prob", "weight", "score"]:
            number = bg._coerce_float(value.get(key))
            if number is not None:
                return number
        return None

    @staticmethod
    def _coerce_list(value: Any) -> List[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        return [value]

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        text = str(value or "").strip()
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        return None

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None


DEFAULT_COVERAGE_XLSX = (
    "taskbench/data_multimedia/"
    "data_multimedia_gpt55_intent_coverage_table.xlsx"
)
DEFAULT_PREDICTION_FILE = (
    "taskbench/data_multimedia/predictions_use_demos_2_reformat_by_self/"
    "qwen3-14b_20260527.json"
)
DEFAULT_TOOL_DESC = "taskbench/data_multimedia/tool_desc_intent.json"
DEFAULT_OUTPUT_JSON = (
    "taskbench/data_multimedia/replan_reformat_by_self/"
    "data_multimedia_qwen3-14b_20260527_intent_guided_replan.json"
)
DEFAULT_OUTPUT_XLSX = (
    "taskbench/data_multimedia/replan_reformat_by_self/"
    "data_multimedia_qwen3-14b_20260527_intent_guided_replan.xlsx"
)
DEFAULT_EVAL_PREDICTION_DIR = "taskbench/data_multimedia/replan_reformat_by_self"
DEFAULT_REPLAN_OUTPUT_DIR_NAME = "replan_reformat_by_self"


def resolve_tool_desc_intent_path_arg(
    raw_tool_desc: Any,
    dataset_config: Mapping[str, Any] | None,
) -> str:
    explicit = str(raw_tool_desc or "").strip()
    if explicit:
        return explicit
    return get_tool_desc_intent_path(dataset_config, default=DEFAULT_TOOL_DESC)


PLANNER_CALL_FAILED_DECISION = "PLANNER_CALL_FAILED"
LEGACY_PLANNER_CALL_FAILED_DECISIONS = {"QWEN_CALL_FAILED", "ERROR"}
PLANNER_CALL_ERROR_PREFIX = "planner_call_error:"
LEGACY_PLANNER_CALL_ERROR_PREFIXES = ("qwen_call_error:",)

OUTPUT_COLUMNS = [
    "id",
    "type",
    "user_request",
    "model_tool_original",
    "intent_tool_hint",
    "intent_hint",
    "covered_intent_tool_hint",
    "missing_intent_tool_hint",
    "covered_intent_hint",
    "missing_intent_hint",
    "replan_variant",
    "graph_candidate_count",
    "replan_decision",
    "result_source",
    "result_source_generic",
    "result",
    "previous_workflow",
    "replanned_workflow",
    "dag_replan_result_source",
    "dag_replan_result_source_generic",
    "dag_replan_result",
    "dag_replan_selection_reason",
    "dag_validation_status",
    "dag_validation_errors",
    "dag_validation_warnings",
    "planner_dag_self_repair_status",
    "planner_dag_self_repair_decision",
    "planner_dag_self_repair_result",
    "raw_planner_dag_self_repair_output",
    "dag_repair_status",
    "dag_repair_operations",
    "keep_original_verifier",
    "coverage_assessment",
    "hint_assessment",
    "selection_reason",
    "change_summary",
    "raw_planner_output",
    "agent_trace",
    "warnings",
]
REQUIRED_COVERAGE_COLUMNS = [
    "id",
    "user_request",
    "intent",
    "intent tool",
    "model tool",
    "coverage_warnings",
]
STRICT_JSON_SYSTEM_PROMPT = (
    "Return one valid compact JSON object only. Do not use markdown or code fences. "
    "Do not output text before or after the JSON. Do not escape underscores."
)
STRICT_JSON_OUTPUT_INSTRUCTION = (
    "Return one compact valid JSON object only: no markdown, no code fence, "
    "no text before/after JSON, no escaped underscores, close all arrays/objects, "
    "evidence/reason <= 80 chars.\n"
)
_THREAD_LOCAL = threading.local()
DEFAULT_SEMANTIC_TOOL_FAMILIES = {
    "text_rewrite": [
        "Article Spinner",
        "Text Paraphraser",
        "Text Simplifier",
        "Text Summarizer",
        "Text Expander",
    ],
    "image_composition": [
        "Image Stitcher",
        "Image Style Transfer",
    ],
    "audio_editing": [
        "Audio Effects",
        "Voice Changer",
        "Audio Noise Reduction",
        "Audio Splicer",
    ],
}
SEMANTIC_TOOL_FAMILIES = DEFAULT_SEMANTIC_TOOL_FAMILIES
REPLAN_VARIANT_BASELINE = "replan"
REPLAN_VARIANT_WITH_GRAPH = "replan_with_graph"
REPLAN_VARIANTS = (REPLAN_VARIANT_BASELINE, REPLAN_VARIANT_WITH_GRAPH)
DEFAULT_REPLAN_VARIANT = REPLAN_VARIANT_BASELINE
DEFAULT_GRAPH_CONTEXT_TOP_K_PER_SOURCE = 3
DEFAULT_GRAPH_CONTEXT_MAX_CANDIDATES = 30
SEMANTIC_TOOL_FAMILY_BY_KEY = {
    re.sub(r"[^a-z0-9]+", "", tool.lower()): family
    for family, tools in SEMANTIC_TOOL_FAMILIES.items()
    for tool in tools
}


def build_semantic_tool_family_index(
    semantic_tool_families: Mapping[str, Iterable[Any]] | None,
) -> Dict[str, str]:
    return {
        normalize_key(tool): str(family)
        for family, tools in (semantic_tool_families or {}).items()
        for tool in tools
        if normalize_key(tool)
    }


def resolve_semantic_tool_families(
    semantic_tool_families: Mapping[str, Any] | None = None,
) -> Dict[str, List[str]]:
    if semantic_tool_families is None:
        return get_semantic_tool_families(None, default=SEMANTIC_TOOL_FAMILIES)
    return get_semantic_tool_families({"semantic_tool_families": semantic_tool_families})


def replan_main() -> int:
    args = parse_replan_args()
    output_json = ensure_json_output_path(resolve_path(args.output_json))
    output_xlsx = resolve_path(args.output_xlsx)
    eval_json = resolve_eval_json_path(args, output_json)
    prepare_output_files(output_json, output_xlsx, resume=args.resume, overwrite=args.overwrite, eval_json=eval_json)
    output_lock = acquire_output_lock(output_json)
    try:
        return run_replan(args, output_json, output_xlsx, eval_json)
    finally:
        release_output_lock(output_lock)


def run_replan(args: argparse.Namespace, output_json: Path, output_xlsx: Path, eval_json: Path | None = None) -> int:
    coverage_path = resolve_path(args.coverage_xlsx)
    coverage_rows = normalize_coverage_rows(bg.read_xlsx_records(coverage_path))
    validate_coverage_table_columns(coverage_rows, coverage_path)

    dataset_config = load_dataset_runtime_config(
        resolve_path(args.dataset_config) if getattr(args, "dataset_config", None) else None
    )
    dataset_dir = infer_dataset_dir(args, dataset_config)
    args.replan_dependency_type = resolve_replan_dependency_type(args, dataset_dir)
    prediction_by_id = load_prediction_results(resolve_path(get_prediction_file_arg(args)))
    coverage_rows = align_coverage_rows_to_predictions(coverage_rows, prediction_by_id)
    if args.limit and args.limit > 0:
        coverage_rows = coverage_rows[: args.limit]
    tool_desc_path = resolve_tool_desc_intent_path_arg(getattr(args, "tool_desc", None), dataset_config)
    tool_desc = load_tool_desc(resolve_path(tool_desc_path), max_tools=args.max_tools)
    tool_catalog = build_tool_catalog(tool_desc)
    transition_index = bg.load_transition_index(args.tool_graph) if getattr(args, "tool_graph", None) else {}
    semantic_tool_families = get_semantic_tool_families(dataset_config, default=SEMANTIC_TOOL_FAMILIES)

    active_ids = {get_case_id(row, row_index) for row_index, row in enumerate(coverage_rows, start=1)}
    resume_results = filter_resume_results_for_active_ids(
        load_resume_results(output_json) if args.resume else [],
        active_ids,
    )
    completed_ids = {str(row.get("id") or "").strip() for row in resume_results if str(row.get("id") or "").strip()}
    pending: List[Tuple[int, Dict[str, Any]]] = []
    for row_index, row in enumerate(coverage_rows, start=1):
        case_id = get_case_id(row, row_index)
        if args.resume and case_id in completed_ids:
            print(f"[{row_index}/{len(coverage_rows)}] skip id={case_id} (resume)")
            continue
        pending.append((row_index, row))

    results = list(resume_results)
    workers = max(1, int(args.workers or 1))
    if workers == 1:
        for row_index, row in pending:
            case_id = get_case_id(row, row_index)
            print(f"[{row_index}/{len(coverage_rows)}] planner replan id={case_id}")
            result = run_one_row(
                row,
                row_index,
                prediction_by_id,
                tool_desc,
                args,
                tool_catalog,
                transition_index,
                dataset_config,
                semantic_tool_families,
            )
            bg.append_jsonl(output_json, result)
            results.append(result)
    else:
        print(f"parallel_workers={workers}, pending_cases={len(pending)}")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    run_one_row,
                    row,
                    row_index,
                    prediction_by_id,
                    tool_desc,
                    args,
                    tool_catalog,
                    transition_index,
                    dataset_config,
                    semantic_tool_families,
                ): (
                    row_index,
                    get_case_id(row, row_index),
                )
                for row_index, row in pending
            }
            for future in as_completed(futures):
                row_index, case_id = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001 - preserve row-level output.
                    result = error_row(case_id, "", {}, [f"worker_error: {type(exc).__name__}: {exc}"])
                bg.append_jsonl(output_json, result)
                results.append(result)
                print(f"[{row_index}/{len(coverage_rows)}] done id={case_id}")

    results = latest_rows_by_id(results)
    write_jsonl(output_json, results)
    write_xlsx(output_xlsx, results)
    if eval_json is not None:
        write_taskbench_eval_json(eval_json, results)
    print(f"saved_json={output_json}")
    print(f"saved_xlsx={output_xlsx}")
    if eval_json is not None:
        print(f"saved_eval_json={eval_json}")
    failed_count = sum(1 for row in results if is_retryable_replan_failure(row))
    if failed_count:
        print(
            f"retryable_failed_rows={failed_count}; rerun with --resume after fixing planner connectivity.",
            file=sys.stderr,
            flush=True,
        )
        return 1
    return 0


def parse_replan_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Use intent coverage hints to ask a planner LLM to replan previous workflows."
    )
    parser.add_argument("--coverage-xlsx", default=DEFAULT_COVERAGE_XLSX)
    parser.add_argument(
        "--prediction-file",
        default=DEFAULT_PREDICTION_FILE,
        help="Prediction file keyed by id.",
    )
    parser.add_argument(
        "--tool-desc",
        default=None,
        help=(
            "Path to TaskBench-style tool_desc_intent.json. "
            "Defaults to dataset_config.tool_desc_intent, then the dataset's tool_desc_intent.json/tool_desc.json."
        ),
    )
    parser.add_argument("--output-json", dest="output_json", default=DEFAULT_OUTPUT_JSON)
    parser.add_argument("--output-xlsx", default=DEFAULT_OUTPUT_XLSX)
    parser.add_argument(
        "--replan-variant",
        choices=REPLAN_VARIANTS,
        default=DEFAULT_REPLAN_VARIANT,
        help="Defaults to replan. The planner prompt includes --tool-graph when available.",
    )
    parser.add_argument("--graph-context-top-k-per-source", type=int, default=DEFAULT_GRAPH_CONTEXT_TOP_K_PER_SOURCE)
    parser.add_argument("--graph-context-max-candidates", type=int, default=DEFAULT_GRAPH_CONTEXT_MAX_CANDIDATES)
    parser.add_argument(
        "--temporal-chain-prior",
        action="store_true",
        help="Temporal-only: add adjacent task-node chain links as an edge prior.",
    )
    parser.add_argument(
        "--temporal-edge-only-replan",
        action="store_true",
        help="Temporal-only: ask the planner to repair task_links without changing task_nodes or arguments.",
    )
    parser.add_argument(
        "--temporal-edge-only-scope",
        choices=("high-risk", "all"),
        default="high-risk",
        help="Temporal edge-only replan scope. high-risk runs only on empty/sparse/invalid links.",
    )
    parser.add_argument(
        "--eval-json",
        default=None,
        help=(
            "Optional TaskBench evaluate-ready JSON path. Disabled by default because "
            "--output-json already contains id/result."
        ),
    )
    parser.add_argument(
        "--eval-prediction-dir",
        default=DEFAULT_EVAL_PREDICTION_DIR,
        help="Default directory for the evaluate-ready JSON when --eval-json is not set.",
    )
    parser.add_argument(
        "--eval-llm-name",
        default=None,
        help="LLM name used when --eval-json is set.",
    )
    parser.add_argument("--no-eval-json", action="store_true", help="Disable evaluate-ready JSON output.")
    parser.add_argument(
        "--planner-llm-config",
        default="configs/qwen.json",
        help="OpenAI-compatible planner LLM config.",
    )
    parser.add_argument(
        "--planner-llm-profile",
        default=None,
        help="Optional planner LLM profile.",
    )
    parser.add_argument(
        "--planner-timeout",
        type=float,
        default=None,
    )
    parser.add_argument(
        "--planner-max-retries",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--dataset-config",
        default=None,
        help="Optional JSON config for dataset-specific prompt variables and rules.",
    )
    parser.add_argument(
        "--replan-dependency-type",
        choices=("auto", "resource", "temporal"),
        default="auto",
        help="Workflow dependency mode for replan prompts and DAG repair. auto infers from tool_desc.json.",
    )
    parser.add_argument("--tool-graph", default=None, help="Optional tool transition graph for DAG edge validation.")
    parser.add_argument(
        "--require-tool-graph-edge",
        action="store_true",
        help="Reject candidate DAG edges that are absent from --tool-graph.",
    )
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--overwrite", action="store_true", help="Delete existing output files before running.")
    parser.add_argument("--dry-run", action="store_true", help="Build rows without calling the planner LLM.")
    parser.add_argument(
        "--dag-self-repair-rounds",
        type=int,
        default=1,
        help="Planner repair attempts after a candidate DAG fails validation. Use 0 to disable.",
    )
    parser.add_argument("--max-tools", type=int, default=80)
    return parser.parse_args()


def prepare_output_files(
    output_json: Path,
    output_xlsx: Path,
    resume: bool,
    overwrite: bool,
    eval_json: Path | None = None,
) -> None:
    if resume and overwrite:
        raise ValueError("--resume and --overwrite cannot be used together")
    output_json.parent.mkdir(parents=True, exist_ok=True)
    if overwrite:
        output_json.unlink(missing_ok=True)
        output_xlsx.unlink(missing_ok=True)
        if eval_json is not None:
            eval_json.unlink(missing_ok=True)
        return
    if output_json.exists() and not resume:
        raise FileExistsError(
            f"output json already exists: {output_json}\n"
            "Use --resume to continue it, --overwrite to replace it, or choose a new output path."
        )


def validate_coverage_table_columns(rows: Sequence[Mapping[str, Any]], path: Path) -> None:
    if not rows:
        return
    available = {str(column).strip().lower() for column in rows[0]}
    missing = [column for column in REQUIRED_COVERAGE_COLUMNS if column.lower() not in available]
    if missing:
        raise ValueError(
            "coverage table must use the new intent coverage schema. "
            f"missing columns={missing}; path={path}"
        )


def normalize_coverage_rows(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [normalize_coverage_row(row) for row in rows if isinstance(row, Mapping)]


def normalize_coverage_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    normalized = dict(row)
    if not get_first(normalized, "intent tool"):
        intent_tool = get_first(
            normalized,
            "gpt4 tool",
            "gpt tool",
            "gpt54mini tool",
            "gpt-5.4-mini tool",
            "gpt5.4mini tool",
        )
        normalized["intent tool"] = intent_tool
    if not get_first(normalized, "intent"):
        intent = get_first(
            normalized,
            "gpt4 intent",
            "gpt intent",
            "gpt54mini intent",
            "gpt-5.4-mini intent",
            "gpt5.4mini intent",
        )
        normalized["intent"] = intent
    return normalized


def with_current_prediction_tool_summary(
    row: Mapping[str, Any],
    previous_workflow: Mapping[str, Any],
) -> Dict[str, Any]:
    normalized = dict(row)
    current_tools = format_hint_terms(workflow_tool_names(previous_workflow))
    if current_tools:
        normalized["model tool"] = current_tools
    return normalized


def align_coverage_rows_to_predictions(
    coverage_rows: Sequence[Mapping[str, Any]],
    prediction_by_id: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    prediction_ids = [str(case_id).strip() for case_id in prediction_by_id if str(case_id).strip()]
    prediction_id_set = set(prediction_ids)
    if not prediction_ids:
        return [dict(row) for row in coverage_rows]
    filtered: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for row_index, row in enumerate(coverage_rows, start=1):
        case_id = get_case_id(row, row_index)
        if case_id in prediction_id_set and case_id not in seen_ids:
            filtered.append(dict(row))
            seen_ids.add(case_id)
    missing_ids = [case_id for case_id in prediction_ids if case_id not in seen_ids]
    for case_id in missing_ids:
        prediction = prediction_by_id.get(case_id, {})
        filtered.append(
            {
                "id": case_id,
                "user_request": get_first(prediction, "user_request", "request"),
                "intent": "",
                "intent tool": "",
                "model tool": "",
                "coverage_warnings": "MISSING_COVERAGE_ROW",
            }
        )
    print(
        "coverage_scope=aligned-with-prediction "
        f"coverage_rows={len(coverage_rows)} prediction_rows={len(prediction_ids)} "
        f"matched_rows={len(seen_ids)} missing_coverage_rows={len(missing_ids)} "
        f"selected_rows={len(filtered)}"
    )
    if not filtered:
        raise ValueError("no overlapping ids between coverage table and prediction file")
    return filtered


def filter_resume_results_for_active_ids(
    resume_results: Sequence[Mapping[str, Any]],
    active_ids: set[str],
) -> List[Dict[str, Any]]:
    if not active_ids:
        return [dict(row) for row in resume_results]
    filtered = [
        dict(row)
        for row in resume_results
        if str(row.get("id") or "").strip() in active_ids
    ]
    skipped = len(resume_results) - len(filtered)
    if skipped:
        print(f"resume_scope=active_prediction_ids skipped_stale_resume_rows={skipped}")
    return filtered


def load_resume_results(output_json: Path) -> List[Dict[str, Any]]:
    if not output_json.exists():
        return []
    latest = latest_rows_by_id(bg.read_jsonl_records(output_json))
    return [row for row in latest if not is_retryable_replan_failure(row)]


def latest_rows_by_id(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    anonymous_rows: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        materialized = dict(row)
        case_id = str(materialized.get("id") or "").strip()
        if not case_id:
            anonymous_rows.append(materialized)
            continue
        latest[case_id] = materialized
    return anonymous_rows + list(latest.values())


def is_retryable_replan_failure(row: Mapping[str, Any]) -> bool:
    decision = str(row.get("replan_decision") or "").strip().upper()
    if decision in {PLANNER_CALL_FAILED_DECISION, *LEGACY_PLANNER_CALL_FAILED_DECISIONS}:
        return True
    warnings = row.get("warnings")
    if isinstance(warnings, str):
        warning_values = [warnings]
    elif isinstance(warnings, (list, tuple, set)):
        warning_values = [str(warning) for warning in warnings]
    else:
        warning_values = []
    warning_text = "\n".join(warning_values)
    call_error_prefixes = (PLANNER_CALL_ERROR_PREFIX, *LEGACY_PLANNER_CALL_ERROR_PREFIXES)
    return any(prefix in warning_text for prefix in call_error_prefixes) or "worker_error:" in warning_text


def acquire_output_lock(output_json: Path) -> Path:
    lock_path = output_json.with_name(output_json.name + ".lock")
    payload = {
        "pid": os.getpid(),
        "argv": sys.argv,
        "output_json": str(output_json),
    }
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        detail = ""
        try:
            detail = lock_path.read_text(encoding="utf-8")
        except OSError:
            pass
        raise RuntimeError(
            f"another process appears to be writing this output: {output_json}\n"
            f"lock file: {lock_path}\n"
            f"{detail}".rstrip()
        ) from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return lock_path


def release_output_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def run_one_row(
    row: Mapping[str, Any],
    row_index: int,
    prediction_by_id: Mapping[str, Dict[str, Any]],
    tool_desc: List[Dict[str, Any]],
    args: argparse.Namespace,
    tool_catalog: Mapping[str, Dict[str, Any]] | None = None,
    transition_index: Mapping[Tuple[str, str], Any] | None = None,
    dataset_config: Mapping[str, Any] | None = None,
    semantic_tool_families: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    semantic_tool_families = (
        get_semantic_tool_families(dataset_config, default=SEMANTIC_TOOL_FAMILIES)
        if semantic_tool_families is None
        else resolve_semantic_tool_families(semantic_tool_families)
    )
    replan_variant = str(getattr(args, "replan_variant", DEFAULT_REPLAN_VARIANT) or DEFAULT_REPLAN_VARIANT)
    replan_dependency_type = normalize_dependency_type(getattr(args, "replan_dependency_type", "resource"))
    case_id = get_case_id(row, row_index)
    user_request = get_first(row, "user_request")
    previous_prediction = prediction_by_id.get(case_id, {})
    previous_workflow = normalize_workflow(previous_prediction.get("result", previous_prediction))
    row = with_current_prediction_tool_summary(row, previous_workflow)
    warnings: List[str] = []
    if not previous_workflow.get("task_nodes"):
        warnings.append("missing previous_workflow task_nodes")

    client_holder: Dict[str, OpenAICompatibleLLMClient] = {}

    def planner_client() -> OpenAICompatibleLLMClient:
        client = client_holder.get("client")
        if client is None:
            client = get_thread_client(
                get_planner_llm_config_arg(args),
                get_planner_llm_profile_arg(args),
                planner_timeout=get_planner_timeout_arg(args),
                planner_max_retries=get_planner_max_retries_arg(args),
            )
            client_holder["client"] = client
        return client

    edge_only_replan_runner = None
    if (
        replan_dependency_type == "temporal"
        and bool(getattr(args, "temporal_edge_only_replan", False))
        and not bool(getattr(args, "dry_run", False))
    ):
        def edge_only_replan_runner(**kwargs: Any) -> Dict[str, Any]:
            prompt = build_temporal_edge_only_replan_prompt(
                user_request=str(kwargs.get("user_request") or ""),
                workflow=kwargs.get("workflow") or {},
                original_task_links=kwargs.get("original_task_links") or [],
                chain_prior_task_links=kwargs.get("chain_prior_task_links") or [],
                graph_candidates=kwargs.get("graph_candidates") or [],
                scope=str(getattr(args, "temporal_edge_only_scope", "high-risk") or "high-risk"),
            )
            raw_output = planner_client().chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You repair only temporal workflow task_links. "
                            f"{STRICT_JSON_SYSTEM_PROMPT}"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ]
            )
            return {"payload": extract_json_object(raw_output), "raw_output": raw_output}

    previous_tool_summary = get_first(row, "model tool")
    intent_tool_hint = get_first(row, "intent tool")
    intent_hint = get_first(row, "intent")
    resolved_tool_catalog = tool_catalog or build_tool_catalog(tool_desc)
    original_workflow_structure = infer_original_workflow_structure(previous_workflow)
    if original_workflow_structure == "single":
        return build_skip_replan_row(
            case_id=case_id,
            coverage_row=row,
            previous_workflow=previous_workflow,
            warnings=warnings,
            reason="original_workflow_structure=single",
            hint=intent_tool_hint or intent_hint,
            status="already_covered",
            tool_catalog=resolved_tool_catalog,
            transition_index=transition_index,
            require_tool_graph_edge=getattr(args, "require_tool_graph_edge", False),
            dataset_config=dataset_config,
            semantic_tool_families=semantic_tool_families,
            replan_variant=replan_variant,
            dependency_type=replan_dependency_type,
            temporal_chain_prior=bool(getattr(args, "temporal_chain_prior", False)),
            temporal_edge_only_scope=getattr(args, "temporal_edge_only_scope", "high-risk"),
            edge_only_replan_runner=edge_only_replan_runner,
        )
    coverage_warnings = get_first(row, "coverage_warnings")
    if coverage_warnings:
        warnings.append(f"coverage_warning: {coverage_warnings}")
        return build_skip_replan_row(
            case_id=case_id,
            coverage_row=row,
            previous_workflow=previous_workflow,
            warnings=warnings,
            reason="coverage_warnings present; no reliable intent coverage hint",
            hint=intent_tool_hint or intent_hint or coverage_warnings,
            status="wrong_or_too_generic",
            tool_catalog=resolved_tool_catalog,
            transition_index=transition_index,
            require_tool_graph_edge=getattr(args, "require_tool_graph_edge", False),
            dataset_config=dataset_config,
            semantic_tool_families=semantic_tool_families,
            replan_variant=replan_variant,
            dependency_type=replan_dependency_type,
            temporal_chain_prior=bool(getattr(args, "temporal_chain_prior", False)),
            temporal_edge_only_scope=getattr(args, "temporal_edge_only_scope", "high-risk"),
            edge_only_replan_runner=edge_only_replan_runner,
        )
    if not split_terms(intent_tool_hint) and not split_terms(intent_hint):
        warnings.append("missing_intent_hint; skipped planner replan")
        return build_skip_replan_row(
            case_id=case_id,
            coverage_row=row,
            previous_workflow=previous_workflow,
            warnings=warnings,
            reason="missing intent tool/intent hint",
            hint="",
            status="wrong_or_too_generic",
            tool_catalog=resolved_tool_catalog,
            transition_index=transition_index,
            require_tool_graph_edge=getattr(args, "require_tool_graph_edge", False),
            dataset_config=dataset_config,
            semantic_tool_families=semantic_tool_families,
            replan_variant=replan_variant,
            dependency_type=replan_dependency_type,
            temporal_chain_prior=bool(getattr(args, "temporal_chain_prior", False)),
            temporal_edge_only_scope=getattr(args, "temporal_edge_only_scope", "high-risk"),
            edge_only_replan_runner=edge_only_replan_runner,
        )

    previous_tool_evidence = " -> ".join(
        split_terms(previous_tool_summary) + workflow_tool_names(previous_workflow)
    )
    tool_hint_coverage = split_tool_hint_coverage(
        previous_tool_evidence,
        intent_tool_hint,
        semantic_tool_families,
    )
    intent_hint_coverage = split_intent_hint_coverage(
        previous_tool_evidence,
        intent_hint,
        resolved_tool_catalog,
    )
    has_tool_hint = bool(split_terms(intent_tool_hint))
    missing_tool_hint = tool_hint_coverage["missing"]
    missing_intent_hint = intent_hint_coverage["missing"]
    if has_tool_hint and not missing_tool_hint:
        return build_skip_replan_row(
            case_id=case_id,
            coverage_row=row,
            previous_workflow=previous_workflow,
            warnings=warnings,
            reason="model_tool_original or previous_workflow covers intent_tool_hint",
            hint=intent_tool_hint,
            status="already_covered",
            tool_catalog=resolved_tool_catalog,
            transition_index=transition_index,
            require_tool_graph_edge=getattr(args, "require_tool_graph_edge", False),
            dataset_config=dataset_config,
            semantic_tool_families=semantic_tool_families,
            replan_variant=replan_variant,
            dependency_type=replan_dependency_type,
            temporal_chain_prior=bool(getattr(args, "temporal_chain_prior", False)),
            temporal_edge_only_scope=getattr(args, "temporal_edge_only_scope", "high-risk"),
            edge_only_replan_runner=edge_only_replan_runner,
        )
    if not has_tool_hint and split_terms(intent_hint) and not missing_intent_hint:
        return build_skip_replan_row(
            case_id=case_id,
            coverage_row=row,
            previous_workflow=previous_workflow,
            warnings=warnings,
            reason="model intents or previous_workflow cover intent_hint",
            hint=intent_hint,
            status="already_covered",
            tool_catalog=resolved_tool_catalog,
            transition_index=transition_index,
            require_tool_graph_edge=getattr(args, "require_tool_graph_edge", False),
            dataset_config=dataset_config,
            semantic_tool_families=semantic_tool_families,
            replan_variant=replan_variant,
            dependency_type=replan_dependency_type,
            temporal_chain_prior=bool(getattr(args, "temporal_chain_prior", False)),
            temporal_edge_only_scope=getattr(args, "temporal_edge_only_scope", "high-risk"),
            edge_only_replan_runner=edge_only_replan_runner,
        )

    graph_candidates: List[Dict[str, Any]] = []
    tool_graph_edges = compact_tool_graph_edges(
        transition_index=transition_index or {},
        tool_catalog=resolved_tool_catalog,
    )

    prompt = build_replan_prompt(
        user_request=user_request,
        previous_workflow=previous_workflow,
        previous_tool_summary=previous_tool_summary,
        intent_tool_hint=intent_tool_hint,
        intent_hint=intent_hint,
        covered_intent_tools=tool_hint_coverage["covered"],
        missing_intent_tools=missing_tool_hint,
        covered_intents=intent_hint_coverage["covered"],
        missing_intents=missing_intent_hint,
        graph_candidates=graph_candidates,
        tool_graph_edges=tool_graph_edges,
        tool_desc=select_relevant_tools(
            tool_desc=tool_desc,
            previous_workflow=previous_workflow,
            previous_tool_summary=previous_tool_summary,
            intent_tool_hint=format_hint_terms(missing_tool_hint or split_terms(intent_tool_hint)),
            intent_hint=format_hint_terms(missing_intent_hint or split_terms(intent_hint)),
            max_tools=args.max_tools,
        ),
        dataset_prompt_rules=get_replan_prompt_rules(dataset_config),
        dataset_prompt_variables=get_replan_prompt_variables(dataset_config),
        semantic_tool_families=semantic_tool_families,
        dependency_type=replan_dependency_type,
    )
    if args.dry_run:
        return build_output_row(
            case_id=case_id,
            coverage_row=row,
            previous_workflow=previous_workflow,
            planner_payload={
                "replan_decision": "DRY_RUN",
                "replanned_workflow": previous_workflow,
                "coverage_assessment": [],
                "change_summary": "dry_run: planner not called",
            },
            raw_planner_output="",
            warnings=warnings,
            tool_catalog=resolved_tool_catalog,
            transition_index=transition_index,
            require_tool_graph_edge=getattr(args, "require_tool_graph_edge", False),
            dataset_config=dataset_config,
            semantic_tool_families=semantic_tool_families,
            replan_variant=replan_variant,
            graph_candidates=graph_candidates,
            dependency_type=replan_dependency_type,
            temporal_chain_prior=bool(getattr(args, "temporal_chain_prior", False)),
            temporal_edge_only_scope=getattr(args, "temporal_edge_only_scope", "high-risk"),
            edge_only_replan_runner=edge_only_replan_runner,
        )

    raw_planner_output = ""
    try:
        raw_planner_output = planner_client().chat(
            messages=[
                {
                    "role": "system",
                    "content": f"You are a workflow replanner. {STRICT_JSON_SYSTEM_PROMPT}",
                },
                {"role": "user", "content": prompt},
            ]
        )
        planner_payload, repaired_planner_json = extract_planner_json_object(raw_planner_output)
        if repaired_planner_json:
            warnings.append("planner_json_repair_applied")
    except Exception as exc:  # noqa: BLE001 - row-level failure should not stop the run.
        planner_payload = fallback_payload_from_unparsed_replan_output(raw_planner_output, previous_workflow)
        if planner_payload:
            warnings.append(f"planner_parse_fallback: {type(exc).__name__}: visible keep decision")
        else:
            warnings.append(f"{PLANNER_CALL_ERROR_PREFIX} {type(exc).__name__}: {exc}")
            planner_payload = {}

    dag_self_repair_runner = None
    if int(getattr(args, "dag_self_repair_rounds", 1) or 0) > 0:
        def dag_self_repair_runner(**kwargs: Any) -> Dict[str, Any]:
            repair_prompt = build_dag_self_repair_prompt(
                user_request=str(kwargs.get("user_request") or ""),
                previous_workflow=kwargs.get("previous_workflow") or {},
                candidate_workflow=kwargs.get("candidate_workflow") or {},
                validation=kwargs.get("validation") or {},
                coverage_row=kwargs.get("coverage_row") or {},
                tool_desc=select_dag_self_repair_tools(
                    tool_desc=tool_desc,
                    previous_workflow=kwargs.get("previous_workflow") or {},
                    candidate_workflow=kwargs.get("candidate_workflow") or {},
                    coverage_row=kwargs.get("coverage_row") or {},
                    validation=kwargs.get("validation") or {},
                    max_tools=args.max_tools,
                ),
                dependency_type=replan_dependency_type,
            )
            raw_repair_output = planner_client().chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You repair workflow DAGs under strict tool-change constraints. "
                            f"{STRICT_JSON_SYSTEM_PROMPT}"
                        ),
                    },
                    {"role": "user", "content": repair_prompt},
                ]
            )
            return {"payload": extract_json_object(raw_repair_output), "raw_output": raw_repair_output}

    return build_output_row(
        case_id=case_id,
        coverage_row=row,
        previous_workflow=previous_workflow,
        planner_payload=planner_payload,
        raw_planner_output=raw_planner_output,
        warnings=warnings,
        tool_catalog=resolved_tool_catalog,
        transition_index=transition_index,
        require_tool_graph_edge=getattr(args, "require_tool_graph_edge", False),
        dag_self_repair_runner=dag_self_repair_runner,
        dataset_config=dataset_config,
        semantic_tool_families=semantic_tool_families,
        replan_variant=replan_variant,
        graph_candidates=graph_candidates,
        dependency_type=replan_dependency_type,
        temporal_chain_prior=bool(getattr(args, "temporal_chain_prior", False)),
        temporal_edge_only_scope=getattr(args, "temporal_edge_only_scope", "high-risk"),
        edge_only_replan_runner=edge_only_replan_runner,
    )


def fallback_payload_from_unparsed_replan_output(
    raw_output: Any,
    previous_workflow: Mapping[str, Any],
) -> Dict[str, Any]:
    decision = extract_visible_replan_decision(raw_output)
    if decision not in {"KEEP_ORIGINAL", "REJECT_INTENT_HINT"}:
        return {}
    return {
        "replan_decision": decision,
        "replanned_workflow": dict(previous_workflow),
        "hint_assessment": [],
        "coverage_assessment": [],
        "change_summary": "parse_fallback: visible keep decision; used previous workflow",
    }


def extract_planner_json_object(raw_output: Any) -> Tuple[Dict[str, Any], bool]:
    """Parse planner JSON, repairing common malformed workflow-array boundaries."""
    candidate = _planner_json_candidate(raw_output)
    try:
        payload = _loads_planner_json_object(candidate)
        return _repair_planner_payload_shape(payload)
    except Exception as first_exc:  # noqa: BLE001 - callers need the original parse error if repair fails.
        for candidate in planner_json_repair_variants(raw_output):
            try:
                payload = _loads_planner_json_object(candidate)
                payload, _ = _repair_planner_payload_shape(payload)
                return payload, True
            except Exception:
                continue
        raise first_exc


def planner_json_repair_variants(raw_output: Any) -> List[str]:
    text = _planner_json_candidate(raw_output)
    if not text:
        return []

    variants: List[str] = []
    balanced = _append_missing_json_closers(text)
    if balanced != text:
        variants.append(balanced)
    repaired = text
    for _ in range(3):
        next_repaired = _repair_planner_json_once(repaired)
        if next_repaired == repaired:
            break
        variants.append(next_repaired)
        balanced = _append_missing_json_closers(next_repaired)
        if balanced != next_repaired:
            variants.append(balanced)
        repaired = next_repaired
    return list(dict.fromkeys(variants))


def _planner_json_candidate(raw_output: Any) -> str:
    text = str(raw_output or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()

    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        return text[first : last + 1].strip()
    return text


def _loads_planner_json_object(text: str) -> Dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload, _ = json.JSONDecoder().raw_decode(text)
    if not isinstance(payload, dict):
        raise ValueError("expected planner JSON object")
    return payload


def _append_missing_json_closers(text: str) -> str:
    stack: List[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char == "}":
            if stack and stack[-1] == "{":
                stack.pop()
        elif char == "]":
            if stack and stack[-1] == "[":
                stack.pop()
    if in_string or not stack:
        return text
    closer = {"{": "}", "[": "]"}
    return text + "".join(closer[item] for item in reversed(stack))


def _repair_planner_json_once(text: str) -> str:
    repaired = text
    # Example: ... "arguments(["<node-1>"]} ...
    repaired = re.sub(r'"(arguments|task_nodes|task_links)\s*\(', r'"\1":', repaired)
    repaired = re.sub(r'"(arguments|task_nodes|task_links)"\s*\(', r'"\1":', repaired)
    # Example: ... "evidence":"Missing intent'}], ...
    repaired = re.sub(
        r'("(?:(?:matched_)?(?:request_)?phrase|matched_intent_term|evidence|reason|change_summary)"\s*:\s*"[^"\r\n]*?)(\})(?=\s*[,}\]])',
        r'\1"\2',
        repaired,
    )
    # Example: ... "arguments":["..."],"},{"id":"node-1",...}
    repaired = re.sub(r'(\])\s*,\s*"\}\s*,\s*(\{)', r"\1},\2", repaired)
    # Example: ... "task_nodes":[{"id":"node-0"},"task_links":[...]
    repaired = re.sub(
        r'("task_nodes"\s*:\s*\[.*?\})(\s*,\s*"task_links"\s*:)',
        r"\1]\2",
        repaired,
        flags=re.DOTALL,
    )
    # Example: ... "task_nodes":[{"id":"node-0"}],task_links":[...]
    repaired = re.sub(r'(\]\s*,\s*)task_links"\s*:', r'\1"task_links":', repaired)
    repaired = re.sub(r"(\]\s*,\s*)task_links\s*:", r'\1"task_links":', repaired)
    # Example: ... {"id":"node-0"},"id":"node-1","task":"..."
    repaired = re.sub(
        r'("task_nodes"\s*:\s*\[.*?\})(\s*,\s*)"id"\s*:',
        r'\1\2{"id":',
        repaired,
        flags=re.DOTALL,
    )
    for array_name, keys in {
        "hint_assessment": ("hint", "intent", "tool", "status", "evidence"),
        "coverage_assessment": ("hint", "intent", "tool", "status", "evidence"),
        "original_tool_changes": ("tool", "action", "replacement", "evidence"),
        "task_nodes": ("id", "task", "arguments"),
        "task_links": ("source", "target", "target_input_slot"),
    }.items():
        repaired = _repair_missing_object_openers_in_planner_array(repaired, array_name, keys)
    # Example: ... {"id":"node-0"},"node-1","task":"..."
    repaired = re.sub(
        r'(\}\s*,\s*)"(node-\d+)"\s*,\s*"task"\s*:',
        r'\1{"id":"\2","task":',
        repaired,
    )
    # Example: ... {"id":"node-0"},"node-1":{"id":"node-1","task":"..."}
    repaired = re.sub(
        r'(\[\s*|\}\s*,\s*)"(node-\d+)"\s*:\s*\{\s*"id"\s*:',
        r'\1{"id":',
        repaired,
    )
    # Example: ... {"id":"node-0"},"node-1":{"task":"..."}
    repaired = re.sub(
        r'(\[\s*|\}\s*,\s*)"(node-\d+)"\s*:\s*\{(?!\s*"id"\s*:)',
        r'\1{"id":"\2",',
        repaired,
    )
    # Example: ... {"id":"node-0"},"{"id":"node-1",...}
    repaired = re.sub(r',\s*"\s*(\{\s*"id"\s*:)', r",\1", repaired)
    return _remove_trailing_commas_outside_strings(repaired)


def _repair_missing_object_openers_in_planner_array(
    text: str,
    array_name: str,
    object_start_keys: Tuple[str, ...],
) -> str:
    key_pattern = "|".join(re.escape(key) for key in object_start_keys)
    return re.sub(
        rf'("{re.escape(array_name)}"\s*:\s*\[.*?\}})(\s*,\s*)"({key_pattern})"\s*:',
        r'\1\2{"\3":',
        text,
        flags=re.DOTALL,
    )


def _remove_trailing_commas_outside_strings(text: str) -> str:
    chars: List[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            chars.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            chars.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                index += 1
                continue
        chars.append(char)
        index += 1
    return "".join(chars)


def _repair_planner_payload_shape(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    workflow = payload.get("replanned_workflow")
    if not isinstance(workflow, Mapping):
        return payload, False

    misplaced_node = {
        key: copy.deepcopy(workflow[key])
        for key in ("id", "task", "arguments")
        if key in workflow
    }
    if not misplaced_node:
        return payload, False

    repaired = copy.deepcopy(payload)
    repaired_workflow = repaired["replanned_workflow"]
    nodes = repaired_workflow.get("task_nodes")
    if not isinstance(nodes, list):
        nodes = []
        repaired_workflow["task_nodes"] = nodes

    misplaced_id = str(misplaced_node.get("id") or "").strip()
    if misplaced_id and not any(
        isinstance(node, Mapping) and str(node.get("id") or "").strip() == misplaced_id
        for node in nodes
    ):
        nodes.append(misplaced_node)

    for key in ("id", "task", "arguments"):
        repaired_workflow.pop(key, None)
    return repaired, True


def extract_visible_replan_decision(raw_output: Any) -> str:
    text = str(raw_output or "").replace("\\_", "_")
    match = re.search(r'"(?:replan_decision|decision)"\s*:\s*"([^"]+)"', text)
    if match is None:
        return ""
    return match.group(1).strip().upper().replace("\\_", "_")


def build_skip_replan_row(
    case_id: str,
    coverage_row: Mapping[str, Any],
    previous_workflow: Mapping[str, Any],
    warnings: List[str],
    reason: str,
    hint: str,
    status: str,
    tool_catalog: Mapping[str, Dict[str, Any]] | None = None,
    transition_index: Mapping[Tuple[str, str], Any] | None = None,
    require_tool_graph_edge: bool = False,
    dataset_config: Mapping[str, Any] | None = None,
    semantic_tool_families: Mapping[str, Any] | None = None,
    replan_variant: str = DEFAULT_REPLAN_VARIANT,
    dependency_type: str = "resource",
    temporal_chain_prior: bool = False,
    temporal_edge_only_scope: str = "high-risk",
    edge_only_replan_runner: Callable[..., Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    return build_output_row(
        case_id=case_id,
        coverage_row=coverage_row,
        previous_workflow=previous_workflow,
        planner_payload={
            "replan_decision": "KEEP_ORIGINAL",
            "replanned_workflow": previous_workflow,
            "coverage_assessment": [
                {
                    "hint": hint,
                    "status": status,
                    "covered_by_previous": status == "already_covered",
                    "covered_by_final": True,
                    "reason": f"{reason}; skipped planner replan.",
                }
            ],
            "hint_assessment": [
                {
                    "hint": hint,
                    "status": status,
                    "evidence": f"{reason}; skipped planner replan.",
                }
            ],
            "change_summary": f"skip_replan: {reason}",
        },
        raw_planner_output="",
        warnings=warnings,
        tool_catalog=tool_catalog,
        transition_index=transition_index,
        require_tool_graph_edge=require_tool_graph_edge,
        dataset_config=dataset_config,
        semantic_tool_families=semantic_tool_families,
        replan_variant=replan_variant,
        dependency_type=dependency_type,
        temporal_chain_prior=temporal_chain_prior,
        temporal_edge_only_scope=temporal_edge_only_scope,
        edge_only_replan_runner=edge_only_replan_runner,
    )


def build_replan_prompt(
    user_request: str,
    previous_workflow: Mapping[str, Any],
    previous_tool_summary: str,
    intent_tool_hint: str,
    intent_hint: str,
    tool_desc: List[Dict[str, Any]],
    covered_intent_tools: Iterable[Any] | None = None,
    missing_intent_tools: Iterable[Any] | None = None,
    covered_intents: Iterable[Any] | None = None,
    missing_intents: Iterable[Any] | None = None,
    graph_candidates: Iterable[Mapping[str, Any]] | None = None,
    tool_graph_edges: Iterable[Mapping[str, Any]] | None = None,
    dataset_prompt_rules: Iterable[str] | None = None,
    dataset_prompt_variables: Mapping[str, Any] | None = None,
    semantic_tool_families: Mapping[str, Any] | None = None,
    dependency_type: str = "resource",
) -> str:
    dependency_type = normalize_dependency_type(dependency_type)
    semantic_tool_families = resolve_semantic_tool_families(semantic_tool_families)
    previous_tool_evidence = " -> ".join(
        split_terms(previous_tool_summary) + workflow_tool_names(previous_workflow)
    )
    if covered_intent_tools is None and missing_intent_tools is None:
        tool_hint_coverage = split_tool_hint_coverage(
            previous_tool_evidence,
            intent_tool_hint,
            semantic_tool_families,
        )
        covered_intent_tools = tool_hint_coverage["covered"]
        missing_intent_tools = tool_hint_coverage["missing"]
    if covered_intents is None and missing_intents is None:
        intent_hint_coverage = split_intent_hint_coverage(
            previous_tool_evidence,
            intent_hint,
            build_tool_catalog(tool_desc),
        )
        covered_intents = intent_hint_coverage["covered"]
        missing_intents = intent_hint_coverage["missing"]
    covered_intent_tools_list = normalize_hint_terms(covered_intent_tools)
    missing_intent_tools_list = normalize_hint_terms(missing_intent_tools)
    covered_intents_list = normalize_hint_terms(covered_intents)
    missing_intents_list = normalize_hint_terms(missing_intents)
    payload = {
        "request": user_request,
        "previous_task_nodes": compact_task_nodes(previous_workflow),
        "previous_task_links": compact_task_links(previous_workflow),
        "model_tools": split_terms(previous_tool_summary),
        "previous_tools": split_terms(previous_tool_summary),
        "intent_tools": split_terms(intent_tool_hint),
        "intents": split_terms(intent_hint),
        "covered_intent_tools": covered_intent_tools_list,
        "missing_intent_tools": missing_intent_tools_list,
        "covered_intents": covered_intents_list,
        "missing_intents": missing_intents_list,
        "semantic_tool_families": semantic_tool_families,
        "tools": compact_tool_desc(tool_desc),
    }
    graph_candidate_list = [dict(candidate) for candidate in normalize_list(graph_candidates) if isinstance(candidate, Mapping)]
    if graph_candidate_list:
        payload["graph_candidates"] = graph_candidate_list
    tool_graph_list = [dict(edge) for edge in normalize_list(tool_graph_edges) if isinstance(edge, Mapping)]
    if tool_graph_list:
        payload["tool_graph"] = tool_graph_list
    variables = dict(dataset_prompt_variables or {})
    if variables:
        payload["dataset_prompt_variables"] = variables
    rules = [str(rule).strip() for rule in (dataset_prompt_rules or []) if str(rule).strip()]
    dataset_rule_text = ""
    if rules:
        dataset_rule_text = (
            "Dataset-specific rules from --dataset-config: "
            + " ".join(f"{index}. {rule}" for index, rule in enumerate(rules, start=1))
            + " "
        )
    graph_rule_text = ""
    if tool_graph_list:
        if dependency_type == "temporal":
            graph_rule_text = (
                "Use tool_graph as a global training-derived temporal prior for task_links only. "
                "Prefer high-probability transitions when the user request requires that API order. "
                "Do not add a transition only because it appears in tool_graph; add it only when needed by the requested workflow. "
            )
        else:
            graph_rule_text = (
                "Use tool_graph as a global training-derived structural prior for task_links and <node-i> arguments. "
                "Prefer high-probability type-compatible transitions when the user request and workflow data flow require an edge. "
                "Do not add a graph edge only because it appears in tool_graph; add it only when it is needed by the requested workflow. "
            )
    elif graph_candidate_list:
        if dependency_type == "temporal":
            graph_rule_text = (
                "Use graph_candidates as local temporal hints for task_links only. "
                "Prefer existing edges and high-probability candidates. "
                "Do not add a transition only because it appears in graph_candidates; add it only when needed by the user request. "
            )
        else:
            graph_rule_text = (
                "Use graph_candidates as local structural hints for task_links and <node-i> arguments. "
                "Prefer existing edges and high-probability type-compatible candidates. "
                "Do not add a graph edge only because it appears in graph_candidates; add it only when needed by the user request and workflow data flow. "
            )
    if dependency_type == "temporal":
        dependency_rule_text = (
            "Dependency mode: temporal. task_links represent API execution order, not resource flow. "
            "Do not use <node-i>, node-i, or bare string arguments. "
            "Each task_node.arguments item must be an object with name and value copied from the user request or an upstream tool name when explicitly needed. "
            "task_links source and target must be exact task names from task_nodes, not node ids. "
        )
        workflow_schema = (
            'Schema: {"replan_decision":"KEEP_ORIGINAL|ADD_MISSING_TOOLS|REPLACE_WRONG_TOOLS|REJECT_INTENT_HINT",'
            '"hint_assessment":[{"hint":"...","status":"already_covered|missing_should_add|equivalent_to_existing|wrong_or_too_generic|previous_tool_wrong","evidence":"..."}],'
            '"original_tool_changes":[{"tool":"...","action":"keep|add_after|replace|remove","replacement":"...","evidence":"..."}],'
            '"edge_changes":[{"action":"keep|add|remove|rewire","source":"task name","target":"task name","evidence":"..."}],'
            '"replanned_workflow":{"task_nodes":[{"task":"exact_tool_id","arguments":[{"name":"parameter_name","value":"parameter_value"}]}],"task_links":[{"source":"task name","target":"task name"}]},'
            '"reason":"..."}\n'
        )
    else:
        dependency_rule_text = (
            "Dependency mode: resource. task_links must match <node-i> references in task arguments. "
            "Each link must connect a source output type to a required target input slot. "
        )
        workflow_schema = (
            'Schema: {"replan_decision":"KEEP_ORIGINAL|ADD_MISSING_TOOLS|REPLACE_WRONG_TOOLS|REJECT_INTENT_HINT",'
            '"hint_assessment":[{"hint":"...","status":"already_covered|missing_should_add|equivalent_to_existing|wrong_or_too_generic|previous_tool_wrong","evidence":"..."}],'
            '"original_tool_changes":[{"tool":"...","action":"keep|add_after|replace|remove","replacement":"...","evidence":"..."}],'
            '"edge_changes":[{"action":"keep|add|remove|rewire","source":"node-0","target":"node-1","target_input_slot":"text","evidence":"..."}],'
            '"replanned_workflow":{"task_nodes":[{"id":"node-0","task":"...","arguments":["...","<node-0>"]}],"task_links":[{"source":"node-0","target":"node-1","target_input_slot":"text"}]},'
            '"reason":"..."}\n'
        )
    return (
        "Replan only if the previous workflow clearly misses an intent or uses a wrong tool. "
        "intent coverage hints are recall hints, not gold labels. Keep correct original tools. "
        "Focus tool additions/replacements on missing_intent_tools and missing_intents; "
        "covered_intent_tools and covered_intents are already satisfied context and must not be duplicated. "
        "Do not replace a previous tool with another tool from the same semantic_tool_families group; treat it as equivalent_to_existing and keep the previous tool. "
        "Do not add a direct shortcut tool when the existing multi-step path already satisfies the same intent. "
        f"{graph_rule_text}"
        f"{dataset_rule_text}"
        "If you replan, output a complete executable DAG, not only a tool list. "
        f"{dependency_rule_text}"
        "Preserve original edges unless adding/replacing a tool requires a justified rewire. "
        "Use exact tool_id values from tools. "
        f"{STRICT_JSON_OUTPUT_INSTRUCTION}"
        f"{workflow_schema}"
        f"Input:\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    )


def build_dag_self_repair_prompt(
    user_request: str,
    previous_workflow: Mapping[str, Any],
    candidate_workflow: Mapping[str, Any],
    validation: Mapping[str, Any],
    coverage_row: Mapping[str, Any],
    tool_desc: List[Dict[str, Any]],
    dependency_type: str = "resource",
) -> str:
    dependency_type = normalize_dependency_type(dependency_type)
    payload = {
        "request": user_request,
        "previous_task_nodes": compact_task_nodes(previous_workflow),
        "previous_task_links": compact_task_links(previous_workflow),
        "candidate_task_nodes": compact_task_nodes(candidate_workflow),
        "candidate_task_links": compact_task_links(candidate_workflow),
        "validator_errors": normalize_list(validation.get("errors")),
        "missing_input_slots": compact_missing_slot_status(validation.get("slot_status")),
        "accepted_tools": workflow_tool_names(candidate_workflow),
        "previous_tools": workflow_tool_names(previous_workflow),
        "intent_tools": split_terms(get_first(coverage_row, "intent tool")),
        "intents": split_terms(get_first(coverage_row, "intent")),
        "tools": compact_tool_desc(tool_desc),
    }
    if dependency_type == "temporal":
        dependency_rule_text = (
            "Dependency mode: temporal. Repair task_links as API execution order only. "
            "Do not add <node-i>, node-i, or bare string arguments. "
            "Arguments must remain objects with name and value; task_links source/target must be task names. "
        )
        repair_schema = (
            'Schema: {"repair_decision":"FIX_DAG_ONLY|ADD_TOOL_FOR_MISSING_SLOT|REMOVE_INVALID_TOOL|CANNOT_REPAIR",'
            '"tool_changes":[{"tool":"...","action":"keep|add|remove","evidence":"..."}],'
            '"edge_changes":[{"action":"keep|add|remove|rewire","source":"task name","target":"task name","evidence":"..."}],'
            '"repaired_workflow":{"task_nodes":[{"task":"exact_tool_id","arguments":[{"name":"parameter_name","value":"parameter_value"}]}],"task_links":[{"source":"task name","target":"task name"}]},'
            '"reason":"..."}\n'
        )
    else:
        dependency_rule_text = (
            "Dependency mode: resource. Every task_link must correspond to a <node-i> argument, "
            "and every <node-i> argument must correspond to a task_link. "
            "Every source output type must satisfy the target input slot. "
        )
        repair_schema = (
            'Schema: {"repair_decision":"FIX_DAG_ONLY|ADD_TOOL_FOR_MISSING_SLOT|REMOVE_INVALID_TOOL|CANNOT_REPAIR",'
            '"tool_changes":[{"tool":"...","action":"keep|add|remove","evidence":"..."}],'
            '"edge_changes":[{"action":"keep|add|remove|rewire","source":"node-0","target":"node-1","target_input_slot":"audio","evidence":"..."}],'
            '"repaired_workflow":{"task_nodes":[{"id":"node-0","task":"...","arguments":["literal","<node-0>"]}],"task_links":[{"source":"node-0","target":"node-1","target_input_slot":"audio"}]},'
            '"reason":"..."}\n'
        )
    return (
        "Repair the workflow DAG. Do not freely rewrite the whole plan. "
        "Prefer FIX_DAG_ONLY by rewiring edges and arguments while keeping accepted_tools. "
        "Add a tool only when validator_errors show a missing input slot that existing tools and request literals cannot satisfy. "
        "Remove a tool only when validator_errors prove it is unknown or semantically unusable. "
        f"{dependency_rule_text}"
        "Use request URL/file/text literals instead of adding a tool when a literal can satisfy the slot. "
        "Use exact tool_id values from tools. "
        f"{STRICT_JSON_OUTPUT_INSTRUCTION}"
        f"{repair_schema}"
        f"Input:\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    )


def build_temporal_edge_only_replan_prompt(
    user_request: str,
    workflow: Mapping[str, Any],
    original_task_links: Iterable[Mapping[str, Any]] | None = None,
    chain_prior_task_links: Iterable[Mapping[str, Any]] | None = None,
    graph_candidates: Iterable[Mapping[str, Any]] | None = None,
    scope: str = "high-risk",
) -> str:
    payload = {
        "request": user_request,
        "scope": scope,
        "task_nodes": compact_task_nodes(workflow),
        "original_task_links": [
            dict(link) for link in normalize_list(original_task_links) if isinstance(link, Mapping)
        ],
        "chain_prior_task_links": [
            dict(link) for link in normalize_list(chain_prior_task_links) if isinstance(link, Mapping)
        ],
        "tool_graph_candidates": [
            dict(candidate)
            for candidate in normalize_list(graph_candidates)
            if isinstance(candidate, Mapping)
        ],
    }
    return (
        "Repair only task_links for a temporal API workflow. "
        "Do not change task_nodes, task order, tool names, or arguments. "
        "task_links represent execution/dependency order, not resource flow. "
        "task_nodes order may be non-topological, so backward source/target pairs are allowed when the request requires them. "
        "Use original_task_links as the default. Use chain_prior_task_links only as a candidate, not a rule. "
        "Use tool_graph_candidates as a training-derived prior, not gold labels. "
        "Every source and target must be an exact task name from task_nodes. "
        "Return no self-links and no cycles. "
        f"{STRICT_JSON_OUTPUT_INSTRUCTION}"
        'Schema: {"decision":"KEEP_ORIGINAL|USE_CHAIN_PRIOR|REVISE_LINKS",'
        '"task_links":[{"source":"exact task name","target":"exact task name"}],'
        '"reason":"..."}\n'
        f"Input:\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    )


def build_temporal_chain_prior_links(workflow: Mapping[str, Any]) -> List[Dict[str, str]]:
    nodes = normalize_nodes_for_repair(workflow.get("task_nodes"))
    links: List[Dict[str, str]] = []
    for index in range(len(nodes) - 1):
        source = str(nodes[index].get("task") or "").strip()
        target = str(nodes[index + 1].get("task") or "").strip()
        if source and target and normalize_key(source) != normalize_key(target):
            links.append({"source": source, "target": target})
    return links


def build_temporal_tool_graph_candidates_for_workflow(
    workflow: Mapping[str, Any],
    tool_catalog: Mapping[str, Dict[str, Any]] | None,
    transition_index: Mapping[Tuple[str, str], Any] | None,
    max_candidates: int = DEFAULT_GRAPH_CONTEXT_MAX_CANDIDATES,
) -> List[Dict[str, Any]]:
    if not tool_catalog or not transition_index:
        return []
    max_candidates = max(1, int(max_candidates or DEFAULT_GRAPH_CONTEXT_MAX_CANDIDATES))
    nodes = normalize_nodes_for_repair(workflow.get("task_nodes"))
    candidates: List[Dict[str, Any]] = []
    for source_index, source_node in enumerate(nodes):
        source_task = str(source_node.get("task") or "").strip()
        source_tool = lookup_catalog_tool(tool_catalog, source_task)
        for target_index, target_node in enumerate(nodes):
            if source_index == target_index:
                continue
            target_task = str(target_node.get("task") or "").strip()
            target_tool = lookup_catalog_tool(tool_catalog, target_task)
            probability = safe_float(
                bg.get_transition_probability(
                    transition_index,
                    source_tool.get("id"),
                    target_tool.get("id"),
                )
            )
            if probability is None or probability <= 0:
                continue
            reverse_probability = safe_float(
                bg.get_transition_probability(
                    transition_index,
                    target_tool.get("id"),
                    source_tool.get("id"),
                )
            )
            candidates.append(
                {
                    "source": source_task,
                    "target": target_task,
                    "source_index": source_index,
                    "target_index": target_index,
                    "transition_probability": round(probability, 6),
                    "reverse_transition_probability": (
                        round(reverse_probability, 6) if reverse_probability is not None else None
                    ),
                    "node_order": "forward" if source_index < target_index else "backward",
                }
            )
    candidates.sort(
        key=lambda item: (
            float(item.get("transition_probability") or 0.0),
            1 if item.get("node_order") == "forward" else 0,
            str(item.get("source") or ""),
            str(item.get("target") or ""),
        ),
        reverse=True,
    )
    return candidates[:max_candidates]


def should_run_temporal_edge_only_replan(
    workflow: Mapping[str, Any],
    scope: str,
    link_errors: Iterable[str] | None = None,
) -> Tuple[bool, str]:
    nodes = normalize_nodes_for_repair(workflow.get("task_nodes"))
    if len(nodes) <= 1:
        return False, "single_or_empty_workflow"
    if str(scope or "high-risk").strip().lower() == "all":
        return True, "scope_all"
    errors = [str(error) for error in normalize_list(link_errors) if str(error).strip()]
    if errors:
        return True, "link_parse_errors"
    edges, edge_errors = normalize_task_links_to_edges(workflow.get("task_links"), nodes)
    if edge_errors:
        return True, "link_parse_errors"
    edges = dedupe_valid_edges(edges, len(nodes), allow_backward=True)
    if not edges:
        return True, "empty_links"
    if len(edges) < len(nodes) - 1:
        return True, "sparse_links"
    if has_cycle(edges, len(nodes)):
        return True, "cycle_detected"
    return False, "scope_high_risk_not_triggered"


def extract_temporal_edge_only_task_links(payload: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], bool]:
    containers = [
        payload,
        payload.get("workflow") if isinstance(payload, Mapping) else None,
        payload.get("repaired_workflow") if isinstance(payload, Mapping) else None,
        payload.get("replanned_workflow") if isinstance(payload, Mapping) else None,
    ]
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        for key in ("task_links", "repaired_task_links", "links"):
            if key not in container:
                continue
            links = normalize_list(container.get(key))
            return [dict(link) for link in links if isinstance(link, Mapping)], True
    return [], False


def run_temporal_edge_only_replan_attempt(
    *,
    edge_only_replan_runner: Callable[..., Dict[str, Any]],
    user_request: str,
    workflow: Mapping[str, Any],
    original_task_links: List[Dict[str, Any]],
    chain_prior_task_links: List[Dict[str, Any]],
    graph_candidates: List[Dict[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]] | None,
    transition_index: Mapping[Tuple[str, str], Any] | None,
    warnings: List[str],
) -> Dict[str, Any]:
    raw_output = ""
    try:
        runner_result = edge_only_replan_runner(
            user_request=user_request,
            workflow=workflow,
            original_task_links=original_task_links,
            chain_prior_task_links=chain_prior_task_links,
            graph_candidates=graph_candidates,
        )
        payload = runner_result.get("payload") or {}
        raw_output = str(runner_result.get("raw_output") or "")
    except Exception as exc:  # noqa: BLE001 - row-level edge-only repair should not stop the run.
        warnings.append(f"temporal_edge_only_replan_error: {type(exc).__name__}: {exc}")
        return {
            "accepted": False,
            "status": "call_failed",
            "decision": "",
            "workflow": dict(workflow),
            "validation": {},
            "raw_output": raw_output,
        }
    if not isinstance(payload, Mapping):
        warnings.append("temporal_edge_only_replan_rejected=payload_not_object")
        return {
            "accepted": False,
            "status": "rejected",
            "decision": "",
            "workflow": dict(workflow),
            "validation": {},
            "raw_output": raw_output,
        }

    decision = str(payload.get("decision") or payload.get("repair_decision") or "").strip().upper()
    task_links, has_task_links = extract_temporal_edge_only_task_links(payload)
    if not task_links and decision == "USE_CHAIN_PRIOR":
        task_links = list(chain_prior_task_links)
    elif not task_links and decision == "KEEP_ORIGINAL":
        task_links = list(original_task_links)
    if not task_links and not has_task_links and normalize_list(workflow.get("task_links")):
        task_links = [dict(link) for link in normalize_list(workflow.get("task_links")) if isinstance(link, Mapping)]
    candidate = copy.deepcopy(dict(workflow))
    candidate["task_links"] = task_links
    validation = validate_workflow_dag(
        candidate,
        tool_catalog=tool_catalog,
        transition_index=transition_index,
        require_tool_graph_edge=False,
        dependency_type="temporal",
    )
    if validation.get("status") == "passed":
        return {
            "accepted": True,
            "status": "accepted",
            "decision": decision,
            "workflow": validation.get("workflow", candidate),
            "validation": validation,
            "raw_output": raw_output,
        }
    warnings.append(
        "temporal_edge_only_replan_validation_failed="
        + json.dumps(validation.get("errors", []), ensure_ascii=False, separators=(",", ":"))
    )
    return {
        "accepted": False,
        "status": "validation_failed",
        "decision": decision,
        "workflow": candidate,
        "validation": validation,
        "raw_output": raw_output,
    }


def apply_temporal_link_repair_stage(
    *,
    workflow: Mapping[str, Any],
    result_source: str,
    selection_reason: str,
    coverage_row: Mapping[str, Any],
    tool_catalog: Mapping[str, Dict[str, Any]] | None,
    transition_index: Mapping[Tuple[str, str], Any] | None,
    dependency_type: str,
    temporal_chain_prior: bool,
    temporal_edge_only_scope: str,
    edge_only_replan_runner: Callable[..., Dict[str, Any]] | None,
    warnings: List[str],
) -> Dict[str, Any]:
    trace: Dict[str, Any] = {
        "dependency_type": dependency_type,
        "chain_prior_enabled": bool(temporal_chain_prior),
        "edge_only_replan_enabled": edge_only_replan_runner is not None,
        "scope": temporal_edge_only_scope,
        "status": "skipped_not_temporal",
    }
    if dependency_type != "temporal":
        return {
            "workflow": dict(workflow),
            "result_source": result_source,
            "selection_reason": selection_reason,
            "dag_validation": None,
            "trace": trace,
        }
    nodes = normalize_nodes_for_repair(workflow.get("task_nodes"))
    if len(nodes) <= 1:
        trace["status"] = "skipped_single_or_empty_workflow"
        return {
            "workflow": dict(workflow),
            "result_source": result_source,
            "selection_reason": selection_reason,
            "dag_validation": None,
            "trace": trace,
        }

    working_workflow = copy.deepcopy(dict(workflow))
    working_workflow["task_nodes"] = nodes
    explicit_edges, link_errors = normalize_task_links_to_edges(working_workflow.get("task_links"), nodes)
    clean_edges = dedupe_valid_edges(explicit_edges, len(nodes), allow_backward=True)
    original_task_links = build_task_links_from_edges(nodes, clean_edges)
    chain_prior_task_links = (
        build_temporal_chain_prior_links(working_workflow) if temporal_chain_prior else []
    )
    graph_candidates = build_temporal_tool_graph_candidates_for_workflow(
        working_workflow,
        tool_catalog=tool_catalog,
        transition_index=transition_index,
    )
    trace.update(
        {
            "original_link_count": len(original_task_links),
            "chain_prior_link_count": len(chain_prior_task_links),
            "graph_candidate_count": len(graph_candidates),
            "link_errors": list(link_errors),
            "chain_prior_task_links": chain_prior_task_links,
            "graph_candidates": graph_candidates,
        }
    )

    if temporal_chain_prior and edge_only_replan_runner is None:
        candidate = copy.deepcopy(working_workflow)
        candidate["task_links"] = chain_prior_task_links
        validation = validate_workflow_dag(
            candidate,
            tool_catalog=tool_catalog,
            transition_index=transition_index,
            require_tool_graph_edge=False,
            dependency_type="temporal",
        )
        trace["chain_prior_validation_status"] = validation.get("status")
        trace["chain_prior_validation_errors"] = validation.get("errors", [])
        if validation.get("status") == "passed":
            trace["status"] = "chain_prior_applied"
            return {
                "workflow": validation.get("workflow", candidate),
                "result_source": result_source,
                "selection_reason": f"{selection_reason}; temporal_chain_prior_applied",
                "dag_validation": validation,
                "trace": trace,
            }
        warnings.append(
            "temporal_chain_prior_validation_failed="
            + json.dumps(validation.get("errors", []), ensure_ascii=False, separators=(",", ":"))
        )

    if edge_only_replan_runner is None:
        trace["status"] = "skipped_edge_only_disabled"
        return {
            "workflow": dict(workflow),
            "result_source": result_source,
            "selection_reason": selection_reason,
            "dag_validation": None,
            "trace": trace,
        }

    should_run, run_reason = should_run_temporal_edge_only_replan(
        working_workflow,
        scope=temporal_edge_only_scope,
        link_errors=link_errors,
    )
    trace["run_reason"] = run_reason
    if not should_run:
        trace["status"] = "skipped_by_scope"
        return {
            "workflow": dict(workflow),
            "result_source": result_source,
            "selection_reason": selection_reason,
            "dag_validation": None,
            "trace": trace,
        }

    attempt = run_temporal_edge_only_replan_attempt(
        edge_only_replan_runner=edge_only_replan_runner,
        user_request=get_first(coverage_row, "user_request"),
        workflow=working_workflow,
        original_task_links=original_task_links,
        chain_prior_task_links=chain_prior_task_links,
        graph_candidates=graph_candidates,
        tool_catalog=tool_catalog,
        transition_index=transition_index,
        warnings=warnings,
    )
    trace.update(
        {
            "status": attempt["status"],
            "decision": attempt.get("decision", ""),
            "raw_output": attempt.get("raw_output", ""),
            "validation_status": attempt.get("validation", {}).get("status", ""),
            "validation_errors": attempt.get("validation", {}).get("errors", []),
        }
    )
    if attempt.get("accepted"):
        return {
            "workflow": attempt["workflow"],
            "result_source": result_source,
            "selection_reason": f"{selection_reason}; temporal_edge_only_replan_passed",
            "dag_validation": attempt["validation"],
            "trace": trace,
        }
    return {
        "workflow": dict(workflow),
        "result_source": result_source,
        "selection_reason": selection_reason,
        "dag_validation": None,
        "trace": trace,
    }


def compact_missing_slot_status(slot_status: Any) -> List[Dict[str, Any]]:
    compact: List[Dict[str, Any]] = []
    for status in normalize_list(slot_status):
        if not isinstance(status, Mapping):
            continue
        missing_slots = normalize_list(status.get("missing_slots"))
        if missing_slots:
            compact.append(
                {
                    "target": status.get("target"),
                    "tool": status.get("tool"),
                    "missing_slots": missing_slots,
                    "required_slots": normalize_list(status.get("required_slots")),
                    "satisfied_slots": normalize_list(status.get("satisfied_slots")),
                }
            )
    return compact


def build_agent_trace(
    *,
    coverage_row: Mapping[str, Any],
    previous_workflow: Mapping[str, Any],
    model_tool_original: str,
    intent_tool_hint: str,
    intent_hint: str,
    tool_hint_coverage: Mapping[str, Any],
    intent_hint_coverage: Mapping[str, Any],
    hint_assessment: List[Any],
    coverage_assessment: List[Any],
    decision: str,
    replanned_workflow: Mapping[str, Any],
    dag_replan_result: Mapping[str, Any],
    dag_replan_result_source: str,
    dag_replan_selection_reason: str,
    dag_validation: Mapping[str, Any],
    require_tool_graph_edge: bool,
    planner_dag_self_repair_status: str,
    planner_dag_self_repair_decision: str,
    planner_dag_self_repair_result: Mapping[str, Any],
    raw_planner_dag_self_repair_output: str,
    dag_repair_status: str,
    dag_repair_operations: List[Dict[str, Any]],
    keep_original_verifier: Mapping[str, Any] | None = None,
    result: Mapping[str, Any],
    result_source: str,
    selection_reason: str,
    change_summary: Any,
    raw_planner_output: str,
    warnings: List[str],
) -> Dict[str, Any]:
    covered_tools = normalize_list(tool_hint_coverage.get("covered"))
    missing_tools = normalize_list(tool_hint_coverage.get("missing"))
    covered_intents = normalize_list(intent_hint_coverage.get("covered"))
    missing_intents = normalize_list(intent_hint_coverage.get("missing"))
    has_hint = bool(split_terms(intent_tool_hint) or split_terms(intent_hint))
    if not has_hint:
        intent_checker_status = "no_hint"
    elif missing_tools or missing_intents:
        intent_checker_status = "not_covered"
    else:
        intent_checker_status = "covered"

    return {
        "intent_detector": {
            "source": "coverage_table",
            "tool_hint": intent_tool_hint,
            "intent_hint": intent_hint,
            "covered_tool_hint": covered_tools,
            "missing_tool_hint": missing_tools,
            "covered_intent_hint": covered_intents,
            "missing_intent_hint": missing_intents,
        },
        "workflow_planner": {
            "source": "prediction_file",
            "tool_summary": model_tool_original,
            "workflow": dict(previous_workflow),
        },
        "intent_checker": {
            "status": intent_checker_status,
            "coverage_assessment": coverage_assessment,
            "hint_assessment": hint_assessment,
            "covered_tools": covered_tools,
            "missing_tools": missing_tools,
            "covered_intents": covered_intents,
            "missing_intents": missing_intents,
        },
        "workflow_replanner": {
            "decision": decision,
            "executed": bool(raw_planner_output),
            "candidate_workflow": dict(replanned_workflow),
            "selected_workflow": dict(dag_replan_result),
            "result_source": dag_replan_result_source,
            "result_source_generic": generic_result_source(dag_replan_result_source),
            "selection_reason": dag_replan_selection_reason,
            "change_summary": change_summary,
            "raw_output": raw_planner_output,
        },
        "structure_detector": {
            "status": dag_validation.get("status", ""),
            "errors": dag_validation.get("errors", []),
            "warnings": dag_validation.get("warnings", []),
            "require_tool_graph_edge": require_tool_graph_edge,
        },
        "workflow_repairer": {
            "planner_self_repair_status": planner_dag_self_repair_status,
            "planner_self_repair_decision": planner_dag_self_repair_decision,
            "planner_self_repair_result": dict(planner_dag_self_repair_result),
            "raw_planner_self_repair_output": raw_planner_dag_self_repair_output,
            "local_repair_status": dag_repair_status,
            "local_repair_operations": dag_repair_operations,
            "keep_original_verifier": dict(keep_original_verifier or {}),
            "final_workflow": dict(result),
            "final_result_source": result_source,
            "final_result_source_generic": generic_result_source(result_source),
            "final_selection_reason": selection_reason,
        },
        "warnings": list(warnings),
        "user_request": get_first(coverage_row, "user_request"),
    }


def build_output_row(
    case_id: str,
    coverage_row: Mapping[str, Any],
    previous_workflow: Mapping[str, Any],
    planner_payload: Mapping[str, Any],
    raw_planner_output: str,
    warnings: List[str],
    tool_catalog: Mapping[str, Dict[str, Any]] | None = None,
    transition_index: Mapping[Tuple[str, str], Any] | None = None,
    require_tool_graph_edge: bool = False,
    dag_self_repair_runner: Callable[..., Dict[str, Any]] | None = None,
    dataset_config: Mapping[str, Any] | None = None,
    semantic_tool_families: Mapping[str, Any] | None = None,
    replan_variant: str = DEFAULT_REPLAN_VARIANT,
    graph_candidates: Iterable[Mapping[str, Any]] | None = None,
    dependency_type: str = "resource",
    temporal_chain_prior: bool = False,
    temporal_edge_only_scope: str = "high-risk",
    edge_only_replan_runner: Callable[..., Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    dependency_type = normalize_dependency_type(dependency_type)
    graph_candidate_list = [dict(candidate) for candidate in normalize_list(graph_candidates) if isinstance(candidate, Mapping)]
    replanned_workflow = normalize_replanned_workflow(planner_payload)
    canonicalize_workflow_tool_names(
        replanned_workflow,
        canonical_tools=(
            split_terms(get_first(coverage_row, "intent tool"))
            + workflow_tool_names(previous_workflow)
        ),
    )
    decision = str(planner_payload.get("replan_decision") or planner_payload.get("decision") or "").strip().upper()
    call_error_prefixes = (PLANNER_CALL_ERROR_PREFIX, *LEGACY_PLANNER_CALL_ERROR_PREFIXES)
    if not decision and any(any(prefix in str(warning) for prefix in call_error_prefixes) for warning in warnings):
        decision = PLANNER_CALL_FAILED_DECISION
    dag_replan_result, dag_replan_result_source, dag_replan_selection_reason = select_final_workflow(
        previous_workflow=previous_workflow,
        replanned_workflow=replanned_workflow,
        decision=decision,
        coverage_row=coverage_row,
        planner_payload=planner_payload,
        warnings=warnings,
        semantic_tool_families=semantic_tool_families,
    )
    result = dag_replan_result
    result_source = dag_replan_result_source
    selection_reason = dag_replan_selection_reason
    dag_validation = {
        "status": "skipped",
        "errors": [],
        "warnings": [],
    }
    dag_repair_status = "not_attempted"
    dag_repair_operations: List[Dict[str, Any]] = []
    planner_dag_self_repair_status = "not_attempted"
    planner_dag_self_repair_decision = ""
    planner_dag_self_repair_result: Dict[str, Any] = {}
    raw_planner_dag_self_repair_output = ""
    if dag_replan_result.get("task_nodes"):
        dag_validation = validate_workflow_dag(
            dag_replan_result,
            tool_catalog=tool_catalog,
            transition_index=transition_index,
            require_tool_graph_edge=require_tool_graph_edge,
            dependency_type=dependency_type,
        )
        if dag_validation["status"] == "passed":
            result = dag_validation.get("workflow", dag_replan_result)
            selection_reason = f"{dag_replan_selection_reason}; dag_validation_passed"
        elif dag_validation["status"] == "failed":
            repair_base_workflow = dag_replan_result
            repair_base_validation = dag_validation
            if dag_self_repair_runner is not None:
                self_repair_result = run_planner_dag_self_repair_attempt(
                    dag_self_repair_runner=dag_self_repair_runner,
                    user_request=get_first(coverage_row, "user_request"),
                    previous_workflow=previous_workflow,
                    candidate_workflow=dag_replan_result,
                    initial_validation=dag_validation,
                    coverage_row=coverage_row,
                    tool_catalog=tool_catalog,
                    transition_index=transition_index,
                    require_tool_graph_edge=require_tool_graph_edge,
                    warnings=warnings,
                    dependency_type=dependency_type,
                )
                planner_dag_self_repair_status = self_repair_result["status"]
                planner_dag_self_repair_decision = self_repair_result["decision"]
                planner_dag_self_repair_result = self_repair_result["workflow"]
                raw_planner_dag_self_repair_output = self_repair_result["raw_output"]
                if self_repair_result["accepted"]:
                    dag_validation = self_repair_result["validation"]
                    dag_validation["status"] = "planner_self_repaired_passed"
                    result = dag_validation.get("workflow", planner_dag_self_repair_result)
                    selection_reason = f"{dag_replan_selection_reason}; planner_dag_self_repair_passed"
                elif self_repair_result.get("workflow"):
                    repair_base_workflow = self_repair_result["workflow"]
                    repair_base_validation = self_repair_result["validation"]
            if planner_dag_self_repair_status == "accepted":
                pass
            else:
                dag_validation = repair_base_validation
                repair_result = repair_workflow_dag(
                    repair_base_workflow,
                    user_request=get_first(coverage_row, "user_request"),
                    tool_catalog=tool_catalog,
                    transition_index=transition_index,
                    dependency_type=dependency_type,
                )
                dag_repair_status = repair_result["status"]
                dag_repair_operations = repair_result["operations"]
                repaired_validation = validate_workflow_dag(
                    repair_result["workflow"],
                    tool_catalog=tool_catalog,
                    transition_index=transition_index,
                    require_tool_graph_edge=require_tool_graph_edge,
                    dependency_type=dependency_type,
                )
                repaired_validation.setdefault("warnings", [])
                repaired_validation["warnings"] = (
                    ["initial_validation_errors=" + json.dumps(dag_validation.get("errors", []), ensure_ascii=False)]
                    + repair_result["warnings"]
                    + repaired_validation["warnings"]
                )
                dag_validation = repaired_validation
                if dag_validation["status"] == "passed":
                    dag_validation["status"] = "repaired_passed"
                    result = dag_validation.get("workflow", repair_result["workflow"])
                    selection_reason = f"{dag_replan_selection_reason}; dag_repair_passed"
                else:
                    dag_repair_status = "failed"
                    warnings.append(
                        "dag_validation_failed="
                        + json.dumps(dag_validation.get("errors", []), ensure_ascii=False, separators=(",", ":"))
                    )
                    result = dict(previous_workflow)
                    result_source = "previous_workflow"
                    selection_reason = "dag_validation_failed: used previous workflow"
    keep_original_verifier: Dict[str, Any] = {}
    if result.get("task_nodes"):
        keep_repair_result = maybe_apply_keep_original_graph_repair(
            workflow=result,
            decision=decision,
            result_source=result_source,
            selection_reason=selection_reason,
            dag_validation=dag_validation,
            dag_repair_status=dag_repair_status,
            dag_repair_operations=dag_repair_operations,
            coverage_row=coverage_row,
            dataset_config=dataset_config,
            tool_catalog=tool_catalog,
            transition_index=transition_index,
            require_tool_graph_edge=require_tool_graph_edge,
            warnings=warnings,
            dependency_type=dependency_type,
        )
        result = keep_repair_result["workflow"]
        result_source = keep_repair_result["result_source"]
        selection_reason = keep_repair_result["selection_reason"]
        dag_validation = keep_repair_result["dag_validation"]
        dag_repair_status = keep_repair_result["dag_repair_status"]
        dag_repair_operations = keep_repair_result["dag_repair_operations"]
        keep_original_verifier = keep_repair_result["verifier"]
    pre_temporal_link_repair_result = copy.deepcopy(result)
    pre_temporal_link_repair_result_source = result_source
    pre_temporal_link_repair_selection_reason = selection_reason
    temporal_link_repair = apply_temporal_link_repair_stage(
        workflow=result,
        result_source=result_source,
        selection_reason=selection_reason,
        coverage_row=coverage_row,
        tool_catalog=tool_catalog,
        transition_index=transition_index,
        dependency_type=dependency_type,
        temporal_chain_prior=temporal_chain_prior,
        temporal_edge_only_scope=temporal_edge_only_scope,
        edge_only_replan_runner=edge_only_replan_runner,
        warnings=warnings,
    )
    result = temporal_link_repair["workflow"]
    result_source = temporal_link_repair["result_source"]
    selection_reason = temporal_link_repair["selection_reason"]
    if temporal_link_repair.get("dag_validation") is not None:
        dag_validation = temporal_link_repair["dag_validation"]
    hint_assessment = normalize_list(planner_payload.get("hint_assessment"))
    coverage_assessment = normalize_list(planner_payload.get("coverage_assessment"))
    if not coverage_assessment:
        coverage_assessment = hint_assessment
    model_tool_original = get_first(coverage_row, "model tool")
    intent_tool_hint = get_first(coverage_row, "intent tool")
    intent_hint = get_first(coverage_row, "intent")
    previous_tool_evidence = " -> ".join(split_terms(model_tool_original) + workflow_tool_names(previous_workflow))
    tool_hint_coverage = split_tool_hint_coverage(
        previous_tool_evidence,
        intent_tool_hint,
        semantic_tool_families,
    )
    intent_hint_coverage = split_intent_hint_coverage(
        previous_tool_evidence,
        intent_hint,
        tool_catalog,
    )
    change_summary = planner_payload.get("change_summary") or planner_payload.get("reason", "")
    agent_trace = build_agent_trace(
        coverage_row=coverage_row,
        previous_workflow=previous_workflow,
        model_tool_original=model_tool_original,
        intent_tool_hint=intent_tool_hint,
        intent_hint=intent_hint,
        tool_hint_coverage=tool_hint_coverage,
        intent_hint_coverage=intent_hint_coverage,
        hint_assessment=hint_assessment,
        coverage_assessment=coverage_assessment,
        decision=decision,
        replanned_workflow=replanned_workflow,
        dag_replan_result=dag_replan_result,
        dag_replan_result_source=dag_replan_result_source,
        dag_replan_selection_reason=dag_replan_selection_reason,
        dag_validation=dag_validation,
        require_tool_graph_edge=require_tool_graph_edge,
        planner_dag_self_repair_status=planner_dag_self_repair_status,
        planner_dag_self_repair_decision=planner_dag_self_repair_decision,
        planner_dag_self_repair_result=planner_dag_self_repair_result,
        raw_planner_dag_self_repair_output=raw_planner_dag_self_repair_output,
        dag_repair_status=dag_repair_status,
        dag_repair_operations=dag_repair_operations,
        keep_original_verifier=keep_original_verifier,
        result=result,
        result_source=result_source,
        selection_reason=selection_reason,
        change_summary=change_summary,
        raw_planner_output=raw_planner_output,
        warnings=warnings,
    )
    agent_trace["graph_context"] = {
        "variant": replan_variant,
        "enabled": replan_variant == REPLAN_VARIANT_WITH_GRAPH,
        "candidate_count": len(graph_candidate_list),
        "candidates": graph_candidate_list,
    }
    agent_trace["temporal_link_repair"] = temporal_link_repair["trace"]

    return {
        "id": case_id,
        "type": get_first(coverage_row, "type"),
        "user_request": get_first(coverage_row, "user_request"),
        "model_tool_original": model_tool_original,
        "intent_tool_hint": intent_tool_hint,
        "intent_hint": intent_hint,
        "covered_intent_tool_hint": format_hint_terms(tool_hint_coverage["covered"]),
        "missing_intent_tool_hint": format_hint_terms(tool_hint_coverage["missing"]),
        "covered_intent_hint": format_hint_terms(intent_hint_coverage["covered"]),
        "missing_intent_hint": format_hint_terms(intent_hint_coverage["missing"]),
        "replan_variant": replan_variant,
        "replan_dependency_type": dependency_type,
        "graph_candidate_count": len(graph_candidate_list),
        "graph_candidates": graph_candidate_list,
        "replan_decision": decision,
        "result_source": result_source,
        "result_source_generic": generic_result_source(result_source),
        "result": result,
        "previous_workflow": dict(previous_workflow),
        "replanned_workflow": replanned_workflow,
        "dag_replan_result_source": dag_replan_result_source,
        "dag_replan_result_source_generic": generic_result_source(dag_replan_result_source),
        "dag_replan_result": dag_replan_result,
        "dag_replan_selection_reason": dag_replan_selection_reason,
        "dag_validation_status": dag_validation.get("status", ""),
        "dag_validation_errors": dag_validation.get("errors", []),
        "dag_validation_warnings": dag_validation.get("warnings", []),
        "planner_dag_self_repair_status": planner_dag_self_repair_status,
        "planner_dag_self_repair_decision": planner_dag_self_repair_decision,
        "planner_dag_self_repair_result": planner_dag_self_repair_result,
        "raw_planner_dag_self_repair_output": raw_planner_dag_self_repair_output,
        "dag_repair_status": dag_repair_status,
        "dag_repair_operations": dag_repair_operations,
        "keep_original_verifier": keep_original_verifier,
        "pre_temporal_link_repair_result": pre_temporal_link_repair_result,
        "pre_temporal_link_repair_result_source": pre_temporal_link_repair_result_source,
        "pre_temporal_link_repair_selection_reason": pre_temporal_link_repair_selection_reason,
        "temporal_link_repair": temporal_link_repair["trace"],
        "coverage_assessment": coverage_assessment,
        "hint_assessment": hint_assessment,
        "selection_reason": selection_reason,
        "change_summary": change_summary,
        "raw_planner_output": raw_planner_output,
        "agent_trace": agent_trace,
        "warnings": warnings,
    }


def run_planner_dag_self_repair_attempt(
    dag_self_repair_runner: Callable[..., Dict[str, Any]],
    user_request: str,
    previous_workflow: Mapping[str, Any],
    candidate_workflow: Mapping[str, Any],
    initial_validation: Mapping[str, Any],
    coverage_row: Mapping[str, Any],
    tool_catalog: Mapping[str, Dict[str, Any]] | None,
    transition_index: Mapping[Tuple[str, str], Any] | None,
    require_tool_graph_edge: bool,
    warnings: List[str],
    dependency_type: str = "resource",
) -> Dict[str, Any]:
    dependency_type = normalize_dependency_type(dependency_type)
    raw_output = ""
    payload: Mapping[str, Any] = {}
    status = "called"
    try:
        runner_result = dag_self_repair_runner(
            user_request=user_request,
            previous_workflow=previous_workflow,
            candidate_workflow=candidate_workflow,
            validation=initial_validation,
            coverage_row=coverage_row,
        )
        payload = runner_result.get("payload") or {}
        raw_output = str(runner_result.get("raw_output") or "")
        for warning in normalize_list(runner_result.get("warnings")):
            warnings.append(f"planner_dag_self_repair_warning: {warning}")
    except Exception as exc:  # noqa: BLE001 - row-level self-repair failure should not stop the run.
        warnings.append(f"planner_dag_self_repair_error: {type(exc).__name__}: {exc}")
        return {
            "accepted": False,
            "status": "call_failed",
            "decision": "",
            "workflow": {},
            "validation": initial_validation,
            "raw_output": raw_output,
        }

    decision = str(payload.get("repair_decision") or payload.get("decision") or "").strip().upper()
    repaired_workflow = normalize_dag_self_repair_workflow(payload)
    canonicalize_workflow_tool_names(
        repaired_workflow,
        canonical_tools=(
            workflow_tool_names(previous_workflow)
            + workflow_tool_names(candidate_workflow)
            + split_terms(get_first(coverage_row, "intent tool"))
            + catalog_tool_ids(tool_catalog)
        ),
    )
    guard_errors = validate_planner_dag_self_repair_change(
        candidate_workflow=candidate_workflow,
        repaired_workflow=repaired_workflow,
        planner_payload=payload,
        coverage_row=coverage_row,
        initial_validation=initial_validation,
        tool_catalog=tool_catalog,
    )
    if guard_errors:
        warnings.append(
            "planner_dag_self_repair_rejected="
            + json.dumps(guard_errors, ensure_ascii=False, separators=(",", ":"))
        )
        return {
            "accepted": False,
            "status": "rejected",
            "decision": decision,
            "workflow": repaired_workflow,
            "validation": initial_validation,
            "raw_output": raw_output,
        }

    validation = validate_workflow_dag(
        repaired_workflow,
        tool_catalog=tool_catalog,
        transition_index=transition_index,
        require_tool_graph_edge=require_tool_graph_edge,
        dependency_type=dependency_type,
    )
    validation.setdefault("warnings", [])
    validation["warnings"] = (
        ["initial_validation_errors=" + json.dumps(initial_validation.get("errors", []), ensure_ascii=False)]
        + ["planner_dag_self_repair_decision=" + decision]
        + validation["warnings"]
    )
    if validation["status"] == "passed":
        return {
            "accepted": True,
            "status": "accepted",
            "decision": decision,
            "workflow": validation.get("workflow", repaired_workflow),
            "validation": validation,
            "raw_output": raw_output,
        }
    warnings.append(
        "planner_dag_self_repair_validation_failed="
        + json.dumps(validation.get("errors", []), ensure_ascii=False, separators=(",", ":"))
    )
    return {
        "accepted": False,
        "status": "validation_failed",
        "decision": decision,
        "workflow": repaired_workflow,
        "validation": validation,
        "raw_output": raw_output,
    }


def normalize_dag_self_repair_workflow(planner_payload: Mapping[str, Any]) -> Dict[str, Any]:
    workflow = normalize_workflow(planner_payload.get("repaired_workflow"))
    if workflow.get("task_nodes"):
        return workflow
    return normalize_replanned_workflow(planner_payload)


def validate_planner_dag_self_repair_change(
    candidate_workflow: Mapping[str, Any],
    repaired_workflow: Mapping[str, Any],
    planner_payload: Mapping[str, Any],
    coverage_row: Mapping[str, Any],
    initial_validation: Mapping[str, Any],
    tool_catalog: Mapping[str, Dict[str, Any]] | None,
) -> List[str]:
    decision = str(planner_payload.get("repair_decision") or planner_payload.get("decision") or "").strip().upper()
    allowed_decisions = {"FIX_DAG_ONLY", "ADD_TOOL_FOR_MISSING_SLOT", "REMOVE_INVALID_TOOL", "CANNOT_REPAIR"}
    if decision not in allowed_decisions:
        return [f"invalid_repair_decision={decision!r}"]
    if decision == "CANNOT_REPAIR":
        return ["cannot_repair"]
    if not repaired_workflow.get("task_nodes"):
        return ["empty_repaired_workflow"]

    candidate_tools = workflow_tool_names(candidate_workflow)
    repaired_tools = workflow_tool_names(repaired_workflow)
    added_tools = tools_missing_from_candidate(repaired_tools, candidate_tools)
    removed_tools = tools_missing_from_candidate(candidate_tools, repaired_tools)
    if decision == "FIX_DAG_ONLY" and (added_tools or removed_tools):
        return ["fix_dag_only_changed_tools"]
    if decision == "ADD_TOOL_FOR_MISSING_SLOT":
        errors: List[str] = []
        if removed_tools:
            errors.append("add_tool_for_missing_slot_removed_tools=" + json.dumps(removed_tools, ensure_ascii=False))
        disallowed_additions = disallowed_self_repair_added_tools(
            added_tools=added_tools,
            coverage_row=coverage_row,
            initial_validation=initial_validation,
            tool_catalog=tool_catalog,
        )
        if disallowed_additions:
            errors.append(
                "add_tool_for_missing_slot_disallowed_tools="
                + json.dumps(disallowed_additions, ensure_ascii=False)
            )
        return errors
    if decision == "REMOVE_INVALID_TOOL":
        errors = []
        if added_tools:
            errors.append("remove_invalid_tool_added_tools=" + json.dumps(added_tools, ensure_ascii=False))
        unsafe_removed = [
            tool
            for tool in removed_tools
            if lookup_catalog_tool(tool_catalog or {}, tool).get("known", False)
        ]
        if unsafe_removed:
            errors.append("remove_invalid_tool_removed_known_tools=" + json.dumps(unsafe_removed, ensure_ascii=False))
        return errors
    return []


def disallowed_self_repair_added_tools(
    added_tools: List[str],
    coverage_row: Mapping[str, Any],
    initial_validation: Mapping[str, Any],
    tool_catalog: Mapping[str, Dict[str, Any]] | None,
) -> List[str]:
    if not added_tools:
        return []
    intent_hint_keys = {
        normalize_key(tool)
        for tool in split_terms(get_first(coverage_row, "intent tool"))
    }
    missing_slots = set(initial_missing_input_slots(initial_validation))
    disallowed: List[str] = []
    for tool in added_tools:
        tool_info = lookup_catalog_tool(tool_catalog or {}, tool)
        output_types = bg.normalize_type_set(tool_info.get("output_types", []))
        if normalize_key(tool) in intent_hint_keys:
            continue
        if missing_slots.intersection(output_types):
            continue
        disallowed.append(tool)
    return disallowed


def initial_missing_input_slots(validation: Mapping[str, Any]) -> List[str]:
    slots: List[str] = []
    for status in normalize_list(validation.get("slot_status")):
        if not isinstance(status, Mapping):
            continue
        for slot in normalize_list(status.get("missing_slots")):
            text = str(slot or "").strip()
            if text:
                slots.append(text)
    return dedupe_preserve_order(slots)


def catalog_tool_ids(tool_catalog: Mapping[str, Dict[str, Any]] | None) -> List[str]:
    if not tool_catalog:
        return []
    return [str(tool.get("id") or "").strip() for tool in tool_catalog.values() if str(tool.get("id") or "").strip()]


def select_final_workflow(
    previous_workflow: Mapping[str, Any],
    replanned_workflow: Mapping[str, Any],
    decision: str,
    coverage_row: Mapping[str, Any],
    planner_payload: Mapping[str, Any],
    warnings: List[str],
    semantic_tool_families: Mapping[str, Any] | None = None,
) -> Tuple[Dict[str, Any], str, str]:
    previous = dict(previous_workflow)
    candidate = dict(replanned_workflow)
    if decision in {
        "KEEP_ORIGINAL",
        "REJECT_INTENT_HINT",
        "DRY_RUN",
        PLANNER_CALL_FAILED_DECISION,
        *LEGACY_PLANNER_CALL_FAILED_DECISIONS,
    }:
        return previous, "previous_workflow", f"{decision.lower() or 'keep_original'}: used previous workflow"

    allowed_replan_decisions = {"ADD_MISSING_TOOLS", "REPLACE_WRONG_TOOLS", "REPLAN", "REBUILD_WITH_ACCEPTED_HINTS"}
    if decision not in allowed_replan_decisions:
        warnings.append(f"invalid_or_empty_replan_decision={decision!r}; used previous workflow")
        return previous, "previous_workflow", "invalid_or_empty_decision: used previous workflow"
    if not replanned_workflow.get("task_nodes"):
        warnings.append("empty_replanned_workflow; used previous workflow")
        return previous, "previous_workflow", "empty_replanned_workflow: used previous workflow"

    previous_tools = workflow_tool_names(previous_workflow)
    candidate_tools = workflow_tool_names(replanned_workflow)
    intent_hint_tools = split_terms(get_first(coverage_row, "intent tool"))
    deleted_tools = tools_missing_from_candidate(previous_tools, candidate_tools)
    added_tools = tools_missing_from_candidate(candidate_tools, previous_tools)
    same_family_replacements = semantic_family_replacements(
        deleted_tools,
        added_tools,
        semantic_tool_families=semantic_tool_families,
    )
    if same_family_replacements:
        warnings.append(
            "same_semantic_family_replacement="
            + json.dumps(same_family_replacements, ensure_ascii=False, separators=(",", ":"))
            + "; used previous workflow"
        )
        return previous, "previous_workflow", "same_semantic_family_replacement: used previous workflow"

    if decision == "REBUILD_WITH_ACCEPTED_HINTS":
        accepted_hint_tools = accepted_intent_hint_tools(planner_payload, intent_hint_tools)
        final_tools = canonicalize_tool_list(
            normalize_list(planner_payload.get("final_tools")) or candidate_tools,
            canonical_tools=previous_tools + intent_hint_tools,
        )
        invalid_tools = tools_outside_allowed_set(
            candidate_tools,
            allowed_tools=previous_tools + accepted_hint_tools,
        )
        if invalid_tools:
            warnings.append(
                "rebuild_used_tools_outside_original_plus_accepted_hints="
                + json.dumps(invalid_tools, ensure_ascii=False)
                + "; used previous workflow"
            )
            return previous, "previous_workflow", "rebuild_used_disallowed_tools: used previous workflow"
        if final_tools and [normalize_key(tool) for tool in final_tools] != [normalize_key(tool) for tool in candidate_tools]:
            warnings.append("final_tools_do_not_match_task_nodes; used previous workflow")
            return previous, "previous_workflow", "final_tools_mismatch: used previous workflow"
        added_missing_hints = tools_missing_from_candidate(accepted_hint_tools, previous_tools)
        added_missing_hints = [
            tool
            for tool in added_missing_hints
            if normalize_key(tool) in {normalize_key(candidate_tool) for candidate_tool in candidate_tools}
        ]
        if not added_missing_hints:
            warnings.append("rebuild_without_accepted_new_hint; used previous workflow")
            return previous, "previous_workflow", "no_accepted_new_hint: used previous workflow"
    else:
        added_missing_hints = tools_missing_from_candidate(intent_hint_tools, previous_tools)
        added_missing_hints = [
            tool
            for tool in added_missing_hints
            if normalize_key(tool) in {normalize_key(candidate_tool) for candidate_tool in candidate_tools}
        ]

    if deleted_tools and not deleted_tools_are_justified(planner_payload, deleted_tools):
        warnings.append(
            "unsafe_replan_deleted_original_tools="
            + json.dumps(deleted_tools, ensure_ascii=False)
            + "; used previous workflow"
        )
        return previous, "previous_workflow", "unsafe_deletion_without_evidence: used previous workflow"

    if not deleted_tools and added_missing_hints and added_tools_look_like_equivalent_shortcuts(planner_payload, added_missing_hints):
        warnings.append(
            "redundant_shortcut_tools="
            + json.dumps(added_missing_hints, ensure_ascii=False)
            + "; used previous workflow"
        )
        return previous, "previous_workflow", "redundant_equivalent_shortcut: used previous workflow"

    if decision == "ADD_MISSING_TOOLS" and not added_missing_hints:
        warnings.append("add_missing_tools_without_added_hint; used previous workflow")
        return previous, "previous_workflow", "no_missing_hint_added: used previous workflow"

    if decision == "REPLAN" and deleted_tools:
        warnings.append("legacy_replan_deleted_original_tools; used previous workflow")
        return previous, "previous_workflow", "legacy_replan_deleted_tools: used previous workflow"

    return candidate, "replan", "accepted_conservative_replan"


def normalize_workflow(value: Any) -> Dict[str, Any]:
    workflow = parse_jsonish(value)
    if not isinstance(workflow, dict):
        return {"task_steps": [], "task_nodes": [], "task_links": []}
    return {
        "task_steps": normalize_list(workflow.get("task_steps")),
        "task_nodes": normalize_list(workflow.get("task_nodes")),
        "task_links": normalize_list(workflow.get("task_links")),
    }


def normalize_replanned_workflow(planner_payload: Mapping[str, Any]) -> Dict[str, Any]:
    workflow = normalize_workflow(planner_payload.get("replanned_workflow"))
    if workflow.get("task_nodes"):
        return workflow
    workflow = normalize_workflow(planner_payload.get("workflow"))
    if workflow.get("task_nodes"):
        return workflow
    task_nodes = normalize_list(planner_payload.get("replanned_task_nodes"))
    return {
        "task_steps": normalize_list(planner_payload.get("replanned_task_steps")),
        "task_nodes": task_nodes,
        "task_links": normalize_list(planner_payload.get("replanned_task_links")),
    }


def compact_task_nodes(workflow: Mapping[str, Any]) -> List[Dict[str, Any]]:
    compact: List[Dict[str, Any]] = []
    for node in normalize_list(workflow.get("task_nodes")):
        if not isinstance(node, Mapping):
            continue
        compact.append(
            {
                "task": str(node.get("task") or "").strip(),
                "arguments": normalize_list(node.get("arguments")),
            }
        )
    return compact


def compact_task_links(workflow: Mapping[str, Any]) -> List[Dict[str, Any]]:
    compact: List[Dict[str, Any]] = []
    for link in normalize_list(workflow.get("task_links")):
        if not isinstance(link, Mapping):
            continue
        source = first_non_empty(link.get("source"), link.get("from"))
        target = first_non_empty(link.get("target"), link.get("to"))
        if source and target:
            compact.append({"source": source, "target": target})
    return compact


def compact_tool_desc(tool_desc: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    compact: List[Dict[str, Any]] = []
    for tool in tool_desc:
        item = {
            "tool_id": tool.get("tool_id", ""),
            "intent": tool.get("intent", ""),
            "in": normalize_list(tool.get("input_types")),
            "out": normalize_list(tool.get("output_types")),
        }
        parameters = normalize_tool_parameters(tool.get("parameters"))
        if parameters:
            item["parameters"] = parameters
        desc = str(tool.get("desc") or "").strip()
        if desc:
            item["desc"] = desc
        compact.append(item)
    return compact


def normalize_tool_parameters(parameters: Any) -> List[Dict[str, str]]:
    compact: List[Dict[str, str]] = []
    for parameter in normalize_list(parameters):
        if isinstance(parameter, Mapping):
            name = str(parameter.get("name") or "").strip()
            if not name:
                continue
            item = {"name": name}
            param_type = str(parameter.get("type") or "").strip()
            if param_type:
                item["type"] = param_type
            desc = str(parameter.get("desc") or parameter.get("description") or "").strip()
            if desc:
                item["desc"] = desc
            compact.append(item)
        else:
            name = str(parameter or "").strip()
            if name:
                compact.append({"name": name})
    return compact


def compact_tool_graph_edges(
    *,
    transition_index: Mapping[Tuple[str, str], Any],
    tool_catalog: Mapping[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not transition_index or not tool_catalog:
        return []
    tools = dedupe_preserve_order(
        str(info.get("id") or "").strip()
        for info in tool_catalog.values()
        if isinstance(info, Mapping) and info.get("known") and str(info.get("id") or "").strip()
    )
    edges: List[Dict[str, Any]] = []
    for source in tools:
        for target in tools:
            if normalize_key(source) == normalize_key(target):
                continue
            probability = safe_float(bg.get_transition_probability(transition_index, source, target))
            if probability is None or probability <= 0:
                continue
            slot = graph_candidate_target_slot(source, target, tool_catalog)
            if not slot:
                continue
            edges.append(
                {
                    "source": source,
                    "target": target,
                    "target_input_slot": slot,
                    "transition_probability": round(probability, 6),
                }
            )
    edges.sort(
        key=lambda edge: (
            str(edge.get("source") or ""),
            -float(edge.get("transition_probability") or 0.0),
            str(edge.get("target") or ""),
        )
    )
    return edges


def build_local_graph_candidates(
    *,
    previous_workflow: Mapping[str, Any],
    previous_tool_summary: Any,
    missing_intent_tools: Iterable[Any],
    tool_catalog: Mapping[str, Dict[str, Any]],
    transition_index: Mapping[Tuple[str, str], Any],
    top_k_per_source: int = DEFAULT_GRAPH_CONTEXT_TOP_K_PER_SOURCE,
    max_candidates: int = DEFAULT_GRAPH_CONTEXT_MAX_CANDIDATES,
) -> List[Dict[str, Any]]:
    if not transition_index or not tool_catalog:
        return []
    top_k_per_source = max(1, int(top_k_per_source or DEFAULT_GRAPH_CONTEXT_TOP_K_PER_SOURCE))
    max_candidates = max(1, int(max_candidates or DEFAULT_GRAPH_CONTEXT_MAX_CANDIDATES))
    previous_tools = canonicalize_graph_context_tools(
        split_terms(previous_tool_summary) + workflow_tool_names(previous_workflow),
        tool_catalog,
    )
    missing_tools = canonicalize_graph_context_tools(missing_intent_tools, tool_catalog)
    if not previous_tools or not missing_tools:
        return []

    previous_keys = {normalize_key(tool) for tool in previous_tools}
    missing_keys = {normalize_key(tool) for tool in missing_tools}
    relevant_tools = dedupe_preserve_order(previous_tools + missing_tools)
    existing_pairs = existing_previous_tool_edge_pairs(previous_workflow, tool_catalog)
    candidates: List[Dict[str, Any]] = []
    for source in relevant_tools:
        for target in relevant_tools:
            source_key = normalize_key(source)
            target_key = normalize_key(target)
            if not source_key or not target_key or source_key == target_key:
                continue
            probability = safe_float(bg.get_transition_probability(transition_index, source, target))
            if probability is None or probability <= 0:
                continue
            slot = graph_candidate_target_slot(source, target, tool_catalog)
            if not slot:
                continue
            relation = graph_candidate_relation(source_key, target_key, previous_keys, missing_keys, existing_pairs)
            if not relation:
                continue
            touches_missing = source_key in missing_keys or target_key in missing_keys
            is_existing = (source_key, target_key) in existing_pairs
            score = probability * 100.0 + (30.0 if is_existing else 0.0) + (20.0 if touches_missing else 0.0)
            candidates.append(
                {
                    "source": source,
                    "target": target,
                    "edge_type": slot,
                    "target_input_slot": slot,
                    "transition_probability": round(probability, 6),
                    "relation": relation,
                    "_score": score,
                }
            )

    candidates.sort(key=lambda item: (item["_score"], item["transition_probability"]), reverse=True)
    selected: List[Dict[str, Any]] = []
    selected_by_source: Dict[str, int] = {}
    for candidate in candidates:
        source_key = normalize_key(candidate.get("source"))
        if selected_by_source.get(source_key, 0) >= top_k_per_source:
            continue
        selected_by_source[source_key] = selected_by_source.get(source_key, 0) + 1
        compact = dict(candidate)
        compact.pop("_score", None)
        selected.append(compact)
        if len(selected) >= max_candidates:
            break
    return selected


def canonicalize_graph_context_tools(
    tools: Iterable[Any],
    tool_catalog: Mapping[str, Dict[str, Any]],
) -> List[str]:
    result: List[str] = []
    for tool in normalize_hint_terms(tools):
        info = lookup_catalog_tool(tool_catalog, tool)
        canonical = str(info.get("id") or tool).strip()
        if canonical and info.get("known", False):
            result.append(canonical)
    return dedupe_preserve_order(result)


def existing_previous_tool_edge_pairs(
    workflow: Mapping[str, Any],
    tool_catalog: Mapping[str, Dict[str, Any]],
) -> set[Tuple[str, str]]:
    nodes = normalize_nodes_for_repair(workflow.get("task_nodes"))
    pairs: set[Tuple[str, str]] = set()
    if not nodes:
        return pairs
    explicit_edges, _ = normalize_task_links_to_edges(workflow.get("task_links"), nodes)
    argument_edges, _ = materialize_edges_from_node_arguments(nodes)
    for edge in dedupe_edges(explicit_edges + argument_edges):
        try:
            source = int(edge["source"])
            target = int(edge["target"])
        except (KeyError, TypeError, ValueError):
            continue
        if not valid_node_index(source, len(nodes)) or not valid_node_index(target, len(nodes)):
            continue
        source_tool = lookup_catalog_tool(tool_catalog, nodes[source].get("task", ""))
        target_tool = lookup_catalog_tool(tool_catalog, nodes[target].get("task", ""))
        source_key = normalize_key(source_tool.get("id"))
        target_key = normalize_key(target_tool.get("id"))
        if source_key and target_key:
            pairs.add((source_key, target_key))
    return pairs


def graph_candidate_target_slot(
    source: str,
    target: str,
    tool_catalog: Mapping[str, Dict[str, Any]],
) -> str:
    source_tool = lookup_catalog_tool(tool_catalog, source)
    target_tool = lookup_catalog_tool(tool_catalog, target)
    if not source_tool.get("known", False) or not target_tool.get("known", False):
        return ""
    source_types = bg.normalize_type_set(source_tool.get("output_types", []))
    target_slots = bg.normalize_type_list(target_tool.get("input_types", []))
    if not source_types or not target_slots:
        return ""
    for slot in target_slots:
        if slot in source_types or "any" in source_types or "*" in source_types:
            return slot
    if "any" in target_slots or "*" in target_slots:
        return target_slots[0]
    return ""


def graph_candidate_relation(
    source_key: str,
    target_key: str,
    previous_keys: set[str],
    missing_keys: set[str],
    existing_pairs: set[Tuple[str, str]],
) -> str:
    if (source_key, target_key) in existing_pairs:
        return "existing_previous_edge"
    source_previous = source_key in previous_keys
    target_previous = target_key in previous_keys
    source_missing = source_key in missing_keys
    target_missing = target_key in missing_keys
    if source_previous and target_missing:
        return "previous_to_missing"
    if source_missing and target_previous:
        return "missing_to_previous"
    if source_previous and target_previous:
        return "previous_to_previous"
    if source_missing and target_missing:
        return "missing_to_missing"
    return ""


def safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def workflow_tool_names(workflow: Mapping[str, Any]) -> List[str]:
    tools: List[str] = []
    for node in normalize_list(workflow.get("task_nodes")):
        if not isinstance(node, Mapping):
            continue
        task = str(node.get("task") or "").strip()
        if task:
            tools.append(task)
    return tools


def infer_original_workflow_structure(workflow: Mapping[str, Any]) -> str:
    tools = workflow_tool_names(workflow)
    if not tools:
        return "empty"
    if len(tools) == 1:
        return "single"
    links = compact_task_links(workflow)
    if len(links) > len(tools) - 1:
        return "dag"
    return "chain"


def canonicalize_workflow_tool_names(workflow: Mapping[str, Any], canonical_tools: Iterable[Any]) -> None:
    by_alias = tool_alias_map(canonical_tools)
    for node in normalize_list(workflow.get("task_nodes")):
        if not isinstance(node, dict):
            continue
        task = str(node.get("task") or "").strip()
        canonical = by_alias.get(normalize_key(task))
        if canonical:
            node["task"] = canonical


def canonicalize_tool_list(tools: Iterable[Any], canonical_tools: Iterable[Any]) -> List[str]:
    by_alias = tool_alias_map(canonical_tools)
    result: List[str] = []
    for tool in tools:
        text = str(tool or "").strip()
        if not text:
            continue
        result.append(by_alias.get(normalize_key(text), text))
    return result


def tool_alias_map(canonical_tools: Iterable[Any]) -> Dict[str, str]:
    by_alias: Dict[str, str] = {}
    for tool in canonical_tools:
        text = str(tool or "").strip()
        if not text:
            continue
        aliases = {
            normalize_key(text),
            normalize_key(text.replace("Downloader", "Download")),
            normalize_key(text.replace("Download", "Downloader")),
        }
        if text.lower().endswith(" downloader"):
            base = text[: -len(" downloader")].strip()
            aliases.add(normalize_key(f"Download {base}"))
            aliases.add(normalize_key(f"{base} Download"))
        for alias in aliases:
            if alias:
                by_alias.setdefault(alias, text)
    return by_alias


def accepted_intent_hint_tools(planner_payload: Mapping[str, Any], intent_hint_tools: List[str]) -> List[str]:
    by_alias = tool_alias_map(intent_hint_tools)
    accepted_statuses = {
        "missingshouldadd",
        "previoustoolwrong",
        "replacementforexistingpath",
        "replaceexistingpath",
        "accepted",
    }
    accepted: List[str] = []
    for raw in normalize_list(planner_payload.get("accepted_hints")):
        canonical = by_alias.get(normalize_key(raw))
        if canonical:
            accepted.append(canonical)

    for item in normalize_list(planner_payload.get("hint_assessment")) + normalize_list(planner_payload.get("coverage_assessment")):
        if not isinstance(item, Mapping):
            continue
        status = normalize_key(item.get("status") or item.get("decision"))
        if status not in accepted_statuses:
            continue
        hint = first_non_empty(
            item.get("tool"),
            item.get("hint"),
            item.get("intent_tool"),
            item.get("intent_tool_or_intent"),
            item.get("intent_hint"),
        )
        canonical = by_alias.get(normalize_key(hint))
        if canonical:
            accepted.append(canonical)

    deduped: List[str] = []
    seen = set()
    for tool in accepted:
        key = normalize_key(tool)
        if key and key not in seen:
            seen.add(key)
            deduped.append(tool)
    return deduped


def tools_outside_allowed_set(candidate_tools: Iterable[Any], allowed_tools: Iterable[Any]) -> List[str]:
    allowed_keys = {normalize_key(tool) for tool in allowed_tools}
    outside: List[str] = []
    seen = set()
    for tool in candidate_tools:
        text = str(tool or "").strip()
        key = normalize_key(text)
        if not key or key in allowed_keys or key in seen:
            continue
        seen.add(key)
        outside.append(text)
    return outside


def tools_missing_from_candidate(required_tools: Iterable[Any], candidate_tools: Iterable[Any]) -> List[str]:
    candidate_keys = {normalize_key(tool) for tool in candidate_tools}
    missing: List[str] = []
    seen = set()
    for tool in required_tools:
        text = str(tool or "").strip()
        key = normalize_key(text)
        if not key or key in candidate_keys or key in seen:
            continue
        seen.add(key)
        missing.append(text)
    return missing


def deleted_tools_are_justified(planner_payload: Mapping[str, Any], deleted_tools: List[str]) -> bool:
    if not deleted_tools:
        return True

    evidence_items = (
        normalize_list(planner_payload.get("original_tool_changes"))
        + normalize_list(planner_payload.get("hint_assessment"))
        + normalize_list(planner_payload.get("coverage_assessment"))
    )
    evidence_texts = [json.dumps(item, ensure_ascii=False).lower() for item in evidence_items]
    evidence_keys = [normalize_key(text) for text in evidence_texts]
    justification_keywords = (
        "wrong",
        "incorrect",
        "mismatch",
        "notrequested",
        "notneeded",
        "replace",
        "replaced",
        "remove",
        "removed",
        "toogeneric",
        "specific",
    )
    for tool in deleted_tools:
        tool_key = normalize_key(tool)
        if not tool_key:
            continue
        has_evidence = any(
            tool_key in evidence_key and any(keyword in evidence_key for keyword in justification_keywords)
            for evidence_key in evidence_keys
        )
        if not has_evidence:
            return False
    return True


def added_tools_look_like_equivalent_shortcuts(planner_payload: Mapping[str, Any], added_tools: List[str]) -> bool:
    evidence_items = (
        normalize_list(planner_payload.get("original_tool_changes"))
        + normalize_list(planner_payload.get("hint_assessment"))
        + normalize_list(planner_payload.get("coverage_assessment"))
        + [planner_payload.get("change_summary", "")]
    )
    evidence_keys = [normalize_key(json.dumps(item, ensure_ascii=False)) for item in evidence_items]
    shortcut_keywords = (
        "equivalent",
        "shortcut",
        "direct",
        "directly",
        "efficient",
        "efficiently",
        "sameintent",
        "samegoal",
        "sameuserintent",
    )
    for tool in added_tools:
        tool_key = normalize_key(tool)
        if not tool_key:
            continue
        if any(
            tool_key in evidence_key and any(keyword in evidence_key for keyword in shortcut_keywords)
            for evidence_key in evidence_keys
        ):
            return True
    return False


def semantic_family(tool: Any, semantic_tool_families: Mapping[str, Any] | None = None) -> str:
    if semantic_tool_families is None:
        return SEMANTIC_TOOL_FAMILY_BY_KEY.get(normalize_key(tool), "")
    return build_semantic_tool_family_index(resolve_semantic_tool_families(semantic_tool_families)).get(
        normalize_key(tool),
        "",
    )


def same_semantic_family(
    tool_a: Any,
    tool_b: Any,
    semantic_tool_families: Mapping[str, Any] | None = None,
) -> bool:
    family_a = semantic_family(tool_a, semantic_tool_families)
    return bool(family_a and family_a == semantic_family(tool_b, semantic_tool_families))


def semantic_family_replacements(
    deleted_tools: Iterable[Any],
    added_tools: Iterable[Any],
    semantic_tool_families: Mapping[str, Any] | None = None,
) -> List[Dict[str, str]]:
    replacements: List[Dict[str, str]] = []
    seen = set()
    family_index = (
        SEMANTIC_TOOL_FAMILY_BY_KEY
        if semantic_tool_families is None
        else build_semantic_tool_family_index(resolve_semantic_tool_families(semantic_tool_families))
    )
    for deleted in deleted_tools:
        deleted_text = str(deleted or "").strip()
        family = family_index.get(normalize_key(deleted_text), "")
        if not family:
            continue
        for added in added_tools:
            added_text = str(added or "").strip()
            key = (normalize_key(deleted_text), normalize_key(added_text))
            if key in seen:
                continue
            if family == family_index.get(normalize_key(added_text), ""):
                seen.add(key)
                replacements.append({"removed": deleted_text, "added": added_text, "family": family})
    return replacements


def validate_workflow_dag(
    workflow: Mapping[str, Any],
    tool_catalog: Mapping[str, Dict[str, Any]] | None = None,
    transition_index: Mapping[Tuple[str, str], Any] | None = None,
    require_tool_graph_edge: bool = False,
    dependency_type: str = "resource",
) -> Dict[str, Any]:
    dependency_type = normalize_dependency_type(dependency_type)
    if not tool_catalog:
        return {"status": "skipped_no_tool_catalog", "errors": [], "warnings": [], "workflow": dict(workflow)}

    normalized = normalize_workflow(workflow)
    nodes = []
    errors: List[str] = []
    validation_warnings: List[str] = []
    for index, raw_node in enumerate(normalize_list(normalized.get("task_nodes"))):
        if not isinstance(raw_node, Mapping):
            errors.append(f"node[{index}] is not an object")
            continue
        node = dict(raw_node)
        task = str(node.get("task") or "").strip()
        node["task"] = task
        node["arguments"] = normalize_list(node.get("arguments"))
        nodes.append(node)
        tool = lookup_catalog_tool(tool_catalog, task)
        if not tool.get("known", False):
            errors.append(f"unknown_tool node-{index}: {task}")

    if not nodes:
        return {"status": "failed", "errors": ["empty task_nodes"], "warnings": validation_warnings, "workflow": normalized}

    explicit_edges, link_errors = normalize_task_links_to_edges(normalized.get("task_links"), nodes)
    errors.extend(link_errors)
    if dependency_type == "temporal":
        edges = dedupe_edges(explicit_edges)
    else:
        argument_edges, argument_errors = materialize_edges_from_node_arguments(nodes)
        errors.extend(argument_errors)

        explicit_edge_keys = edge_key_set_from_pairs(explicit_edges)
        argument_edge_keys = edge_key_set_from_pairs(argument_edges)
        if explicit_edges:
            missing_links = sorted(argument_edge_keys - explicit_edge_keys)
            missing_arguments = sorted(explicit_edge_keys - argument_edge_keys)
            for source, target in missing_links:
                errors.append(f"argument_ref_without_task_link node-{source}->node-{target}")
            for source, target in missing_arguments:
                errors.append(f"task_link_without_argument_ref node-{source}->node-{target}")
            edges = dedupe_edges(explicit_edges)
        else:
            edges = dedupe_edges(argument_edges)
            if edges:
                validation_warnings.append("task_links_missing; materialized from <node-i> arguments")

    valid_edges: List[Dict[str, int]] = []
    for edge in edges:
        source = edge["source"]
        target = edge["target"]
        if not valid_node_index(source, len(nodes)) or not valid_node_index(target, len(nodes)):
            errors.append(f"invalid_edge node-{source}->node-{target}")
            continue
        if source == target or (dependency_type != "temporal" and source > target):
            errors.append(f"future_or_self_edge node-{source}->node-{target}")
            continue
        valid_edges.append(edge)
    edges = valid_edges

    if has_cycle(edges, len(nodes)):
        errors.append("cycle_detected")

    transition_index = transition_index or {}
    for edge in edges:
        source = edge["source"]
        target = edge["target"]
        source_tool = lookup_catalog_tool(tool_catalog, nodes[source].get("task", ""))
        target_tool = lookup_catalog_tool(tool_catalog, nodes[target].get("task", ""))
        if dependency_type == "resource":
            compatibility = bg.type_compatible(source_tool.get("output_types", []), target_tool.get("input_types", []))
            if compatibility is False:
                errors.append(
                    "type_mismatch "
                    f"node-{source}({source_tool.get('id')}:{source_tool.get('output_types', [])})"
                    f"->node-{target}({target_tool.get('id')}:{target_tool.get('input_types', [])})"
                )
            elif compatibility == "unknown":
                validation_warnings.append(f"type_unknown node-{source}->node-{target}")
        if transition_index:
            probability = bg.get_transition_probability(transition_index, source_tool.get("id"), target_tool.get("id"))
            if probability is None:
                message = f"tool_graph_edge_missing {source_tool.get('id')}-> {target_tool.get('id')}"
                if require_tool_graph_edge:
                    errors.append(message)
                else:
                    validation_warnings.append(message)

    slot_status = []
    if dependency_type == "resource":
        for target_index in range(len(nodes)):
            status = input_slot_status_for_node(target_index, nodes, edges, tool_catalog)
            slot_status.append(status)
            if status["missing_slots"]:
                errors.append(
                    "missing_required_input_slots "
                    f"node-{target_index}({nodes[target_index].get('task')}):"
                    + ",".join(status["missing_slots"])
                )

    normalized["task_nodes"] = nodes
    normalized["task_links"] = build_task_links_from_edges(nodes, edges)
    return {
        "status": "failed" if errors else "passed",
        "errors": errors,
        "warnings": validation_warnings,
        "workflow": normalized,
        "slot_status": slot_status,
    }


def repair_workflow_dag(
    workflow: Mapping[str, Any],
    user_request: str,
    tool_catalog: Mapping[str, Dict[str, Any]] | None,
    transition_index: Mapping[Tuple[str, str], Any] | None = None,
    dependency_type: str = "resource",
) -> Dict[str, Any]:
    dependency_type = normalize_dependency_type(dependency_type)
    if not tool_catalog:
        return {"status": "skipped_no_tool_catalog", "workflow": dict(workflow), "operations": [], "warnings": []}

    normalized = normalize_workflow(workflow)
    nodes = normalize_nodes_for_repair(normalized.get("task_nodes"))
    operations: List[Dict[str, Any]] = []
    repair_warnings: List[str] = []
    if not nodes:
        return {"status": "failed", "workflow": normalized, "operations": [], "warnings": ["empty task_nodes"]}

    explicit_edges, link_errors = normalize_task_links_to_edges(normalized.get("task_links"), nodes)
    if dependency_type == "temporal":
        if link_errors:
            repair_warnings.append("ignored_link_errors=" + json.dumps(link_errors, ensure_ascii=False))
        edges = dedupe_valid_edges(explicit_edges, len(nodes), operations, allow_backward=True)
        repaired = dict(normalized)
        repaired["task_nodes"] = nodes
        repaired["task_links"] = build_task_links_from_edges(nodes, edges)
        return {
            "status": "repaired" if operations else "unchanged",
            "workflow": repaired,
            "operations": operations,
            "warnings": repair_warnings,
        }

    argument_edges, argument_errors = materialize_edges_from_node_arguments(nodes)
    if link_errors:
        repair_warnings.append("ignored_link_errors=" + json.dumps(link_errors, ensure_ascii=False))
    if argument_errors:
        repair_warnings.append("ignored_argument_errors=" + json.dumps(argument_errors, ensure_ascii=False))

    edges = merge_repairable_edges(explicit_edges + argument_edges, nodes, tool_catalog, operations)
    rebuild_arguments_from_edges(nodes, edges, operations)
    fill_missing_literal_slots(nodes, edges, user_request, tool_catalog, operations)
    edges = add_unique_compatible_missing_edges(nodes, edges, tool_catalog, operations, transition_index)
    rebuild_arguments_from_edges(nodes, edges, operations)
    fill_missing_literal_slots(nodes, edges, user_request, tool_catalog, operations)

    repaired = dict(normalized)
    repaired["task_nodes"] = nodes
    repaired["task_links"] = build_task_links_from_edges(nodes, edges)
    return {
        "status": "repaired" if operations else "unchanged",
        "workflow": repaired,
        "operations": operations,
        "warnings": repair_warnings,
    }


def normalize_nodes_for_repair(raw_nodes: Any) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    for raw_node in normalize_list(raw_nodes):
        if not isinstance(raw_node, Mapping):
            continue
        node = copy.deepcopy(dict(raw_node))
        node["task"] = str(node.get("task") or "").strip()
        node["arguments"] = normalize_list(node.get("arguments"))
        nodes.append(node)
    return nodes


def merge_repairable_edges(
    raw_edges: Iterable[Mapping[str, int]],
    nodes: List[Dict[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]],
    operations: List[Dict[str, Any]],
) -> List[Dict[str, int]]:
    merged: List[Dict[str, int]] = []
    seen_raw: set[Tuple[int, int]] = set()
    seen: set[Tuple[int, int]] = set()
    for raw_edge in raw_edges:
        source = int(raw_edge.get("source", -1))
        target = int(raw_edge.get("target", -1))
        raw_pair = (source, target)
        if raw_pair in seen_raw:
            continue
        seen_raw.add(raw_pair)
        if not valid_node_index(source, len(nodes)) or not valid_node_index(target, len(nodes)):
            operations.append({"action": "drop_invalid_edge", "source": source, "target": target})
            continue
        if source >= target:
            operations.append({"action": "drop_future_or_self_edge", "source": source, "target": target})
            continue
        source_tool = lookup_catalog_tool(tool_catalog, nodes[source].get("task", ""))
        target_tool = lookup_catalog_tool(tool_catalog, nodes[target].get("task", ""))
        compatibility = bg.type_compatible(source_tool.get("output_types", []), target_tool.get("input_types", []))
        if compatibility is False:
            operations.append(
                {
                    "action": "drop_type_mismatch_edge",
                    "source": source,
                    "target": target,
                    "source_tool": source_tool.get("id"),
                    "target_tool": target_tool.get("id"),
                }
            )
            continue
        pair = (source, target)
        if pair in seen:
            continue
        seen.add(pair)
        merged.append({"source": source, "target": target})
    return merged


def rebuild_arguments_from_edges(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
    operations: List[Dict[str, Any]],
) -> None:
    incoming_by_target: Dict[int, List[int]] = {}
    for edge in edges:
        incoming_by_target.setdefault(edge["target"], []).append(edge["source"])
    for target, node in enumerate(nodes):
        old_arguments = normalize_list(node.get("arguments"))
        literal_arguments = [argument for argument in old_arguments if parse_node_reference(argument) is None]
        node_refs = [f"<node-{source}>" for source in sorted(set(incoming_by_target.get(target, [])))]
        new_arguments = dedupe_preserve_order(node_refs + literal_arguments)
        if new_arguments != old_arguments:
            operations.append(
                {
                    "action": "sync_arguments_with_edges",
                    "node": target,
                    "old_arguments": old_arguments,
                    "new_arguments": new_arguments,
                }
            )
            node["arguments"] = new_arguments


def fill_missing_literal_slots(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
    user_request: str,
    tool_catalog: Mapping[str, Dict[str, Any]],
    operations: List[Dict[str, Any]],
) -> None:
    request_literals = extract_request_literals_by_type(user_request)
    for target_index, node in enumerate(nodes):
        while True:
            status = input_slot_status_for_node(target_index, nodes, edges, tool_catalog)
            missing_slots = list(status.get("missing_slots", []))
            if not missing_slots:
                break
            changed = False
            for slot in missing_slots:
                literal = choose_literal_for_slot(slot, request_literals, normalize_list(node.get("arguments")))
                if literal is None:
                    continue
                node.setdefault("arguments", [])
                node["arguments"].append(literal)
                operations.append(
                    {
                        "action": "fill_literal_slot_from_request",
                        "node": target_index,
                        "slot": slot,
                        "literal": literal,
                    }
                )
                changed = True
                break
            if not changed:
                break


def add_unique_compatible_missing_edges(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
    tool_catalog: Mapping[str, Dict[str, Any]],
    operations: List[Dict[str, Any]],
    transition_index: Mapping[Tuple[str, str], Any] | None = None,
) -> List[Dict[str, int]]:
    repaired = dedupe_edges(edges)
    existing = edge_key_set_from_pairs(repaired)
    transition_index = transition_index or {}
    changed = True
    while changed:
        changed = False
        for target_index in range(len(nodes)):
            status = input_slot_status_for_node(target_index, nodes, repaired, tool_catalog)
            for slot in list(status.get("missing_slots", [])):
                candidates = []
                for source_index in range(target_index):
                    if (source_index, target_index) in existing:
                        continue
                    source_tool = lookup_catalog_tool(tool_catalog, nodes[source_index].get("task", ""))
                    source_types = bg.normalize_type_set(source_tool.get("output_types", []))
                    if slot in source_types or "any" in source_types or "*" in source_types:
                        candidates.append(source_index)
                source = choose_repair_edge_source(
                    candidates,
                    target_index=target_index,
                    nodes=nodes,
                    tool_catalog=tool_catalog,
                    transition_index=transition_index,
                )
                if source is None:
                    continue
                repaired.append({"source": source, "target": target_index})
                existing.add((source, target_index))
                transition_probability = repair_transition_probability(
                    source,
                    target_index,
                    nodes=nodes,
                    tool_catalog=tool_catalog,
                    transition_index=transition_index,
                )
                action = "add_unique_compatible_edge" if len(candidates) == 1 else "add_tool_graph_preferred_edge"
                operations.append(
                    {
                        "action": action,
                        "source": source,
                        "target": target_index,
                        "slot": slot,
                        "candidate_count": len(candidates),
                        "transition_probability": transition_probability,
                    }
                )
                changed = True
                break
            if changed:
                break
    return repaired


def choose_repair_edge_source(
    candidates: List[int],
    target_index: int,
    nodes: List[Dict[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]],
    transition_index: Mapping[Tuple[str, str], Any],
) -> int | None:
    if len(candidates) == 1:
        return candidates[0]
    if not candidates or not transition_index:
        return None
    scored = []
    for source_index in candidates:
        probability = repair_transition_probability(
            source_index,
            target_index,
            nodes=nodes,
            tool_catalog=tool_catalog,
            transition_index=transition_index,
        )
        if probability is None or probability <= 0:
            continue
        scored.append((probability, source_index))
    if not scored:
        return None
    scored.sort(reverse=True)
    best_probability, best_source = scored[0]
    if len(scored) > 1 and best_probability == scored[1][0]:
        return None
    return best_source


def repair_transition_probability(
    source_index: int,
    target_index: int,
    nodes: List[Dict[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]],
    transition_index: Mapping[Tuple[str, str], Any],
) -> float | None:
    if not transition_index:
        return None
    source_tool = lookup_catalog_tool(tool_catalog, nodes[source_index].get("task", ""))
    target_tool = lookup_catalog_tool(tool_catalog, nodes[target_index].get("task", ""))
    probability = bg.get_transition_probability(transition_index, source_tool.get("id"), target_tool.get("id"))
    if probability is None:
        return None
    try:
        return float(probability)
    except (TypeError, ValueError):
        return None


def maybe_apply_keep_original_graph_repair(
    *,
    workflow: Mapping[str, Any],
    decision: str,
    result_source: str,
    selection_reason: str,
    dag_validation: Mapping[str, Any],
    dag_repair_status: str,
    dag_repair_operations: List[Dict[str, Any]],
    coverage_row: Mapping[str, Any],
    dataset_config: Mapping[str, Any] | None,
    tool_catalog: Mapping[str, Dict[str, Any]] | None,
    transition_index: Mapping[Tuple[str, str], Any] | None,
    require_tool_graph_edge: bool,
    warnings: List[str],
    dependency_type: str = "resource",
) -> Dict[str, Any]:
    dependency_type = normalize_dependency_type(dependency_type)
    config = normalize_keep_original_graph_repair_config(
        get_keep_original_graph_repair_config(dataset_config)
    )
    if dependency_type == "temporal" or not config.get("enabled", False):
        return {
            "workflow": dict(workflow),
            "result_source": result_source,
            "selection_reason": selection_reason,
            "dag_validation": dict(dag_validation),
            "dag_repair_status": dag_repair_status,
            "dag_repair_operations": dag_repair_operations,
            "verifier": {},
        }

    assessment, repaired_workflow, repair_trace = assess_keep_original_graph_repair(
        workflow=workflow,
        decision=decision,
        result_source=result_source,
        coverage_row=coverage_row,
        tool_catalog=tool_catalog,
        transition_index=transition_index,
        optional_threshold=float(config["optional_threshold"]),
    )
    risk_threshold = float(config["risk_threshold"])
    assessment["risk_threshold"] = risk_threshold
    assessment["risk_flagged"] = bool(assessment.get("eligible")) and float(assessment.get("risk_score") or 0.0) >= risk_threshold
    assessment["repair_mode"] = "graph"
    assessment["repair_applied"] = False

    if not assessment["risk_flagged"]:
        return {
            "workflow": dict(workflow),
            "result_source": result_source,
            "selection_reason": selection_reason,
            "dag_validation": dict(dag_validation),
            "dag_repair_status": dag_repair_status,
            "dag_repair_operations": dag_repair_operations,
            "verifier": assessment,
        }

    if int((assessment.get("operation_counts") or {}).get("global_select_graph_edge", 0)) <= 0:
        assessment["repair_status"] = "skipped_no_graph_edge_candidate"
        return {
            "workflow": dict(workflow),
            "result_source": result_source,
            "selection_reason": selection_reason,
            "dag_validation": dict(dag_validation),
            "dag_repair_status": dag_repair_status,
            "dag_repair_operations": dag_repair_operations,
            "verifier": assessment,
        }

    repaired_validation = validate_workflow_dag(
        repaired_workflow,
        tool_catalog=tool_catalog,
        transition_index=transition_index,
        require_tool_graph_edge=require_tool_graph_edge,
    )
    assessment["repair_validation_status"] = repaired_validation.get("status", "")
    assessment["repair_validation_errors"] = repaired_validation.get("errors", [])
    assessment["repair_validation_warnings"] = repaired_validation.get("warnings", [])
    if repaired_validation.get("status") != "passed":
        assessment["repair_status"] = "validation_failed"
        warnings.append(
            "keep_original_graph_repair_validation_failed="
            + json.dumps(repaired_validation.get("errors", []), ensure_ascii=False, separators=(",", ":"))
        )
        return {
            "workflow": dict(workflow),
            "result_source": result_source,
            "selection_reason": selection_reason,
            "dag_validation": dict(dag_validation),
            "dag_repair_status": dag_repair_status,
            "dag_repair_operations": dag_repair_operations,
            "verifier": assessment,
        }

    final_workflow = repaired_validation.get("workflow", repaired_workflow)
    assessment["repair_status"] = "accepted"
    assessment["repair_applied"] = True
    repaired_validation = dict(repaired_validation)
    repaired_validation["status"] = "keep_original_graph_repaired_passed"
    repaired_validation.setdefault("warnings", [])
    repaired_validation["warnings"] = [
        "keep_original_verifier_risk_score=" + str(assessment.get("risk_score", 0.0))
    ] + list(repaired_validation.get("warnings", []))
    operations = list(dag_repair_operations)
    operations.append(
        {
            "action": "keep_original_verifier_flagged",
            "risk_score": assessment.get("risk_score"),
            "risk_threshold": risk_threshold,
            "reasons": assessment.get("reasons", []),
        }
    )
    operations.extend(normalize_list(repair_trace.get("operations")))
    return {
        "workflow": final_workflow,
        "result_source": "keep_original_graph_repair",
        "selection_reason": f"{selection_reason}; keep_original_graph_repair_passed",
        "dag_validation": repaired_validation,
        "dag_repair_status": "keep_original_graph_repaired",
        "dag_repair_operations": operations,
        "verifier": assessment,
    }


def normalize_keep_original_graph_repair_config(config: Mapping[str, Any] | None) -> Dict[str, Any]:
    return {
        "enabled": truthy_config_value(config.get("enabled", False)) if isinstance(config, Mapping) else False,
        "risk_threshold": coerce_float_config_value(
            config.get("risk_threshold", 25.0) if isinstance(config, Mapping) else 25.0,
            default=25.0,
        ),
        "optional_threshold": coerce_float_config_value(
            config.get("optional_threshold", 0.05) if isinstance(config, Mapping) else 0.05,
            default=0.05,
        ),
    }


def truthy_config_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def coerce_float_config_value(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def assess_keep_original_graph_repair(
    *,
    workflow: Mapping[str, Any],
    decision: str,
    result_source: str,
    coverage_row: Mapping[str, Any],
    tool_catalog: Mapping[str, Dict[str, Any]] | None,
    transition_index: Mapping[Tuple[str, str], Any] | None,
    optional_threshold: float,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    normalized = normalize_workflow(workflow)
    nodes = normalize_nodes_for_repair(normalized.get("task_nodes"))
    eligible = (
        str(decision or "").strip().upper() == "KEEP_ORIGINAL"
        and generic_result_source(result_source) == "previous_workflow"
        and bool(nodes)
        and bool(tool_catalog)
        and bool(transition_index)
    )
    if not tool_catalog or not transition_index:
        return (
            {
                "eligible": False,
                "status": "skipped_missing_tool_catalog_or_graph",
                "risk_score": 0.0,
                "reasons": [],
                "operation_counts": {},
                "repair_applied": False,
            },
            dict(workflow),
            {"operations": []},
        )

    validation = validate_workflow_dag(
        normalized,
        tool_catalog=tool_catalog,
        transition_index=transition_index,
        require_tool_graph_edge=False,
    )
    repaired_workflow, graph_trace = keep_original_graph_repair_workflow(
        normalized,
        tool_catalog=tool_catalog,
        transition_index=transition_index,
        optional_threshold=optional_threshold,
    )
    operation_counts = count_keep_graph_operations(graph_trace.get("operations", []))
    hint_delta = gpt_tool_hint_delta_tools(coverage_row, nodes)
    missing_slots = initial_missing_input_slots(validation)
    validation_errors = list(validation.get("errors", []) or [])
    validation_warnings = relevant_keep_validation_warnings(validation.get("warnings", []) or [])
    low_graph_edges = count_low_probability_argument_edges(
        nodes,
        tool_catalog=tool_catalog,
        transition_index=transition_index,
        threshold=0.04,
    )

    reasons: List[Dict[str, Any]] = []
    score = 0.0
    if str(get_first(coverage_row, "missing_intent_hint") or "").strip():
        score += 60.0
        reasons.append({"name": "missing_intent_hint", "score": 60.0})
    if str(get_first(coverage_row, "missing_intent_tool_hint") or "").strip():
        score += 60.0
        reasons.append({"name": "missing_tool_hint", "score": 60.0})
    if hint_delta:
        value = min(35.0, 20.0 + 5.0 * len(hint_delta))
        score += value
        reasons.append({"name": "gpt_hint_absent_tools", "score": value, "tools": hint_delta})
    if validation_errors:
        value = min(70.0, 25.0 * len(validation_errors))
        score += value
        reasons.append({"name": "dag_validation_errors", "score": value, "count": len(validation_errors)})
    if validation_warnings:
        value = min(25.0, 5.0 * len(validation_warnings))
        score += value
        reasons.append({"name": "dag_validation_warnings", "score": value, "count": len(validation_warnings)})
    if missing_slots:
        value = min(45.0, 15.0 * len(missing_slots))
        score += value
        reasons.append({"name": "missing_input_slots", "score": value, "slots": missing_slots})
    selected_edge_count = operation_counts.get("global_select_graph_edge", 0)
    if selected_edge_count:
        value = min(55.0, 25.0 + 10.0 * selected_edge_count)
        score += value
        reasons.append({"name": "graph_repair_selects_new_edges", "score": value, "count": selected_edge_count})
    if low_graph_edges:
        value = min(30.0, 6.0 * low_graph_edges)
        score += value
        reasons.append({"name": "low_probability_edges", "score": value, "count": low_graph_edges})

    return (
        {
            "eligible": eligible,
            "status": "assessed" if eligible else "not_eligible",
            "risk_score": round(score, 4) if eligible else 0.0,
            "reasons": reasons if eligible else [],
            "hint_delta_tools": hint_delta if eligible else [],
            "operation_counts": operation_counts if eligible else {},
            "validation_status": validation.get("status"),
            "validation_errors": validation_errors,
            "validation_warnings": validation_warnings,
            "missing_input_slots": missing_slots,
            "low_probability_edges": low_graph_edges,
            "repair_trace": graph_trace,
        },
        repaired_workflow,
        graph_trace,
    )


def keep_original_graph_repair_workflow(
    workflow: Mapping[str, Any],
    tool_catalog: Mapping[str, Dict[str, Any]],
    transition_index: Mapping[Tuple[str, str], Any],
    optional_threshold: float,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    nodes = normalize_nodes_for_repair(workflow.get("task_nodes"))
    if not nodes:
        return dict(workflow), {"status": "skipped_empty_nodes", "operations": []}

    explicit_edges, explicit_errors = normalize_task_links_to_edges(workflow.get("task_links"), nodes)
    argument_edges, argument_errors = materialize_edges_from_node_arguments(nodes)
    original_pairs = edge_key_set_from_pairs(explicit_edges + argument_edges)
    operations: List[Dict[str, Any]] = []
    selected_edges = choose_keep_global_edges(
        nodes=nodes,
        tool_catalog=tool_catalog,
        transition_index=transition_index,
        original_pairs=original_pairs,
        optional_threshold=optional_threshold,
        operations=operations,
    )
    rebuild_arguments_from_edges(nodes, selected_edges, operations)
    final_edges = dedupe_edges(selected_edges)
    repaired = dict(workflow)
    repaired["task_nodes"] = nodes
    repaired["task_links"] = build_task_links_from_edges(nodes, final_edges)
    validation = validate_workflow_dag(
        repaired,
        tool_catalog=tool_catalog,
        transition_index=transition_index,
        require_tool_graph_edge=False,
    )
    return repaired, {
        "status": "changed" if any(op.get("action") == "global_select_graph_edge" for op in operations) else "unchanged",
        "mode": "global",
        "argument_sync": "rebuild",
        "operations": operations,
        "explicit_link_errors": explicit_errors,
        "argument_errors": argument_errors,
        "validation_status": validation.get("status"),
        "validation_errors": validation.get("errors", []),
        "validation_warnings": validation.get("warnings", []),
    }


def choose_keep_global_edges(
    nodes: List[Dict[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]],
    transition_index: Mapping[Tuple[str, str], Any],
    original_pairs: set[Tuple[int, int]],
    optional_threshold: float,
    operations: List[Dict[str, Any]],
) -> List[Dict[str, int]]:
    selected_edges: List[Dict[str, int]] = []
    selected_pairs: set[Tuple[int, int]] = set()
    for target_index in range(1, len(nodes)):
        for edge in choose_keep_global_target_edges(
            target_index=target_index,
            nodes=nodes,
            tool_catalog=tool_catalog,
            transition_index=transition_index,
            original_pairs=original_pairs,
            optional_threshold=optional_threshold,
            operations=operations,
        ):
            pair = (edge["source"], edge["target"])
            if pair in selected_pairs:
                continue
            selected_pairs.add(pair)
            selected_edges.append(edge)
    return selected_edges


def choose_keep_global_target_edges(
    target_index: int,
    nodes: List[Dict[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]],
    transition_index: Mapping[Tuple[str, str], Any],
    original_pairs: set[Tuple[int, int]],
    optional_threshold: float,
    operations: List[Dict[str, Any]],
) -> List[Dict[str, int]]:
    target_slots = required_slots_for_repair_node(target_index, nodes, tool_catalog)
    if not target_slots:
        return []

    literal_counts = literal_slot_counts_for_repair_node(target_index, nodes, target_slots)
    literal_argument_count = count_literal_arguments_for_repair_node(nodes[target_index])
    selected: List[Dict[str, int]] = []
    used_sources: set[int] = set()
    selected_slots: set[str] = set()
    for slot in target_slots:
        literal_count = literal_counts.get(slot, 0)
        allow_literal_replacement = not (
            literal_count > 0 and target_slots.count(slot) > 1 and slot in selected_slots
        )
        candidate = choose_best_keep_global_edge_for_slot(
            slot=slot,
            target_index=target_index,
            nodes=nodes,
            tool_catalog=tool_catalog,
            transition_index=transition_index,
            original_pairs=original_pairs,
            used_sources=used_sources,
            target_slots=target_slots,
            literal_count=literal_count,
            literal_argument_count=literal_argument_count,
            optional_threshold=optional_threshold,
            allow_literal_replacement=allow_literal_replacement,
        )
        if candidate is None:
            if literal_count > 0:
                literal_counts[slot] = max(0, literal_count - 1)
            continue

        selected.append({"source": candidate["source"], "target": target_index})
        used_sources.add(candidate["source"])
        selected_slots.add(slot)
        operations.append(
            {
                "action": candidate["action"],
                "source": candidate["source"],
                "target": target_index,
                "slot": slot,
                "score": round(candidate["score"], 4),
                "literal_score": round(candidate["literal_score"], 4),
                "transition_probability": candidate["transition_probability"],
            }
        )
    return selected


def choose_best_keep_global_edge_for_slot(
    slot: str,
    target_index: int,
    nodes: Sequence[Mapping[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]],
    transition_index: Mapping[Tuple[str, str], Any],
    original_pairs: set[Tuple[int, int]],
    used_sources: set[int],
    target_slots: Sequence[str],
    literal_count: int,
    literal_argument_count: int,
    optional_threshold: float,
    allow_literal_replacement: bool,
) -> Dict[str, Any] | None:
    literal_score = keep_global_literal_score(slot, literal_count, literal_argument_count)
    candidates: List[Tuple[float, int, Dict[str, Any]]] = []
    for source_index in range(target_index):
        if source_index in used_sources:
            continue
        if slot not in matching_source_slots_for_repair_node(
            source_index,
            target_index,
            nodes,
            tool_catalog,
            [slot],
        ):
            continue

        probability = keep_transition_probability(source_index, target_index, nodes, tool_catalog, transition_index)
        is_original = (source_index, target_index) in original_pairs
        if not is_original and (probability is None or probability <= 0):
            continue
        if literal_count > 0 and not is_original:
            if not allow_literal_replacement:
                continue
            if probability is None or probability < optional_threshold:
                continue
            if not keep_optional_slot_allowed(slot, target_slots):
                continue

        score = keep_edge_score(
            source_index=source_index,
            target_index=target_index,
            probability=probability if probability is not None else 0.0,
            is_original=is_original,
        )
        if not is_original:
            score -= keep_global_non_original_distance_penalty(source_index, target_index, literal_count)
        if literal_count > 0 and not is_original:
            score -= keep_global_literal_replacement_margin(slot, literal_argument_count)
        candidates.append(
            (
                score,
                source_index,
                {
                    "source": source_index,
                    "score": score,
                    "literal_score": literal_score,
                    "transition_probability": probability,
                    "action": "global_keep_original_edge" if is_original else "global_select_graph_edge",
                },
            )
        )

    if not candidates:
        return None
    candidates.sort(reverse=True)
    _, _, candidate = candidates[0]
    if literal_count > 0 and candidate["score"] < literal_score:
        return None
    if literal_count == 0 and (candidate["source"], target_index) not in original_pairs:
        if candidate["transition_probability"] is None or candidate["transition_probability"] <= 0:
            return None
    return candidate


def required_slots_for_repair_node(
    target_index: int,
    nodes: Sequence[Mapping[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]],
) -> List[str]:
    target_tool = lookup_catalog_tool(tool_catalog, nodes[target_index].get("task", ""))
    return bg.normalize_type_list(target_tool.get("input_types", []))


def literal_slot_counts_for_repair_node(
    target_index: int,
    nodes: Sequence[Mapping[str, Any]],
    target_slots: Sequence[str],
) -> Dict[str, int]:
    remaining = list(target_slots)
    counts: Dict[str, int] = {}
    for argument in normalize_list(nodes[target_index].get("arguments")):
        if parse_node_reference(argument) is not None:
            continue
        matched = bg.consume_first_matching_slot(remaining, infer_literal_argument_types(argument))
        if matched:
            counts[matched] = counts.get(matched, 0) + 1
    return counts


def count_literal_arguments_for_repair_node(node: Mapping[str, Any]) -> int:
    return sum(
        1
        for argument in normalize_list(node.get("arguments"))
        if parse_node_reference(argument) is None
    )


def matching_source_slots_for_repair_node(
    source_index: int,
    target_index: int,
    nodes: Sequence[Mapping[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]],
    remaining: Sequence[str],
) -> List[str]:
    if source_index >= target_index or source_index < 0:
        return []
    source_tool = lookup_catalog_tool(tool_catalog, nodes[source_index].get("task", ""))
    source_types = bg.normalize_type_set(source_tool.get("output_types", []))
    return [slot for slot in remaining if slot in source_types or "any" in source_types or "*" in source_types]


def keep_global_literal_score(slot: str, literal_count: int, literal_argument_count: int) -> float:
    if literal_count <= 0:
        return 0.0
    if slot in {"image", "audio", "video"}:
        score = 17.5
    elif slot == "url":
        score = 16.0
    elif slot == "text":
        score = 12.0
    else:
        score = 14.0
    if literal_count > 1:
        score += min(6.0, float(literal_count - 1) * 3.0)
    if slot == "text" and literal_argument_count > 1:
        score += 4.0
    return score


def keep_global_literal_replacement_margin(slot: str, literal_argument_count: int) -> float:
    if slot == "text" and literal_argument_count > 1:
        return 2.0
    return 2.0


def keep_global_non_original_distance_penalty(source_index: int, target_index: int, literal_count: int) -> float:
    distance = max(1, target_index - source_index)
    if distance <= 1:
        return 0.0
    step_penalty = 8.0 if literal_count > 0 else 4.0
    return float(distance - 1) * step_penalty


def keep_optional_slot_allowed(slot: str, target_slots: Sequence[str]) -> bool:
    unique_slots = set(target_slots)
    if len(unique_slots) <= 1:
        return True
    return slot != "text"


def keep_edge_score(source_index: int, target_index: int, probability: float, is_original: bool) -> float:
    distance = max(1, target_index - source_index)
    score = probability * 100.0
    score += 35.0 if is_original else 0.0
    score += 8.0 if distance == 1 else 0.0
    score += 3.0 / distance
    return score


def keep_transition_probability(
    source_index: int,
    target_index: int,
    nodes: Sequence[Mapping[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]],
    transition_index: Mapping[Tuple[str, str], Any],
) -> float | None:
    source_tool = lookup_catalog_tool(tool_catalog, nodes[source_index].get("task", ""))
    target_tool = lookup_catalog_tool(tool_catalog, nodes[target_index].get("task", ""))
    value = bg.get_transition_probability(transition_index, source_tool.get("id"), target_tool.get("id"))
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def gpt_tool_hint_delta_tools(coverage_row: Mapping[str, Any], nodes: Sequence[Mapping[str, Any]]) -> List[str]:
    current = {normalize_key(node.get("task")) for node in nodes}
    delta = []
    for tool_name in split_terms(
        get_first(coverage_row, "intent tool")
    ):
        if normalize_key(tool_name) not in current:
            delta.append(tool_name)
    return dedupe_preserve_order(delta)


def count_keep_graph_operations(operations: Iterable[Any]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for operation in operations:
        if not isinstance(operation, Mapping):
            continue
        action = str(operation.get("action") or "")
        counts[action] = counts.get(action, 0) + 1
    return counts


def relevant_keep_validation_warnings(warnings: Iterable[Any]) -> List[str]:
    ignored = {"task_links_missing; materialized from <node-i> arguments"}
    return [str(item) for item in warnings if str(item) not in ignored]


def count_low_probability_argument_edges(
    nodes: List[Dict[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]],
    transition_index: Mapping[Tuple[str, str], Any],
    threshold: float,
) -> int:
    edges, _ = materialize_edges_from_node_arguments(nodes)
    count = 0
    for edge in edges:
        source = int(edge["source"])
        target = int(edge["target"])
        if not valid_node_index(source, len(nodes)) or not valid_node_index(target, len(nodes)):
            continue
        probability = keep_transition_probability(source, target, nodes, tool_catalog, transition_index)
        if probability is None or probability < threshold:
            count += 1
    return count


def extract_request_literals_by_type(user_request: str) -> Dict[str, List[str]]:
    text = str(user_request or "")
    values: Dict[str, List[str]] = {"url": [], "audio": [], "video": [], "image": [], "text": []}
    for match in re.finditer(r"https?://[^\s'\"<>]+", text, flags=re.IGNORECASE):
        add_literal(values, "url", cleanup_literal(match.group(0)))
    for match in re.finditer(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s'\"<>]*)?", text, flags=re.IGNORECASE):
        add_literal(values, "url", cleanup_literal(match.group(0)))
    for match in re.finditer(r"\b[^\s'\"<>]+\.(?:mp3|wav|wma|ogg|aac|flac|aiff|au)\b", text, flags=re.IGNORECASE):
        add_literal(values, "audio", cleanup_literal(match.group(0)))
    for match in re.finditer(r"\b[^\s'\"<>]+\.(?:mp4|avi|mov|flv|wmv|mkv|webm|m4v|mpg|mpeg)\b", text, flags=re.IGNORECASE):
        add_literal(values, "video", cleanup_literal(match.group(0)))
    for match in re.finditer(r"\b[^\s'\"<>]+\.(?:jpg|jpeg|png|gif|bmp|tiff|svg|ico)\b", text, flags=re.IGNORECASE):
        add_literal(values, "image", cleanup_literal(match.group(0)))
    for pattern in (r'"([^"\n]{1,500})"', r"(?<!\w)'([^'\n]{1,500})'(?!\w)"):
        for match in re.finditer(pattern, text):
            add_literal(values, "text", cleanup_literal(match.group(1)))
    if text.strip():
        add_literal(values, "text", text.strip())
    return values


def add_literal(values: Dict[str, List[str]], slot: str, literal: str) -> None:
    if literal and literal not in values.setdefault(slot, []):
        values[slot].append(literal)


def cleanup_literal(value: str) -> str:
    return str(value or "").strip().strip(".,;:)]}")


def choose_literal_for_slot(slot: str, request_literals: Mapping[str, List[str]], existing_arguments: List[Any]) -> str | None:
    existing = {str(argument) for argument in existing_arguments}
    for literal in request_literals.get(slot, []):
        if literal not in existing:
            return literal
    return None


def infer_literal_argument_types(argument: Any) -> List[str]:
    inferred = list(bg.infer_literal_argument_types(argument))
    text = str(argument or "").strip().lower()
    if re.search(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s'\"<>]*)?", text) and "url" not in inferred:
        inferred.append("url")
    return dedupe_preserve_order(inferred)


def dedupe_preserve_order(values: Iterable[Any]) -> List[Any]:
    deduped: List[Any] = []
    seen = set()
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return deduped


def build_tool_catalog(tool_desc: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    catalog: Dict[str, Dict[str, Any]] = {}
    for tool in tool_desc:
        if not isinstance(tool, Mapping):
            continue
        tool_id = str(tool.get("tool_id") or tool.get("id") or "").strip()
        if not tool_id:
            continue
        input_type_value = tool.get("input_types") if "input_types" in tool else tool.get("input-type")
        output_type_value = tool.get("output_types") if "output_types" in tool else tool.get("output-type")
        catalog[normalize_key(tool_id)] = {
            "id": tool_id,
            "intent": str(tool.get("intent") or "").strip(),
            "desc": str(tool.get("desc") or "").strip(),
            "parameters": normalize_tool_parameters(tool.get("parameters")),
            "input_types": [
                str(item).strip()
                for item in normalize_list(input_type_value)
                if str(item).strip()
            ],
            "output_types": [
                str(item).strip()
                for item in normalize_list(output_type_value)
                if str(item).strip()
            ],
            "known": True,
        }
    return catalog


def lookup_catalog_tool(tool_catalog: Mapping[str, Dict[str, Any]], task_name: Any) -> Dict[str, Any]:
    text = str(task_name or "").strip()
    for candidate in [text, re.sub(r"\s*\(.*\)\s*$", "", text).strip()]:
        key = normalize_key(candidate)
        if key in tool_catalog:
            return dict(tool_catalog[key])
    return {"id": text, "intent": "unknown", "desc": "", "input_types": [], "output_types": [], "known": False}


def normalize_task_links_to_edges(raw_links: Any, nodes: List[Dict[str, Any]]) -> Tuple[List[Dict[str, int]], List[str]]:
    edges: List[Dict[str, int]] = []
    errors: List[str] = []
    node_index = build_node_lookup(nodes)
    for link_index, raw_link in enumerate(normalize_list(raw_links)):
        if not isinstance(raw_link, Mapping):
            errors.append(f"task_link[{link_index}] is not an object")
            continue
        source = resolve_node_ref(first_non_empty(raw_link.get("source"), raw_link.get("from")), node_index)
        target = resolve_node_ref(first_non_empty(raw_link.get("target"), raw_link.get("to")), node_index)
        if source is None or target is None:
            errors.append(f"task_link[{link_index}] has unknown source/target")
            continue
        edges.append({"source": source, "target": target})
    return edges, errors


def materialize_edges_from_node_arguments(nodes: List[Dict[str, Any]]) -> Tuple[List[Dict[str, int]], List[str]]:
    edges: List[Dict[str, int]] = []
    errors: List[str] = []
    for target_index, node in enumerate(nodes):
        for argument in normalize_list(node.get("arguments")):
            source = parse_node_reference(argument)
            if source is None:
                continue
            if not valid_node_index(source, len(nodes)):
                errors.append(f"invalid_node_ref node-{target_index} argument={argument!r}")
                continue
            if source >= target_index:
                errors.append(f"future_or_self_node_ref node-{target_index} argument={argument!r}")
            edges.append({"source": source, "target": target_index})
    return edges, errors


def input_slot_status_for_node(
    target_index: int,
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
    tool_catalog: Mapping[str, Dict[str, Any]],
) -> Dict[str, Any]:
    target_tool = lookup_catalog_tool(tool_catalog, nodes[target_index].get("task", ""))
    remaining = bg.normalize_type_list(target_tool.get("input_types", []))
    satisfied: List[Dict[str, Any]] = []
    for edge in edges:
        if edge.get("target") != target_index:
            continue
        source_tool = lookup_catalog_tool(tool_catalog, nodes[edge["source"]].get("task", ""))
        matched = bg.consume_first_matching_slot(remaining, source_tool.get("output_types", []))
        if matched:
            satisfied.append({"kind": "node", "source": edge["source"], "slot": matched})
    for argument in normalize_list(nodes[target_index].get("arguments")):
        if parse_node_reference(argument) is not None:
            continue
        matched = bg.consume_first_matching_slot(remaining, infer_literal_argument_types(argument))
        if matched:
            satisfied.append({"kind": "literal", "value": argument, "slot": matched})
    return {
        "target": target_index,
        "tool": nodes[target_index].get("task", ""),
        "required_slots": bg.normalize_type_list(target_tool.get("input_types", [])),
        "satisfied_slots": satisfied,
        "missing_slots": list(remaining),
    }


def build_node_lookup(nodes: List[Dict[str, Any]]) -> Dict[str, int]:
    lookup: Dict[str, int] = {}
    for index, node in enumerate(nodes):
        values = [
            f"node-{index}",
            f"<node-{index}>",
            str(index),
            str(node.get("id") or "").strip(),
            str(node.get("task") or "").strip(),
        ]
        for value in values:
            if value:
                lookup.setdefault(normalize_key(value), index)
    return lookup


def resolve_node_ref(value: Any, node_index: Mapping[str, int]) -> int | None:
    parsed = parse_node_reference(value)
    if parsed is not None:
        return parsed
    key = normalize_key(value)
    return node_index.get(key)


def parse_node_reference(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = re.fullmatch(r"<?node[-_](\d+)>?", text, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def valid_node_index(index: int, node_count: int) -> bool:
    return isinstance(index, int) and 0 <= index < node_count


def dedupe_edges(edges: Iterable[Mapping[str, int]]) -> List[Dict[str, int]]:
    deduped: List[Dict[str, int]] = []
    seen: set[Tuple[int, int]] = set()
    for edge in edges:
        pair = (int(edge["source"]), int(edge["target"]))
        if pair in seen:
            continue
        seen.add(pair)
        deduped.append({"source": pair[0], "target": pair[1]})
    return deduped


def dedupe_valid_edges(
    edges: Iterable[Mapping[str, int]],
    node_count: int,
    operations: List[Dict[str, Any]] | None = None,
    allow_backward: bool = False,
) -> List[Dict[str, int]]:
    valid_edges: List[Dict[str, int]] = []
    seen: set[Tuple[int, int]] = set()
    for edge in edges:
        try:
            source = int(edge["source"])
            target = int(edge["target"])
        except (KeyError, TypeError, ValueError):
            if operations is not None:
                operations.append({"action": "drop_malformed_edge", "edge": dict(edge) if isinstance(edge, Mapping) else edge})
            continue
        if not valid_node_index(source, node_count) or not valid_node_index(target, node_count):
            if operations is not None:
                operations.append({"action": "drop_invalid_edge", "source": source, "target": target})
            continue
        if source == target:
            if operations is not None:
                operations.append({"action": "drop_future_or_self_edge", "source": source, "target": target})
            continue
        if not allow_backward and source > target:
            if operations is not None:
                operations.append({"action": "drop_future_or_self_edge", "source": source, "target": target})
            continue
        pair = (source, target)
        if pair in seen:
            if operations is not None:
                operations.append({"action": "drop_duplicate_edge", "source": source, "target": target})
            continue
        seen.add(pair)
        valid_edges.append({"source": source, "target": target})
    return valid_edges


def dedupe_valid_forward_edges(
    edges: Iterable[Mapping[str, int]],
    node_count: int,
    operations: List[Dict[str, Any]] | None = None,
) -> List[Dict[str, int]]:
    return dedupe_valid_edges(edges, node_count, operations, allow_backward=False)


def edge_key_set_from_pairs(edges: Iterable[Mapping[str, int]]) -> set[Tuple[int, int]]:
    return {(int(edge["source"]), int(edge["target"])) for edge in edges}


def has_cycle(edges: List[Dict[str, int]], node_count: int) -> bool:
    adjacency: Dict[int, List[int]] = {}
    for edge in edges:
        adjacency.setdefault(edge["source"], []).append(edge["target"])
    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(node: int) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for child in adjacency.get(node, []):
            if visit(child):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(index) for index in range(node_count))


def build_task_links_from_edges(nodes: List[Dict[str, Any]], edges: List[Dict[str, int]]) -> List[Dict[str, str]]:
    links: List[Dict[str, str]] = []
    for edge in edges:
        source = edge["source"]
        target = edge["target"]
        if valid_node_index(source, len(nodes)) and valid_node_index(target, len(nodes)):
            links.append({"source": str(nodes[source].get("task") or ""), "target": str(nodes[target].get("task") or "")})
    return links


def load_prediction_results(path: Path) -> Dict[str, Dict[str, Any]]:
    result_by_id: Dict[str, Dict[str, Any]] = {}
    for row in read_json_records(path):
        case_id = str(row.get("id") or row.get("ID") or "").strip()
        if case_id:
            result_by_id[case_id] = row
    return result_by_id


def read_json_records(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return []
    if text[0] in "[{":
        try:
            payload = json.loads(text)
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
            if isinstance(payload, dict):
                return [payload]
        except json.JSONDecodeError:
            pass
    return bg.read_jsonl_records(path)


def load_tool_desc(path: Path, max_tools: int) -> List[Dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    nodes = payload.get("nodes", []) if isinstance(payload, Mapping) else []
    tools = []
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        tools.append(
            {
                "tool_id": str(node.get("id") or "").strip(),
                "intent": str(node.get("intent") or "").strip(),
                "desc": str(node.get("desc") or "").strip(),
                "parameters": normalize_tool_parameters(node.get("parameters")),
                "input_types": normalize_list(node.get("input-type")),
                "output_types": normalize_list(node.get("output-type")),
            }
        )
    return tools


def select_relevant_tools(
    tool_desc: List[Dict[str, Any]],
    previous_workflow: Mapping[str, Any],
    previous_tool_summary: str,
    intent_tool_hint: str,
    intent_hint: str,
    max_tools: int,
) -> List[Dict[str, Any]]:
    wanted = {
        normalize_key(term)
        for term in (
            split_terms(previous_tool_summary)
            + split_terms(intent_tool_hint)
            + split_terms(intent_hint)
            + [
                str(node.get("task") or "").strip()
                for node in normalize_list(previous_workflow.get("task_nodes"))
                if isinstance(node, Mapping)
            ]
        )
        if normalize_key(term)
    }
    selected: List[Dict[str, Any]] = []
    seen = set()
    for tool in tool_desc:
        tool_keys = {
            normalize_key(tool.get("tool_id")),
            normalize_key(tool.get("intent")),
        }
        if not wanted.intersection(tool_keys):
            continue
        key = normalize_key(tool.get("tool_id"))
        if key in seen:
            continue
        seen.add(key)
        selected.append(tool)

    if selected:
        return selected[:max_tools] if max_tools and max_tools > 0 else selected
    return tool_desc[:max_tools] if max_tools and max_tools > 0 else tool_desc


def select_dag_self_repair_tools(
    tool_desc: List[Dict[str, Any]],
    previous_workflow: Mapping[str, Any],
    candidate_workflow: Mapping[str, Any],
    coverage_row: Mapping[str, Any],
    validation: Mapping[str, Any],
    max_tools: int,
) -> List[Dict[str, Any]]:
    wanted_keys = {
        normalize_key(tool)
        for tool in (
            workflow_tool_names(previous_workflow)
            + workflow_tool_names(candidate_workflow)
            + split_terms(get_first(coverage_row, "model tool"))
            + split_terms(get_first(coverage_row, "intent tool"))
            + split_terms(get_first(coverage_row, "intent"))
        )
        if normalize_key(tool)
    }
    missing_slots = set(initial_missing_input_slots(validation))
    selected: List[Dict[str, Any]] = []
    seen = set()
    for tool in tool_desc:
        tool_keys = {normalize_key(tool.get("tool_id")), normalize_key(tool.get("intent"))}
        outputs = bg.normalize_type_set(tool.get("output_types", []))
        if not wanted_keys.intersection(tool_keys) and not missing_slots.intersection(outputs):
            continue
        key = normalize_key(tool.get("tool_id"))
        if key in seen:
            continue
        seen.add(key)
        selected.append(tool)
    if selected:
        return selected[:max_tools] if max_tools and max_tools > 0 else selected
    return tool_desc[:max_tools] if max_tools and max_tools > 0 else tool_desc


def split_terms(value: Any) -> List[str]:
    text = str(value or "").strip()
    if not text:
        return []
    parts = re.split(r"\s*(?:->|,|;|\n)\s*", text)
    return [part.strip() for part in parts if part.strip()]


def normalize_hint_terms(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        terms = split_terms(value)
    elif isinstance(value, (list, tuple)):
        terms = []
        for item in value:
            if isinstance(item, str):
                terms.extend(split_terms(item))
            else:
                text = str(item or "").strip()
                if text:
                    terms.append(text)
    else:
        text = str(value or "").strip()
        terms = [text] if text else []
    return [term for term in dedupe_preserve_order(terms) if normalize_key(term)]


def format_hint_terms(value: Any) -> str:
    return " -> ".join(normalize_hint_terms(value))


def split_tool_hint_coverage(
    model_tool_original: Any,
    intent_tool_hint: Any,
    semantic_tool_families: Mapping[str, Any] | None = None,
) -> Dict[str, List[str]]:
    model_terms = normalize_hint_terms(model_tool_original)
    hint_terms = normalize_hint_terms(intent_tool_hint)
    if not hint_terms:
        return {"covered": [], "missing": []}
    if not model_terms:
        return {"covered": [], "missing": hint_terms}

    model_keys = {normalize_key(term) for term in model_terms}
    family_index = (
        SEMANTIC_TOOL_FAMILY_BY_KEY
        if semantic_tool_families is None
        else build_semantic_tool_family_index(resolve_semantic_tool_families(semantic_tool_families))
    )
    covered: List[str] = []
    missing: List[str] = []
    for hint_tool in hint_terms:
        hint_key = normalize_key(hint_tool)
        if hint_key in model_keys:
            covered.append(hint_tool)
            continue
        hint_family = family_index.get(hint_key, "")
        if hint_family and any(family_index.get(normalize_key(model_tool), "") == hint_family for model_tool in model_terms):
            covered.append(hint_tool)
            continue
        missing.append(hint_tool)
    return {"covered": covered, "missing": missing}


def split_intent_hint_coverage(
    model_tool_original: Any,
    intent_hint: Any,
    tool_catalog: Mapping[str, Dict[str, Any]] | None = None,
) -> Dict[str, List[str]]:
    hint_intents = normalize_hint_terms(intent_hint)
    if not hint_intents:
        return {"covered": [], "missing": []}
    covered_intent_keys = model_intent_keys(model_tool_original, tool_catalog or {})
    covered: List[str] = []
    missing: List[str] = []
    for hint_intent in hint_intents:
        if normalize_key(hint_intent) in covered_intent_keys:
            covered.append(hint_intent)
        else:
            missing.append(hint_intent)
    return {"covered": covered, "missing": missing}


def model_intent_keys(
    model_tool_original: Any,
    tool_catalog: Mapping[str, Dict[str, Any]],
) -> set[str]:
    keys = set()
    for term in normalize_hint_terms(model_tool_original):
        key = normalize_key(term)
        if key:
            keys.add(key)
        tool_info = lookup_catalog_tool(tool_catalog, term) if tool_catalog else {}
        intent = str(tool_info.get("intent") or "").strip()
        if intent and intent.lower() != "unknown":
            keys.add(normalize_key(intent))
    return keys


def tool_hint_covered_by_model(
    model_tool_original: Any,
    intent_tool_hint: Any,
    semantic_tool_families: Mapping[str, Any] | None = None,
) -> bool:
    model_terms = normalize_hint_terms(model_tool_original)
    intent_terms = normalize_hint_terms(intent_tool_hint)
    if not model_terms or not intent_terms:
        return False
    return not split_tool_hint_coverage(model_terms, intent_terms, semantic_tool_families)["missing"]


def normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def normalize_dependency_type(value: Any, default: str = "resource") -> str:
    text = str(value or "").strip().lower()
    if text in {"resource", "temporal"}:
        return text
    return default


def normalize_list(value: Any) -> List[Any]:
    value = parse_jsonish(value)
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str) and not value.strip():
        return []
    return [value]


def parse_jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def get_first(row: Mapping[str, Any], *keys: str) -> str:
    lowered = {str(key).strip().lower(): value for key, value in row.items()}
    for key in keys:
        value = lowered.get(str(key).strip().lower())
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def get_case_id(row: Mapping[str, Any], row_index: int) -> str:
    return get_first(row, "id", "ID", "case_id") or f"row-{row_index}"


def get_thread_client(
    llm_config: Any,
    llm_profile: str | None,
    planner_timeout: float | None = None,
    planner_max_retries: int | None = None,
) -> OpenAICompatibleLLMClient:
    signature = (
        str(llm_config or ""),
        str(llm_profile or ""),
        str(planner_timeout or ""),
        str(planner_max_retries if planner_max_retries is not None else ""),
    )
    if getattr(_THREAD_LOCAL, "client_signature", None) != signature:
        if planner_timeout is None and planner_max_retries is None:
            _THREAD_LOCAL.client = OpenAICompatibleLLMClient(llm_config_path=llm_config, llm_profile=llm_profile)
        else:
            config_client = OpenAICompatibleLLMClient(llm_config_path=llm_config, llm_profile=llm_profile)
            config = config_client.resolve_config()
            if planner_timeout is not None:
                config["timeout"] = planner_timeout
            if planner_max_retries is not None:
                config["max_retries"] = planner_max_retries
            _THREAD_LOCAL.client = OpenAICompatibleLLMClient(llm_config=config)
        _THREAD_LOCAL.client_signature = signature
    return _THREAD_LOCAL.client


def write_xlsx(path: Path, rows: List[Dict[str, Any]]) -> None:
    xlsx_rows: List[List[Any]] = [OUTPUT_COLUMNS]
    for row in rows:
        xlsx_rows.append([xlsx_value(row.get(column, "")) for column in OUTPUT_COLUMNS])
    bg.write_xlsx_rows(path, xlsx_rows)


def workflow_for_taskbench_eval(workflow: Any, dependency_type: str | None = None) -> Dict[str, Any]:
    normalized = normalize_workflow(workflow)
    if str(dependency_type or "").strip().lower() != "temporal":
        return normalized

    cleaned = copy.deepcopy(normalized)
    cleaned_nodes: List[Any] = []
    for raw_node in normalize_list(cleaned.get("task_nodes")):
        if not isinstance(raw_node, Mapping):
            cleaned_nodes.append(raw_node)
            continue
        node = dict(raw_node)
        arguments = normalize_list(node.get("arguments"))
        node["arguments"] = [argument for argument in arguments if isinstance(argument, Mapping)]
        cleaned_nodes.append(node)
    cleaned["task_nodes"] = cleaned_nodes
    return cleaned


def write_taskbench_eval_json(
    path: Path,
    rows: Iterable[Mapping[str, Any]],
    dependency_type: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            case_id = str(row.get("id") or "").strip()
            if not case_id:
                continue
            workflow = workflow_for_taskbench_eval(row.get("result"), dependency_type=dependency_type)
            handle.write(
                json.dumps(
                    {
                        "id": case_id,
                        "result": workflow,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )


def xlsx_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def error_row(case_id: str, user_request: str, previous_workflow: Mapping[str, Any], warnings: List[str]) -> Dict[str, Any]:
    agent_trace = {
        "intent_detector": {
            "source": "coverage_table",
            "tool_hint": "",
            "intent_hint": "",
            "covered_tool_hint": [],
            "missing_tool_hint": [],
            "covered_intent_hint": [],
            "missing_intent_hint": [],
        },
        "workflow_planner": {
            "source": "prediction_file",
            "tool_summary": "",
            "workflow": dict(previous_workflow),
        },
        "intent_checker": {
            "status": "error",
            "coverage_assessment": [],
            "hint_assessment": [],
            "covered_tools": [],
            "missing_tools": [],
            "covered_intents": [],
            "missing_intents": [],
        },
        "workflow_replanner": {
            "decision": "ERROR",
            "executed": False,
            "candidate_workflow": {},
            "selected_workflow": dict(previous_workflow),
            "result_source": "previous_workflow",
            "result_source_generic": "previous_workflow",
            "selection_reason": "worker_error: used previous workflow",
            "change_summary": "",
            "raw_output": "",
        },
        "structure_detector": {
            "status": "skipped",
            "errors": [],
            "warnings": [],
            "require_tool_graph_edge": False,
        },
        "workflow_repairer": {
            "planner_self_repair_status": "not_attempted",
            "planner_self_repair_decision": "",
            "planner_self_repair_result": {},
            "raw_planner_self_repair_output": "",
            "local_repair_status": "not_attempted",
            "local_repair_operations": [],
            "keep_original_verifier": {},
            "final_workflow": dict(previous_workflow),
            "final_result_source": "previous_workflow",
            "final_result_source_generic": "previous_workflow",
            "final_selection_reason": "worker_error: used previous workflow",
        },
        "warnings": list(warnings),
        "user_request": user_request,
    }
    return {
        "id": case_id,
        "type": "",
        "user_request": user_request,
        "model_tool_original": "",
        "intent_tool_hint": "",
        "intent_hint": "",
        "covered_intent_tool_hint": "",
        "missing_intent_tool_hint": "",
        "covered_intent_hint": "",
        "missing_intent_hint": "",
        "replan_decision": "ERROR",
        "result_source": "previous_workflow",
        "result_source_generic": "previous_workflow",
        "result": dict(previous_workflow),
        "previous_workflow": dict(previous_workflow),
        "replanned_workflow": {},
        "dag_replan_result_source": "previous_workflow",
        "dag_replan_result_source_generic": "previous_workflow",
        "dag_replan_result": dict(previous_workflow),
        "dag_replan_selection_reason": "worker_error: used previous workflow",
        "dag_validation_status": "skipped",
        "dag_validation_errors": [],
        "dag_validation_warnings": [],
        "planner_dag_self_repair_status": "not_attempted",
        "planner_dag_self_repair_decision": "",
        "planner_dag_self_repair_result": {},
        "raw_planner_dag_self_repair_output": "",
        "dag_repair_status": "not_attempted",
        "dag_repair_operations": [],
        "keep_original_verifier": {},
        "coverage_assessment": [],
        "hint_assessment": [],
        "selection_reason": "worker_error: used previous workflow",
        "change_summary": "",
        "raw_planner_output": "",
        "agent_trace": agent_trace,
        "warnings": warnings,
    }


def get_prediction_file_arg(args: argparse.Namespace) -> Any:
    return getattr(args, "prediction_file", None) or DEFAULT_PREDICTION_FILE


def get_planner_llm_config_arg(args: argparse.Namespace) -> Any:
    return getattr(args, "planner_llm_config", None) or "configs/qwen.json"


def get_planner_llm_profile_arg(args: argparse.Namespace) -> str | None:
    return getattr(args, "planner_llm_profile", None)


def get_planner_timeout_arg(args: argparse.Namespace) -> float | None:
    return getattr(args, "planner_timeout", None)


def get_planner_max_retries_arg(args: argparse.Namespace) -> int | None:
    return getattr(args, "planner_max_retries", None)


def generic_result_source(value: Any) -> str:
    source = str(value or "").strip()
    return {
        "previous_qwen_workflow": "previous_workflow",
        "qwen_replan": "replan",
        "planner_replan": "replan",
    }.get(source, source)


def resolve_path(raw: Any) -> Path:
    path = Path(str(raw)).expanduser()
    return path.resolve() if path.is_absolute() else (REPO_ROOT / path).resolve()


def resolve_eval_json_path(args: argparse.Namespace, output_json: Path) -> Path | None:
    if getattr(args, "no_eval_json", False):
        return None
    raw_eval_json = getattr(args, "eval_json", None)
    if raw_eval_json:
        return ensure_json_output_path(resolve_path(raw_eval_json))
    return None


def ensure_json_output_path(path: Path) -> Path:
    if path.suffix.lower() == ".jsonl":
        return path.with_suffix(".json")
    return path


def sanitize_eval_llm_name(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "replan"
    return re.sub(r'[<>:"/\\\\|?*]+', "_", text)


# Unified pipeline entrypoint and edge-only graph repair.
DEFAULT_COVERAGE_XLSX = (
    "taskbench/data_multimedia/"
    "data_multimedia_gpt55_intent_coverage_table.xlsx"
)
DEFAULT_PREDICTION_FILE = (
    "taskbench/data_multimedia/predictions_use_demos_2_reformat_by_self/"
    "qwen3-14b_20260527.json"
)
DEFAULT_TOOL_DESC = "taskbench/data_multimedia/tool_desc_intent.json"
DEFAULT_TOOL_GRAPH = "taskbench/data_multimedia/tool_transition_graph.json"
DEFAULT_DATASET_CONFIG = "agent/memory_guided_workflow/configs/taskbench_multimedia_dataset_config.json"
DEFAULT_DATASET_CONFIGS_BY_DATASET = {
    "data_multimedia": "agent/memory_guided_workflow/configs/taskbench_multimedia_dataset_config.json",
    "data_huggingface": "agent/memory_guided_workflow/configs/taskbench_huggingface_dataset_config.json",
    "data_dailylifeapis": "agent/memory_guided_workflow/configs/taskbench_dailylife_dataset_config.json",
}
DEFAULT_GOLD_FILE = "taskbench/data_multimedia/data.json"
DEFAULT_SAMPLE_FILE = "taskbench/data_multimedia/multimedia_test_data.json"
DEFAULT_REPLAN_JSON = (
    "taskbench/data_multimedia/replan_reformat_by_self/"
    "data_multimedia_qwen3-14b_20260527_intent_guided_replan.json"
)
DEFAULT_REPLAN_XLSX = (
    "taskbench/data_multimedia/replan_reformat_by_self/"
    "data_multimedia_qwen3-14b_20260527_intent_guided_replan.xlsx"
)
DEFAULT_REPLAN_EVAL_JSON = (
    "taskbench/data_multimedia/replan_reformat_by_self/"
    "data_multimedia_qwen3-14b_20260527_intent_guided_replan.json"
)
DEFAULT_FINAL_JSON = (
    "taskbench/data_multimedia/replan_reformat_by_self/"
    "data_multimedia_qwen3-14b_20260527_intent_audited_replan_edge_repair_final.json"
)
DEFAULT_FINAL_EVAL_JSON = ""
DEFAULT_METRICS_JSON = (
    "taskbench/data_multimedia/replan_reformat_by_self/"
    "data_multimedia_qwen3-14b_20260527_intent_audited_replan_edge_repair_metrics.json"
)


DEFAULT_PREDICTION_FILES_BY_DATASET = {
    "data_multimedia": "qwen3-14b_20260527.json",
    "data_huggingface": "qwen3-14b_20260627.json",
}

DEFAULT_TEST_DATA_FILES_BY_DATASET = {
    "data_multimedia": "multimedia_test_data.json",
    "data_huggingface": "huggingface_test_data.json",
    "data_dailylifeapis": "dailylife_test_data.json",
}


def infer_dataset_dir(args: argparse.Namespace, dataset_config: Mapping[str, Any]) -> Path:
    for raw_path in dataset_path_candidates(args, dataset_config):
        dataset_dir = find_taskbench_data_dir(raw_path)
        if dataset_dir is not None:
            return dataset_dir
    return resolve_path("taskbench/data_multimedia")


def dataset_path_candidates(
    args: argparse.Namespace,
    dataset_config: Mapping[str, Any],
) -> List[Any]:
    candidates: List[Any] = [
        getattr(args, "coverage_xlsx", None),
        getattr(args, "prediction_file", None),
        getattr(args, "sample_file", None),
        getattr(args, "gold_file", None),
        getattr(args, "tool_graph", None),
        getattr(args, "tool_desc", None),
    ]
    for section_name in ("coverage_prompt_variables", "replan_prompt_variables"):
        section = dataset_config.get(section_name)
        if isinstance(section, Mapping):
            candidates.append(section.get("dataset"))
    return candidates


def find_taskbench_data_dir(raw_path: Any) -> Path | None:
    text = str(raw_path or "").strip()
    if not text:
        return None
    path = resolve_path(text)
    for candidate in (path, *path.parents):
        if candidate.name.startswith("data_") and candidate.parent.name == "taskbench":
            return candidate
    return None


def apply_dataset_path_defaults(
    args: argparse.Namespace,
    dataset_dir: Path,
    dataset_config: Mapping[str, Any],
) -> None:
    if not getattr(args, "coverage_xlsx", None):
        args.coverage_xlsx = str(dataset_dir / f"{dataset_dir.name}_gpt55_intent_coverage_table.xlsx")
    if not getattr(args, "prediction_file", None):
        args.prediction_file = str(default_prediction_file(dataset_dir))
    if not getattr(args, "sample_file", None):
        args.sample_file = str(default_sample_file(dataset_dir))
    if not getattr(args, "gold_file", None):
        args.gold_file = str(dataset_dir / "data.json")
    if not getattr(args, "tool_graph", None):
        tool_graph = dataset_dir / "tool_transition_graph.json"
        args.tool_graph = str(tool_graph) if tool_graph.exists() else ""
    if not getattr(args, "tool_desc", None):
        args.tool_desc = get_tool_desc_intent_path(dataset_config, default="")
    if not getattr(args, "tool_desc", None):
        args.tool_desc = str(default_tool_desc_file(dataset_dir))

    output_dir = dataset_dir / DEFAULT_REPLAN_OUTPUT_DIR_NAME
    output_stem = default_replan_output_stem(dataset_dir, args.prediction_file)
    replan_label = replan_output_label(getattr(args, "replan_variant", DEFAULT_REPLAN_VARIANT))
    final_label = final_output_label(getattr(args, "replan_variant", DEFAULT_REPLAN_VARIANT))
    output_defaults = {
        "replan_json": output_dir / f"{output_stem}_{replan_label}.json",
        "replan_xlsx": output_dir / f"{output_stem}_{replan_label}.xlsx",
        "final_json": output_dir / f"{output_stem}_{final_label}_final.json",
        "metrics_json": output_dir / f"{output_stem}_{final_label}_metrics.json",
        "ablation_table_xlsx": output_dir / f"{output_stem}_{final_label}_ablation_table.xlsx",
    }
    for attr, default_path in output_defaults.items():
        if not getattr(args, attr, None):
            setattr(args, attr, str(default_path))


def maybe_apply_dataset_config_default(args: argparse.Namespace, dataset_dir: Path) -> Dict[str, Any]:
    inferred_config = DEFAULT_DATASET_CONFIGS_BY_DATASET.get(dataset_dir.name)
    current_config = str(getattr(args, "dataset_config", "") or "").strip()
    if inferred_config and (
        not current_config
        or normalize_path_text(current_config) == normalize_path_text(DEFAULT_DATASET_CONFIG)
    ):
        args.dataset_config = inferred_config
    return load_dataset_runtime_config(
        resolve_path(args.dataset_config) if getattr(args, "dataset_config", None) else None
    )


def normalize_path_text(path_text: str) -> str:
    return str(path_text or "").replace("\\", "/").strip().lower()


def default_prediction_file(dataset_dir: Path) -> Path:
    prediction_dir = dataset_dir / "predictions_use_demos_2_reformat_by_self"
    preferred = DEFAULT_PREDICTION_FILES_BY_DATASET.get(dataset_dir.name)
    if preferred:
        preferred_path = prediction_dir / preferred
        if preferred_path.exists():
            return preferred_path
    candidates = [
        path
        for path in prediction_dir.glob("*.json")
        if path.is_file() and path.stat().st_size > 0
    ]
    if candidates:
        return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[0]
    raise FileNotFoundError(f"no prediction JSON found in {prediction_dir}; pass --prediction-file")


def default_sample_file(dataset_dir: Path) -> Path:
    preferred = DEFAULT_TEST_DATA_FILES_BY_DATASET.get(dataset_dir.name)
    if preferred:
        preferred_path = dataset_dir / preferred
        if preferred_path.exists():
            return preferred_path
    candidates = sorted(dataset_dir.glob("*_test_data.json"))
    if candidates:
        return candidates[0]
    return dataset_dir / "data.json"


def default_tool_desc_file(dataset_dir: Path) -> Path:
    intent_tool_desc = dataset_dir / "tool_desc_intent.json"
    if intent_tool_desc.exists():
        return intent_tool_desc
    return dataset_dir / "tool_desc.json"


def default_replan_output_stem(dataset_dir: Path, prediction_file: Any) -> str:
    model_name = sanitize_eval_llm_name(Path(str(prediction_file)).stem)
    return f"{dataset_dir.name}_{model_name}"


def replan_output_label(replan_variant: Any) -> str:
    if str(replan_variant or "").strip() == REPLAN_VARIANT_WITH_GRAPH:
        return "intent_guided_replan_with_graph"
    return "intent_guided_replan"


def final_output_label(replan_variant: Any) -> str:
    if str(replan_variant or "").strip() == REPLAN_VARIANT_WITH_GRAPH:
        return "intent_audited_replan_with_graph_edge_repair"
    return "intent_audited_replan_edge_repair"


def main() -> int:
    args = parse_args()
    dataset_config = load_dataset_runtime_config(
        resolve_path(args.dataset_config) if getattr(args, "dataset_config", None) else None
    )
    dataset_dir = infer_dataset_dir(args, dataset_config)
    dataset_config = maybe_apply_dataset_config_default(args, dataset_dir)
    apply_dataset_path_defaults(args, dataset_dir, dataset_config)
    replan_json = ensure_json_output_path(resolve_path(args.replan_json))
    replan_xlsx = resolve_path(args.replan_xlsx)
    replan_eval_json = resolve_optional_json_output_path(args.replan_eval_json)
    final_json = ensure_json_output_path(resolve_path(args.final_json))
    final_eval_json = resolve_optional_json_output_path(args.final_eval_json)
    metrics_json = resolve_optional_json_output_path(args.metrics_json)
    ablation_table_xlsx = resolve_optional_path(args.ablation_table_xlsx)

    if not args.skip_replan:
        replan_status = run_replan_stage(args, replan_json, replan_xlsx, replan_eval_json)
        if replan_status != 0 and not args.allow_partial_repair:
            print("replan_stage_failed; skip edge repair. Use --resume after planner recovers.")
            return replan_status
    elif not replan_json.exists():
        raise FileNotFoundError(f"--skip-replan needs an existing --replan-json: {replan_json}")

    replan_rows = latest_rows_by_id(read_json_records(replan_json))
    final_rows = run_edge_repair_stage(args, replan_rows)

    final_json.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(final_json, final_rows)
    print(f"saved_final_json={final_json}")

    if final_eval_json is not None:
        write_taskbench_eval_json(
            final_eval_json,
            final_rows,
            dependency_type=resolve_evaluation_dependency_type(args, dataset_dir),
        )
        print(f"saved_final_eval_json={final_eval_json}")

    if metrics_json is not None and args.sample_file and args.gold_file:
        metrics = build_metrics(args, replan_rows, final_rows)
        metrics_json.parent.mkdir(parents=True, exist_ok=True)
        metrics_json.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
        print_metrics(metrics)
        print(f"saved_metrics={metrics_json}")
        if ablation_table_xlsx is not None:
            written_ablation_table = write_ablation_table_xlsx(ablation_table_xlsx, metrics["ablation_table"])
            print(f"saved_ablation_table={written_ablation_table}")

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run intent-audited replanning followed by edge-only graph repair."
    )
    parser.add_argument(
        "--coverage-xlsx",
        default=None,
        help=(
            "Intent coverage table. Defaults to "
            "taskbench/data_<dataset>/data_<dataset>_gpt55_intent_coverage_table.xlsx."
        ),
    )
    parser.add_argument(
        "--prediction-file",
        default=None,
        help="Original prediction file keyed by id. Defaults to the dataset's known TaskBench prediction file.",
    )
    parser.add_argument(
        "--tool-desc",
        default=None,
        help=(
            "Path to TaskBench-style tool_desc_intent.json. "
            "Defaults to dataset_config.tool_desc_intent, then multimedia tool_desc_intent.json."
        ),
    )
    parser.add_argument("--tool-graph", default=None)
    parser.add_argument("--dataset-config", default=DEFAULT_DATASET_CONFIG)
    parser.add_argument("--sample-file", default=None)
    parser.add_argument("--gold-file", default=None)
    parser.add_argument("--planner-llm-config", default="configs/qwen.json")
    parser.add_argument("--planner-llm-profile", default=None)
    parser.add_argument("--planner-timeout", type=float, default=None)
    parser.add_argument("--planner-max-retries", type=int, default=None)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--replan-dependency-type",
        choices=("auto", "resource", "temporal"),
        default="auto",
        help="Workflow dependency mode for replan prompts and DAG repair. auto infers from tool_desc.json.",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-replan", action="store_true")
    parser.add_argument(
        "--allow-partial-repair",
        action="store_true",
        help="Continue to edge repair even if some planner rows are retryable failures.",
    )
    parser.add_argument("--dag-self-repair-rounds", type=int, default=1)
    parser.add_argument("--max-tools", type=int, default=80)
    parser.add_argument("--require-tool-graph-edge", action="store_true")
    parser.add_argument(
        "--replan-variant",
        choices=REPLAN_VARIANTS,
        default=DEFAULT_REPLAN_VARIANT,
        help="Defaults to replan. The planner prompt includes --tool-graph when available.",
    )
    parser.add_argument("--graph-context-top-k-per-source", type=int, default=DEFAULT_GRAPH_CONTEXT_TOP_K_PER_SOURCE)
    parser.add_argument("--graph-context-max-candidates", type=int, default=DEFAULT_GRAPH_CONTEXT_MAX_CANDIDATES)
    parser.add_argument(
        "--temporal-chain-prior",
        action="store_true",
        help="Temporal-only: add adjacent task-node chain links as an edge prior.",
    )
    parser.add_argument(
        "--temporal-edge-only-replan",
        action="store_true",
        help="Temporal-only: ask the planner to repair task_links without changing task_nodes or arguments.",
    )
    parser.add_argument(
        "--temporal-edge-only-scope",
        choices=("high-risk", "all"),
        default="high-risk",
        help="Temporal edge-only replan scope. high-risk runs only on empty/sparse/invalid links.",
    )

    parser.add_argument("--replan-json", dest="replan_json", default=None)
    parser.add_argument("--replan-xlsx", default=None)
    parser.add_argument(
        "--replan-eval-json",
        default=None,
        help="Optional evaluate-only JSON output. Disabled by default because --replan-json already contains id/result.",
    )
    parser.add_argument("--final-json", dest="final_json", default=None)
    parser.add_argument(
        "--final-eval-json",
        default=None,
        help="Optional evaluate-only JSON output. Disabled by default because --final-json already contains id/result.",
    )
    parser.add_argument("--metrics-json", default=None)
    parser.add_argument(
        "--original-metrics-json",
        default=None,
        help=(
            "Existing TaskBench metric JSON for the original prediction file. "
            "Defaults to <dataset>/metrics_reformat_by_self/<prediction_stem>.json when present."
        ),
    )
    parser.add_argument(
        "--evaluation-dependency-type",
        choices=("auto", "resource", "temporal"),
        default="auto",
        help="TaskBench dependency type used for ablation evaluation. Defaults to auto from tool_desc.json.",
    )
    parser.add_argument("--ablation-table-xlsx", default=None)

    parser.add_argument(
        "--edge-mode",
        choices=("conservative", "greedy", "global"),
        default="global",
    )
    parser.add_argument("--optional-threshold", type=float, default=0.05)
    parser.add_argument(
        "--argument-sync",
        choices=("append", "rebuild"),
        default="append",
    )
    parser.add_argument(
        "--evaluation-scope",
        choices=("common-with-prediction", "all"),
        default="common-with-prediction",
        help="Use common-with-prediction when the raw prediction file has a different row count.",
    )
    return parser.parse_args()


def run_replan_stage(
    args: argparse.Namespace,
    output_json: Path,
    output_xlsx: Path,
    eval_json: Path | None,
) -> int:
    replan_args = argparse.Namespace(
        coverage_xlsx=args.coverage_xlsx,
        prediction_file=args.prediction_file,
        tool_desc=args.tool_desc,
        output_json=str(output_json),
        output_xlsx=str(output_xlsx),
        eval_json=str(eval_json) if eval_json is not None else None,
        eval_prediction_dir=None,
        eval_llm_name=None,
        no_eval_json=eval_json is None,
        planner_llm_config=args.planner_llm_config,
        planner_llm_profile=args.planner_llm_profile,
        planner_timeout=args.planner_timeout,
        planner_max_retries=args.planner_max_retries,
        dataset_config=args.dataset_config,
        replan_dependency_type=args.replan_dependency_type,
        tool_graph=args.tool_graph,
        require_tool_graph_edge=args.require_tool_graph_edge,
        replan_variant=args.replan_variant,
        graph_context_top_k_per_source=args.graph_context_top_k_per_source,
        graph_context_max_candidates=args.graph_context_max_candidates,
        temporal_chain_prior=getattr(args, "temporal_chain_prior", False),
        temporal_edge_only_replan=getattr(args, "temporal_edge_only_replan", False),
        temporal_edge_only_scope=getattr(args, "temporal_edge_only_scope", "high-risk"),
        workers=args.workers,
        limit=args.limit,
        resume=args.resume,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        dag_self_repair_rounds=args.dag_self_repair_rounds,
        max_tools=args.max_tools,
    )
    prepare_output_files(
        output_json,
        output_xlsx,
        resume=args.resume,
        overwrite=args.overwrite,
        eval_json=eval_json,
    )
    output_lock = acquire_output_lock(output_json)
    try:
        return run_replan(replan_args, output_json, output_xlsx, eval_json)
    finally:
        release_output_lock(output_lock)


def run_edge_repair_stage(
    args: argparse.Namespace,
    rows: Iterable[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    dataset_config = load_dataset_runtime_config(
        resolve_path(args.dataset_config) if getattr(args, "dataset_config", None) else None
    )
    dataset_dir = infer_dataset_dir(args, dataset_config)
    dependency_type = resolve_replan_dependency_type(args, dataset_dir)
    tool_desc_path = resolve_tool_desc_intent_path_arg(getattr(args, "tool_desc", None), dataset_config)
    tool_desc = load_tool_desc(resolve_path(tool_desc_path), max_tools=0)
    tool_catalog = build_tool_catalog(tool_desc)
    transition_index = bg.load_transition_index(args.tool_graph)
    repaired_rows = []
    for row in rows:
        repaired_rows.append(
            repair_prediction_row(
                row,
                tool_catalog=tool_catalog,
                transition_index=transition_index,
                mode=args.edge_mode,
                optional_threshold=args.optional_threshold,
                argument_sync=args.argument_sync,
                dependency_type=dependency_type,
            )
        )
    return repaired_rows


def build_metrics(
    args: argparse.Namespace,
    replan_rows: Sequence[Mapping[str, Any]],
    final_rows: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    prediction_rows = read_json_records(resolve_path(args.prediction_file))
    metric_replan_rows = list(replan_rows)
    metric_final_rows = list(final_rows)
    if args.evaluation_scope == "common-with-prediction":
        prediction_ids = {str(row.get("id") or "") for row in prediction_rows if str(row.get("id") or "")}
        output_ids = {str(row.get("id") or "") for row in metric_final_rows if str(row.get("id") or "")}
        metric_ids = prediction_ids & output_ids
        prediction_rows = [row for row in prediction_rows if str(row.get("id") or "") in metric_ids]
        metric_replan_rows = [row for row in metric_replan_rows if str(row.get("id") or "") in metric_ids]
        metric_final_rows = [row for row in metric_final_rows if str(row.get("id") or "") in metric_ids]
    metric_replan_rows = strip_temporal_link_repair_from_metric_rows(metric_replan_rows)

    dataset_config = load_dataset_runtime_config(
        resolve_path(args.dataset_config) if getattr(args, "dataset_config", None) else None
    )
    dataset_dir = infer_dataset_dir(args, dataset_config)
    original_metrics_json = resolve_original_metrics_json(args, dataset_dir)
    dependency_type = resolve_evaluation_dependency_type(args, dataset_dir)
    stage_rows = {
        "original": prediction_rows,
        "intent_replan": metric_replan_rows,
        "edge_repaired": metric_final_rows,
    }
    stages_to_evaluate = None if original_metrics_json is None else ("intent_replan", "edge_repaired")
    (
        stage_metrics,
        stage_prediction_files,
        stage_metric_files,
        taskbench_prediction_dir,
        taskbench_metric_dir,
    ) = evaluate_ablation_stages_with_taskbench(
        dataset_dir=dataset_dir,
        eval_prediction_dir=build_taskbench_eval_prediction_dir(args, dataset_dir),
        eval_metric_dir=build_taskbench_eval_metric_dir(args, dataset_dir),
        stage_rows=stage_rows,
        stages_to_evaluate=stages_to_evaluate,
        dependency_type=dependency_type,
    )
    if original_metrics_json is not None:
        original_prediction_file = resolve_path(args.prediction_file)
        stage_metrics["original"] = original_metric_from_existing_taskbench_metrics(
            metric_file=original_metrics_json,
            prediction_file=original_prediction_file,
            input_rows=len(prediction_rows),
        )
        stage_prediction_files["original"] = str(original_prediction_file)
        stage_metric_files["original"] = str(original_metrics_json)
    metrics = {
        "evaluation_scope": args.evaluation_scope,
        "evaluation_engine": "taskbench.evaluate",
        "metric_reader": "print_evaluate_metrics_table.extract_metric_row",
        "original_metric_source": "existing" if original_metrics_json is not None else "evaluated",
        "taskbench_dependency_type": dependency_type,
        "taskbench_data_dir": str(dataset_dir),
        "taskbench_prediction_dir": taskbench_prediction_dir,
        "taskbench_metric_dir": taskbench_metric_dir,
        "taskbench_stage_prediction_files": stage_prediction_files,
        "taskbench_stage_metric_files": stage_metric_files,
        "original": stage_metrics["original"],
        "intent_replan": stage_metrics["intent_replan"],
        "edge_repaired": stage_metrics["edge_repaired"],
        "replan_summary": summarize_replan_rows(metric_replan_rows),
        "edge_repair_summary": summarize_repairs(metric_final_rows),
    }
    metrics["delta_replan_vs_original"] = metric_delta(metrics["intent_replan"], metrics["original"])
    metrics["delta_edge_vs_replan"] = metric_delta(metrics["edge_repaired"], metrics["intent_replan"])
    metrics["delta_final_vs_original"] = metric_delta(metrics["edge_repaired"], metrics["original"])
    metrics["ablation_table"] = build_ablation_table(metrics)
    return metrics


TEMPORAL_LINK_REPAIR_APPLIED_STATUSES = {"accepted", "chain_prior_applied"}


def strip_temporal_link_repair_from_metric_rows(
    rows: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    return [strip_temporal_link_repair_from_metric_row(row) for row in rows]


def strip_temporal_link_repair_from_metric_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    output = copy.deepcopy(dict(row))
    trace = output.get("temporal_link_repair")
    if not isinstance(trace, Mapping):
        agent_trace = output.get("agent_trace")
        if isinstance(agent_trace, Mapping):
            trace = agent_trace.get("temporal_link_repair")
    if not isinstance(trace, Mapping):
        return output
    status = str(trace.get("status") or "").strip()
    if status not in TEMPORAL_LINK_REPAIR_APPLIED_STATUSES:
        return output

    pre_repair_workflow = select_pre_temporal_link_repair_workflow(output)
    if not pre_repair_workflow.get("task_nodes"):
        return output
    output["result"] = pre_repair_workflow
    output["result_source"] = str(
        output.get("pre_temporal_link_repair_result_source")
        or output.get("dag_replan_result_source")
        or output.get("result_source")
        or ""
    )
    output["result_source_generic"] = generic_result_source(output["result_source"])
    output["selection_reason"] = str(
        output.get("pre_temporal_link_repair_selection_reason")
        or output.get("dag_replan_selection_reason")
        or output.get("selection_reason")
        or ""
    )
    output["metric_temporal_link_repair_removed"] = True
    return output


def select_pre_temporal_link_repair_workflow(row: Mapping[str, Any]) -> Dict[str, Any]:
    for key in (
        "pre_temporal_link_repair_result",
        "dag_replan_result",
        "replanned_workflow",
        "previous_workflow",
    ):
        value = row.get(key)
        if isinstance(value, Mapping):
            workflow = normalize_workflow(value)
            if workflow.get("task_nodes"):
                return workflow
    return {}


TASKBENCH_ABLATION_METRICS = ("f1", "ed", "link", "argument")
TASKBENCH_ABLATION_SPLIT = "overall"
TASKBENCH_ABLATION_N_TOOL = "overall"
TASKBENCH_ABLATION_DEPENDENCY_TYPE = "resource"


def resolve_evaluation_dependency_type(args: argparse.Namespace, dataset_dir: Path) -> str:
    requested = str(getattr(args, "evaluation_dependency_type", "auto") or "auto").strip().lower()
    if requested in {"resource", "temporal"}:
        return requested
    return infer_taskbench_dependency_type(dataset_dir)


def resolve_replan_dependency_type(args: argparse.Namespace, dataset_dir: Path) -> str:
    requested = str(getattr(args, "replan_dependency_type", "auto") or "auto").strip().lower()
    if requested in {"resource", "temporal"}:
        return requested
    return infer_taskbench_dependency_type(dataset_dir)


def infer_taskbench_dependency_type(dataset_dir: Path) -> str:
    tool_desc_path = dataset_dir / "tool_desc.json"
    payload = json.loads(tool_desc_path.read_text(encoding="utf-8-sig"))
    nodes = payload.get("nodes", []) if isinstance(payload, Mapping) else []
    if nodes and isinstance(nodes[0], Mapping):
        first_node = nodes[0]
        if "input-type" in first_node and "output-type" in first_node:
            return "resource"
    return "temporal"


def resolve_original_metrics_json(args: argparse.Namespace, dataset_dir: Path) -> Path | None:
    explicit = resolve_optional_path(getattr(args, "original_metrics_json", None))
    if explicit is not None:
        return explicit if explicit.exists() else None
    prediction_stem = Path(str(getattr(args, "prediction_file", ""))).stem
    if not prediction_stem:
        return None
    candidate = dataset_dir / "metrics_reformat_by_self" / f"{prediction_stem}.json"
    return candidate if candidate.exists() else None


def build_taskbench_eval_prediction_dir(args: argparse.Namespace, dataset_dir: Path) -> Path:
    metrics_path = resolve_optional_path(getattr(args, "metrics_json", None))
    if metrics_path is not None:
        candidate = metrics_path.parent / f"{metrics_path.stem}_eval_inputs"
        if path_is_relative_to(candidate, dataset_dir):
            return candidate
    output_stem = default_replan_output_stem(dataset_dir, getattr(args, "prediction_file", "replan"))
    return dataset_dir / DEFAULT_REPLAN_OUTPUT_DIR_NAME / "eval_inputs" / output_stem


def build_taskbench_eval_metric_dir(args: argparse.Namespace, dataset_dir: Path) -> Path:
    metrics_path = resolve_optional_path(getattr(args, "metrics_json", None))
    if metrics_path is not None:
        return metrics_path.parent / f"{metrics_path.stem}_eval_metrics"
    output_stem = default_replan_output_stem(dataset_dir, getattr(args, "prediction_file", "replan"))
    return dataset_dir / DEFAULT_REPLAN_OUTPUT_DIR_NAME / "eval_metrics" / output_stem


def path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def evaluate_ablation_stages_with_taskbench(
    dataset_dir: Path,
    eval_prediction_dir: Path,
    eval_metric_dir: Path,
    stage_rows: Mapping[str, Iterable[Mapping[str, Any]]],
    stages_to_evaluate: Iterable[str] | None = None,
    dependency_type: str = TASKBENCH_ABLATION_DEPENDENCY_TYPE,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str], Dict[str, str], str, str]:
    stage_prediction_files, input_counts, prediction_dir = write_ablation_stage_inputs(
        dataset_dir=dataset_dir,
        eval_prediction_dir=eval_prediction_dir,
        stage_rows=stage_rows,
        dependency_type=dependency_type,
    )
    stage_names = tuple(stages_to_evaluate) if stages_to_evaluate is not None else tuple(stage_rows.keys())
    metrics, stage_metric_files = evaluate_saved_ablation_stage_inputs(
        dataset_dir=dataset_dir,
        prediction_dir=prediction_dir,
        eval_metric_dir=eval_metric_dir,
        stage_prediction_files=stage_prediction_files,
        input_counts=input_counts,
        stage_names=stage_names,
        dependency_type=dependency_type,
    )
    return metrics, stage_prediction_files, stage_metric_files, prediction_dir, str(eval_metric_dir)


def write_ablation_stage_inputs(
    dataset_dir: Path,
    eval_prediction_dir: Path,
    stage_rows: Mapping[str, Iterable[Mapping[str, Any]]],
    dependency_type: str | None = None,
) -> Tuple[Dict[str, str], Dict[str, int], str]:
    eval_prediction_dir.mkdir(parents=True, exist_ok=True)
    prediction_dir = taskbench_prediction_dir_arg(eval_prediction_dir, dataset_dir)
    stage_prediction_files: Dict[str, str] = {}
    input_counts: Dict[str, int] = {}
    for stage, rows in stage_rows.items():
        row_list = list(rows)
        llm_name = sanitize_eval_llm_name(stage)
        stage_file = eval_prediction_dir / f"{llm_name}.json"
        write_taskbench_eval_json(stage_file, row_list, dependency_type=dependency_type)
        stage_prediction_files[stage] = str(stage_file)
        input_counts[stage] = len(row_list)
    return stage_prediction_files, input_counts, prediction_dir


def evaluate_saved_ablation_stage_inputs(
    dataset_dir: Path,
    prediction_dir: str,
    eval_metric_dir: Path,
    stage_prediction_files: Mapping[str, str],
    input_counts: Mapping[str, int],
    stage_names: Iterable[str],
    dependency_type: str,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    taskbench_evaluate, tool_desc, tool_map, tool_output_type_map, tool_map_reverse = load_taskbench_evaluate_context(
        dataset_dir,
        dependency_type=dependency_type,
    )
    eval_metric_dir.mkdir(parents=True, exist_ok=True)
    metrics: Dict[str, Dict[str, Any]] = {}
    stage_metric_files: Dict[str, str] = {}
    for stage in stage_names:
        llm_name = sanitize_eval_llm_name(stage)
        all_metric_dict: Dict[str, Dict[str, Any]] = {}
        taskbench_evaluate.evaluate(
            str(dataset_dir),
            prediction_dir,
            llm_name,
            TASKBENCH_ABLATION_SPLIT,
            TASKBENCH_ABLATION_N_TOOL,
            TASKBENCH_ABLATION_METRICS,
            tool_desc,
            tool_map,
            tool_output_type_map,
            tool_map_reverse,
            all_metric_dict,
            dependency_type=dependency_type,
        )
        raw_metric = dict(all_metric_dict.get(f"{TASKBENCH_ABLATION_SPLIT}_{TASKBENCH_ABLATION_N_TOOL}", {}))
        metric_file = eval_metric_dir / f"{llm_name}.json"
        metric_file.write_text(json.dumps(all_metric_dict, ensure_ascii=False, indent=2), encoding="utf-8")
        metric_table_row = extract_print_evaluate_metric_row(metric_file)
        metrics[stage] = taskbench_metric_to_ablation_metric(
            raw_metric,
            metric_table_row=metric_table_row,
            input_rows=int(input_counts.get(stage, 0) or 0),
            stage_file=Path(stage_prediction_files[stage]),
            metric_file=metric_file,
        )
        stage_metric_files[stage] = str(metric_file)
    return metrics, stage_metric_files


def extract_print_evaluate_metric_row(metric_file: Path) -> Dict[str, Any]:
    from agent.memory_guided_workflow.experiments.print_evaluate_metrics_table import extract_metric_row

    row = extract_metric_row(metric_file, section=f"{TASKBENCH_ABLATION_SPLIT}_{TASKBENCH_ABLATION_N_TOOL}")
    if row is None:
        raise ValueError(f"missing N-F1/E-F1/NED metrics in {metric_file}")
    return row


def original_metric_from_existing_taskbench_metrics(
    metric_file: Path,
    prediction_file: Path,
    input_rows: int,
) -> Dict[str, Any]:
    raw_metric = load_taskbench_metric_section(metric_file)
    metric_table_row = extract_print_evaluate_metric_row(metric_file)
    return taskbench_metric_to_ablation_metric(
        raw_metric,
        metric_table_row=metric_table_row,
        input_rows=input_rows,
        stage_file=prediction_file,
        metric_file=metric_file,
    )


def load_taskbench_metric_section(metric_file: Path) -> Dict[str, Any]:
    payload = json.loads(metric_file.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        return {}
    section = payload.get(f"{TASKBENCH_ABLATION_SPLIT}_{TASKBENCH_ABLATION_N_TOOL}")
    if isinstance(section, Mapping):
        return dict(section)
    return dict(payload)


def load_taskbench_evaluate_context(
    dataset_dir: Path,
    dependency_type: str,
) -> Tuple[Any, Mapping[str, Any], Dict[str, int], Dict[str, str] | None, Dict[int, str]]:
    from taskbench import evaluate as taskbench_evaluate

    tool_desc = json.loads((dataset_dir / "tool_desc.json").read_text(encoding="utf-8-sig"))
    tool_nodes = tool_desc.get("nodes", []) if isinstance(tool_desc, Mapping) else []
    tool_map = {tool["id"]: index + 1 for index, tool in enumerate(tool_nodes)}
    tool_map_reverse = {index + 1: tool["id"] for index, tool in enumerate(tool_nodes)}
    tool_map_reverse[0] = "NEGATIVE"
    tool_map["<PAD>"] = -1
    tool_output_type_map = None
    if dependency_type == "resource":
        tool_output_type_map = {
            taskbench_evaluate._normalize_task_name(tool["id"]): first_tool_output_type(tool)
            for tool in tool_nodes
        }
    return taskbench_evaluate, tool_desc, tool_map, tool_output_type_map, tool_map_reverse


def first_tool_output_type(tool: Mapping[str, Any]) -> str:
    output_types = tool.get("output-type", [])
    if isinstance(output_types, list) and output_types:
        return str(output_types[0])
    return "none"


def taskbench_prediction_dir_arg(eval_prediction_dir: Path, dataset_dir: Path) -> str:
    relative = eval_prediction_dir.resolve().relative_to(dataset_dir.resolve())
    return str(relative).replace("\\", "/")


def taskbench_metric_to_ablation_metric(
    raw_metric: Mapping[str, Any],
    metric_table_row: Mapping[str, Any],
    input_rows: int,
    stage_file: Path,
    metric_file: Path,
) -> Dict[str, Any]:
    all_samples = int(raw_metric.get("all_samples", 0) or 0)
    support = int(raw_metric.get("link_supports", raw_metric.get("node_supports", 0)) or 0)
    return {
        "engine": "taskbench.evaluate",
        "metric_reader": "print_evaluate_metrics_table.extract_metric_row",
        "prediction_file": str(stage_file),
        "metric_file": str(metric_file),
        "input_rows": input_rows,
        "prediction_ids": all_samples,
        "common_ids": all_samples,
        "support": support,
        "node_micro_precision": metric_float(raw_metric.get("node_micro_precision_no_matching")),
        "node_micro_recall": metric_float(raw_metric.get("node_micro_recall_no_matching")),
        "node_micro_f1": metric_float(metric_table_row.get("n_f1")),
        "edge_micro_f1": metric_float(metric_table_row.get("e_f1")),
        "ned": metric_float(metric_table_row.get("ned")),
        "print_evaluate_metrics_table_row": dict(metric_table_row),
        "taskbench_metric": dict(raw_metric),
    }


def metric_float(value: Any) -> float:
    if value is None:
        return 0.0
    return float(value)


def summarize_replan_rows(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    decision_counts: Dict[str, int] = {}
    source_counts: Dict[str, int] = {}
    retryable_failed = 0
    total = 0
    for row in rows:
        total += 1
        decision = str(row.get("replan_decision") or "")
        source = str(row.get("result_source_generic") or row.get("result_source") or "")
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
        source_counts[source] = source_counts.get(source, 0) + 1
        if is_retryable_replan_failure(row):
            retryable_failed += 1
    return {
        "rows": total,
        "decision_counts": decision_counts,
        "source_counts": source_counts,
        "retryable_failed": retryable_failed,
    }


def metric_delta(left: Mapping[str, Any], right: Mapping[str, Any]) -> Dict[str, float]:
    return {
        "node_micro_f1": left["node_micro_f1"] - right["node_micro_f1"],
        "edge_micro_f1": left["edge_micro_f1"] - right["edge_micro_f1"],
        "ned": left["ned"] - right["ned"],
    }


ABLATION_TABLE_COLUMNS = [
    "stage",
    "intent_auditor",
    "replan",
    "edge_repair",
    "rows",
    "support",
    "n_f1",
    "e_f1",
    "ned",
    "delta_n_f1_vs_prev",
    "delta_e_f1_vs_prev",
    "delta_ned_vs_prev",
    "delta_n_f1_vs_original",
    "delta_e_f1_vs_original",
    "delta_ned_vs_original",
]


ABLATION_TABLE_HEADERS = {
    "stage": "Stage",
    "intent_auditor": "Intent Auditor",
    "replan": "Replan",
    "edge_repair": "Edge Repair",
    "rows": "Rows",
    "support": "Support",
    "n_f1": "N-F1",
    "e_f1": "E-F1",
    "ned": "NED",
    "delta_n_f1_vs_prev": "Delta N-F1 vs Prev",
    "delta_e_f1_vs_prev": "Delta E-F1 vs Prev",
    "delta_ned_vs_prev": "Delta NED vs Prev",
    "delta_n_f1_vs_original": "Delta N-F1 vs Original",
    "delta_e_f1_vs_original": "Delta E-F1 vs Original",
    "delta_ned_vs_original": "Delta NED vs Original",
}


ABLATION_STAGE_SPECS = [
    ("original", "off", "off", "off"),
    ("intent_replan", "on", "on", "off"),
    ("edge_repaired", "on", "on", "on"),
]


def build_ablation_table(metrics: Mapping[str, Any]) -> List[Dict[str, Any]]:
    original = metrics["original"]
    replan_summary = metrics.get("replan_summary", {})
    repair_summary = metrics.get("edge_repair_summary", {})
    rows: List[Dict[str, Any]] = []
    previous_metric: Mapping[str, Any] | None = None
    for stage, intent_auditor, replan, edge_repair in ABLATION_STAGE_SPECS:
        current_metric = metrics[stage]
        previous_delta = metric_delta(current_metric, previous_metric) if previous_metric is not None else None
        original_delta = metric_delta(current_metric, original)
        rows.append(
            {
                "stage": stage,
                "intent_auditor": intent_auditor,
                "replan": replan,
                "edge_repair": edge_repair,
                "rows": ablation_stage_rows(stage, current_metric, replan_summary, repair_summary),
                "support": int(current_metric.get("support", 0) or 0),
                "n_f1": percent(current_metric.get("node_micro_f1")),
                "e_f1": percent(current_metric.get("edge_micro_f1")),
                "ned": percent(current_metric.get("ned")),
                "delta_n_f1_vs_prev": percent(previous_delta.get("node_micro_f1")) if previous_delta else "",
                "delta_e_f1_vs_prev": percent(previous_delta.get("edge_micro_f1")) if previous_delta else "",
                "delta_ned_vs_prev": percent(previous_delta.get("ned")) if previous_delta else "",
                "delta_n_f1_vs_original": percent(original_delta.get("node_micro_f1")),
                "delta_e_f1_vs_original": percent(original_delta.get("edge_micro_f1")),
                "delta_ned_vs_original": percent(original_delta.get("ned")),
            }
        )
        previous_metric = current_metric
    return rows


def ablation_stage_rows(
    stage: str,
    current_metric: Mapping[str, Any],
    replan_summary: Mapping[str, Any],
    repair_summary: Mapping[str, Any],
) -> int:
    if stage == "intent_replan":
        return int(replan_summary.get("rows", 0) or 0)
    if stage == "edge_repaired":
        return int(repair_summary.get("rows", 0) or 0)
    return int(current_metric.get("prediction_ids", 0) or 0)


def percent(value: Any) -> float:
    return round(float(value or 0.0) * 100.0, 4)


def write_ablation_table_xlsx(path: Path, rows: Sequence[Mapping[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    xlsx_rows: List[List[Any]] = [[ABLATION_TABLE_HEADERS[column] for column in ABLATION_TABLE_COLUMNS]]
    for row in rows:
        xlsx_rows.append([row.get(column, "") for column in ABLATION_TABLE_COLUMNS])
    try:
        bg.write_xlsx_rows(path, xlsx_rows)
        return path
    except PermissionError:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        fallback = path.with_name(f"{path.stem}.fallback_{timestamp}{path.suffix}")
        bg.write_xlsx_rows(fallback, xlsx_rows)
        print(f"ablation_table_permission_denied={path}")
        return fallback


def print_metrics(metrics: Mapping[str, Any]) -> None:
    print_ablation_table(metrics.get("ablation_table", []))
    print("replan_summary=" + json.dumps(metrics.get("replan_summary", {}), ensure_ascii=False))
    print("edge_repair_summary=" + json.dumps(metrics.get("edge_repair_summary", {}), ensure_ascii=False))


def print_ablation_table(rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        return
    display_rows = [
        [format_ablation_cell(column, row.get(column, "")) for column in ABLATION_TABLE_COLUMNS]
        for row in rows
    ]
    headers = [ABLATION_TABLE_HEADERS[column] for column in ABLATION_TABLE_COLUMNS]
    widths = [
        max(len(header), *(len(row[index]) for row in display_rows))
        for index, header in enumerate(headers)
    ]
    header = " | ".join(value.ljust(widths[index]) for index, value in enumerate(headers))
    separator = "-+-".join("-" * width for width in widths)
    print("ablation_table:")
    print(header)
    print(separator)
    for row in display_rows:
        print(" | ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def format_ablation_cell(column: str, value: Any) -> str:
    if value == "":
        return ""
    if isinstance(value, float):
        return f"{value:+.2f}" if column.startswith("delta_") else f"{value:.2f}"
    return str(value)



def repair_prediction_row(
    row: Mapping[str, Any],
    tool_catalog: Mapping[str, Dict[str, Any]],
    transition_index: Mapping[Tuple[str, str], Any],
    mode: str,
    optional_threshold: float,
    argument_sync: str,
    dependency_type: str = "resource",
) -> Dict[str, Any]:
    result = row.get("result") if isinstance(row.get("result"), Mapping) else row
    workflow = normalize_workflow(result)
    original_result = copy.deepcopy(result) if isinstance(result, Mapping) else dict(workflow)
    repaired, trace = graph_constrained_repair_workflow(
        workflow,
        tool_catalog=tool_catalog,
        transition_index=transition_index,
        mode=mode,
        optional_threshold=optional_threshold,
        argument_sync=argument_sync,
        dependency_type=dependency_type,
    )
    trace = dict(trace)
    accepted = trace.get("validation_status") != "failed"
    trace["applied"] = accepted
    if not accepted:
        trace["rejection_reason"] = "validation_failed"
    output = dict(row)
    output["result"] = repaired if accepted else original_result
    output["graph_repair_trace"] = trace
    return output


def graph_constrained_repair_workflow(
    workflow: Mapping[str, Any],
    tool_catalog: Mapping[str, Dict[str, Any]],
    transition_index: Mapping[Tuple[str, str], Any],
    mode: str = "conservative",
    optional_threshold: float = 0.08,
    argument_sync: str = "append",
    dependency_type: str = "resource",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    dependency_type = normalize_dependency_type(dependency_type)
    nodes = normalize_nodes_for_repair(workflow.get("task_nodes"))
    if not nodes:
        return dict(workflow), {"status": "skipped_empty_nodes", "operations": []}

    explicit_edges, explicit_errors = normalize_task_links_to_edges(workflow.get("task_links"), nodes)
    argument_edges, argument_errors = (
        ([], []) if dependency_type == "temporal" else materialize_edges_from_node_arguments(nodes)
    )
    if dependency_type == "temporal":
        operations: List[Dict[str, Any]] = []
        original_edges = dedupe_valid_edges(explicit_edges, len(nodes), operations, allow_backward=True)
        final_edges = original_edges
        repaired = dict(workflow)
        repaired["task_nodes"] = nodes
        repaired["task_links"] = build_task_links_from_edges(nodes, final_edges)
        validation = validate_workflow_dag(
            repaired,
            tool_catalog=tool_catalog,
            transition_index=transition_index,
            require_tool_graph_edge=False,
            dependency_type=dependency_type,
        )
        return repaired, {
            "status": "changed" if operations else "unchanged",
            "mode": mode,
            "argument_sync": "none",
            "dependency_type": dependency_type,
            "operations": operations,
            "explicit_link_errors": explicit_errors,
            "argument_errors": argument_errors,
            "validation_status": validation.get("status"),
            "validation_errors": validation.get("errors", []),
            "validation_warnings": validation.get("warnings", []),
        }
    original_pairs = edge_key_set(explicit_edges + argument_edges)

    operations: List[Dict[str, Any]] = []
    selected_edges: List[Dict[str, int]] = []
    selected_pairs: set[Tuple[int, int]] = set()

    if mode == "global":
        selected_edges = choose_global_edges(
            nodes=nodes,
            tool_catalog=tool_catalog,
            transition_index=transition_index,
            original_pairs=original_pairs,
            optional_threshold=optional_threshold,
            operations=operations,
        )
    else:
        for target_index in range(len(nodes)):
            target_edges = choose_target_edges(
                target_index=target_index,
                nodes=nodes,
                tool_catalog=tool_catalog,
                transition_index=transition_index,
                original_pairs=original_pairs,
                mode=mode,
                optional_threshold=optional_threshold,
                operations=operations,
            )
            for edge in target_edges:
                pair = (edge["source"], edge["target"])
                if pair not in selected_pairs:
                    selected_pairs.add(pair)
                    selected_edges.append(edge)

    if dependency_type == "temporal":
        final_edges = dedupe_valid_edges(selected_edges, len(nodes), operations, allow_backward=True)
    elif argument_sync == "rebuild":
        rebuild_arguments_from_edges(nodes, selected_edges, operations)
        final_edges = dedupe_edges(selected_edges)
    else:
        append_missing_argument_refs(nodes, selected_edges, argument_edges, operations)
        final_argument_edges, final_argument_errors = materialize_edges_from_node_arguments(nodes)
        if final_argument_errors:
            operations.append({"action": "final_argument_errors", "errors": final_argument_errors})
        final_edges = dedupe_edges(final_argument_edges + selected_edges)
    repaired = dict(workflow)
    repaired["task_nodes"] = nodes
    repaired["task_links"] = build_task_links_with_slots(nodes, final_edges, tool_catalog)
    validation = validate_workflow_dag(
        repaired,
        tool_catalog=tool_catalog,
        transition_index=transition_index,
        require_tool_graph_edge=False,
        dependency_type=dependency_type,
    )
    return repaired, {
        "status": "changed" if operations else "unchanged",
        "mode": mode,
        "argument_sync": argument_sync,
        "dependency_type": dependency_type,
        "operations": operations,
        "explicit_link_errors": explicit_errors,
        "argument_errors": argument_errors,
        "validation_status": validation.get("status"),
        "validation_errors": validation.get("errors", []),
        "validation_warnings": validation.get("warnings", []),
    }


def append_missing_argument_refs(
    nodes: List[Dict[str, Any]],
    selected_edges: Iterable[Mapping[str, int]],
    existing_argument_edges: Iterable[Mapping[str, int]],
    operations: List[Dict[str, Any]],
) -> None:
    existing_pairs = edge_key_set(existing_argument_edges)
    for edge in selected_edges:
        try:
            source = int(edge["source"])
            target = int(edge["target"])
        except (KeyError, TypeError, ValueError):
            continue
        if not valid_node_index(source, len(nodes)) or not valid_node_index(target, len(nodes)):
            continue
        if (source, target) in existing_pairs:
            continue
        old_arguments = normalize_list(nodes[target].get("arguments"))
        node_ref = f"<node-{source}>"
        if node_ref in old_arguments:
            existing_pairs.add((source, target))
            continue
        nodes[target]["arguments"] = old_arguments + [node_ref]
        existing_pairs.add((source, target))
        operations.append(
            {
                "action": "append_argument_ref_for_graph_edge",
                "source": source,
                "target": target,
                "argument": node_ref,
            }
        )


def choose_global_edges(
    nodes: List[Dict[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]],
    transition_index: Mapping[Tuple[str, str], Any],
    original_pairs: set[Tuple[int, int]],
    optional_threshold: float,
    operations: List[Dict[str, Any]],
) -> List[Dict[str, int]]:
    selected_edges: List[Dict[str, int]] = []
    selected_pairs: set[Tuple[int, int]] = set()
    for target_index in range(1, len(nodes)):
        for edge in choose_global_target_edges(
            target_index=target_index,
            nodes=nodes,
            tool_catalog=tool_catalog,
            transition_index=transition_index,
            original_pairs=original_pairs,
            optional_threshold=optional_threshold,
            operations=operations,
        ):
            pair = (edge["source"], edge["target"])
            if pair not in selected_pairs:
                selected_pairs.add(pair)
                selected_edges.append(edge)
    return selected_edges


def choose_global_target_edges(
    target_index: int,
    nodes: List[Dict[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]],
    transition_index: Mapping[Tuple[str, str], Any],
    original_pairs: set[Tuple[int, int]],
    optional_threshold: float,
    operations: List[Dict[str, Any]],
) -> List[Dict[str, int]]:
    target_slots = required_slots_for_node(target_index, nodes, tool_catalog)
    if not target_slots:
        return []

    literal_counts = literal_slot_counts_for_node(target_index, nodes, target_slots)
    literal_argument_count = count_literal_arguments(nodes[target_index])
    selected: List[Dict[str, int]] = []
    used_sources: set[int] = set()
    selected_slots: set[str] = set()

    for slot in target_slots:
        literal_count = literal_counts.get(slot, 0)
        allow_literal_replacement = not (
            literal_count > 0 and target_slots.count(slot) > 1 and slot in selected_slots
        )
        candidate = choose_best_global_edge_for_slot(
            slot=slot,
            target_index=target_index,
            nodes=nodes,
            tool_catalog=tool_catalog,
            transition_index=transition_index,
            original_pairs=original_pairs,
            used_sources=used_sources,
            target_slots=target_slots,
            literal_count=literal_count,
            literal_argument_count=literal_argument_count,
            optional_threshold=optional_threshold,
            allow_literal_replacement=allow_literal_replacement,
        )
        if candidate is None:
            if literal_count > 0:
                literal_counts[slot] = max(0, literal_count - 1)
            continue

        selected.append({"source": candidate["source"], "target": target_index})
        used_sources.add(candidate["source"])
        selected_slots.add(slot)
        operations.append(
            {
                "action": candidate["action"],
                "source": candidate["source"],
                "target": target_index,
                "slot": slot,
                "score": round(candidate["score"], 4),
                "literal_score": round(candidate["literal_score"], 4),
                "transition_probability": candidate["transition_probability"],
            }
        )
    return selected


def choose_best_global_edge_for_slot(
    slot: str,
    target_index: int,
    nodes: Sequence[Mapping[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]],
    transition_index: Mapping[Tuple[str, str], Any],
    original_pairs: set[Tuple[int, int]],
    used_sources: set[int],
    target_slots: Sequence[str],
    literal_count: int,
    literal_argument_count: int,
    optional_threshold: float,
    allow_literal_replacement: bool,
) -> Dict[str, Any] | None:
    literal_score = global_literal_score(slot, literal_count, literal_argument_count)
    candidates: List[Tuple[float, int, Dict[str, Any]]] = []
    for source_index in range(target_index):
        if source_index in used_sources:
            continue
        if slot not in matching_source_slots(source_index, target_index, nodes, tool_catalog, [slot]):
            continue

        probability = transition_probability(source_index, target_index, nodes, tool_catalog, transition_index)
        is_original = (source_index, target_index) in original_pairs
        if not is_original and (probability is None or probability <= 0):
            continue
        if literal_count > 0 and not is_original:
            if not allow_literal_replacement:
                continue
            if probability is None or probability < optional_threshold:
                continue
            if not optional_slot_allowed(slot, target_slots):
                continue

        score = edge_score(
            source_index=source_index,
            target_index=target_index,
            probability=probability if probability is not None else 0.0,
            is_original=is_original,
        )
        if not is_original:
            score -= global_non_original_distance_penalty(source_index, target_index, literal_count)
        if literal_count > 0 and not is_original:
            score -= global_literal_replacement_margin(slot, literal_argument_count)
        candidates.append(
            (
                score,
                source_index,
                {
                    "source": source_index,
                    "score": score,
                    "literal_score": literal_score,
                    "transition_probability": probability,
                    "action": "global_keep_original_edge" if is_original else "global_select_graph_edge",
                },
            )
        )

    if not candidates:
        return None
    candidates.sort(reverse=True)
    _, _, candidate = candidates[0]
    if literal_count > 0 and candidate["score"] < literal_score:
        return None
    if literal_count == 0 and not (candidate["source"], target_index) in original_pairs:
        if candidate["transition_probability"] is None or candidate["transition_probability"] <= 0:
            return None
    return candidate


def literal_slot_counts_for_node(
    target_index: int,
    nodes: Sequence[Mapping[str, Any]],
    target_slots: Sequence[str],
) -> Dict[str, int]:
    remaining = list(target_slots)
    counts: Dict[str, int] = {}
    for argument in normalize_list(nodes[target_index].get("arguments")):
        if parse_node_reference(argument) is not None:
            continue
        matched = bg.consume_first_matching_slot(remaining, infer_literal_argument_types(argument))
        if matched:
            counts[matched] = counts.get(matched, 0) + 1
    return counts


def count_literal_arguments(node: Mapping[str, Any]) -> int:
    return sum(
        1
        for argument in normalize_list(node.get("arguments"))
        if parse_node_reference(argument) is None
    )


def global_literal_score(slot: str, literal_count: int, literal_argument_count: int) -> float:
    if literal_count <= 0:
        return 0.0
    if slot in {"image", "audio", "video"}:
        score = 17.5
    elif slot == "url":
        score = 16.0
    elif slot == "text":
        score = 12.0
    else:
        score = 14.0
    if literal_count > 1:
        score += min(6.0, float(literal_count - 1) * 3.0)
    if slot == "text" and literal_argument_count > 1:
        score += 4.0
    return score


def global_literal_replacement_margin(slot: str, literal_argument_count: int) -> float:
    if slot == "text" and literal_argument_count > 1:
        return 2.0
    return 2.0


def global_non_original_distance_penalty(source_index: int, target_index: int, literal_count: int) -> float:
    distance = max(1, target_index - source_index)
    if distance <= 1:
        return 0.0
    step_penalty = 8.0 if literal_count > 0 else 4.0
    return float(distance - 1) * step_penalty


def choose_target_edges(
    target_index: int,
    nodes: List[Dict[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]],
    transition_index: Mapping[Tuple[str, str], Any],
    original_pairs: set[Tuple[int, int]],
    mode: str,
    optional_threshold: float,
    operations: List[Dict[str, Any]],
) -> List[Dict[str, int]]:
    if target_index <= 0:
        return []

    remaining = required_slots_for_node(target_index, nodes, tool_catalog)
    selected: List[Dict[str, int]] = []
    used_sources: set[int] = set()

    original_sources = sorted(source for source, target in original_pairs if target == target_index)
    while remaining:
        original_choice = choose_best_original_source_for_slot(
            target_index=target_index,
            nodes=nodes,
            tool_catalog=tool_catalog,
            transition_index=transition_index,
            original_sources=original_sources,
            used_sources=used_sources,
            remaining=remaining,
        )
        if original_choice is None:
            break
        source_index, matched, probability = original_choice
        consume_named_slot(remaining, matched)
        selected.append({"source": source_index, "target": target_index})
        used_sources.add(source_index)
        operations.append(
            {
                "action": "keep_original_graph_edge",
                "source": source_index,
                "target": target_index,
                "slot": matched,
                "transition_probability": probability,
            }
        )

    consume_literal_slots(target_index, nodes, remaining)
    while remaining:
        slot = remaining[0]
        source = choose_best_source_for_slot(
            slot=slot,
            target_index=target_index,
            nodes=nodes,
            tool_catalog=tool_catalog,
            transition_index=transition_index,
            used_sources=used_sources,
            original_pairs=original_pairs,
        )
        if source is None:
            break
        matched = consume_source_slot(source, target_index, nodes, tool_catalog, remaining)
        if not matched:
            break
        probability = transition_probability(source, target_index, nodes, tool_catalog, transition_index)
        selected.append({"source": source, "target": target_index})
        used_sources.add(source)
        operations.append(
            {
                "action": "add_required_graph_edge",
                "source": source,
                "target": target_index,
                "slot": matched,
                "transition_probability": probability,
            }
        )
        consume_literal_slots(target_index, nodes, remaining)

    if mode == "greedy" and not selected:
        optional = choose_optional_source(
            target_index=target_index,
            nodes=nodes,
            tool_catalog=tool_catalog,
            transition_index=transition_index,
            threshold=optional_threshold,
        )
        if optional is not None:
            source, probability, slot = optional
            selected.append({"source": source, "target": target_index})
            operations.append(
                {
                    "action": "add_optional_graph_edge",
                    "source": source,
                    "target": target_index,
                    "slot": slot,
                    "transition_probability": probability,
                }
            )

    return selected


def choose_best_original_source_for_slot(
    target_index: int,
    nodes: Sequence[Mapping[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]],
    transition_index: Mapping[Tuple[str, str], Any],
    original_sources: Sequence[int],
    used_sources: set[int],
    remaining: Sequence[str],
) -> Tuple[int, str, float | None] | None:
    candidates = []
    for source_index in original_sources:
        if source_index in used_sources:
            continue
        matched_slots = matching_source_slots(source_index, target_index, nodes, tool_catalog, remaining)
        if not matched_slots:
            continue
        probability = transition_probability(source_index, target_index, nodes, tool_catalog, transition_index)
        probability_for_score = probability if probability is not None else 0.0
        for slot in matched_slots:
            candidates.append(
                (
                    edge_score(
                        source_index=source_index,
                        target_index=target_index,
                        probability=probability_for_score,
                        is_original=True,
                    ),
                    -remaining.index(slot),
                    source_index,
                    slot,
                    probability,
                )
            )
    if not candidates:
        return None
    candidates.sort(reverse=True)
    _, _, source_index, slot, probability = candidates[0]
    return source_index, slot, probability


def matching_source_slots(
    source_index: int,
    target_index: int,
    nodes: Sequence[Mapping[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]],
    remaining: Sequence[str],
) -> List[str]:
    if source_index >= target_index or source_index < 0:
        return []
    source_tool = lookup_catalog_tool(tool_catalog, nodes[source_index].get("task", ""))
    source_types = bg.normalize_type_set(source_tool.get("output_types", []))
    return [slot for slot in remaining if slot in source_types or "any" in source_types or "*" in source_types]


def consume_named_slot(remaining: List[str], slot: str) -> None:
    try:
        remaining.pop(remaining.index(slot))
    except ValueError:
        return


def required_slots_for_node(
    target_index: int,
    nodes: Sequence[Mapping[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]],
) -> List[str]:
    target_tool = lookup_catalog_tool(tool_catalog, nodes[target_index].get("task", ""))
    return bg.normalize_type_list(target_tool.get("input_types", []))


def consume_source_slot(
    source_index: int,
    target_index: int,
    nodes: Sequence[Mapping[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]],
    remaining: List[str],
) -> str | None:
    if source_index >= target_index or source_index < 0:
        return None
    source_tool = lookup_catalog_tool(tool_catalog, nodes[source_index].get("task", ""))
    return bg.consume_first_matching_slot(remaining, source_tool.get("output_types", []))


def consume_literal_slots(
    target_index: int,
    nodes: Sequence[Mapping[str, Any]],
    remaining: List[str],
) -> None:
    for argument in normalize_list(nodes[target_index].get("arguments")):
        if parse_node_reference(argument) is not None:
            continue
        bg.consume_first_matching_slot(remaining, infer_literal_argument_types(argument))


def choose_best_source_for_slot(
    slot: str,
    target_index: int,
    nodes: Sequence[Mapping[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]],
    transition_index: Mapping[Tuple[str, str], Any],
    used_sources: set[int],
    original_pairs: set[Tuple[int, int]],
) -> int | None:
    candidates = []
    for source_index in range(target_index):
        if source_index in used_sources:
            continue
        source_tool = lookup_catalog_tool(tool_catalog, nodes[source_index].get("task", ""))
        source_types = bg.normalize_type_set(source_tool.get("output_types", []))
        if slot not in source_types and "any" not in source_types and "*" not in source_types:
            continue
        probability = transition_probability(source_index, target_index, nodes, tool_catalog, transition_index)
        if probability is None or probability <= 0:
            continue
        candidates.append(
            (
                edge_score(
                    source_index=source_index,
                    target_index=target_index,
                    probability=probability,
                    is_original=(source_index, target_index) in original_pairs,
                ),
                source_index,
            )
        )
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def choose_optional_source(
    target_index: int,
    nodes: Sequence[Mapping[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]],
    transition_index: Mapping[Tuple[str, str], Any],
    threshold: float,
) -> Tuple[int, float, str] | None:
    target_slots = required_slots_for_node(target_index, nodes, tool_catalog)
    if not target_slots:
        return None
    candidates = []
    for source_index in range(target_index):
        if target_index - source_index != 1:
            continue
        source_tool = lookup_catalog_tool(tool_catalog, nodes[source_index].get("task", ""))
        source_types = bg.normalize_type_set(source_tool.get("output_types", []))
        matched_slots = [slot for slot in target_slots if slot in source_types or "any" in source_types or "*" in source_types]
        matched_slots = [slot for slot in matched_slots if optional_slot_allowed(slot, target_slots)]
        if not matched_slots:
            continue
        probability = transition_probability(source_index, target_index, nodes, tool_catalog, transition_index)
        if probability is None or probability < threshold:
            continue
        candidates.append(
            (
                edge_score(source_index, target_index, probability, is_original=False),
                source_index,
                probability,
                matched_slots[0],
            )
        )
    if not candidates:
        return None
    candidates.sort(reverse=True)
    _, source, probability, slot = candidates[0]
    return source, probability, slot


def optional_slot_allowed(slot: str, target_slots: Sequence[str]) -> bool:
    unique_slots = set(target_slots)
    if len(unique_slots) <= 1:
        return True
    return slot != "text"


def edge_score(source_index: int, target_index: int, probability: float, is_original: bool) -> float:
    distance = max(1, target_index - source_index)
    score = probability * 100.0
    score += 35.0 if is_original else 0.0
    score += 8.0 if distance == 1 else 0.0
    score += 3.0 / distance
    return score


def transition_probability(
    source_index: int,
    target_index: int,
    nodes: Sequence[Mapping[str, Any]],
    tool_catalog: Mapping[str, Dict[str, Any]],
    transition_index: Mapping[Tuple[str, str], Any],
) -> float | None:
    source_tool = lookup_catalog_tool(tool_catalog, nodes[source_index].get("task", ""))
    target_tool = lookup_catalog_tool(tool_catalog, nodes[target_index].get("task", ""))
    value = bg.get_transition_probability(transition_index, source_tool.get("id"), target_tool.get("id"))
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_task_links_with_slots(
    nodes: Sequence[Mapping[str, Any]],
    edges: Iterable[Mapping[str, int]],
    tool_catalog: Mapping[str, Dict[str, Any]],
) -> List[Dict[str, str]]:
    links = []
    for edge in edges:
        source = int(edge["source"])
        target = int(edge["target"])
        if not valid_node_index(source, len(nodes)) or not valid_node_index(target, len(nodes)):
            continue
        source_tool = lookup_catalog_tool(tool_catalog, nodes[source].get("task", ""))
        target_tool = lookup_catalog_tool(tool_catalog, nodes[target].get("task", ""))
        source_types = bg.normalize_type_set(source_tool.get("output_types", []))
        target_slots = bg.normalize_type_list(target_tool.get("input_types", []))
        slot = next((item for item in target_slots if item in source_types), "")
        links.append(
            {
                "source": str(nodes[source].get("task") or ""),
                "target": str(nodes[target].get("task") or ""),
                "target_input_slot": slot,
            }
        )
    return links


def summarize_repairs(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    total = 0
    changed = 0
    operation_counts: Dict[str, int] = {}
    validation_counts: Dict[str, int] = {}
    temporal_status_counts: Dict[str, int] = {}
    temporal_changed = 0
    for row in rows:
        total += 1
        trace = row.get("graph_repair_trace") if isinstance(row, Mapping) else {}
        if not isinstance(trace, Mapping):
            trace = {}
        operations = trace.get("operations") if isinstance(trace.get("operations"), list) else []
        applied = trace.get("applied", True)
        graph_changed = bool(operations and applied)
        for operation in operations:
            if not isinstance(operation, Mapping):
                continue
            action = str(operation.get("action") or "")
            operation_counts[action] = operation_counts.get(action, 0) + 1
        status = str(trace.get("validation_status") or "")
        if applied is False:
            status = f"rejected_{status or trace.get('status') or 'unknown'}"
        validation_counts[status] = validation_counts.get(status, 0) + 1

        temporal_trace = row.get("temporal_link_repair") if isinstance(row, Mapping) else {}
        if not isinstance(temporal_trace, Mapping):
            temporal_trace = {}
        temporal_status = str(temporal_trace.get("status") or "")
        if temporal_status:
            temporal_status_counts[temporal_status] = temporal_status_counts.get(temporal_status, 0) + 1
        temporal_row_changed = temporal_link_repair_changed_result(row) if isinstance(row, Mapping) else False
        if temporal_row_changed:
            temporal_changed += 1
        if graph_changed or temporal_row_changed:
            changed += 1
    return {
        "rows": total,
        "changed_rows": changed,
        "operation_counts": operation_counts,
        "validation_counts": validation_counts,
        "temporal_changed_rows": temporal_changed,
        "temporal_status_counts": temporal_status_counts,
    }


def temporal_link_repair_changed_result(row: Mapping[str, Any]) -> bool:
    trace = row.get("temporal_link_repair")
    if not isinstance(trace, Mapping):
        return False
    status = str(trace.get("status") or "").strip()
    if status not in TEMPORAL_LINK_REPAIR_APPLIED_STATUSES:
        return False
    before = select_pre_temporal_link_repair_workflow(row)
    after_value = row.get("result")
    if not isinstance(after_value, Mapping):
        return False
    after = normalize_workflow(after_value)
    return task_links_signature(before) != task_links_signature(after)


def task_links_signature(workflow: Mapping[str, Any]) -> str:
    links = [dict(link) for link in normalize_list(workflow.get("task_links")) if isinstance(link, Mapping)]
    return json.dumps(links, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def evaluate_predictions(
    sample_file: Path,
    gold_file: Path,
    prediction_file: Path,
    tool_desc_file: Path,
) -> Dict[str, Any]:
    return evaluate_rows(
        sample_file=sample_file,
        gold_file=gold_file,
        prediction_rows=read_json_records(prediction_file),
        tool_desc_file=tool_desc_file,
    )


def evaluate_rows(
    sample_file: Path,
    gold_file: Path,
    prediction_rows: Iterable[Mapping[str, Any]],
    tool_desc_file: Path,
) -> Dict[str, Any]:
    tool_desc = json.loads(tool_desc_file.read_text(encoding="utf-8-sig"))
    tool_names = [str(tool.get("id") or "") for tool in tool_desc.get("nodes", [])]
    tool_map = {str(tool.get("id") or ""): index + 1 for index, tool in enumerate(tool_desc.get("nodes", []))}
    output_type_map = {
        normalize_task_name(tool.get("id")): bg.normalize_type_list(tool.get("output-type", []))[0]
        if bg.normalize_type_list(tool.get("output-type", []))
        else "none"
        for tool in tool_desc.get("nodes", [])
    }

    sample_ids = {str(row.get("id") or "") for row in read_json_records(sample_file)}
    labels = {}
    for row in read_json_records(gold_file):
        case_id = str(row.get("id") or "")
        if case_id in sample_ids:
            labels[case_id] = normalize_label_sample(row)
    predictions = {
        str(row.get("id") or ""): normalize_prediction_row(row)
        for row in prediction_rows
        if str(row.get("id") or "")
    }
    ids = sorted(set(labels) & set(predictions))

    label_names: List[List[str]] = []
    prediction_names: List[List[str]] = []
    label_links: List[List[Tuple[str, str]]] = []
    prediction_links: List[List[Tuple[str, str]]] = []
    skipped = 0
    for case_id in ids:
        try:
            label = labels[case_id]
            prediction = predictions[case_id]
            label_node_names, label_link_rows = materialize_resource_links(label, output_type_map)
            prediction_node_names, prediction_link_rows = materialize_resource_links(prediction["result"], output_type_map)
        except Exception:
            skipped += 1
            continue
        label_names.append(label_node_names)
        prediction_names.append(prediction_node_names)
        label_links.append([(link["source"], link["target"]) for link in label_link_rows])
        prediction_links.append([(link["source"], link["target"]) for link in prediction_link_rows])

    node_precision, node_recall, node_f1 = micro_f1(label_names, prediction_names, known_types=tool_names)
    edge_precision, edge_recall, edge_f1 = micro_f1(label_links, prediction_links)
    ratios = [
        sequence_ratio(
            [tool_map.get(name, 0) for name in prediction],
            [tool_map.get(name, 0) for name in label],
        )
        for label, prediction in zip(label_names, prediction_names)
    ]
    ned = 1.0 - (sum(ratios) / len(ratios) if ratios else 0.0)
    return {
        "sample_ids": len(sample_ids),
        "gold_ids": len(labels),
        "prediction_ids": len(predictions),
        "common_ids": len(ids),
        "support": len(label_names),
        "skipped": skipped,
        "node_micro_precision": node_precision,
        "node_micro_recall": node_recall,
        "node_micro_f1": node_f1,
        "edge_micro_precision": edge_precision,
        "edge_micro_recall": edge_recall,
        "edge_micro_f1": edge_f1,
        "ned": ned,
    }


def normalize_label_sample(row: Mapping[str, Any]) -> Dict[str, Any]:
    sample = dict(row)
    nodes = parse_jsonish(sample.get("task_nodes", sample.get("tool_nodes", [])))
    links = parse_jsonish(sample.get("task_links", sample.get("tool_links", [])))
    sample["task_nodes"] = normalize_nodes(nodes)
    sample["task_links"] = [dict(link) for link in links if isinstance(link, Mapping)] if isinstance(links, list) else []
    return sample


def normalize_prediction_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    result = row.get("result") if isinstance(row.get("result"), Mapping) else {}
    normalized = normalize_label_sample(result)
    return {"id": str(row.get("id") or ""), "result": normalized}


def normalize_nodes(raw_nodes: Any) -> List[Dict[str, Any]]:
    nodes = []
    if not isinstance(raw_nodes, list):
        return nodes
    for node in raw_nodes:
        if not isinstance(node, Mapping):
            continue
        normalized = dict(node)
        if "task" not in normalized:
            normalized["task"] = normalized.get("test") or normalized.get("function") or ""
        if "arguments" not in normalized and isinstance(normalized.get("parameters"), Mapping):
            normalized["arguments"] = list(normalized["parameters"].values())
        normalized["arguments"] = normalize_list(normalized.get("arguments"))
        nodes.append(normalized)
    return nodes


def materialize_resource_links(
    workflow: Mapping[str, Any],
    output_type_map: Mapping[str, str],
) -> Tuple[List[str], List[Dict[str, str]]]:
    nodes = normalize_nodes(workflow.get("task_nodes", []))
    node_names = [normalize_task_name(node.get("task")) for node in nodes]
    incoming_sources = build_incoming_source_map(workflow.get("task_links", []))
    links = []
    for target_index, node in enumerate(nodes):
        current_task = node_names[target_index]
        for argument in normalize_list(node.get("arguments")):
            source_index = resolve_node_reference(
                argument,
                current_index=target_index,
                node_names=node_names,
                current_task=current_task,
                incoming_sources=incoming_sources,
            )
            if source_index is None:
                continue
            source_task = node_names[source_index]
            links.append(
                {
                    "source": source_task,
                    "target": current_task,
                    "target_input_slot": output_type_map.get(source_task, "other"),
                }
            )
    return node_names, links


def build_incoming_source_map(raw_links: Any) -> Dict[str, set[str]]:
    incoming_sources: Dict[str, set[str]] = {}
    for link in normalize_list(raw_links):
        if not isinstance(link, Mapping):
            continue
        source = normalize_task_name(link.get("source", ""))
        target = normalize_task_name(link.get("target", ""))
        if source and target:
            incoming_sources.setdefault(target, set()).add(source)
    return incoming_sources


def resolve_node_reference(
    argument: Any,
    current_index: int,
    node_names: Sequence[str],
    current_task: str,
    incoming_sources: Mapping[str, set[str]],
) -> int | None:
    source = parse_node_reference(argument)
    if source is None:
        return None
    candidates = []
    for candidate in (source, source - 1):
        if 0 <= candidate < len(node_names) and candidate < current_index and candidate not in candidates:
            candidates.append(candidate)
    if not candidates:
        return None
    expected_sources = incoming_sources.get(normalize_task_name(current_task), set())
    if expected_sources:
        matched = [candidate for candidate in candidates if normalize_task_name(node_names[candidate]) in expected_sources]
        if len(matched) == 1:
            return matched[0]
        if matched:
            candidates = matched
    if source == current_index and (source - 1) in candidates:
        return source - 1
    if source in candidates:
        return source
    return max(candidates)


def micro_f1(
    gold_items: Sequence[Sequence[Any]],
    prediction_items: Sequence[Sequence[Any]],
    known_types: Sequence[Any] | None = None,
) -> Tuple[float, float, float]:
    known = set(known_types) if known_types is not None else None
    true_positive = false_positive = false_negative = 0
    for gold, prediction in zip(gold_items, prediction_items):
        gold_set = set(gold)
        prediction_set = set(prediction)
        if known is not None:
            gold_set = {item for item in gold_set if item in known}
            prediction_set = {item for item in prediction_set if item in known}
        true_positive += len(gold_set & prediction_set)
        false_positive += len(prediction_set - gold_set)
        false_negative += len(gold_set - prediction_set)
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def sequence_ratio(prediction: Sequence[Any], label: Sequence[Any]) -> float:
    denominator = len(prediction) + len(label)
    if denominator == 0:
        return 1.0
    return (denominator - indel_distance(prediction, label)) / denominator


def indel_distance(left: Sequence[Any], right: Sequence[Any]) -> int:
    previous = list(range(len(right) + 1))
    for row_index, left_item in enumerate(left, start=1):
        current = [row_index] + [0] * len(right)
        for column_index, right_item in enumerate(right, start=1):
            substitution_cost = 0 if left_item == right_item else 2
            current[column_index] = min(
                previous[column_index] + 1,
                current[column_index - 1] + 1,
                previous[column_index - 1] + substitution_cost,
            )
        previous = current
    return previous[-1]

def read_json_records(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return []
    if text[0] in "[{":
        try:
            payload = json.loads(text)
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
            if isinstance(payload, dict):
                return [payload]
        except json.JSONDecodeError:
            pass
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            rows.append(json.loads(stripped))
    return rows


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def edge_key_set(edges: Iterable[Mapping[str, int]]) -> set[Tuple[int, int]]:
    pairs = set()
    for edge in edges:
        try:
            pairs.add((int(edge["source"]), int(edge["target"])))
        except (KeyError, TypeError, ValueError):
            continue
    return pairs


def parse_jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def normalize_task_name(value: Any) -> str:
    return str(value or "").replace("_", " ")

def resolve_path(raw: Any) -> Path:
    path = Path(str(raw)).expanduser()
    return path.resolve() if path.is_absolute() else (REPO_ROOT / path).resolve()


def resolve_optional_path(raw: Any) -> Path | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return resolve_path(text)


def resolve_optional_json_output_path(raw: Any) -> Path | None:
    path = resolve_optional_path(raw)
    if path is None:
        return None
    return ensure_json_output_path(path)




if __name__ == "__main__":
    raise SystemExit(main())

