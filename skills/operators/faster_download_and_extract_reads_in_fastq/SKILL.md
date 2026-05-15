---
name: faster_download_and_extract_reads_in_fastq
description: WorkflowHub step 'Faster Download and Extract Reads in FASTQ' generated from toolshed_g2_bx_psu_edu_repos_iuc_sra_tools_fasterq_dump_2_10_4+galaxy1.
---

# faster_download_and_extract_reads_in_fastq

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: []

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
