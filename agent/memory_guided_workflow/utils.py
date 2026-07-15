import json
import re
from collections.abc import Iterable as IterableABC, Mapping
from pathlib import Path
from typing import Any, Dict, Iterable, List


_JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)
_MARKDOWN_ESCAPED_PUNCTUATION_PATTERN = re.compile(r"\\([_`*#[\](){}.!+-])")
_JSON_DECODER = json.JSONDecoder()


def coerce_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def coerce_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def load_dataset_runtime_config(path: str | Path | None) -> Dict[str, Any]:
    if not path:
        return {}
    resolved = Path(path)
    if not resolved.exists():
        raise FileNotFoundError(f"dataset config not found: {resolved}")
    payload = json.loads(resolved.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"dataset config must be a JSON object: {resolved}")
    return dict(payload)


def get_coverage_prompt_rules(config: Mapping[str, Any] | None) -> List[str]:
    return _string_list(_config_value(config, "coverage_prompt_rules"))


def get_coverage_prompt_variables(config: Mapping[str, Any] | None) -> Dict[str, Any]:
    return _dict(_config_value(config, "coverage_prompt_variables"))


def get_replan_prompt_rules(config: Mapping[str, Any] | None) -> List[str]:
    return _string_list(_config_value(config, "replan_prompt_rules"))


def get_replan_prompt_variables(config: Mapping[str, Any] | None) -> Dict[str, Any]:
    return _dict(_config_value(config, "replan_prompt_variables"))


def get_keep_original_graph_repair_config(config: Mapping[str, Any] | None) -> Dict[str, Any]:
    return _dict(_config_value(config, "keep_original_graph_repair"))


def get_tool_desc_intent_path(config: Mapping[str, Any] | None, default: str | None = None) -> str:
    for key in (
        "tool_desc_intent",
        "tool_desc_intent_path",
        "intent_tool_desc",
        "intent_tool_desc_path",
    ):
        value = _config_value(config, key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return str(default or "").strip()


def get_semantic_tool_families(
    config: Mapping[str, Any] | None,
    default: Mapping[str, Iterable[Any]] | None = None,
) -> Dict[str, List[str]]:
    raw = _config_value(config, "semantic_tool_families")
    if not isinstance(raw, Mapping):
        raw = default or {}
    families: Dict[str, List[str]] = {}
    for family, tools in dict(raw).items():
        family_name = str(family).strip()
        if not family_name:
            continue
        normalized_tools = _string_list(tools)
        if normalized_tools:
            families[family_name] = normalized_tools
    return families


def extract_json_object(raw_text: Any) -> Dict[str, Any]:
    text = str(raw_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    candidates = [text]
    first_object_index = text.find("{")
    if first_object_index > 0:
        candidates.append(text[first_object_index:])
    match = _JSON_OBJECT_PATTERN.search(text)
    if match is not None:
        candidates.append(match.group(0))

    first_error: json.JSONDecodeError | None = None
    for candidate in _unique_texts(candidates):
        try:
            return _loads_json_object(candidate)
        except json.JSONDecodeError as exc:
            if first_error is None:
                first_error = exc

    if first_error is not None:
        raise first_error
    raise ValueError("expected a JSON object")


def _config_value(config: Mapping[str, Any] | None, key: str) -> Any:
    if not isinstance(config, Mapping):
        return None
    return config.get(key)


def _dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, IterableABC) and not isinstance(value, Mapping):
        values = list(value)
    else:
        values = [value]
    return [str(item).strip() for item in values if str(item).strip()]


def _loads_json_object(candidate: str) -> Dict[str, Any]:
    first_error: json.JSONDecodeError | None = None
    for variant in _json_repair_variants(candidate):
        try:
            payload = json.loads(variant)
        except json.JSONDecodeError as exc:
            if first_error is None:
                first_error = exc
        else:
            return _ensure_json_object(payload)

        try:
            payload, _ = _JSON_DECODER.raw_decode(variant)
        except json.JSONDecodeError as exc:
            if first_error is None:
                first_error = exc
        else:
            return _ensure_json_object(payload)

    if first_error is not None:
        raise first_error
    raise ValueError("expected a JSON object")


def _json_repair_variants(candidate: str) -> List[str]:
    text = candidate.strip()
    markdown_repaired = _MARKDOWN_ESCAPED_PUNCTUATION_PATTERN.sub(r"\1", text)
    return list(
        _unique_texts(
            [
                text,
                markdown_repaired,
                _append_missing_closing_braces(text),
                _append_missing_closing_braces(markdown_repaired),
            ]
        )
    )


def _append_missing_closing_braces(text: str) -> str:
    open_braces = 0
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
        elif char == "{":
            open_braces += 1
        elif char == "}":
            open_braces -= 1
    if in_string or open_braces <= 0:
        return text
    return text + ("}" * open_braces)


def _ensure_json_object(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    return payload


def _unique_texts(values: Iterable[str]) -> List[str]:
    unique: List[str] = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique
