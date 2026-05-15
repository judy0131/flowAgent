---
name: faster_download_and_extract_reads_in_fastq
description: [Genomics - Read pre-processing] WorkflowHub step 'Faster Download and Extract Reads in FASTQ' generated from toolshed_g2_bx_psu_edu_repos_iuc_sra_tools_fasterq_dump_2_10_4+galaxy1. Context: Preprocessing of raw SARS-CoV-2 reads. More info can be found at https://covid19.galaxyproject.org/genomics/
---

# faster_download_and_extract_reads_in_fastq

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: []

Workflow context:
- workflow_names: ['Genomics - Read pre-processing']
- source_cwls: ['workflow-2-1.crate/Genomics-1-PreProcessing_with_download.cwl']
- more_info_urls: ['https://covid19.galaxyproject.org/genomics/', 'https://workflowhub.eu/workflows/2?version=1']
- doc_summary: Preprocessing of raw SARS-CoV-2 reads. More info can be found at https://covid19.galaxyproject.org/genomics/

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
