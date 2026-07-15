from __future__ import annotations

import argparse
from collections.abc import Iterable as IterableABC
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

try:
    from agent.memory_guided_workflow.llm_client import OpenAICompatibleLLMClient
    from agent.memory_guided_workflow.utils import (
        extract_json_object,
        get_coverage_prompt_rules,
        get_coverage_prompt_variables,
        get_tool_desc_intent_path,
        load_dataset_runtime_config,
    )
except ImportError:
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent.memory_guided_workflow.llm_client import OpenAICompatibleLLMClient
    from agent.memory_guided_workflow.utils import (
        extract_json_object,
        get_coverage_prompt_rules,
        get_coverage_prompt_variables,
        get_tool_desc_intent_path,
        load_dataset_runtime_config,
    )


DEFAULT_TOOL_DESC_PATH = (
    Path(__file__).resolve().parents[3] / "taskbench" / "data_multimedia" / "tool_desc_intent.json"
)
DEFAULT_INPUT_FILE = (
    Path(__file__).resolve().parents[1] / "samples" / "miwp_sample_10_each.jsonl"
)
DEFAULT_QWEN_RESULT_FILE = (
    Path(__file__).resolve().parents[3]
    / "taskbench"
    / "data_multimedia"
    / "predictions_use_demos_2_reformat_by_self"
    / "qwen3-14b_20260527.json"
)
DEFAULT_GOLD_RESULT_FILE = (
    Path(__file__).resolve().parents[3] / "taskbench" / "data_multimedia" / "data.json"
)
DEFAULT_BATCH_OUTPUT_PATH = (
    Path(__file__).resolve().parents[1] / "outputs" / "intent_llm_coverage_table.csv"
)


def resolve_tool_desc_intent_path_arg(
    raw_tool_desc: Any,
    dataset_config: Mapping[str, Any] | None,
) -> str:
    explicit = str(raw_tool_desc or "").strip()
    if explicit:
        return explicit
    return get_tool_desc_intent_path(dataset_config, default=str(DEFAULT_TOOL_DESC_PATH))

COVERAGE_TABLE_COLUMNS = [
    "id",
    "user_request",
    "intent",
    "intent tool",
    "model tool",
    "coverage_warnings",
]
BATCH_COLUMNS = COVERAGE_TABLE_COLUMNS
COMPARISON_TABLE_COLUMNS = COVERAGE_TABLE_COLUMNS

URL_DOWNLOAD_TERMS = ("download", "fetch", "retrieve", "obtain")
RETRYABLE_WARNING_MARKERS = (
    "LLM_COVERAGE_PARSE_FAILED",
    "LLM_COVERAGE_CALL_FAILED",
    "BATCH_CASE_FAILED",
    "COMPARISON_ROW_FAILED",
)


@dataclass
class IntentCoverageRow:
    intent: str
    tool_ids: List[str]
    coverage_type: str
    confidence: float
    matched_request_phrase: str
    matched_intent_term: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "tool_ids": list(self.tool_ids),
            "coverage_type": self.coverage_type,
            "confidence": self.confidence,
            "matched_request_phrase": self.matched_request_phrase,
            "matched_intent_term": self.matched_intent_term,
            "reason": self.reason,
        }


@dataclass
class IntentCoverageResult:
    covered_intents: List[IntentCoverageRow] = field(default_factory=list)
    raw_llm_output: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "covered_intents": [row.to_dict() for row in self.covered_intents],
            "warnings": list(self.warnings),
            "raw_llm_output": self.raw_llm_output,
        }


