---
name: samtools_fastx
description: WorkflowHub step 'Samtools fastx' generated from toolshed_g2_bx_psu_edu_repos_iuc_samtools_fastx_samtools_fastx_1_9+galaxy1.
---

# samtools_fastx

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: ['mergesamfiles']

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
