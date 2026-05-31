from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
BASE = Path(__file__).resolve().parent

OUT_MD = BASE / "o2_family_contribution.md"
OUT_FAMILY_CSV = BASE / "o2_family_contribution_family.csv"
OUT_VARIANT_CSV = BASE / "o2_family_contribution_variant.csv"
OUT_JSON = BASE / "o2_family_contribution.json"

FAMILY_ORDER = [
    "original",
    "minimal",
    "action_coverage",
    "parallel_dag",
    "dependency_first",
    "parameter_copy",
]

VARIANT_ORDER = [
    "original/baseline",
    "minimal/fewest_tools",
    "minimal/fewest_transformations",
    "action_coverage/strict_explicit_action_coverage",
    "action_coverage/step_by_step_decomposition",
    "action_coverage/preserve_every_user_requested_operation",
    "parallel_dag/preserve_independent_branches",
    "parallel_dag/avoid_forcing_dags_into_chains",
    "dependency_first/semantic_dependency_continuity",
    "parameter_copy/exact_parameter_copy",
]

SPLITS = ["single", "chain", "dag"]


def parse_json_maybe(value):
    return json.loads(value) if isinstance(value, str) else value


def structural_signature(obj):
    obj = parse_json_maybe(obj)
    nodes = parse_json_maybe(obj["task_nodes"])
    links = parse_json_maybe(obj["task_links"])
    tasks = [node["task"] for node in nodes]
    edges = [(link["source"], link["target"]) for link in links]
    return tasks, edges


