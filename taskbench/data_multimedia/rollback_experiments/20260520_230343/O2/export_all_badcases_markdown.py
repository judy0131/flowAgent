from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
BASE = Path(__file__).resolve().parent
OUT_MD = BASE / "all_badcases_detailed.md"

SPLIT_ORDER = {"single": 0, "chain": 1, "dag": 2}


def parse_json_maybe(value):
    return json.loads(value) if isinstance(value, str) else value


def workflow_summary(obj):
    obj = parse_json_maybe(obj)
    nodes = parse_json_maybe(obj["task_nodes"])
    links = parse_json_maybe(obj["task_links"])
    tasks = [node["task"] for node in nodes]
    return {
        "tasks": tasks,
        "task_chain": " -> ".join(tasks),
        "node_arguments": [
            {"task": node["task"], "arguments": node.get("arguments", [])} for node in nodes
        ],
        "edge_strings": [f"{link['source']} -> {link['target']}" for link in links],
    }


def load_data():
    meta = {}
    with (ROOT / "taskbench/data_multimedia/data.json").open("r", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            meta[str(row["id"])] = row

    oracle = {}
    with (BASE / "oracle_analysis/oracle_case_details.jsonl").open("r", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            oracle[str(row["id"])] = row

    candidate_dump = {}
    with (BASE / "candidate_dumps/pipeline_orchestrator_agent_gpt-4.1_20260521.jsonl").open(
        "r", encoding="utf-8"
    ) as f:
        for line in f:
            row = json.loads(line)
            candidate_dump[str(row["id"])] = row

    return meta, oracle, candidate_dump


def build_records():
    meta, oracle, candidate_dump = load_data()
    records = []
    for case_id, oracle_row in oracle.items():
        if oracle_row.get("selected_exact"):
            continue

        meta_row = meta[case_id]
        dump_row = candidate_dump[case_id]
        gold = workflow_summary(
            {"task_nodes": meta_row["tool_nodes"], "task_links": meta_row["tool_links"]}
        )
        selected = workflow_summary(dump_row["selected_result"])

        candidates = []
        best_id = oracle_row["best_quality_candidate_id"]
        best_candidate_summary = None
        has_exact_gold_candidate = False
        exact_gold_candidate_ids = []

        for candidate in sorted(
            dump_row["candidates"], key=lambda item: item.get("candidate_id", item.get("id", 0))
        ):
            candidate_id = candidate.get("candidate_id", candidate.get("id"))
            candidate_workflow = workflow_summary(candidate["result"])
            exact_match = bool(candidate.get("exact_match"))
            if exact_match:
                has_exact_gold_candidate = True
                exact_gold_candidate_ids.append(candidate_id)

            summary = {
                "candidate_id": candidate_id,
                "family_name": candidate.get("family_name"),
                "variant_name": candidate.get("variant_name"),
                "quality_score": candidate.get("quality_score"),
                "tasks": candidate_workflow["tasks"],
                "node_f1": candidate.get("node_f1"),
                "edge_f1": candidate.get("edge_f1"),
                "arg_value_f1": candidate.get("arg_value_f1"),
                "exact_match": exact_match,
                "planner_score": candidate.get("score"),
                "task_chain": candidate_workflow["task_chain"],
                "edge_strings": candidate_workflow["edge_strings"],
                "node_arguments": candidate_workflow["node_arguments"],
            }
            candidates.append(summary)
            if candidate_id == best_id:
                best_candidate_summary = summary

        if best_candidate_summary is None:
            raise ValueError(f"best candidate not found for case {case_id}")

        oracle_best_matches_gold_structure = (
            gold["tasks"] == best_candidate_summary["tasks"]
            and gold["edge_strings"] == best_candidate_summary["edge_strings"]
        )

        records.append(
            {
                "case_id": case_id,
                "type": meta_row["type"],
                "instruction": meta_row["instruction"],
                "oracle_better": bool(oracle_row.get("oracle_better")),
                "selection_route": dump_row.get("selection_route"),
                "has_exact_gold_candidate": has_exact_gold_candidate,
                "exact_gold_candidate_ids": exact_gold_candidate_ids,
                "oracle_best_matches_gold_structure": oracle_best_matches_gold_structure,
                "structural_unique_candidate_count": oracle_row.get(
                    "structural_unique_candidate_count"
                ),
                "exact_unique_candidate_count": oracle_row.get("exact_unique_candidate_count"),
                "selected_candidate_id": oracle_row.get("selected_candidate_id"),
                "selected_family_name": oracle_row.get("selected_family_name"),
                "selected_variant_name": oracle_row.get("selected_variant_name"),
                "selected_quality_score": oracle_row.get("selected_quality_score"),
                "selected_node_f1": oracle_row.get("selected_node_f1"),
                "selected_edge_f1": oracle_row.get("selected_edge_f1"),
                "selected_exact": oracle_row.get("selected_exact"),
                "best_quality_candidate_id": oracle_row.get("best_quality_candidate_id"),
                "best_quality_family_name": oracle_row.get("best_quality_family_name"),
                "best_quality_variant_name": oracle_row.get("best_quality_variant_name"),
                "best_quality_score": oracle_row.get("best_quality_score"),
                "best_node_f1": oracle_row.get("best_node_f1"),
                "best_edge_f1": oracle_row.get("best_edge_f1"),
                "rerank_regret": oracle_row.get("rerank_regret"),
                "gold": gold,
                "selected": selected,
                "oracle_best": best_candidate_summary,
                "candidates": candidates,
            }
        )

    records.sort(
        key=lambda item: (
            SPLIT_ORDER[item["type"]],
            not item["oracle_better"],
            -(item["rerank_regret"] or 0.0),
            item["case_id"],
        )
    )
    return records


def write_markdown(records):
    by_split = {
        "single": [record for record in records if record["type"] == "single"],
        "chain": [record for record in records if record["type"] == "chain"],
        "dag": [record for record in records if record["type"] == "dag"],
    }
    total_with_exact = sum(1 for record in records if record["has_exact_gold_candidate"])
    total_oracle_best_structural_match = sum(
        1 for record in records if record["oracle_best_matches_gold_structure"]
    )

    lines = [
        "# O2 All Badcases",
        "",
        "- Experiment: `20260520_230343 / O2`",
        "- Definition: `badcase = selected_exact == false`",
        "- Total badcases: "
        f"`{len(records)}` (`single={len(by_split['single'])}`, `chain={len(by_split['chain'])}`, `dag={len(by_split['dag'])}`)",
        "- Badcases with exact gold candidate in pool: "
        f"`{total_with_exact}` / `{len(records)}` = `{(total_with_exact / len(records)):.4%}`",
        "- Oracle-best matches gold structure (workflow + edges, ignoring node args): "
        f"`{total_oracle_best_structural_match}` / `{len(records)}` = "
        f"`{(total_oracle_best_structural_match / len(records)):.4%}`",
        "",
        "## Summary",
        "",
        "| Type | CaseId | OracleBetter | HasExactGoldCandidate | OracleBestMatchesGoldStructure | Selected | Best | Regret | Unique Candidates |",
        "| --- | --- | ---: | ---: | ---: | --- | --- | ---: | --- |",
    ]

    for record in records:
        lines.append(
            f"| {record['type']} | {record['case_id']} | {record['oracle_better']} | "
            f"{record['has_exact_gold_candidate']} | {record['oracle_best_matches_gold_structure']} | "
            f"{record['selected_family_name']}/{record['selected_variant_name']} | "
            f"{record['best_quality_family_name']}/{record['best_quality_variant_name']} | "
            f"{record['rerank_regret']:.4f} | "
            f"{record['structural_unique_candidate_count']} / {record['exact_unique_candidate_count']} |"
        )

    for split in ["single", "chain", "dag"]:
        lines.extend(["", f"## {split.upper()}"])
        for record in by_split[split]:
            lines.extend(
                [
                    "",
                    f"### {record['case_id']}",
                    "",
                    f"- Oracle better: `{record['oracle_better']}`",
                    f"- Selection route: `{record['selection_route']}`",
                    f"- Has exact gold candidate in pool: `{record['has_exact_gold_candidate']}`",
                    f"- Exact gold candidate ids: `{record['exact_gold_candidate_ids']}`",
                    "- Oracle-best matches gold structure "
                    "(workflow + edges, ignoring node args): "
                    f"`{record['oracle_best_matches_gold_structure']}`",
                    "- Structural / exact unique candidates: "
                    f"`{record['structural_unique_candidate_count']} / {record['exact_unique_candidate_count']}`",
                    f"- Instruction: {record['instruction']}",
                    "",
                    "**Gold**",
                    "",
                    f"- Workflow: `{record['gold']['task_chain']}`",
                ]
            )
            if record["gold"]["edge_strings"]:
                lines.append(f"- Edges: `{'; '.join(record['gold']['edge_strings'])}`")
            lines.append(
                f"- Node args: `{json.dumps(record['gold']['node_arguments'], ensure_ascii=False)}`"
            )

            lines.extend(["", "**Selected**", ""])
            selected_edge_f1 = (
                "" if record["selected_edge_f1"] is None else f"{record['selected_edge_f1']:.4f}"
            )
            lines.extend(
                [
                    "- Candidate: "
                    f"`#{record['selected_candidate_id']}` | "
                    f"`{record['selected_family_name']}/{record['selected_variant_name']}`",
                    "- Metrics: "
                    f"`quality={record['selected_quality_score']:.4f}, "
                    f"node_f1={record['selected_node_f1']:.4f}, "
                    f"edge_f1={selected_edge_f1}, "
                    f"exact={record['selected_exact']}`",
                    f"- Workflow: `{record['selected']['task_chain']}`",
                ]
            )
            if record["selected"]["edge_strings"]:
                lines.append(f"- Edges: `{'; '.join(record['selected']['edge_strings'])}`")
            lines.append(
                f"- Node args: `{json.dumps(record['selected']['node_arguments'], ensure_ascii=False)}`"
            )

            lines.extend(["", "**Oracle Best**", ""])
            best_edge_f1 = (
                "" if record["best_edge_f1"] is None else f"{record['best_edge_f1']:.4f}"
            )
            lines.extend(
                [
                    "- Candidate: "
                    f"`#{record['best_quality_candidate_id']}` | "
                    f"`{record['best_quality_family_name']}/{record['best_quality_variant_name']}`",
                    "- Metrics: "
                    f"`quality={record['best_quality_score']:.4f}, "
                    f"node_f1={record['best_node_f1']:.4f}, "
                    f"edge_f1={best_edge_f1}, "
                    f"regret={record['rerank_regret']:.4f}`",
                    f"- Workflow: `{record['oracle_best']['task_chain']}`",
                ]
            )
            if record["oracle_best"]["edge_strings"]:
                lines.append(f"- Edges: `{'; '.join(record['oracle_best']['edge_strings'])}`")
            lines.append(
                "- Node args: "
                f"`{json.dumps(record['oracle_best']['node_arguments'], ensure_ascii=False)}`"
            )

            lines.extend(
                [
                    "",
                    "**All 10 Candidates**",
                    "",
                    "| # | Family | Variant | Quality | nF1 | eF1 | Exact | Workflow | Edges |",
                    "| ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- |",
                ]
            )
            for candidate in record["candidates"]:
                edge_f1 = "" if candidate["edge_f1"] is None else f"{candidate['edge_f1']:.4f}"
                lines.append(
                    f"| {candidate['candidate_id']} | {candidate['family_name']} | "
                    f"{candidate['variant_name']} | {candidate['quality_score']:.4f} | "
                    f"{candidate['node_f1']:.4f} | {edge_f1} | {candidate['exact_match']} | "
                    f"{candidate['task_chain']} | {'; '.join(candidate['edge_strings'])} |"
                )

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def main():
    records = build_records()
    write_markdown(records)
    print(OUT_MD)


if __name__ == "__main__":
    main()
