import json
import re
from typing import Any, Dict, List


_JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)


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


def extract_json_object(raw_text: Any) -> Dict[str, Any]:
    text = str(raw_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_PATTERN.search(text)
        if match is None:
            raise
        payload = json.loads(match.group(0))

    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    return payload
