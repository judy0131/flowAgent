---
name: markduplicates
description: [Genomics - PE Variation] WorkflowHub step 'MarkDuplicates' generated from toolshed_g2_bx_psu_edu_repos_devteam_picard_picard_MarkDuplicates_2_18_2_2. Context: Analysis of variation within individual COVID-19 samples using Illumina Paired End data. More info can be found at https://covid19.galaxyproject.org/genomics/
---

# markduplicates

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: ['bowtie2', 'filter_sam_or_bam_output_sam_or_bam']

Workflow context:
- workflow_names: ['Genomics - PE Variation', 'Genomics - SE Variation']
- source_cwls: ['workflow-7-1.crate/Genomics-4-PE_Variation.cwl', 'workflow-8-1.crate/Genomics-4-SE_Variation.cwl']
- more_info_urls: ['https://covid19.galaxyproject.org/genomics/', 'https://workflowhub.eu/workflows/7?version=1', 'https://workflowhub.eu/workflows/8?version=1']
- doc_summary: Analysis of variation within individual COVID-19 samples using Illumina Paired End data. More info can be found at https://covid19.galaxyproject.org/genomics/

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
