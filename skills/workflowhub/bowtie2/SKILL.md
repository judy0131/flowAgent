---
name: bowtie2
description: [Genomics - SE Variation] WorkflowHub step 'Bowtie2' generated from toolshed_g2_bx_psu_edu_repos_devteam_bowtie2_bowtie2_2_3_4_3+galaxy0. Context: Analysis of variation within individual COVID-19 samples using Illumina Single End data. More info can be found at https://covid19.galaxyproject.org/genomics/
---

# bowtie2

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: ['fastp', 'snpeff_build']

Workflow context:
- workflow_names: ['Genomics - SE Variation']
- source_cwls: ['workflow-8-1.crate/Genomics-4-SE_Variation.cwl']
- more_info_urls: ['https://covid19.galaxyproject.org/genomics/', 'https://workflowhub.eu/workflows/8?version=1']
- doc_summary: Analysis of variation within individual COVID-19 samples using Illumina Single End data. More info can be found at https://covid19.galaxyproject.org/genomics/

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
