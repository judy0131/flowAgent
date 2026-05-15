---
name: map_with_minimap2
description: WorkflowHub step 'Map with minimap2' generated from toolshed_g2_bx_psu_edu_repos_iuc_minimap2_minimap2_2_17+galaxy0.
---

# map_with_minimap2

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: []

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
