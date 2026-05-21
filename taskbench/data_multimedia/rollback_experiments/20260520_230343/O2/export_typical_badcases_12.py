from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
BASE = Path(__file__).resolve().parent

OUT_MD = BASE / "typical_badcases_12_by_split.md"
OUT_JSON = BASE / "typical_badcases_12_by_split.json"
OUT_CSV = BASE / "typical_badcases_12_by_split_summary.csv"

CHOSEN = [
    "11656312",
    "13336269",
    "24435782",
    "96133316",
    "45875119",
    "31461277",
    "29292224",
    "31788289",
    "27258164",
    "79560754",
    "13018270",
    "26579656",
]

CASE_NOTES = {
    "11656312": "单工具视频搜索题被扩成两步，平白多出 Video-to-Image；说明模型会把“找视频教程”误解成“搜视频后再抽帧”。",
    "13336269": "工具选对了，但检索 query 被改写，gold 的 `high-resolution breathtaking sunset` 被弱化成 `a beautiful sunset`，属于参数拷贝失败。",
    "24435782": "节点正确，但数值参数 `75%` 被改写成长文本说明，属于字面值拷贝 / 参数规范化失败。",
    "96133316": "把纯文本搜图误判成 `Image Search (by Image)`，还凭空引入 `example.jpg`；属于模态幻觉。",
    "45875119": "漏掉用户显式要求的 `Image Colorizer`，中间步骤缺失；candidate pool 里已有多个满分候选。",
    "31461277": "节点都对，但把 `Image Style Transfer` 和 `Image Colorizer` 的顺序做反了，属于变换顺序错误。",
    "29292224": "长链任务里丢了最终 `Text Search`，同时把多分支依赖压扁到错误上游；`action_coverage` 候选明显更接近 gold。",
    "31788289": "主链基本正确，但末端额外幻觉出 `Text Translator`，属于过度规划 / 额外步骤。",
    "27258164": "DAG/chain 边方向错误，先降噪再变声，导致首段依赖倒置；`minimal` 候选能修正。",
    "79560754": "节点都对，但 `Topic Generator` 和 `Text-to-Video` 都绑到了 grammar 输出，而不是 summary/topic 分支；属于上游绑定错误。",
    "13018270": "把 effect 描述直接塞进 `Audio Effects`，漏掉 gold 中的 `Text Simplifier` 支路；说明模型会压缩辅助文本分支。",
    "26579656": "节点都对，但 `Video-to-Image` 接成了 stabilized video，而不是 original download 分支；是典型 DAG 分支绑定错误。",
}

CASE_ARCHETYPES = {
    "11656312": "extra_step_recoverable",
    "13336269": "parameter_copy_failure",
    "24435782": "parameter_normalization_failure",
    "96133316": "wrong_tool_hallucination",
    "45875119": "missing_required_step_recoverable",
    "31461277": "transformation_order_error",
    "29292224": "long_chain_dependency_collapse",
    "31788289": "extra_terminal_step",
    "27258164": "edge_direction_error",
    "79560754": "upstream_binding_error",
    "13018270": "collapsed_auxiliary_branch",
    "26579656": "dag_branch_binding_error",
}

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
        "edges": [{"source": link["source"], "target": link["target"]} for link in links],
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
    for case_id in CHOSEN:
        meta_row = meta[case_id]
        oracle_row = oracle[case_id]
        dump_row = candidate_dump[case_id]

        gold = workflow_summary(
            {"task_nodes": meta_row["tool_nodes"], "task_links": meta_row["tool_links"]}
        )
        selected = workflow_summary(dump_row["selected_result"])

        best_id = oracle_row["best_quality_candidate_id"]
        best_candidate = next(
            c
            for c in dump_row["candidates"]
            if c.get("candidate_id") == best_id or c.get("id") == best_id
        )
        oracle_best = workflow_summary(best_candidate["result"])

        candidates = []
        for candidate in sorted(
            dump_row["candidates"], key=lambda item: item.get("candidate_id", item.get("id", 0))
        ):
            candidate_workflow = workflow_summary(candidate["result"])
            candidates.append(
                {
                    "candidate_id": candidate.get("candidate_id", candidate.get("id")),
                    "family_name": candidate.get("family_name"),
                    "variant_name": candidate.get("variant_name"),
                    "strategy_name": candidate.get("strategy_name"),
                    "quality_score": candidate.get("quality_score"),
                    "node_f1": candidate.get("node_f1"),
                    "edge_f1": candidate.get("edge_f1"),
                    "arg_value_f1": candidate.get("arg_value_f1"),
                    "exact_match": candidate.get("exact_match"),
                    "planner_score": candidate.get("score"),
                    "dependency_check_pass": candidate.get("dependency_check"),
                    "validation_status": candidate.get("validation_status"),
                    "task_chain": candidate_workflow["task_chain"],
                    "edge_strings": candidate_workflow["edge_strings"],
                    "node_arguments": candidate_workflow["node_arguments"],
                }
            )

        records.append(
            {
                "case_id": case_id,
                "type": meta_row["type"],
                "instruction": meta_row["instruction"],
                "issue_archetype": CASE_ARCHETYPES[case_id],
                "why_typical_cn": CASE_NOTES[case_id],
                "oracle_better": oracle_row["oracle_better"],
                "selection_route": dump_row.get("selection_route"),
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
                "structural_unique_candidate_count": oracle_row.get(
                    "structural_unique_candidate_count"
                ),
                "exact_unique_candidate_count": oracle_row.get("exact_unique_candidate_count"),
                "gold": gold,
                "selected": selected,
                "oracle_best": oracle_best,
                "candidates": candidates,
            }
        )

    records.sort(key=lambda item: (SPLIT_ORDER[item["type"]], CHOSEN.index(item["case_id"])))
    return records


