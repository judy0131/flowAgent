import csv
from pathlib import Path
from typing import Any, Dict, List


def _fallback_rows() -> List[Dict[str, Any]]:
    return [
        {"region": "east", "sales": 120},
        {"region": "south", "sales": 80},
        {"region": "east", "sales": 60},
        {"region": "north", "sales": 100},
    ]


def run(args: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    path = Path(str(args.get("path", "demo.csv")))
    rows: List[Dict[str, Any]] = []

    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(dict(row))
    else:
        rows = _fallback_rows()

    ctx["data"] = rows
    ctx["source_path"] = str(path)
    return {"path": str(path), "rows": len(rows)}
