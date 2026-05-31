# O2 Candidate Prompt Families

O2 is configured in `run_minimal_rollback_experiment.py` with `candidate_prompt_mode="orthogonal_v2"`.
The table below lists the candidate families and the extra `Planning strategy` prompt appended for each family.
All candidates still share the same base constrained planner prompt.

| # | Candidate family | Variant | Temperature | Extra planning strategy prompt |
|---:|---|---|---:|---|
| 1 | `original` | `baseline` | model default | Empty prompt. No extra strategy hint is appended. |
| 2 | `minimal` | `fewest_tools` | `0.0` | Use the fewest tools possible while still satisfying the explicit request.<br>Collapse optional intermediate steps unless they are required for correctness.<br>Prefer a shorter workflow over a more descriptive workflow when both are valid. |
| 3 | `minimal` | `fewest_transformations` | `0.05` | Minimize the number of transformations applied to the artifact.<br>Avoid adding rewrites, cleanup, or conversion hops unless the user explicitly requested them.<br>Prefer a direct producer-to-consumer path over multi-hop reformulation. |
| 4 | `action_coverage` | `strict_explicit_action_coverage` | `0.05` | Enumerate every explicit user-requested action internally before planning.<br>Ensure each explicit action is covered by at least one tool.<br>Do not skip search, summarize, transcribe, denoise, combine, generate, or convert when explicitly requested. |
| 5 | `action_coverage` | `step_by_step_decomposition` | `0.1` | Decompose the request into sequential sub-goals before selecting tools.<br>Map each sub-goal to the most direct executable step.<br>Preserve the user-requested operation order when the request implies an order. |
| 6 | `action_coverage` | `preserve_every_user_requested_operation` | `0.12` | Preserve every user-requested operation, even if a shorter workflow exists.<br>Do not compress multiple explicit operations into one semantic shortcut when separate tools are needed to show the requested actions.<br>If the request asks for multiple post-processing operations, keep them explicit in the workflow. |
| 7 | `parallel_dag` | `preserve_independent_branches` | `0.1` | Preserve independent branches when the request implies parallel downstream use of the same artifact.<br>If two downstream tools can consume the same upstream output, allow them to branch instead of forcing a chain.<br>Favor a DAG when multiple outputs or parallel post-processing are requested. |
| 8 | `parallel_dag` | `avoid_forcing_dags_into_chains` | `0.15` | Do not linearize independent operations only because the modalities match.<br>When a branch can terminate independently, keep it independent.<br>Prefer topologies that preserve semantic parallelism instead of inventing unnecessary serial dependencies. |
| 9 | `dependency_first` | `semantic_dependency_continuity` | `0.08` | Maximize semantic dependency continuity between adjacent steps.<br>For each downstream tool, bind it to the upstream node whose output is directly consumed.<br>Do not attach a downstream tool to an earlier node only because the schema superficially fits. |
| 10 | `parameter_copy` | `exact_parameter_copy` | `0.0` | Copy filenames, styles, phrases, effect names, and parameter values exactly from the user request.<br>Do not paraphrase or normalize literal user values unless a tool requires an upstream `<node-i>` reference.<br>Preserve concrete user-provided values even when an abstract paraphrase sounds more natural. |

Source references:

- O2 group config: `taskbench/pipelineOrchastration/run_minimal_rollback_experiment.py`
- Orthogonal v2 strategy specs: `agent/pipeline_orchestrator/planning_mixin.py`
