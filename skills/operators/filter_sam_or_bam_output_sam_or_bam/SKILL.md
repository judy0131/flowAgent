---
name: filter_sam_or_bam_output_sam_or_bam
description: WorkflowHub step 'Filter SAM or BAM, output SAM or BAM' generated from toolshed_g2_bx_psu_edu_repos_devteam_samtool_filter2_samtool_filter2_1_8+galaxy1.
---

# filter_sam_or_bam_output_sam_or_bam

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: ['map_with_bwa_mem', 'map_with_minimap2']

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
