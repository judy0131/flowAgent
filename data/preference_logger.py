import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class PreferenceLogger:
    def __init__(self, output_path: Optional[Path] = None):
        default_path = Path(__file__).resolve().parent / "preferences" / "preference_samples.jsonl"
        self.output_path = output_path or default_path
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _normalize_plan(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for idx, step in enumerate(steps, start=1):
            normalized.append(
                {
                    "id": idx,
                    "skill": step.get("skill"),
                    "args": step.get("args", {}),
                    "reason": step.get("reason"),
                }
            )
        return normalized

    def log_candidates(
        self,
        prompt: str,
        candidates: List[Dict[str, Any]],
        selected_plan_id: int,
        execution_mode: str,
        selected_execution: Optional[Dict[str, Any]] = None,
    ) -> None:
        record = {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "prompt": prompt,
            "selected_plan_id": selected_plan_id,
            "execution_mode": execution_mode,
            "candidates": [
                {
                    "id": item.get("id"),
                    "score": item.get("score"),
                    "strategy_hint": item.get("strategy_hint"),
                    "steps": self._normalize_plan(item.get("steps", [])),
                }
                for item in candidates
            ],
            "selected_execution": selected_execution,
        }
        with self.output_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

