# -*- coding: utf-8 -*-
"""LLM-based intent coverage analysis for MIWP badcases.

This script is experiment-only. It does not modify the MIWP runtime,
planner, retrieval, verification, or evaluation pipeline.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from xml.etree import ElementTree as ET


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.memory_guided_workflow.llm_client import OpenAICompatibleLLMClient
from agent.memory_guided_workflow.utils import extract_json_object


DEFAULT_INPUT_XLSX = (
    "agent/memory_guided_workflow/metrics/miwp_three_tables/"
    "03_badcase_report_20260604_gptjudge_utf8_pre_request_dag_no_repair.xlsx"
)
DEFAULT_TOOL_DESC = "taskbench/data_multimedia/tool_desc.json"
DEFAULT_OUTPUT_XLSX = (
    "agent/memory_guided_workflow/metrics/miwp_three_tables/"
    "03_badcase_report_20260604_intent_coverage_llm.xlsx"
)
DEFAULT_OUTPUT_JSONL = (
    "agent/memory_guided_workflow/metrics/miwp_three_tables/"
    "03_badcase_report_20260604_intent_coverage_llm.jsonl"
)

HEADERS = [
    "ID",
    "\u662f\u5426\u7f3a\u5931intent",
    "missing_intents",
    "detected_intents",
    "covered_intents_from_task_steps",
    "reason",
    "raw_detector_output",
    "raw_coverage_output",
]

XML_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def main() -> int:
    args = parse_args()
    input_xlsx = Path(args.input_xlsx)
    output_xlsx = Path(args.output_xlsx)
    output_jsonl = Path(args.output_jsonl)

    rows = read_xlsx_records(input_xlsx)
    tools = load_tool_desc(args.tool_desc)
    valid_intents = {
        tool["intent"].lower()
        for tool in tools
        if tool.get("intent")
    }
    client = OpenAICompatibleLLMClient(
        llm_config_path=args.llm_config,
        llm_profile=args.llm_profile,
    )

    selected_rows = rows[: args.limit] if args.limit else rows
    analysis_rows = [
        row
        for row in selected_rows
        if str(row.get("Type", "")).strip().lower() != "single"
    ]
    skipped_single_count = len(selected_rows) - len(analysis_rows)
    if skipped_single_count:
        print(f"[INFO] skip {skipped_single_count} single rows")
    existing_rows = read_jsonl_records(output_jsonl) if args.resume else []
    result_by_id: Dict[str, Dict[str, Any]] = {
        str(row.get("ID", "")).strip(): row
        for row in existing_rows
        if str(row.get("ID", "")).strip()
    }

    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    if not args.resume and output_jsonl.exists():
        output_jsonl.unlink()

    for index, row in enumerate(analysis_rows, start=1):
        case_id = str(row.get("ID", "")).strip()
        if args.resume and case_id in result_by_id:
            print(f"[{index}/{len(analysis_rows)}] skip id={case_id} (resume)")
            continue

        user_request = str(row.get("pre_request", "")).strip()
        task_steps = extract_task_steps(row.get("pre_result", ""))

        print(f"[{index}/{len(analysis_rows)}] analyzing id={case_id}")
        detector_payload, raw_detector = detect_intents_with_llm(
            client=client,
            user_request=user_request,
            available_tools=tools,
        )
        detected_intents = normalize_detected_intents(
            detector_payload,
            valid_intents=valid_intents,
        )

        coverage_payload, raw_coverage = judge_coverage_with_llm(
            client=client,
            user_request=user_request,
            task_steps=task_steps,
            detected_intents=detected_intents,
        )
        normalized = normalize_coverage_payload(coverage_payload)

        result_row = {
            "ID": case_id,
            "\u662f\u5426\u7f3a\u5931intent": "\u662f" if normalized["is_missing_intent"] else "\u5426",
            "missing_intents": normalized["missing_intents"],
            "detected_intents": detected_intents,
            "covered_intents_from_task_steps": normalized["covered_intents"],
            "reason": normalized["reason"],
            "raw_detector_output": raw_detector,
            "raw_coverage_output": raw_coverage,
        }
        result_by_id[case_id] = result_row
        append_jsonl(output_jsonl, result_row)

    output_xlsx.parent.mkdir(parents=True, exist_ok=True)
    result_rows = [
        result_by_id[case_id]
        for case_id in [
            str(row.get("ID", "")).strip()
            for row in analysis_rows
        ]
        if case_id in result_by_id
    ]
    write_xlsx(output_xlsx, result_rows)
    write_jsonl(output_jsonl, result_rows)
    print(f"[DONE] xlsx={output_xlsx}")
    print(f"[DONE] jsonl={output_jsonl}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run LLM-based intent coverage analysis for MIWP badcases."
    )
    parser.add_argument("--input-xlsx", default=DEFAULT_INPUT_XLSX)
    parser.add_argument(
        "--tool-desc",
        default=DEFAULT_TOOL_DESC,
        help=(
            "tool_desc.json path. The detector reads each tool's id, desc, "
            "and intent to identify required user-request intents."
        ),
    )
    parser.add_argument("--output-xlsx", default=DEFAULT_OUTPUT_XLSX)
    parser.add_argument("--output-jsonl", default=DEFAULT_OUTPUT_JSONL)
    parser.add_argument("--llm-config", default="configs/qwen.json")
    parser.add_argument("--llm-profile", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from output-jsonl and skip case IDs that are already present.",
    )
    return parser.parse_args()


def load_tool_desc(tool_desc: str) -> List[Dict[str, str]]:
    raw_path = str(tool_desc or "").strip()
    return load_tools_from_tool_desc(Path(raw_path or DEFAULT_TOOL_DESC))


def load_tools_from_tool_desc(path: Path) -> List[Dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    nodes = payload.get("nodes", payload) if isinstance(payload, dict) else payload
    tools: List[Dict[str, str]] = []
    seen = set()
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        tool_id = str(node.get("id", "")).strip()
        if not tool_id:
            continue
        key = tool_id.lower()
        if key in seen:
            continue
        seen.add(key)
        intent = str(node.get("intent", "") or "unknown").strip()
        tools.append(
            {
                "id": tool_id,
                "desc": str(node.get("desc", "")).strip(),
                "intent": "" if intent.lower() == "unknown" else intent,
            }
        )
    return tools


def detect_intents_with_llm(
    client: OpenAICompatibleLLMClient,
    user_request: str,
    available_tools: List[Dict[str, str]],
) -> tuple[Dict[str, Any], str]:
    prompt = build_intent_detector_prompt(
        user_request=user_request,
        available_tools=available_tools,
    )
    raw_text = client.chat(
        messages=[
            {"role": "system", "content": "Return JSON only."},
            {"role": "user", "content": prompt},
        ]
    )
    return extract_json_object(raw_text), raw_text


def judge_coverage_with_llm(
    client: OpenAICompatibleLLMClient,
    user_request: str,
    task_steps: List[str],
    detected_intents: List[Dict[str, Any]],
) -> tuple[Dict[str, Any], str]:
    prompt = build_intent_coverage_prompt(
        user_request=user_request,
        task_steps=task_steps,
        detected_intents=detected_intents,
    )
    raw_text = client.chat(
        messages=[
            {"role": "system", "content": "Return JSON only."},
            {"role": "user", "content": prompt},
        ]
    )
    return extract_json_object(raw_text), raw_text


def build_intent_detector_prompt(
    user_request: str,
    available_tools: List[Dict[str, str]],
) -> str:
    tools_json = json.dumps(available_tools, ensure_ascii=False, indent=2)
    return f"""
