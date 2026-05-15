---
name: snpeff_eff
description: [Genomics - PE Variation] WorkflowHub step 'SnpEff eff' generated from toolshed_g2_bx_psu_edu_repos_iuc_snpeff_snpEff_4_3+T_galaxy1. Context: Analysis of variation within individual COVID-19 samples using Illumina Paired End data. More info can be found at https://covid19.galaxyproject.org/genomics/
---

# snpeff_eff

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: ['call_variants', 'snpeff_build']

Workflow context:
- workflow_names: ['Genomics - PE Variation', 'Genomics - SE Variation']
- source_cwls: ['workflow-7-1.crate/Genomics-4-PE_Variation.cwl', 'workflow-8-1.crate/Genomics-4-SE_Variation.cwl']
- more_info_urls: ['https://covid19.galaxyproject.org/genomics/', 'https://workflowhub.eu/workflows/7?version=1', 'https://workflowhub.eu/workflows/8?version=1']
- doc_summary: Analysis of variation within individual COVID-19 samples using Illumina Paired End data. More info can be found at https://covid19.galaxyproject.org/genomics/

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
