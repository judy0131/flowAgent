from typing import Any, Dict, List


def _to_float(v: Any) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def run(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    field = str(args.get("field", ""))
    op = str(args.get("op", "eq")).lower()
    op_alias = {
        "==": "eq",
        ">": "gt",
        "<": "lt",
    }
    op = op_alias.get(op, op)
    value = args.get("value")
    rows: List[Dict[str, Any]] = ctx.get("data", [])

    if op == "eq":
        out = [r for r in rows if str(r.get(field)) == str(value)]
    elif op == "contains":
        out = [r for r in rows if str(value) in str(r.get(field, ""))]
    elif op == "gt":
        out = [r for r in rows if _to_float(r.get(field)) > _to_float(value)]
    elif op == "lt":
        out = [r for r in rows if _to_float(r.get(field)) < _to_float(value)]
    else:
        raise ValueError(f"unsupported op: {op}")

    ctx["data"] = out
    return {"remain_rows": len(out), "op": op, "field": field}