def load_data():
    meta = {}
    with (ROOT / "taskbench/data_multimedia/data.json").open("r", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            meta[str(row["id"])] = row

    oracle_rows = []
    with (BASE / "oracle_analysis/oracle_case_details.jsonl").open("r", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            row["type"] = meta[str(row["id"])]["type"]
            oracle_rows.append(row)

    candidate_dump = {}
    with (BASE / "candidate_dumps/pipeline_orchestrator_agent_gpt-4.1_20260521.jsonl").open(
        "r", encoding="utf-8"
    ) as f:
        for line in f:
            row = json.loads(line)
            candidate_dump[str(row["id"])] = row

    return meta, oracle_rows, candidate_dump


def init_bucket():
    return {
        "oracle_best_count": 0,
        "oracle_best_structure_match_count": 0,
        "oracle_best_exact_match_count": 0,
        "oracle_best_quality_sum": 0.0,
        "oracle_better_count": 0,
        "oracle_better_structure_match_count": 0,
        "oracle_better_exact_match_count": 0,
        "oracle_better_regret_sum": 0.0,
        "oracle_better_quality_gain_sum": 0.0,
        "oracle_better_cases": [],
        "upper_bound_quality_sum": 0.0,
        "upper_bound_exact_match_count": 0,
        "upper_bound_structure_match_count": 0,
        "support": 0,
        "split_counts": Counter(),
        "oracle_better_split_counts": Counter(),
    }


def build_stats():
    meta, oracle_rows, candidate_dump = load_data()
    total_cases = len(oracle_rows)
    oracle_better_total = sum(1 for row in oracle_rows if row["oracle_better"])
    badcase_total = sum(1 for row in oracle_rows if not row["selected_exact"])

    family_stats = {family: init_bucket() for family in FAMILY_ORDER}
    variant_stats = {variant: init_bucket() for variant in VARIANT_ORDER}

    for row in oracle_rows:
        case_id = str(row["id"])
        split = row["type"]
        dump = candidate_dump[case_id]
        gold_sig = structural_signature(
            {"task_nodes": meta[case_id]["tool_nodes"], "task_links": meta[case_id]["tool_links"]}
        )

        candidates = dump["candidates"]
        family_best_per_case = {}
        variant_map = {}
        for candidate in candidates:
            family = str(candidate.get("family_name", "")).strip()
            variant = f"{family}/{str(candidate.get('variant_name', '')).strip()}"
            quality = float(candidate.get("quality_score", 0.0))
            exact_match = bool(candidate.get("exact_match"))
            struct_match = structural_signature(candidate["result"]) == gold_sig

            variant_map[variant] = {
                "quality": quality,
                "exact_match": exact_match,
                "struct_match": struct_match,
            }

            existing = family_best_per_case.get(family)
            if existing is None or quality > existing["quality"]:
                family_best_per_case[family] = {
                    "quality": quality,
                    "exact_match": exact_match,
                    "struct_match": struct_match,
                }

        for family in FAMILY_ORDER:
            if family not in family_best_per_case:
                continue
            family_stats[family]["support"] += 1
            family_stats[family]["upper_bound_quality_sum"] += family_best_per_case[family]["quality"]
            family_stats[family]["upper_bound_exact_match_count"] += int(
                family_best_per_case[family]["exact_match"]
            )
            family_stats[family]["upper_bound_structure_match_count"] += int(
                family_best_per_case[family]["struct_match"]
            )

        for variant in VARIANT_ORDER:
            if variant not in variant_map:
                continue
            variant_stats[variant]["support"] += 1
            variant_stats[variant]["upper_bound_quality_sum"] += variant_map[variant]["quality"]
            variant_stats[variant]["upper_bound_exact_match_count"] += int(
                variant_map[variant]["exact_match"]
            )
            variant_stats[variant]["upper_bound_structure_match_count"] += int(
                variant_map[variant]["struct_match"]
            )

        best_family = str(row["best_quality_family_name"]).strip()
        best_variant = f"{best_family}/{str(row['best_quality_variant_name']).strip()}"
        best_quality = float(row["best_quality_score"])
        best_candidate_id = row["best_quality_candidate_id"]
        best_candidate = next(
            c for c in candidates if c.get("candidate_id") == best_candidate_id or c.get("id") == best_candidate_id
        )
        best_exact = bool(best_candidate.get("exact_match"))
        best_struct_match = structural_signature(best_candidate["result"]) == gold_sig

        family_bucket = family_stats[best_family]
        family_bucket["oracle_best_count"] += 1
        family_bucket["oracle_best_structure_match_count"] += int(best_struct_match)
        family_bucket["oracle_best_exact_match_count"] += int(best_exact)
        family_bucket["oracle_best_quality_sum"] += best_quality
        family_bucket["split_counts"][split] += 1

        variant_bucket = variant_stats[best_variant]
        variant_bucket["oracle_best_count"] += 1
        variant_bucket["oracle_best_structure_match_count"] += int(best_struct_match)
        variant_bucket["oracle_best_exact_match_count"] += int(best_exact)
        variant_bucket["oracle_best_quality_sum"] += best_quality
        variant_bucket["split_counts"][split] += 1

        if row["oracle_better"]:
            regret = float(row["rerank_regret"])
            quality_gain = float(row["best_quality_score"]) - float(row["selected_quality_score"])

            family_bucket["oracle_better_count"] += 1
            family_bucket["oracle_better_structure_match_count"] += int(best_struct_match)
            family_bucket["oracle_better_exact_match_count"] += int(best_exact)
            family_bucket["oracle_better_regret_sum"] += regret
            family_bucket["oracle_better_quality_gain_sum"] += quality_gain
            family_bucket["oracle_better_cases"].append(case_id)
            family_bucket["oracle_better_split_counts"][split] += 1

            variant_bucket["oracle_better_count"] += 1
            variant_bucket["oracle_better_structure_match_count"] += int(best_struct_match)
            variant_bucket["oracle_better_exact_match_count"] += int(best_exact)
            variant_bucket["oracle_better_regret_sum"] += regret
            variant_bucket["oracle_better_quality_gain_sum"] += quality_gain
            variant_bucket["oracle_better_cases"].append(case_id)
            variant_bucket["oracle_better_split_counts"][split] += 1

    return {
        "total_cases": total_cases,
        "oracle_better_total": oracle_better_total,
        "badcase_total": badcase_total,
        "family_stats": family_stats,
        "variant_stats": variant_stats,
    }


def finalize_rows(stats_dict, total_cases, oracle_better_total, baseline_name):
    rows = []
    baseline_upper_bound = None
    if baseline_name in stats_dict and stats_dict[baseline_name]["support"]:
        baseline_upper_bound = (
            stats_dict[baseline_name]["upper_bound_quality_sum"] / stats_dict[baseline_name]["support"]
        )

    for name, bucket in stats_dict.items():
        support = bucket["support"] or 1
        oracle_best_count = bucket["oracle_best_count"]
        oracle_better_count = bucket["oracle_better_count"]
        upper_bound_mean_quality = bucket["upper_bound_quality_sum"] / support
        row = {
            "Name": name,
            "OracleBestCount": oracle_best_count,
            "OracleBestRate": oracle_best_count / total_cases,
            "OracleBestMeanQuality": (
                bucket["oracle_best_quality_sum"] / oracle_best_count if oracle_best_count else 0.0
            ),
            "OracleBestStructureMatchCount": bucket["oracle_best_structure_match_count"],
            "OracleBestStructureMatchRateWithinWins": (
                bucket["oracle_best_structure_match_count"] / oracle_best_count if oracle_best_count else 0.0
            ),
            "OracleBestExactMatchCount": bucket["oracle_best_exact_match_count"],
            "OracleBetterCount": oracle_better_count,
            "OracleBetterShare": (
                oracle_better_count / oracle_better_total if oracle_better_total else 0.0
            ),
            "OracleBetterMeanRegret": (
                bucket["oracle_better_regret_sum"] / oracle_better_count if oracle_better_count else 0.0
            ),
            "OracleBetterTotalRegret": bucket["oracle_better_regret_sum"],
            "OracleBetterStructureMatchCount": bucket["oracle_better_structure_match_count"],
            "OracleBetterExactMatchCount": bucket["oracle_better_exact_match_count"],
            "UpperBoundMeanQuality": upper_bound_mean_quality,
            "UpperBoundExactMatchRate": bucket["upper_bound_exact_match_count"] / support,
            "UpperBoundStructureMatchRate": bucket["upper_bound_structure_match_count"] / support,
            "UpperBoundDeltaVsOriginal": (
                upper_bound_mean_quality - baseline_upper_bound
                if baseline_upper_bound is not None
                else 0.0
            ),
            "SplitWins": dict(bucket["split_counts"]),
            "OracleBetterSplitWins": dict(bucket["oracle_better_split_counts"]),
            "OracleBetterCases": bucket["oracle_better_cases"],
        }
        rows.append(row)
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = [
        "Name",
        "OracleBestCount",
        "OracleBestRate",
        "OracleBestMeanQuality",
        "OracleBestStructureMatchCount",
        "OracleBestStructureMatchRateWithinWins",
        "OracleBestExactMatchCount",
        "OracleBetterCount",
        "OracleBetterShare",
        "OracleBetterMeanRegret",
        "OracleBetterTotalRegret",
        "OracleBetterStructureMatchCount",
        "OracleBetterExactMatchCount",
        "UpperBoundMeanQuality",
        "UpperBoundExactMatchRate",
        "UpperBoundStructureMatchRate",
        "UpperBoundDeltaVsOriginal",
        "SplitWins",
        "OracleBetterSplitWins",
        "OracleBetterCases",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            out = dict(row)
            out["OracleBestRate"] = f"{row['OracleBestRate']:.4%}"
            out["OracleBestMeanQuality"] = f"{row['OracleBestMeanQuality']:.4f}"
            out["OracleBestStructureMatchRateWithinWins"] = (
                f"{row['OracleBestStructureMatchRateWithinWins']:.4%}"
            )
            out["OracleBetterShare"] = f"{row['OracleBetterShare']:.4%}"
            out["OracleBetterMeanRegret"] = f"{row['OracleBetterMeanRegret']:.4f}"
            out["OracleBetterTotalRegret"] = f"{row['OracleBetterTotalRegret']:.4f}"
            out["UpperBoundMeanQuality"] = f"{row['UpperBoundMeanQuality']:.4f}"
            out["UpperBoundExactMatchRate"] = f"{row['UpperBoundExactMatchRate']:.4%}"
            out["UpperBoundStructureMatchRate"] = f"{row['UpperBoundStructureMatchRate']:.4%}"
            out["UpperBoundDeltaVsOriginal"] = f"{row['UpperBoundDeltaVsOriginal']:.4f}"
            out["SplitWins"] = json.dumps(row["SplitWins"], ensure_ascii=False)
            out["OracleBetterSplitWins"] = json.dumps(row["OracleBetterSplitWins"], ensure_ascii=False)
            out["OracleBetterCases"] = ",".join(row["OracleBetterCases"])
            writer.writerow(out)


def write_markdown(
    family_rows: list[dict],
    variant_rows: list[dict],
    total_cases: int,
    oracle_better_total: int,
    badcase_total: int,
) -> None:
    lines = [
        "# O2 Family Contribution",
        "",
        "- Experiment: `20260520_230343 / O2`",
        f"- Total cases: `{total_cases}`",
        f"- Badcases (`selected_exact = false`): `{badcase_total}`",
        f"- Oracle-better cases: `{oracle_better_total}`",
        "- Definition:",
        "  - `OracleBestCount`: 该 family / variant 在该 case 上成为 oracle best 的次数",
        "  - `OracleBetterCount`: 只看 `oracle_better=true` 的 case，这个 family / variant 真正把 selected 拉高的次数",
        "  - `UpperBoundMeanQuality`: 每个 case 里只看这个 family / variant 自己时，能达到的平均质量上界",
        "  - `StructureMatch`: 只比较 workflow + edges，不看 node args",
        "",
        "## Family-Level Contribution",
        "",
        "| Family | OracleBestCount | OracleBestRate | OracleBetterCount | OracleBetterShare | OracleBetterMeanRegret | UpperBoundMeanQuality | DeltaVsOriginal | OracleBestStructureMatchCount | OracleBetterCases |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in family_rows:
        lines.append(
            f"| {row['Name']} | {row['OracleBestCount']} | {row['OracleBestRate']:.4%} | "
            f"{row['OracleBetterCount']} | {row['OracleBetterShare']:.4%} | "
            f"{row['OracleBetterMeanRegret']:.4f} | {row['UpperBoundMeanQuality']:.4f} | "
            f"{row['UpperBoundDeltaVsOriginal']:.4f} | {row['OracleBestStructureMatchCount']} | "
            f"{', '.join(row['OracleBetterCases'])} |"
        )

    lines.extend(
        [
            "",
            "## Variant-Level Contribution",
            "",
            "| Variant | OracleBestCount | OracleBetterCount | OracleBetterShare | OracleBetterMeanRegret | UpperBoundMeanQuality | DeltaVsOriginal | OracleBetterCases |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in variant_rows:
        if row["OracleBestCount"] == 0 and row["OracleBetterCount"] == 0 and abs(row["UpperBoundDeltaVsOriginal"]) < 1e-12:
            pass
        lines.append(
            f"| {row['Name']} | {row['OracleBestCount']} | {row['OracleBetterCount']} | "
            f"{row['OracleBetterShare']:.4%} | {row['OracleBetterMeanRegret']:.4f} | "
            f"{row['UpperBoundMeanQuality']:.4f} | {row['UpperBoundDeltaVsOriginal']:.4f} | "
            f"{', '.join(row['OracleBetterCases'])} |"
        )

    lines.extend(
        [
            "",
            "## Quick Read",
            "",
            "- `original` 仍然是绝对主力；如果只看 oracle best 覆盖，它赢了绝大多数 case。",
            "- `minimal` 是最主要的补救 family；如果只看 `oracle_better` 子集，它贡献最大。",
            "- `action_coverage` 和 `parallel_dag` 只在少数 hard cases 上提供增量，但这些增量是结构性的，不是噪声。",
            "- `dependency_first` / `parameter_copy` 在这批实验里没有成为 oracle best，但它们的单 family 上界仍然可比较，说明问题更像是 winning power 不足，而不是完全无效。",
        ]
    )
    OUT_MD.write_text("\n".join(lines), encoding="utf-8-sig")


def main() -> None:
    raw = build_stats()
    family_rows = finalize_rows(
        raw["family_stats"],
        raw["total_cases"],
        raw["oracle_better_total"],
        baseline_name="original",
    )
    variant_rows = finalize_rows(
        raw["variant_stats"],
        raw["total_cases"],
        raw["oracle_better_total"],
        baseline_name="original/baseline",
    )

    family_rows.sort(key=lambda row: FAMILY_ORDER.index(row["Name"]))
    variant_rows.sort(key=lambda row: VARIANT_ORDER.index(row["Name"]))

    payload = {
        "experiment": "20260520_230343/O2",
        "total_cases": raw["total_cases"],
        "badcase_total": raw["badcase_total"],
        "oracle_better_total": raw["oracle_better_total"],
        "family_rows": family_rows,
        "variant_rows": variant_rows,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(OUT_FAMILY_CSV, family_rows)
    write_csv(OUT_VARIANT_CSV, variant_rows)
    write_markdown(
        family_rows,
        variant_rows,
        raw["total_cases"],
        raw["oracle_better_total"],
        raw["badcase_total"],
    )
    print(OUT_MD)
    print(OUT_FAMILY_CSV)
    print(OUT_VARIANT_CSV)
    print(OUT_JSON)


if __name__ == "__main__":
    main()
