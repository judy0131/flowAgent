---
name: map_with_bwa_mem
description: WorkflowHub step 'Map with BWA-MEM' generated from toolshed_g2_bx_psu_edu_repos_devteam_bwa_bwa_mem_0_7_17_1.
---

# map_with_bwa_mem

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: ['fastp']

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