class IntentLLMCoverageExperiment:
    """Ask an LLM which tool_desc intents are covered by a user request."""

    def __init__(
        self,
        tool_desc_path: str | Path = DEFAULT_TOOL_DESC_PATH,
        intents: Iterable[str] | None = None,
        intent_tool_ids: Mapping[str, Iterable[str]] | None = None,
        intent_tool_descs: Mapping[str, Iterable[Mapping[str, Any]]] | None = None,
        llm_client: OpenAICompatibleLLMClient | None = None,
        llm_config_path: Any = None,
        llm_config: Dict[str, Any] | None = None,
        llm_profile: str | None = None,
        coverage_prompt_rules: Iterable[str] | None = None,
        coverage_prompt_variables: Mapping[str, Any] | None = None,
    ) -> None:
        if intents is None:
            self.intent_tools = load_intent_tools(tool_desc_path)
            self.intent_tool_ids = {
                intent: [tool["tool_id"] for tool in tools if tool.get("tool_id")]
                for intent, tools in self.intent_tools.items()
            }
            self.intents = list(self.intent_tool_ids)
        else:
            self.intents = list(intents)
            self.intent_tool_ids = {
                str(intent): [str(tool_id) for tool_id in _coerce_list(tool_ids)]
                for intent, tool_ids in dict(intent_tool_ids or {}).items()
            }
            self.intent_tools = _normalize_intent_tools(
                self.intents,
                self.intent_tool_ids,
                intent_tool_descs,
            )
            for intent, tools in self.intent_tools.items():
                tool_ids = self.intent_tool_ids.setdefault(intent, [])
                for tool in tools:
                    tool_id = tool.get("tool_id")
                    if tool_id and tool_id not in tool_ids:
                        tool_ids.append(tool_id)
        self.intent_lookup = {intent.lower(): intent for intent in self.intents}
        self.coverage_prompt_rules = [
            str(rule).strip() for rule in (coverage_prompt_rules or []) if str(rule).strip()
        ]
        self.coverage_prompt_variables = dict(coverage_prompt_variables or {})
        self.llm_client = llm_client or OpenAICompatibleLLMClient(
            llm_config_path=llm_config_path,
            llm_config=llm_config,
            llm_profile=llm_profile,
        )

    def run(self, user_request: str) -> IntentCoverageResult:
        raw_text = ""
        try:
            raw_text = self.llm_client.chat(
                messages=[
                    {"role": "system", "content": "Return JSON only."},
                    {"role": "user", "content": self._build_prompt(user_request)},
                ]
            )
        except Exception as exc:
            if _is_fatal_llm_setup_error(exc):
                raise
            return IntentCoverageResult(
                raw_llm_output={"raw_text": str(raw_text or "")},
                warnings=[f"LLM_COVERAGE_CALL_FAILED: {_format_exception(exc)}"],
            )

        try:
            raw_payload = extract_json_object(raw_text)
        except Exception as exc:
            return IntentCoverageResult(
                raw_llm_output={"raw_text": str(raw_text or "")},
                warnings=[f"LLM_COVERAGE_PARSE_FAILED: {_format_exception(exc)}"],
            )
        rows, warnings = self._normalize_rows(raw_payload)
        return IntentCoverageResult(
            covered_intents=rows,
            raw_llm_output=raw_payload,
            warnings=warnings,
        )

    def _build_prompt(self, user_request: str) -> str:
        intent_payload = [
            {
                "intent": intent,
                "expanded_terms": _split_identifier(intent),
                "candidate_tools": self.intent_tools.get(intent, []),
            }
            for intent in self.intents
        ]
        url_download_candidates = _url_download_candidates(self.intent_tools)
        prompt_variables = dict(self.coverage_prompt_variables)
        if url_download_candidates:
            prompt_variables.setdefault("url_download_candidates", url_download_candidates)
        dataset_rule_section = ""
        if self.coverage_prompt_rules:
            dataset_rule_section = (
                "\nDataset-specific coverage rules from --dataset-config:\n"
                + "\n".join(
                    f"{index}. {rule}" for index, rule in enumerate(self.coverage_prompt_rules, start=1)
                )
                + "\n"
            )
        dataset_variable_section = ""
        if prompt_variables:
            dataset_variable_section = (
                "\nDataset-specific prompt variables:\n"
                f"{json.dumps(prompt_variables, ensure_ascii=False, indent=2)}\n"
            )
        return f"""
You are an intent coverage judge.

Input:
1. A user request.
2. A list of allowed intents.

Your task:
Select every intent from the allowed intent list that is covered by the user
request. Use candidate tool descriptions only as evidence to disambiguate
similar allowed intents. Do not infer execution order.

Coverage rules:
1. Return JSON only.
2. Only output intents that appear in the allowed intent list. The JSON value
   at covered_intents[*].intent must be copied verbatim from
   allowed_intents[*].intent. Do not invent, paraphrase, shorten, translate, or
   normalize intent names. If no allowed intent fits, omit it. Before finalizing
   the JSON, validate every covered_intents row and delete any row whose intent
   is not identical to one of the allowed_intents[*].intent values.
3. Cover an intent if the request uses the same word, a morphological variant,
   or a clear synonym of the intent terms.
4. Decompose the request into separate action phrases first. Coordinated
   actions joined by commas, "and", "then", "also", "before", "after", or
   similar sequencing language can cover multiple intents.
5. Evaluate every allowed intent independently against every action phrase.
   Do not collapse two matched intents into one merely because they share a
   broad verb such as rewrite, convert, generate, search, download, or analyze.
6. If separate request phrases match separate allowed intents, return all of
   those intents even when they operate on the same input or intermediate text.
7. When multiple allowed intents are similar, compare the request phrase against
   the expanded intent terms and candidate tool descriptions. Prefer the intent
   whose object type and modifiers are more specific matches. If both have
   independent evidence, return both.
8. Do not select intents that are merely related to the content topic.
9. Be detailed in the reason. Explain the request phrase and the intent term or
   synonym that caused coverage.
10. Do not output tool ids, tool descriptions, or execution order.
11. confidence must be a number from 0 to 1.
{dataset_rule_section}{dataset_variable_section}

Required JSON schema:
{{
  "covered_intents": [
    {{
      "intent": "copy one exact allowed_intents[*].intent value",
      "coverage_type": "direct|synonym|morphological|strongly_implied",
      "confidence": 0.0,
      "matched_request_phrase": "exact phrase or short span from the request",
      "matched_intent_term": "intent word/term/synonym that matched",
      "reason": "detailed reason"
    }}
  ]
}}

allowed_intents:
{json.dumps(intent_payload, ensure_ascii=False, indent=2)}

user_request:
{user_request}

Return JSON only.
""".strip()

    def _normalize_rows(self, payload: Mapping[str, Any]) -> tuple[List[IntentCoverageRow], List[str]]:
        raw_rows = (
            payload.get("covered_intents")
            or payload.get("intents")
            or payload.get("rows")
            or payload.get("covered")
        )
        rows: List[IntentCoverageRow] = []
        warnings: List[str] = []
        seen = set()

        for index, raw_row in enumerate(_coerce_list(raw_rows), start=1):
            if isinstance(raw_row, str):
                raw_row = {"intent": raw_row}
            if not isinstance(raw_row, Mapping):
                warnings.append(f"Skipped non-object row at position {index}.")
                continue

            raw_intent = _read_text(raw_row, "intent", "name")
            intent = self.intent_lookup.get(str(raw_intent or "").strip().lower())
            if not intent:
                warnings.append(f"Skipped row {index}: unknown intent '{raw_intent}'.")
                continue
            if intent.lower() in seen:
                continue
            seen.add(intent.lower())

            rows.append(
                IntentCoverageRow(
                    intent=intent,
                    tool_ids=list(self.intent_tool_ids.get(intent, [])),
                    coverage_type=_read_text(raw_row, "coverage_type", "type") or "direct",
                    confidence=_read_confidence(raw_row.get("confidence")),
                    matched_request_phrase=_read_text(raw_row, "matched_request_phrase", "request_evidence", "evidence"),
                    matched_intent_term=_read_text(raw_row, "matched_intent_term", "matched_term", "intent_term"),
                    reason=_read_text(raw_row, "reason", "coverage_reason", "说明"),
                )
            )

        return rows, warnings

def load_intents(tool_desc_path: str | Path) -> List[str]:
    return list(load_intent_tool_ids(tool_desc_path))


def load_intent_tool_ids(tool_desc_path: str | Path) -> Dict[str, List[str]]:
    return {
        intent: [tool["tool_id"] for tool in tools if tool.get("tool_id")]
        for intent, tools in load_intent_tools(tool_desc_path).items()
    }


def load_intent_tools(tool_desc_path: str | Path) -> Dict[str, List[Dict[str, Any]]]:
    payload = json.loads(Path(tool_desc_path).read_text(encoding="utf-8-sig"))
    raw_nodes = payload.get("nodes", []) if isinstance(payload, Mapping) else []
    intent_tools: Dict[str, List[Dict[str, Any]]] = {}
    for raw_node in raw_nodes:
        if not isinstance(raw_node, Mapping):
            continue
        intent = str(raw_node.get("intent") or "").strip()
        if not intent:
            continue
        tool_id = str(raw_node.get("id") or "").strip()
        desc = str(raw_node.get("desc") or "").strip()
        tools = intent_tools.setdefault(intent, [])
        if tool_id and not any(tool.get("tool_id") == tool_id for tool in tools):
            tools.append(
                {
                    "tool_id": tool_id,
                    "desc": desc,
                    "input_types": _read_string_list(raw_node, "input-type", "input_type", "input_types"),
                    "output_types": _read_string_list(raw_node, "output-type", "output_type", "output_types"),
                }
            )
    return intent_tools


