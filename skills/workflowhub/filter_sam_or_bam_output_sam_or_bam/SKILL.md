---
name: filter_sam_or_bam_output_sam_or_bam
description: [Genomics - Read pre-processing] WorkflowHub step 'Filter SAM or BAM, output SAM or BAM' generated from toolshed_g2_bx_psu_edu_repos_devteam_samtool_filter2_samtool_filter2_1_8+galaxy1. Context: Preprocessing of raw SARS-CoV-2 reads. More info can be found at https://covid19.galaxyproject.org/genomics/
---

# filter_sam_or_bam_output_sam_or_bam

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: ['map_with_bwa_mem', 'map_with_minimap2']

Workflow context:
- workflow_names: ['Genomics - PE Variation', 'Genomics - Read pre-processing', 'Genomics - Read pre-processing without downloading from SRA']
- source_cwls: ['workflow-2-1.crate/Genomics-1-PreProcessing_with_download.cwl', 'workflow-4-1.crate/Genomics-1-PreProcessing_without_downloading_from_SRA.cwl', 'workflow-7-1.crate/Genomics-4-PE_Variation.cwl']
- more_info_urls: ['https://covid19.galaxyproject.org/genomics/', 'https://workflowhub.eu/workflows/2?version=1', 'https://workflowhub.eu/workflows/4?version=1', 'https://workflowhub.eu/workflows/7?version=1']
- doc_summary: Preprocessing of raw SARS-CoV-2 reads. More info can be found at https://covid19.galaxyproject.org/genomics/

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
