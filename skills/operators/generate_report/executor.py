from typing import Any, Dict, List


def run(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    title = str(args.get("title", "pipeline report"))
    rows: List[Dict[str, Any]] = ctx.get("data", [])
    report = {
        "title": title,
        "row_count": len(rows),
        "last_total": ctx.get("last_total"),
        "source_path": ctx.get("source_path"),
        "trace": ctx.get("trace", []),
    }
    ctx["report"] = report
    return report