You are an Intent Detector for task-step omission review.

Goal:
Given:

a user request
a list of tools from tool_desc.json

determine which user-request intents are explicitly or implicitly required.

This is an analysis-only task used to check whether an existing task decomposition missed required intents.
Do NOT generate tasks.
Do NOT generate workflows.
Do NOT generate tool sequences.

Only identify which intents are likely required by the user request.

user_request

tool_desc.json

Each tool contains:

id
desc
intent

For each tool:

Read the tool description.
Infer the core capability provided by the tool.
Use the tool's intent as the candidate intent label.
Determine whether the user request explicitly or implicitly requires that intent.
Consider both direct wording and semantic paraphrases.
Be conservative.

Only select an intent when there is strong evidence that the user request requires it.
Deduplicate by intent. Multiple tools may support the same intent; output that intent once.

An intent may be required even if the user does not use the exact tool wording.

For example:

A request for "a shorter version" may require the "summarize" intent if a tool description says it summarizes or shortens text.
A request to "get information from inside a file" may require the "extract" intent if a tool description says it extracts content from that file type.

These examples illustrate semantic matching only. Do not treat them as fixed rules.

{{
  "detected_intents": [
    {{
      "intent": "...",
      "supporting_tool_ids": ["..."],
      "evidence": "Why this intent is required by the user request"
    }}
  ]
}}

