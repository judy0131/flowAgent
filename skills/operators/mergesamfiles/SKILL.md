---
name: mergesamfiles
description: WorkflowHub step 'MergeSamFiles' generated from toolshed_g2_bx_psu_edu_repos_devteam_picard_picard_MergeSamFiles_2_18_2_1.
---

# mergesamfiles

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: ['filter_sam_or_bam_output_sam_or_bam']

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
