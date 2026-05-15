import json
from typing import Any, Dict


def _safe_json_dumps(payload: Any, *, ensure_ascii: bool = False) -> str:
    seen: set[int] = set()

    def _sanitize(obj: Any) -> Any:
        if isinstance(obj, (str, int, float, bool)) or obj is None:
            return obj

        oid = id(obj)
        if oid in seen:
            return "<circular_ref>"

        if isinstance(obj, dict):
            seen.add(oid)
            out: Dict[str, Any] = {}
            for k, v in obj.items():
                out[str(k)] = _sanitize(v)
            seen.remove(oid)
            return out

        if isinstance(obj, (list, tuple, set)):
            seen.add(oid)
            out = [_sanitize(v) for v in obj]
            seen.remove(oid)
            return out

        return str(obj)

    return json.dumps(_sanitize(payload), ensure_ascii=ensure_ascii, indent=2)
