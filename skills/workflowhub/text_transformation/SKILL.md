---
name: text_transformation
description: [Genomics - MRCA analysis] WorkflowHub step 'Text transformation' generated from toolshed_g2_bx_psu_edu_repos_bgruening_text_processing_tp_sed_tool_1_1_1. Context: Dating the most recent common ancestor (MRCA) of SARS-CoV-2. The workflow is used to extract full length sequences of SARS-CoV-2, tidy up their names in FASTA files, produce a multiple sequences al...
---

# text_transformation

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: ['normalizefasta']

Workflow context:
- workflow_names: ['Genomics - MRCA analysis']
- source_cwls: ['workflow-6-1.crate/Genomics-3-MRCA.cwl']
- more_info_urls: ['https://covid19.galaxyproject.org/genomics/', 'https://workflowhub.eu/workflows/6?version=1']
- doc_summary: Dating the most recent common ancestor (MRCA) of SARS-CoV-2. The workflow is used to extract full length sequences of SARS-CoV-2, tidy up their names in FASTA files, produce a multiple sequences al...

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