Only output intents present in tool_desc.json.
Do not output intents that are merely possible.
Only output intents strongly supported by the request.
Do not infer intents from weak keyword overlap.
Do not generate workflows.
Do not generate tasks.
Do not generate dependencies.
Return JSON only.

user_request:
{user_request}

tool_desc.json:
{tools_json}
""".strip()


def build_intent_coverage_prompt(
    user_request: str,
    task_steps: List[str],
    detected_intents: List[Dict[str, Any]],
) -> str:
    task_steps_json = json.dumps(task_steps, ensure_ascii=False, indent=2)
    detected_json = json.dumps(detected_intents, ensure_ascii=False, indent=2)
    return f"""
You are an Intent Coverage Reviewer for workflow planning experiments.

You are NOT generating a workflow.
You are NOT generating tasks.
You are reviewing an existing task decomposition produced by another model.

Input:
1. user_request
2. existing task_steps
3. detected_intents from an intent detector

Goal:
Determine whether each detected intent is already covered by the existing task_steps.

Rules:
- Be conservative.
- Consider semantic paraphrases in task_steps.
- Do NOT require exact keyword matches.
- For example, a task step saying "find a summary" may cover search but may not cover summarize if it does not actually create a summary from retrieved text.
- A task step saying "find images related to sentiment" does not cover sentiment analysis unless it produces sentiment information.
- Do NOT report presentation actions as missing intents.
- Do NOT propose tasks, tools, or workflows.
- Output JSON only.

Output JSON schema:
{{
  "is_missing_intent": true,
  "covered_intents": [
    {{
      "intent": "...",
      "evidence": "Why existing task_steps cover it"
    }}
  ],
  "missing_intents": [
    {{
      "intent": "...",
      "evidence": "Why user_request needs it",
      "why_missing": "Why existing task_steps do not cover it"
    }}
  ],
  "reason": "Short overall explanation"
}}

user_request:
{user_request}

existing task_steps:
{task_steps_json}

