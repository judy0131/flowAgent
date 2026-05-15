---
name: multiqc
description: WorkflowHub step 'MultiQC' generated from toolshed_g2_bx_psu_edu_repos_iuc_multiqc_multiqc_1_7.
---

# multiqc

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: ['fastp', 'fastqc']

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
