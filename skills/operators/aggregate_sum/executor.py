from typing import Any, Dict, List


def _to_float(v: Any) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def run(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    field = str(args.get("field", "sales"))
    rows: List[Dict[str, Any]] = ctx.get("data", [])
    total = sum(_to_float(row.get(field)) for row in rows)
    ctx["last_total"] = total
    return {"field": field, "sum": total}
