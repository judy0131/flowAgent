---
name: samtools_stats
description: [Genomics - PE Variation] WorkflowHub step 'Samtools stats' generated from toolshed_g2_bx_psu_edu_repos_devteam_samtools_stats_samtools_stats_2_0_2+galaxy2. Context: Analysis of variation within individual COVID-19 samples using Illumina Paired End data. More info can be found at https://covid19.galaxyproject.org/genomics/
---

# samtools_stats

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: ['filter_sam_or_bam_output_sam_or_bam']

Workflow context:
- workflow_names: ['Genomics - PE Variation']
- source_cwls: ['workflow-7-1.crate/Genomics-4-PE_Variation.cwl']
- more_info_urls: ['https://covid19.galaxyproject.org/genomics/', 'https://workflowhub.eu/workflows/7?version=1']
- doc_summary: Analysis of variation within individual COVID-19 samples using Illumina Paired End data. More info can be found at https://covid19.galaxyproject.org/genomics/

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
