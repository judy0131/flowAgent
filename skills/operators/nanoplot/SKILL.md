---
name: nanoplot
description: WorkflowHub step 'NanoPlot' generated from toolshed_g2_bx_psu_edu_repos_iuc_nanoplot_nanoplot_1_25_0+galaxy1.
---

# nanoplot

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: []

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