def write_json(records):
    payload = {
        "experiment": "20260520_230343/O2",
        "selection_rule": (
            "从 O2 的 selected_exact=false badcase 中各选 4 个 single / chain / dag；"
            "优先覆盖 oracle-better、高 regret 和有代表性的失败模式。"
        ),
        "case_count": len(records),
        "cases": records,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(records):
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "Type",
                "CaseId",
                "OracleBetter",
                "IssueArchetype",
                "SelectedFamily",
                "BestFamily",
                "SelectedQuality",
                "BestQuality",
                "RerankRegret",
                "StructuralUniqueCandidates",
                "ExactUniqueCandidates",
                "SelectedWorkflow",
                "GoldWorkflow",
                "BestWorkflow",
                "ChineseExplanation",
            ],
        )
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "Type": record["type"],
                    "CaseId": record["case_id"],
                    "OracleBetter": record["oracle_better"],
                    "IssueArchetype": record["issue_archetype"],
                    "SelectedFamily": (
                        f"{record['selected_family_name']}/{record['selected_variant_name']}"
                    ),
                    "BestFamily": (
                        f"{record['best_quality_family_name']}/{record['best_quality_variant_name']}"
                    ),
                    "SelectedQuality": (
                        f"{record['selected_quality_score']:.4f}"
                        if record["selected_quality_score"] is not None
                        else ""
                    ),
                    "BestQuality": (
                        f"{record['best_quality_score']:.4f}"
                        if record["best_quality_score"] is not None
                        else ""
                    ),
                    "RerankRegret": (
                        f"{record['rerank_regret']:.4f}"
                        if record["rerank_regret"] is not None
                        else ""
                    ),
                    "StructuralUniqueCandidates": record["structural_unique_candidate_count"],
                    "ExactUniqueCandidates": record["exact_unique_candidate_count"],
                    "SelectedWorkflow": record["selected"]["task_chain"],
                    "GoldWorkflow": record["gold"]["task_chain"],
                    "BestWorkflow": record["oracle_best"]["task_chain"],
                    "ChineseExplanation": record["why_typical_cn"],
                }
            )


def write_markdown(records):
    lines = [
        "# O2 Typical Badcases (12 cases, 4 per split)",
        "",
        "- Experiment: `20260520_230343 / O2`",
        "- Selection rule: from `selected_exact = false` badcases, choose 4 each for `single / chain / dag`; prioritize `oracle-better`, high regret, and representative failure archetypes.",
        "- Each case keeps `gold`, `selected`, `oracle-best`, and all 10 candidate summaries.",
        "",
        "## Summary",
        "",
        "| Type | CaseId | OracleBetter | Archetype | Selected | Best | Regret | Unique Candidates |",
        "| --- | --- | ---: | --- | --- | --- | ---: | --- |",
    ]

    for record in records:
        lines.append(
            f"| {record['type']} | {record['case_id']} | {record['oracle_better']} | "
            f"{record['issue_archetype']} | "
            f"{record['selected_family_name']}/{record['selected_variant_name']} | "
            f"{record['best_quality_family_name']}/{record['best_quality_variant_name']} | "
            f"{record['rerank_regret']:.4f} | "
            f"{record['structural_unique_candidate_count']} / {record['exact_unique_candidate_count']} |"
        )

    for split in ["single", "chain", "dag"]:
        lines.extend(["", f"## {split.upper()}"])
        for record in [item for item in records if item["type"] == split]:
            lines.extend(
                [
                    "",
                    f"### {record['case_id']}",
                    "",
                    f"- Archetype: `{record['issue_archetype']}`",
                    f"- 中文解释: {record['why_typical_cn']}",
                    f"- Oracle better: `{record['oracle_better']}`",
                    f"- Selection route: `{record['selection_route']}`",
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
            edge_f1 = (
                ""
                if record["selected_edge_f1"] is None
                else f"{record['selected_edge_f1']:.4f}"
            )
            lines.extend(
                [
                    "- Candidate: "
                    f"`#{record['selected_candidate_id']}` | "
                    f"`{record['selected_family_name']}/{record['selected_variant_name']}`",
                    "- Metrics: "
                    f"`quality={record['selected_quality_score']:.4f}, "
                    f"node_f1={record['selected_node_f1']:.4f}, "
                    f"edge_f1={edge_f1}, "
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
                f"- Node args: `{json.dumps(record['oracle_best']['node_arguments'], ensure_ascii=False)}`"
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
                edge_f1 = (
                    "" if candidate["edge_f1"] is None else f"{candidate['edge_f1']:.4f}"
                )
                lines.append(
                    f"| {candidate['candidate_id']} | {candidate['family_name']} | "
                    f"{candidate['variant_name']} | {candidate['quality_score']:.4f} | "
                    f"{candidate['node_f1']:.4f} | {edge_f1} | {candidate['exact_match']} | "
                    f"{candidate['task_chain']} | {'; '.join(candidate['edge_strings'])} |"
                )

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def main():
    records = build_records()
    write_json(records)
    write_csv(records)
    write_markdown(records)
    print(OUT_MD)
    print(OUT_JSON)
    print(OUT_CSV)


if __name__ == "__main__":
    main()
