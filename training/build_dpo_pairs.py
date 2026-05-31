import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _plan_key(plan: List[Dict[str, Any]]) -> str:
    normalized = [{"skill": s.get("skill"), "args": s.get("args", {})} for s in plan]
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True)


def _default_label(candidates: List[Dict[str, Any]], selected_plan_id: int) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    chosen = None
    rejected: List[Dict[str, Any]] = []
    for item in candidates:
        if item.get("id") == selected_plan_id:
            chosen = item
        else:
            rejected.append(item)
    return chosen, rejected


def _to_dpo_pairs(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    prompt = record.get("prompt", "")
    candidates = record.get("candidates", [])
    selected_plan_id = record.get("selected_plan_id")
    chosen, rejected_list = _default_label(candidates, selected_plan_id)
    if not chosen:
        return []

    chosen_text = json.dumps(chosen.get("steps", []), ensure_ascii=False, sort_keys=True)
    pairs: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for rejected in rejected_list:
        rejected_text = json.dumps(rejected.get("steps", []), ensure_ascii=False, sort_keys=True)
        sig = f"{prompt}::{_plan_key(chosen.get('steps', []))}::{_plan_key(rejected.get('steps', []))}"
        if sig in seen:
            continue
        seen.add(sig)
        pairs.append(
            {
                "prompt": prompt,
                "chosen": chosen_text,
                "rejected": rejected_text,
                "meta": {
                    "chosen_id": chosen.get("id"),
                    "rejected_id": rejected.get("id"),
                    "chosen_score": chosen.get("score"),
                    "rejected_score": rejected.get("score"),
                    "strategy_hint_chosen": chosen.get("strategy_hint"),
                    "strategy_hint_rejected": rejected.get("strategy_hint"),
                },
            }
        )
    return pairs


def build_pairs(input_jsonl: Path, output_jsonl: Path) -> int:
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with input_jsonl.open("r", encoding="utf-8") as fin, output_jsonl.open("w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            pairs = _to_dpo_pairs(record)
            for sample in pairs:
                fout.write(json.dumps(sample, ensure_ascii=False) + "\n")
                total += 1
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Build DPO pairwise preference dataset from candidate plan logs.")
    parser.add_argument("--input", type=Path, default=Path("data/preferences/preference_samples.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("data/preferences/dpo_pairs.jsonl"))
    args = parser.parse_args()

    total = build_pairs(args.input, args.output)
    print(f"Generated {total} DPO pairs -> {args.output}")


if __name__ == "__main__":
    main()