def render_markdown_table(result: IntentCoverageResult | Iterable[IntentCoverageRow]) -> str:
    rows = result.covered_intents if isinstance(result, IntentCoverageResult) else list(result)
    lines = [
        "| intent | tools | 覆盖类型 | 置信度 |",
        "|---|---|---|---:|",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_markdown_cell(row.intent),
                    _escape_markdown_cell(", ".join(row.tool_ids)),
                    _escape_markdown_cell(row.coverage_type),
                    f"{row.confidence:.2f}",
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def run_batch(
    experiment: IntentLLMCoverageExperiment,
    input_file: str | Path,
    gold_result_file: str | Path | None = None,
    qwen_result_file: str | Path | None = None,
    user_request_by_id: Mapping[str, str] | None = None,
    max_cases: int = 0,
    workers: int = 1,
    checkpoint_file: str | Path | None = None,
    resume: bool = False,
) -> List[Dict[str, Any]]:
    cases = load_json_records(input_file)
    if max_cases > 0:
        cases = cases[:max_cases]
    gold_results = load_gold_results(gold_result_file) if gold_result_file else {}
    qwen_results = load_qwen_results(qwen_result_file) if qwen_result_file else {}
    worker_count = max(1, int(workers or 1))
    checkpoint_path = Path(checkpoint_file) if checkpoint_file else None
    if checkpoint_path and not resume:
        _reset_checkpoint_file(checkpoint_path)
    checkpoint_by_key = _load_checkpoint_by_key(checkpoint_path, mode="batch") if resume else {}
    ordered_rows: List[List[Dict[str, Any]] | None] = [None] * len(cases)
    pending_cases: List[tuple[int, int, Mapping[str, Any]]] = []
    skipped_count = 0
    for position, case in enumerate(cases):
        case_id = str(case.get("id") or "").strip()
        key = _case_checkpoint_key(case_id, position)
        checkpoint_record = checkpoint_by_key.get(key)
        if checkpoint_record and checkpoint_record.get("status") == "ok":
            rows = checkpoint_record.get("rows")
            if isinstance(rows, list):
                user_request = _resolve_user_request(case, user_request_by_id or {})
                normalized_rows = [
                    normalize_coverage_table_row(
                        row,
                        case_id=case_id,
                        user_request=user_request,
                        gold_result=gold_results.get(case_id),
                        qwen_result=qwen_results.get(case_id),
                    )
                    for row in rows
                    if isinstance(row, Mapping)
                ]
                if normalized_rows and not _batch_rows_need_coverage_rerun(normalized_rows):
                    ordered_rows[position] = normalized_rows
                    skipped_count += 1
                    continue
        pending_cases.append((position + 1, position, case))

    if checkpoint_path and resume:
        _print_resume_summary("batch", skipped_count, len(pending_cases), checkpoint_path)

    if worker_count == 1:
        for index, position, case in pending_cases:
            rows = _run_batch_case(
                experiment=experiment,
                index=index,
                total=len(cases),
                case=case,
                gold_results=gold_results,
                qwen_results=qwen_results,
                user_request_by_id=user_request_by_id or {},
            )
            ordered_rows[position] = rows
            _append_batch_checkpoint(checkpoint_path, position, case, rows)
        return _flatten_ordered_batch_rows(ordered_rows)

    print(
        f"Running batch with {worker_count} workers for {len(pending_cases)} pending cases...",
        file=sys.stderr,
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_position = {
            executor.submit(
                _run_batch_case,
                experiment=experiment,
                index=index,
                total=len(cases),
                case=case,
                gold_results=gold_results,
                qwen_results=qwen_results,
                user_request_by_id=user_request_by_id or {},
            ): index - 1
            for index, position, case in pending_cases
        }
        for completed_count, future in enumerate(as_completed(future_to_position), start=1):
            position = future_to_position[future]
            case = cases[position]
            try:
                ordered_rows[position] = future.result()
            except Exception as exc:
                if _is_fatal_llm_setup_error(exc):
                    raise
                case_id = str(case.get("id") or "").strip()
                user_request = str(case.get("user_request") or case.get("instruction") or "").strip()
                ordered_rows[position] = [
                    _empty_batch_row(
                        case_id=case_id,
                        user_request=user_request,
                        gold_result=gold_results.get(case_id),
                        qwen_result=qwen_results.get(case_id),
                        reason=f"BATCH_CASE_FAILED: {_format_exception(exc)}",
                    )
                ]
            _append_batch_checkpoint(checkpoint_path, position, case, ordered_rows[position] or [])
            case_id = str(cases[position].get("id") or "").strip()
            print(
                f"[{completed_count}/{len(pending_cases)}] Finished case id={case_id}",
                file=sys.stderr,
                flush=True,
            )

    return _flatten_ordered_batch_rows(ordered_rows)


def _run_batch_case(
    experiment: IntentLLMCoverageExperiment,
    index: int,
    total: int,
    case: Mapping[str, Any],
    gold_results: Mapping[str, Any],
    qwen_results: Mapping[str, Any],
    user_request_by_id: Mapping[str, str],
) -> List[Dict[str, Any]]:
    case_id = str(case.get("id") or "").strip()
    user_request = _resolve_user_request(case, user_request_by_id)
    if not user_request:
        return [
            _empty_batch_row(
                case_id=case_id,
                user_request=user_request,
                gold_result=gold_results.get(case_id),
                qwen_result=qwen_results.get(case_id),
                reason="missing user_request",
            )
        ]

    print(
        f"[{index}/{total}] Calling LLM for case id={case_id}",
        file=sys.stderr,
        flush=True,
    )
    try:
        result = experiment.run(user_request)
    except Exception as exc:
        if _is_fatal_llm_setup_error(exc):
            raise
        return [
            _empty_batch_row(
                case_id=case_id,
                user_request=user_request,
                gold_result=gold_results.get(case_id),
                qwen_result=qwen_results.get(case_id),
                reason=f"BATCH_CASE_FAILED: {_format_exception(exc)}",
            )
        ]
    return coverage_result_to_batch_rows(
        case_id=case_id,
        user_request=user_request,
        gold_result=gold_results.get(case_id),
        qwen_result=qwen_results.get(case_id),
        result=result,
    )


def coverage_result_to_batch_rows(
    case_id: str,
    user_request: str,
    gold_result: Any,
    qwen_result: Any,
    result: IntentCoverageResult,
) -> List[Dict[str, Any]]:
    return [
        normalize_coverage_table_row(
            {
                "id": case_id,
                "user_request": user_request,
                "intent": "; ".join(row.intent for row in result.covered_intents if row.intent),
                "intent tool": "; ".join(
                    ", ".join(row.tool_ids)
                    for row in result.covered_intents
                    if row.tool_ids
                ),
                "model tool": " -> ".join(_extract_workflow_tool_names(qwen_result)),
                "coverage_warnings": "; ".join(result.warnings),
            }
        )
    ]


def write_batch_table(rows: List[Dict[str, Any]], output_path: str | Path, output_format: str) -> None:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "jsonl":
        with output.open("w", encoding="utf-8") as file:
            for row in rows:
                file.write(json.dumps(row, ensure_ascii=False) + "\n")
        return
    if output_format == "xlsx":
        _write_xlsx_table(rows, BATCH_COLUMNS, output, "intent_coverage")
        return

    with output.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=BATCH_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run_comparison_table_batch(
    experiment: IntentLLMCoverageExperiment,
    input_file: str | Path,
    gold_result_file: str | Path | None = None,
    qwen_result_file: str | Path | None = None,
    user_request_by_id: Mapping[str, str] | None = None,
    existing_coverage_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    max_cases: int = 0,
    workers: int = 1,
    checkpoint_file: str | Path | None = None,
    resume: bool = False,
) -> List[Dict[str, str]]:
    cases = load_json_records(input_file)
    if max_cases > 0:
        cases = cases[:max_cases]
    gold_results = load_gold_results(gold_result_file) if gold_result_file else {}
    qwen_results = load_qwen_results(qwen_result_file) if qwen_result_file else {}
    worker_count = max(1, int(workers or 1))
    checkpoint_path = Path(checkpoint_file) if checkpoint_file else None
    if checkpoint_path and not resume:
        _reset_checkpoint_file(checkpoint_path)
    checkpoint_by_key = _load_checkpoint_by_key(checkpoint_path, mode="comparison") if resume else {}
    ordered_rows: List[Dict[str, str] | None] = [None] * len(cases)
    pending_cases: List[tuple[int, int, Mapping[str, Any]]] = []
    skipped_count = 0
    user_request_by_id = user_request_by_id or {}
    existing_coverage_by_id = existing_coverage_by_id or {}
    for position, case in enumerate(cases):
        case_id = str(case.get("id") or "").strip()
        user_request = _resolve_user_request(case, user_request_by_id)
        gold_result = _resolve_case_gold_result(case, gold_results.get(case_id))
        qwen_result = _resolve_case_qwen_result(case, qwen_results.get(case_id))
        existing_row = existing_coverage_by_id.get(case_id)
        if existing_row is not None:
            normalized = normalize_coverage_table_row(
                existing_row,
                case_id=case_id,
                user_request=user_request,
                gold_result=gold_result,
                qwen_result=qwen_result,
            )
            if _row_has_usable_coverage(normalized):
                ordered_rows[position] = normalized
                skipped_count += 1
                continue
        key = _case_checkpoint_key(case_id, position)
        checkpoint_record = checkpoint_by_key.get(key)
        if checkpoint_record and checkpoint_record.get("status") == "ok":
            row = checkpoint_record.get("row")
            if isinstance(row, Mapping):
                normalized = normalize_coverage_table_row(
                    row,
                    case_id=case_id,
                    user_request=user_request,
                    gold_result=gold_result,
                    qwen_result=qwen_result,
                )
                if _row_has_usable_coverage(normalized):
                    ordered_rows[position] = normalized
                    skipped_count += 1
                    continue
        pending_cases.append((position + 1, position, case))

    if checkpoint_path and resume:
        _print_resume_summary("comparison table", skipped_count, len(pending_cases), checkpoint_path)

    if worker_count == 1:
        for index, position, case in pending_cases:
            row = _run_comparison_table_case(
                experiment=experiment,
                index=index,
                total=len(cases),
                case=case,
                gold_results=gold_results,
                qwen_results=qwen_results,
                user_request_by_id=user_request_by_id,
            )
            ordered_rows[position] = row
            _append_comparison_checkpoint(checkpoint_path, position, case, row)
        return [row for row in ordered_rows if row is not None]

    print(
        f"Running comparison table batch with {worker_count} workers for {len(pending_cases)} pending cases...",
        file=sys.stderr,
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_position = {
            executor.submit(
                _run_comparison_table_case,
                experiment=experiment,
                index=index,
                total=len(cases),
                case=case,
                gold_results=gold_results,
                qwen_results=qwen_results,
                user_request_by_id=user_request_by_id,
            ): index - 1
            for index, position, case in pending_cases
        }
        for completed_count, future in enumerate(as_completed(future_to_position), start=1):
            position = future_to_position[future]
            case = cases[position]
            try:
                ordered_rows[position] = future.result()
            except Exception as exc:
                if _is_fatal_llm_setup_error(exc):
                    raise
                case_id = str(case.get("id") or "").strip()
                user_request = _resolve_user_request(case, user_request_by_id)
                ordered_rows[position] = _comparison_error_row(
                    case=case,
                    case_id=case_id,
                    user_request=user_request,
                    gold_result=_resolve_case_gold_result(case, gold_results.get(case_id)),
                    qwen_result=_resolve_case_qwen_result(case, qwen_results.get(case_id)),
                    reason=f"COMPARISON_ROW_FAILED: {_format_exception(exc)}",
                )
            _append_comparison_checkpoint(checkpoint_path, position, case, ordered_rows[position] or {})
            case_id = str(cases[position].get("id") or "").strip()
            print(
                f"[{completed_count}/{len(pending_cases)}] Finished comparison row id={case_id}",
                file=sys.stderr,
                flush=True,
            )

    return [row for row in ordered_rows if row is not None]


def _run_comparison_table_case(
    experiment: IntentLLMCoverageExperiment,
    index: int,
    total: int,
    case: Mapping[str, Any],
    gold_results: Mapping[str, Any],
    qwen_results: Mapping[str, Any],
    user_request_by_id: Mapping[str, str],
) -> Dict[str, str]:
    case_id = str(case.get("id") or "").strip()
    user_request = _resolve_user_request(case, user_request_by_id)
    gold_result = _resolve_case_gold_result(case, gold_results.get(case_id))
    qwen_result = _resolve_case_qwen_result(case, qwen_results.get(case_id))
    if not user_request:
        result = IntentCoverageResult()
    else:
        print(
            f"[{index}/{total}] Calling LLM for comparison row id={case_id}",
            file=sys.stderr,
            flush=True,
        )
        try:
            result = experiment.run(user_request)
        except Exception as exc:
            if _is_fatal_llm_setup_error(exc):
                raise
            result = IntentCoverageResult(
                warnings=[f"COMPARISON_ROW_FAILED: {_format_exception(exc)}"],
            )
    return coverage_result_to_comparison_row(
        case=case,
        case_id=case_id,
        user_request=user_request,
        gold_result=gold_result,
        qwen_result=qwen_result,
        result=result,
    )


def coverage_result_to_comparison_row(
    case: Mapping[str, Any],
    case_id: str,
    user_request: str,
    gold_result: Any,
    qwen_result: Any,
    result: IntentCoverageResult,
) -> Dict[str, str]:
    intent_tools = [
        ", ".join(row.tool_ids)
        for row in result.covered_intents
        if row.tool_ids
    ]
    intents = [
        row.intent
        for row in result.covered_intents
        if row.intent
    ]
    prediction_tools = " -> ".join(_extract_workflow_tool_names(qwen_result))
    return normalize_coverage_table_row(
        {
            "id": case_id,
            "user_request": user_request,
            "intent": "; ".join(intents),
            "intent tool": "; ".join(intent_tools),
            "model tool": prediction_tools,
            "coverage_warnings": "; ".join(result.warnings),
        }
    )


def normalize_coverage_table_row(
    row: Mapping[str, Any],
    case_id: str | None = None,
    user_request: str | None = None,
    gold_result: Any = None,
    qwen_result: Any = None,
) -> Dict[str, str]:
    resolved_id = str(case_id or row.get("id") or "").strip()
    resolved_user_request = str(
        user_request
        or row.get("user_request")
        or row.get("user request")
        or row.get("instruction")
        or ""
    ).strip()
    raw_intent = _read_text(row, "intent", "gpt4 intent", "gpt4_intent")
    raw_intent_tool = _read_text(row, "intent tool", "intent_tool", "gpt4 tool", "gpt4_tool")
    intent = _normalize_coverage_intent_cell(raw_intent)
    intent_tool = _normalize_coverage_tool_cell(raw_intent_tool)
    parsed_intent_cell = _parse_jsonish_field(raw_intent)
    if not intent_tool and isinstance(parsed_intent_cell, (Mapping, list)):
        intent_tool = _normalize_coverage_tool_cell(parsed_intent_cell)
    model_tool = _read_text(row, "model tool", "model_tool", "qwen tool", "qwen_tool")
    if not model_tool:
        model_tool = " -> ".join(_extract_workflow_tool_names(qwen_result))
    return {
        "id": resolved_id,
        "user_request": resolved_user_request,
        "intent": intent,
        "intent tool": intent_tool,
        "model tool": model_tool,
        "coverage_warnings": str(row.get("coverage_warnings") or "").strip(),
    }


def _normalize_coverage_intent_cell(value: Any) -> str:
    parsed = _parse_jsonish_field(value)
    if isinstance(parsed, Mapping):
        nested = parsed.get("covered_intents") or parsed.get("intents") or parsed.get("rows")
        if nested is not None:
            return _join_unique(
                _normalize_coverage_intent_cell(item)
                for item in _coerce_list(nested)
            )
        return _empty_jsonish_to_blank(parsed.get("intent") or parsed.get("name"))
    if isinstance(parsed, list):
        return _join_unique(_normalize_coverage_intent_cell(item) for item in parsed)
    return _empty_jsonish_to_blank(parsed)


def _normalize_coverage_tool_cell(value: Any) -> str:
    parsed = _parse_jsonish_field(value)
    if isinstance(parsed, Mapping):
        nested = parsed.get("covered_intents") or parsed.get("intents") or parsed.get("rows")
        if nested is not None:
            return _join_unique(_normalize_coverage_tool_cell(item) for item in _coerce_list(nested))
        tools = (
            parsed.get("tool_ids")
            or parsed.get("tools")
            or parsed.get("tool_id")
            or parsed.get("tool")
        )
        return _normalize_coverage_tool_cell(tools)
    if isinstance(parsed, list):
        return _join_unique(_normalize_coverage_tool_cell(item) for item in parsed)
    return _empty_jsonish_to_blank(parsed)


def _empty_jsonish_to_blank(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower() in {"[]", "{}", "null", "none", "nan"}:
        return ""
    return text


def _join_unique(values: Iterable[Any]) -> str:
    seen = set()
    joined: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        joined.append(text)
    return "; ".join(joined)


def build_intent_detector_trace(user_request: str, result: IntentCoverageResult) -> Dict[str, Any]:
    return {
        "agent": "intent_detector",
        "user_request": user_request,
        "covered_intents": [
            {
                "intent": row.intent,
                "tool_ids": list(row.tool_ids),
                "coverage_type": row.coverage_type,
                "confidence": row.confidence,
                "matched_request_phrase": row.matched_request_phrase,
                "matched_intent_term": row.matched_intent_term,
                "reason": row.reason,
            }
            for row in result.covered_intents
        ],
        "warnings": list(result.warnings),
        "raw_llm_output": result.raw_llm_output,
    }


def _comparison_error_row(
    case: Mapping[str, Any],
    case_id: str,
    user_request: str,
    gold_result: Any,
    qwen_result: Any,
    reason: str,
) -> Dict[str, str]:
    return coverage_result_to_comparison_row(
        case=case,
        case_id=case_id,
        user_request=user_request,
        gold_result=gold_result,
        qwen_result=qwen_result,
        result=IntentCoverageResult(warnings=[reason]),
    )


def write_comparison_table(rows: List[Dict[str, str]], output_path: str | Path, output_format: str) -> None:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "table-xlsx":
        _write_xlsx_table(rows, COMPARISON_TABLE_COLUMNS, output, "intent_coverage")
        return
    if output_format == "table-md":
        lines = [
            "| " + " | ".join(COMPARISON_TABLE_COLUMNS) + " |",
            "| " + " | ".join("---" for _ in COMPARISON_TABLE_COLUMNS) + " |",
        ]
        for row in rows:
            lines.append(
                "| "
                + " | ".join(_escape_markdown_cell(row.get(column, "")) for column in COMPARISON_TABLE_COLUMNS)
                + " |"
            )
        output.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    with output.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=COMPARISON_TABLE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_xlsx_table(
    rows: List[Dict[str, Any]],
    columns: List[str],
    output_path: Path,
    sheet_name: str,
) -> None:
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise RuntimeError("Writing xlsx output requires openpyxl. Install it with: pip install openpyxl") from exc

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet_name[:31]
    worksheet.append(columns)
    for row in rows:
        worksheet.append([_xlsx_cell(row.get(column, "")) for column in columns])

    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    for column_index, column in enumerate(columns, start=1):
        width = min(max(len(column) + 2, 12), 60)
        worksheet.column_dimensions[worksheet.cell(row=1, column=column_index).column_letter].width = width

    workbook.save(output_path)


def _resolve_case_gold_result(case: Mapping[str, Any], fallback: Any = None) -> Any:
    if case.get("gold_result") is not None:
        return _parse_jsonish_field(case.get("gold_result"))
    if case.get("gold_workflow") is not None:
        return _parse_jsonish_field(case.get("gold_workflow"))
    return fallback


def _resolve_case_qwen_result(case: Mapping[str, Any], fallback: Any = None) -> Any:
    if case.get("prediction_result") is not None:
        return _parse_jsonish_field(case.get("prediction_result"))
    if case.get("qwen_result") is not None:
        return _parse_jsonish_field(case.get("qwen_result"))
    if case.get("predicted_workflow") is not None:
        return _parse_jsonish_field(case.get("predicted_workflow"))
    if case.get("result") is not None:
        return _parse_jsonish_field(case.get("result"))
    return fallback


def _extract_workflow_tool_names(workflow: Any) -> List[str]:
    payload = _parse_jsonish_field(workflow)
    if not isinstance(payload, Mapping):
        return []
    nodes = (
        payload.get("tool_nodes")
        or payload.get("task_nodes")
        or payload.get("nodes")
        or []
    )
    if isinstance(nodes, str):
        nodes = _parse_jsonish_field(nodes)
    if not isinstance(nodes, list):
        return []
    tools: List[str] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        task = str(node.get("task") or node.get("id") or "").strip()
        if task:
            tools.append(task)
    return tools


def load_json_records(path: str | Path) -> List[Dict[str, Any]]:
    resolved = Path(path)
    text = resolved.read_text(encoding="utf-8-sig").strip()
    if not text:
        return []
    if text[0] in "[{":
        try:
            payload = json.loads(text)
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
            if isinstance(payload, dict):
                if isinstance(payload.get("records"), list):
                    return [item for item in payload["records"] if isinstance(item, dict)]
                if isinstance(payload.get("data"), list):
                    return [item for item in payload["data"] if isinstance(item, dict)]
                return [payload]
        except json.JSONDecodeError:
            pass

    records: List[Dict[str, Any]] = []
    for line_no, line in enumerate(resolved.read_text(encoding="utf-8-sig").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        payload = json.loads(stripped)
        if not isinstance(payload, dict):
            raise ValueError(f"JSONL line {line_no} must be an object: {resolved}")
        records.append(payload)
    return records


def load_user_requests(path: str | Path | None) -> Dict[str, str]:
    if not path:
        return {}
    resolved = Path(path)
    if not resolved.exists():
        return {}
    requests: Dict[str, str] = {}
    for record in load_json_records(resolved):
        case_id = str(record.get("id") or "").strip()
        user_request = str(record.get("user_request") or record.get("instruction") or "").strip()
        if case_id and user_request:
            requests[case_id] = user_request
    return requests


def resolve_user_requests_file(path: str | Path | None, input_file: str | Path | None = None) -> Path | None:
    if path:
        return Path(path)
    if input_file:
        input_path = Path(input_file)
        sibling = input_path.parent / "user_requests.json"
        if sibling.exists():
            return sibling
    return None


def load_existing_coverage_tables(paths: Iterable[str]) -> Dict[str, Dict[str, str]]:
    rows_by_id: Dict[str, Dict[str, str]] = {}
    for raw_path in paths:
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.exists():
            raise FileNotFoundError(f"existing coverage table not found: {path}")
        suffix = path.suffix.lower()
        if suffix == ".xlsx":
            from agent.memory_guided_workflow.experiments import baseline_guided_workflow_critic_experiment as bg

            rows = bg.read_xlsx_records(path)
        elif suffix == ".jsonl":
            rows = load_json_records(path)
        else:
            with path.open("r", encoding="utf-8-sig", newline="") as file:
                rows = [dict(row) for row in csv.DictReader(file)]
        for row in rows:
            normalized = normalize_coverage_table_row(row)
            case_id = normalized["id"]
            if case_id:
                rows_by_id[case_id] = normalized
    return rows_by_id


def load_qwen_results(path: str | Path) -> Dict[str, Any]:
    result_by_id: Dict[str, Any] = {}
    for record in load_json_records(path):
        case_id = str(record.get("id") or "").strip()
        if not case_id:
            continue
        result_by_id[case_id] = record.get("result", record)
    return result_by_id


def load_prediction_results(path: str | Path) -> Dict[str, Any]:
    return load_qwen_results(path)


def load_gold_results(path: str | Path) -> Dict[str, Any]:
    result_by_id: Dict[str, Any] = {}
    for record in load_json_records(path):
        case_id = str(record.get("id") or "").strip()
        if not case_id:
            continue
        result_by_id[case_id] = {
            "tool_steps": _parse_jsonish_field(record.get("tool_steps")),
            "tool_nodes": _parse_jsonish_field(record.get("tool_nodes")),
            "tool_links": _parse_jsonish_field(record.get("tool_links")),
        }
    return result_by_id


def _url_download_candidates(
    intent_tools: Mapping[str, Iterable[Mapping[str, Any]]]
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for intent, tools in intent_tools.items():
        matched_tools = []
        for tool in tools:
            input_types = set(_coerce_text_list(tool.get("input_types")))
            if "url" not in input_types:
                continue
            evidence = " ".join(
                [
                    str(intent),
                    str(tool.get("tool_id") or ""),
                    str(tool.get("desc") or ""),
                ]
            ).lower()
            if not any(term in evidence for term in URL_DOWNLOAD_TERMS):
                continue
            matched_tools.append(dict(tool))
        if matched_tools:
            candidates.append(
                {
                    "intent": intent,
                    "expanded_terms": _split_identifier(intent),
                    "candidate_tools": matched_tools,
                }
            )
    return candidates


def _split_identifier(value: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value or ""))
    text = re.sub(r"[^A-Za-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _read_text(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _read_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))


def _read_string_list(row: Mapping[str, Any], *keys: str) -> List[str]:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        return _coerce_text_list(value)
    return []


def _coerce_text_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [value]
    elif isinstance(value, IterableABC) and not isinstance(value, Mapping):
        parts = list(value)
    else:
        parts = [value]
    return [str(part).strip().lower() for part in parts if str(part).strip()]


def _coerce_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _normalize_intent_tools(
    intents: Iterable[str],
    intent_tool_ids: Mapping[str, Iterable[str]],
    intent_tool_descs: Mapping[str, Iterable[Mapping[str, Any]]] | None,
) -> Dict[str, List[Dict[str, Any]]]:
    normalized: Dict[str, List[Dict[str, Any]]] = {}
    raw_descs = dict(intent_tool_descs or {})
    for intent in intents:
        intent_text = str(intent)
        tools: List[Dict[str, Any]] = []
        for raw_tool in _coerce_list(raw_descs.get(intent_text)):
            if not isinstance(raw_tool, Mapping):
                continue
            tool_id = str(raw_tool.get("tool_id") or raw_tool.get("id") or "").strip()
            desc = str(raw_tool.get("desc") or raw_tool.get("description") or "").strip()
            if tool_id and not any(tool.get("tool_id") == tool_id for tool in tools):
                tools.append(
                    {
                        "tool_id": tool_id,
                        "desc": desc,
                        "input_types": _read_string_list(raw_tool, "input_types", "input-type", "input_type"),
                        "output_types": _read_string_list(raw_tool, "output_types", "output-type", "output_type"),
                    }
                )
        for tool_id in intent_tool_ids.get(intent_text, []):
            tool_text = str(tool_id).strip()
            if tool_text and not any(tool.get("tool_id") == tool_text for tool in tools):
                tools.append({"tool_id": tool_text, "desc": "", "input_types": [], "output_types": []})
        normalized[intent_text] = tools
    return normalized


def _default_checkpoint_path(output_path: str | Path) -> Path:
    return Path(f"{output_path}.checkpoint.jsonl")


def _preflight_llm_client(llm_client: Any) -> None:
    resolve_config = getattr(llm_client, "resolve_config", None)
    if not callable(resolve_config):
        return
    config = resolve_config()
    resolve_api_key = getattr(llm_client, "resolve_api_key", None)
    if callable(resolve_api_key):
        resolve_api_key(config)


def _is_fatal_llm_setup_error(exc: Exception) -> bool:
    if isinstance(exc, (FileNotFoundError, PermissionError)):
        return True
    if isinstance(exc, ValueError):
        text = str(exc).lower()
        return "llm config" in text or "api_key" in text or "api key" in text
    return False


def _case_checkpoint_key(case_id: str, position: int) -> str:
    return case_id if case_id else f"__position_{position}"


def _load_checkpoint_by_key(path: Path | None, mode: str) -> Dict[str, Dict[str, Any]]:
    if path is None or not path.exists():
        return {}

    records_by_key: Dict[str, Dict[str, Any]] = {}
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError as exc:
            print(
                f"Skipped invalid checkpoint line {line_no} in {path}: {_format_exception(exc)}",
                file=sys.stderr,
                flush=True,
            )
            continue
        if not isinstance(record, Mapping) or record.get("mode") != mode:
            continue
        key = str(record.get("key") or record.get("id") or "").strip()
        if not key:
            position = record.get("position")
            if isinstance(position, int):
                key = _case_checkpoint_key("", position)
        if key:
            records_by_key[key] = dict(record)
    return records_by_key


def _reset_checkpoint_file(path: Path | None) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def _append_checkpoint_record(path: Path | None, record: Mapping[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")
        file.flush()


def _append_batch_checkpoint(
    path: Path | None,
    position: int,
    case: Mapping[str, Any],
    rows: List[Dict[str, Any]],
) -> None:
    case_id = str(case.get("id") or "").strip()
    _append_checkpoint_record(
        path,
        {
            "mode": "batch",
            "key": _case_checkpoint_key(case_id, position),
            "position": position,
            "id": case_id,
            "status": "failed" if _batch_rows_need_coverage_rerun(rows) else "ok",
            "rows": rows,
        },
    )


def _append_comparison_checkpoint(
    path: Path | None,
    position: int,
    case: Mapping[str, Any],
    row: Dict[str, str],
) -> None:
    case_id = str(case.get("id") or "").strip()
    _append_checkpoint_record(
        path,
        {
            "mode": "comparison",
            "key": _case_checkpoint_key(case_id, position),
            "position": position,
            "id": case_id,
            "status": "failed" if _row_needs_coverage_rerun(row) else "ok",
            "row": row,
        },
    )


def _row_has_retryable_failure(row: Mapping[str, Any]) -> bool:
    warning_text = str(row.get("coverage_warnings") or "")
    return any(marker in warning_text for marker in RETRYABLE_WARNING_MARKERS)


def _row_has_usable_coverage(row: Mapping[str, Any]) -> bool:
    normalized = normalize_coverage_table_row(row)
    has_intent = bool(normalized.get("intent") or normalized.get("intent tool"))
    return has_intent and not _row_has_retryable_failure(normalized)


def _row_needs_coverage_rerun(row: Mapping[str, Any]) -> bool:
    return not _row_has_usable_coverage(row)


def _batch_rows_have_retryable_failure(rows: Iterable[Mapping[str, Any]]) -> bool:
    return any(_row_has_retryable_failure(row) for row in rows)


def _batch_rows_need_coverage_rerun(rows: Iterable[Mapping[str, Any]]) -> bool:
    materialized = list(rows)
    return not materialized or any(_row_needs_coverage_rerun(row) for row in materialized)


def _all_rows_retryable_failed(rows: Iterable[Mapping[str, Any]]) -> bool:
    materialized = list(rows)
    return bool(materialized) and all(_row_has_retryable_failure(row) for row in materialized)


def _count_retryable_failed_rows(rows: Iterable[Mapping[str, Any]]) -> int:
    return sum(1 for row in rows if _row_has_retryable_failure(row))


def _count_incomplete_coverage_rows(rows: Iterable[Mapping[str, Any]]) -> int:
    return sum(1 for row in rows if _row_needs_coverage_rerun(row))


def _flatten_ordered_batch_rows(ordered_rows: Iterable[List[Dict[str, Any]] | None]) -> List[Dict[str, Any]]:
    table_rows: List[Dict[str, Any]] = []
    for rows in ordered_rows:
        if rows:
            table_rows.extend(rows)
    return table_rows


def _print_resume_summary(mode: str, skipped_count: int, pending_count: int, checkpoint_path: Path) -> None:
    print(
        f"Resuming {mode}: skipped {skipped_count} completed cases; rerunning {pending_count} cases. "
        f"checkpoint={checkpoint_path}",
        file=sys.stderr,
        flush=True,
    )


def _format_exception(exc: Exception) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ").strip()
    if not message:
        message = exc.__class__.__name__
    return f"{exc.__class__.__name__}: {message}"


def _escape_markdown_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def _empty_batch_row(
    case_id: str,
    user_request: str,
    gold_result: Any,
    qwen_result: Any,
    reason: str,
) -> Dict[str, Any]:
    return normalize_coverage_table_row(
        {
            "id": case_id,
            "user_request": user_request,
            "intent": "",
            "intent tool": "",
            "model tool": " -> ".join(_extract_workflow_tool_names(qwen_result)),
            "coverage_warnings": reason,
        }
    )


def _resolve_user_request(case: Mapping[str, Any], user_request_by_id: Mapping[str, str]) -> str:
    case_id = str(case.get("id") or "").strip()
    return str(
        user_request_by_id.get(case_id)
        or case.get("user_request")
        or case.get("instruction")
        or ""
    ).strip()


def _json_cell(value: Any) -> str:
    return json.dumps(_parse_jsonish_field(value), ensure_ascii=False, indent=2)


def _xlsx_cell(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def _parse_jsonish_field(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    if text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ask an LLM which tool_desc intents are covered by a user request."
    )
    parser.add_argument("--request", default=None, help="Natural-language user request.")
    parser.add_argument(
        "--input-file",
        default=None,
        help="JSONL/JSON case file for batch mode. Defaults can be enabled with --use-default-miwp-sample.",
    )
    parser.add_argument(
        "--use-default-miwp-sample",
        action="store_true",
        help=f"Use default input file: {DEFAULT_INPUT_FILE}",
    )
    parser.add_argument(
        "--gold-result-file",
        default=None,
        help="Legacy optional TaskBench data.json file. Current coverage table output does not require gold tools.",
    )
    parser.add_argument(
        "--user-requests-file",
        default=None,
        help=(
            "JSON/JSONL file keyed by id. Its user_request text overrides data.json instruction. "
            "If omitted, the script uses user_requests.json next to --input-file when present."
        ),
    )
    parser.add_argument(
        "--prediction-file",
        "--qwen-result-file",
        dest="prediction_file",
        default=None,
        help=(
            "Optional JSONL/JSON prediction file keyed by id for filling the model tool column. "
            "--qwen-result-file is kept as a legacy alias."
        ),
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_BATCH_OUTPUT_PATH),
        help="Output table path for batch mode.",
    )
    parser.add_argument(
        "--checkpoint-file",
        default=None,
        help="Incremental JSONL checkpoint path. Defaults to '<output>.checkpoint.jsonl'.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume from checkpoint. Completed cases are skipped; rows marked as LLM/API/parse failures "
            "are rerun."
        ),
    )
    parser.add_argument(
        "--existing-coverage-xlsx",
        action="append",
        default=[],
        help=(
            "Existing coverage table to reuse. Rows with non-empty intent/intent tool and no retryable "
            "warning are skipped; empty or failed rows are regenerated."
        ),
    )
    parser.add_argument(
        "--output-format",
        choices=("csv", "jsonl", "xlsx", "table", "table-md", "table-xlsx"),
        default="csv",
        help=(
            "Output format for batch mode. csv/jsonl/xlsx keep the raw coverage rows; "
            "table/table-md/table-xlsx export model/intent coverage comparison rows."
        ),
    )
    parser.add_argument("--max-cases", type=int, default=0, help="Batch debug limit. 0 means all cases.")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of concurrent LLM calls in batch mode. 1 keeps the original sequential behavior.",
    )
    parser.add_argument(
        "--tool-desc",
        default=None,
        help=(
            "Path to TaskBench-style tool_desc_intent.json used for intent coverage. "
            "Defaults to dataset_config.tool_desc_intent, then multimedia tool_desc_intent.json."
        ),
    )
    parser.add_argument(
        "--dataset-config",
        default=None,
        help="Optional JSON config for dataset-specific aliases and prompt variables.",
    )
    parser.add_argument(
        "--llm-config",
        default="configs/openai_gpt5.4_mini.json",
        help="OpenAI-compatible LLM config path.",
    )
    parser.add_argument(
        "--llm-profile",
        default=None,
        help="Optional profile name when the LLM config defines profiles.",
    )
    parser.add_argument(
        "--llm-timeout",
        type=float,
        default=None,
        help="Override LLM request timeout in seconds for this run.",
    )
    parser.add_argument(
        "--llm-max-retries",
        type=int,
        default=None,
        help="Override OpenAI-compatible client max_retries for this run.",
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    args = parser.parse_args(argv)
    args.qwen_result_file = args.prediction_file

    llm_config: Dict[str, Any] | None = None
    llm_config_path = args.llm_config
    llm_profile = args.llm_profile
    if args.llm_timeout is not None or args.llm_max_retries is not None:
        try:
            config_client = OpenAICompatibleLLMClient(
                llm_config_path=args.llm_config,
                llm_profile=args.llm_profile,
            )
            llm_config = config_client.resolve_config()
        except Exception as exc:
            parser.error(_format_exception(exc))
        if args.llm_timeout is not None:
            llm_config["timeout"] = args.llm_timeout
        if args.llm_max_retries is not None:
            llm_config["max_retries"] = args.llm_max_retries
        llm_config_path = None
        llm_profile = None
    try:
        dataset_config = load_dataset_runtime_config(args.dataset_config)
    except Exception as exc:
        parser.error(_format_exception(exc))

    experiment = IntentLLMCoverageExperiment(
        tool_desc_path=resolve_tool_desc_intent_path_arg(args.tool_desc, dataset_config),
        llm_config_path=llm_config_path,
        llm_config=llm_config,
        llm_profile=llm_profile,
        coverage_prompt_rules=get_coverage_prompt_rules(dataset_config),
        coverage_prompt_variables=get_coverage_prompt_variables(dataset_config),
    )
    try:
        _preflight_llm_client(experiment.llm_client)
    except Exception as exc:
        parser.error(_format_exception(exc))

    input_file = args.input_file
    if args.use_default_miwp_sample and input_file is None:
        input_file = str(DEFAULT_INPUT_FILE)
    if input_file:
        checkpoint_file = args.checkpoint_file or str(_default_checkpoint_path(args.output))
        try:
            user_requests_file = resolve_user_requests_file(args.user_requests_file, input_file)
            user_request_by_id = load_user_requests(user_requests_file)
            existing_coverage_by_id = load_existing_coverage_tables(args.existing_coverage_xlsx)
        except Exception as exc:
            parser.error(_format_exception(exc))
        if args.output_format in {"table", "table-md", "table-xlsx"}:
            rows = run_comparison_table_batch(
                experiment=experiment,
                input_file=input_file,
                gold_result_file=args.gold_result_file,
                qwen_result_file=args.prediction_file,
                user_request_by_id=user_request_by_id,
                existing_coverage_by_id=existing_coverage_by_id,
                max_cases=args.max_cases,
                workers=args.workers,
                checkpoint_file=checkpoint_file,
                resume=args.resume,
            )
            if _all_rows_retryable_failed(rows):
                print(
                    "all rows failed with retryable LLM errors; final output was not written. "
                    f"Fix the LLM/API issue and rerun with --resume. checkpoint: {checkpoint_file}",
                    file=sys.stderr,
                    flush=True,
                )
                return 1
            write_comparison_table(rows, args.output, args.output_format)
            failed_count = _count_retryable_failed_rows(rows)
        else:
            rows = run_batch(
                experiment=experiment,
                input_file=input_file,
                gold_result_file=args.gold_result_file,
                qwen_result_file=args.prediction_file,
                user_request_by_id=user_request_by_id,
                max_cases=args.max_cases,
                workers=args.workers,
                checkpoint_file=checkpoint_file,
                resume=args.resume,
            )
            if _all_rows_retryable_failed(rows):
                print(
                    "all rows failed with retryable LLM errors; final output was not written. "
                    f"Fix the LLM/API issue and rerun with --resume. checkpoint: {checkpoint_file}",
                    file=sys.stderr,
                    flush=True,
                )
                return 1
            write_batch_table(rows, args.output, args.output_format)
            failed_count = _count_retryable_failed_rows(rows)
        print(f"wrote {len(rows)} rows to {args.output}")
        print(f"checkpoint: {checkpoint_file}")
        incomplete_count = _count_incomplete_coverage_rows(rows)
        if failed_count:
            print(
                f"{failed_count} rows still have retryable LLM failures. "
                "Rerun the same command with --resume to fill them.",
                file=sys.stderr,
                flush=True,
            )
            return 1
        if incomplete_count:
            print(
                f"{incomplete_count} rows still have empty intent coverage. "
                "Rerun with --existing-coverage-xlsx pointing to this output to fill them.",
                file=sys.stderr,
                flush=True,
            )
            return 1
        return 0

    if not args.request:
        parser.error("one of --request, --input-file, or --use-default-miwp-sample is required")

    print(
        f"Calling LLM to judge coverage over {len(experiment.intents)} intents...",
        file=sys.stderr,
        flush=True,
    )
    result = experiment.run(args.request)

    if args.format == "json":
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_markdown_table(result))
        if result.warnings:
            print()
            print("Warnings:")
            for warning in result.warnings:
                print(f"- {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
