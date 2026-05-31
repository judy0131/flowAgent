﻿# O2 Prompt Families Only

当前 `O2` 使用的是 `candidate_prompt_mode="orthogonal_v2"`。  
如果把所有候选共享的基础模板也单独算一条，那么可以整理成 `1 + 10` 条：

| # | Family | Variant | Prompt Focus | 中文解释 |
| ---: | --- | --- | --- | --- |
| 1 | base | shared_base_prompt | You are a constrained workflow planner.<br>Your task is to convert the user instruction into the minimal executable tool invocation graph<br>Important rules:<br>1.Use only tools from the available skill list.<br>2. Every selected tool must correspond to an explicit user-requested action.<br>3. Do not add optional, helpful, bridge, or intermediate tools unless the user explicitly requires them.<br>4. Do not omit any explicit user-requested action.<br>5. Do not replace one tool with a multi-tool workaround if the exact tool exists.<br>6. Copy user-provided file names, phrases, topics, styles, and parameter values exactly.<br>7. Use <node-i> only when the downstream tool directly consumes the output of node i.<br>8.task nodes must be in execution order.<br>9.task_links must exactly match the <node-i> references in arguments.<br>10. Return JSON only. | 所有 O2 candidate 共用的基础 planner prompt。真正的 family 差异是在这个 base 之上叠加 `Planning strategy`。 |
| 2 | original | baseline | No extra strategy hint. | 原始基线 prompt，不额外加策略提示。 |
| 3 | minimal | fewest_tools | Use the fewest tools possible while still satisfying the explicit request. Collapse optional intermediate steps unless required. | 优先最少工具数；如果中间步骤不是必须，就尽量收缩。 |
| 4 | minimal | fewest_transformations | Minimize the number of transformations. Prefer direct producer-to-consumer paths over multi-hop reformulation. | 优先最少变换次数；减少多跳改写和中间转换。 |
| 5 | action_coverage | strict_explicit_action_coverage | Enumerate every explicit user-requested action internally and ensure each is covered by at least one tool. | 严格覆盖用户显式要求的每个动作，避免漏步骤。 |
| 6 | action_coverage | step_by_step_decomposition | Decompose the request into sequential sub-goals and map each sub-goal to the most direct executable step. | 先拆子目标，再逐步映射工具，强调顺序化分解。 |
| 7 | action_coverage | preserve_every_user_requested_operation | Preserve every user-requested operation even if a shorter workflow exists. | 即使有更短路径，也保留每一个用户明确要求的操作。 |
| 8 | parallel_dag | preserve_independent_branches | Preserve independent branches when parallel downstream use is implied. | 如果请求天然有并行分支，就保留 DAG 分支结构。 |
| 9 | parallel_dag | avoid_forcing_dags_into_chains | Do not linearize independent operations only because modalities match. | 不要因为 modality 能接上，就把独立分支强行串成链。 |
| 10 | dependency_first | semantic_dependency_continuity | Maximize semantic dependency continuity between adjacent steps. | 强调语义依赖连续性，下游节点要接到真正被消费的上游输出。 |
| 11 | parameter_copy | exact_parameter_copy | Copy filenames, styles, phrases, effect names, and parameter values exactly from the user request. | 文件名、style、effect、参数值都尽量逐字拷贝。 |

补充说明：

- `base` 不是单独生成的 candidate family，而是所有 candidate 共用的基础模板。
- `original` 才是第一个真正会生成 candidate 的 family，并且它不额外加 `strategy_hint`。
- 其余 9 个 family 的差异主要体现在 `Planning strategy` 这段提示词。
- 在生成第 2 个及之后的 candidate 时，代码还会额外追加一句“尽量和已有 candidate 结构不同”的 distinctness 提示。

代码来源：

- `taskbench/pipelineOrchastration/run_minimal_rollback_experiment.py`
- `agent/pipeline_orchestrator/planning_mixin.py`
