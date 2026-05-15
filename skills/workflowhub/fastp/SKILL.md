---
name: fastp
description: [Genomics - Read pre-processing] WorkflowHub step 'fastp' generated from toolshed_g2_bx_psu_edu_repos_iuc_fastp_fastp_0_19_5+galaxy1. Context: Preprocessing of raw SARS-CoV-2 reads. More info can be found at https://covid19.galaxyproject.org/genomics/
---

# fastp

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: []

Workflow context:
- workflow_names: ['Genomics - PE Variation', 'Genomics - Read pre-processing', 'Genomics - Read pre-processing without downloading from SRA', 'Genomics - SE Variation']
- source_cwls: ['workflow-2-1.crate/Genomics-1-PreProcessing_with_download.cwl', 'workflow-4-1.crate/Genomics-1-PreProcessing_without_downloading_from_SRA.cwl', 'workflow-7-1.crate/Genomics-4-PE_Variation.cwl', 'workflow-8-1.crate/Genomics-4-SE_Variation.cwl']
- more_info_urls: ['https://covid19.galaxyproject.org/genomics/', 'https://workflowhub.eu/workflows/2?version=1', 'https://workflowhub.eu/workflows/4?version=1', 'https://workflowhub.eu/workflows/7?version=1', 'https://workflowhub.eu/workflows/8?version=1']
- doc_summary: Preprocessing of raw SARS-CoV-2 reads. More info can be found at https://covid19.galaxyproject.org/genomics/

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
