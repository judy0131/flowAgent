---
name: fastqc
description: WorkflowHub step 'FastQC' generated from toolshed_g2_bx_psu_edu_repos_devteam_fastqc_fastqc_0_72+galaxy1.
---

# fastqc

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: []

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