detected_intents:
{detected_json}
""".strip()


def normalize_detected_intents(
    payload: Dict[str, Any],
    valid_intents: set[str],
) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    seen = set()
    for item in payload.get("detected_intents", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        intent = str(item.get("intent", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        key = intent.lower()
        if key not in valid_intents or key in seen:
            continue
        seen.add(key)
        supporting_tool_ids = item.get("supporting_tool_ids", [])
        if not isinstance(supporting_tool_ids, list):
            supporting_tool_ids = []
        result.append(
            {
                "intent": intent,
                "supporting_tool_ids": [
                    str(tool_id).strip()
                    for tool_id in supporting_tool_ids
                    if str(tool_id).strip()
                ],
                "evidence": evidence,
            }
        )
    return result


def normalize_coverage_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    missing = normalize_intent_items(payload.get("missing_intents", []), include_why_missing=True)
    covered = normalize_intent_items(payload.get("covered_intents", []), include_why_missing=False)
    is_missing = bool(payload.get("is_missing_intent", bool(missing)))
    return {
        "is_missing_intent": is_missing,
        "missing_intents": missing,
        "covered_intents": covered,
        "reason": str(payload.get("reason", "")).strip(),
    }


def normalize_intent_items(payload: Any, include_why_missing: bool) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    seen = set()
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        intent = str(item.get("intent", "")).strip()
        key = intent.lower()
        if not intent or key in seen:
            continue
        normalized = {
            "intent": intent,
            "evidence": str(item.get("evidence", "")).strip(),
        }
        if include_why_missing:
            normalized["why_missing"] = str(item.get("why_missing", "")).strip()
        seen.add(key)
        items.append(normalized)
    return items


def extract_task_steps(pre_result: Any) -> List[str]:
    if isinstance(pre_result, str):
        try:
            payload = json.loads(pre_result)
        except json.JSONDecodeError:
            return []
    elif isinstance(pre_result, dict):
        payload = pre_result
    else:
        return []
    return [
        str(item).strip()
        for item in payload.get("task_steps", [])
        if str(item).strip()
    ]


def read_xlsx_records(path: Path) -> List[Dict[str, str]]:
    rows = read_xlsx_rows(path)
    if not rows:
        return []
    header = rows[0]
    records: List[Dict[str, str]] = []
    for row in rows[1:]:
        record = {header[index]: row[index] if index < len(row) else "" for index in range(len(header))}
        if str(record.get("ID", "")).strip():
            records.append(record)
    return records


def read_xlsx_rows(path: Path) -> List[List[str]]:
    with zipfile.ZipFile(path) as archive:
        shared_strings = read_shared_strings(archive)
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))

    rows: List[List[str]] = []
    for row in sheet.findall(".//a:sheetData/a:row", XML_NS):
        values: List[str] = []
        for cell in row.findall("a:c", XML_NS):
            index = excel_column_index(cell.attrib.get("r", "A1"))
            while len(values) <= index:
                values.append("")
            values[index] = read_cell_text(cell, shared_strings)
        rows.append(values)
    return rows


def read_shared_strings(archive: zipfile.ZipFile) -> List[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    shared: List[str] = []
    for item in root.findall("a:si", XML_NS):
        shared.append("".join(text.text or "" for text in item.findall(".//a:t", XML_NS)))
    return shared


def read_cell_text(cell: ET.Element, shared_strings: List[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "s":
        value = cell.find("a:v", XML_NS)
        if value is None or value.text is None:
            return ""
        return shared_strings[int(value.text)]
    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.findall(".//a:t", XML_NS))
    value = cell.find("a:v", XML_NS)
    return value.text if value is not None and value.text is not None else ""


def excel_column_index(cell_ref: str) -> int:
    letters = "".join(char for char in str(cell_ref) if char.isalpha())
    result = 0
    for char in letters:
        result = result * 26 + ord(char.upper()) - 64
    return max(result - 1, 0)


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl_records(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"[WARN] skip invalid JSONL line {line_number}: {exc}")
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_xlsx(path: Path, rows: List[Dict[str, Any]]) -> None:
    table_rows = [
        [
            row.get("ID", ""),
            row.get("\u662f\u5426\u7f3a\u5931intent", ""),
            json.dumps(
                row.get("missing_intents", []),
                ensure_ascii=False,
                separators=(",", ": "),
            ),
            json.dumps(
                row.get("detected_intents", []),
                ensure_ascii=False,
                separators=(",", ": "),
            ),
            json.dumps(
                row.get("covered_intents_from_task_steps", []),
                ensure_ascii=False,
                separators=(",", ": "),
            ),
            row.get("reason", ""),
            row.get("raw_detector_output", ""),
            row.get("raw_coverage_output", ""),
        ]
        for row in rows
    ]
    xlsx_rows = [HEADERS] + table_rows
    write_xlsx_rows(path, xlsx_rows)


def write_xlsx_rows(path: Path, rows: List[List[Any]]) -> None:
    dim = f"A1:{column_name(len(HEADERS) - 1)}{len(rows)}"
    widths = [14, 16, 64, 96, 76, 92, 80, 80]
    cols = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, 1)
    )
    row_xml = []
    for row_index, row in enumerate(rows, 1):
        style = 1 if row_index == 1 else 2
        height = 24 if row_index == 1 else 92
        cells = "".join(
            cell_xml(value, row_index, col_index, style)
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
    write_minimal_xlsx_package(path, sheet_xml)


def cell_xml(value: Any, row_index: int, col_index: int, style: int) -> str:
    ref = f"{column_name(col_index)}{row_index}"
    text = "" if value is None else str(value)
    text = "".join(char for char in text if char in "\t\n\r" or ord(char) >= 32)
    escaped = html.escape(text, quote=False)
    return (
        f'<c r="{ref}" t="inlineStr" s="{style}">'
        f'<is><t xml:space="preserve">{escaped}</t></is></c>'
    )


def column_name(index: int) -> str:
    result = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def write_minimal_xlsx_package(path: Path, sheet_xml: str) -> None:
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
  <sheets><sheet name="intent_coverage_llm" sheetId="1" r:id="rId1"/></sheets>
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


if __name__ == "__main__":
    raise SystemExit(main())
