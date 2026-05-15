---
name: collapse_collection
description: [Genomics - MRCA analysis] WorkflowHub step 'Collapse Collection' generated from toolshed_g2_bx_psu_edu_repos_nml_collapse_collections_collapse_dataset_4_1. Context: Dating the most recent common ancestor (MRCA) of SARS-CoV-2. The workflow is used to extract full length sequences of SARS-CoV-2, tidy up their names in FASTA files, produce a multiple sequences al...
---

# collapse_collection

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: ['snpsift_extract_fields', 'text_transformation']

Workflow context:
- workflow_names: ['Genomics - MRCA analysis', 'Genomics - PE Variation', 'Genomics - SE Variation']
- source_cwls: ['workflow-6-1.crate/Genomics-3-MRCA.cwl', 'workflow-7-1.crate/Genomics-4-PE_Variation.cwl', 'workflow-8-1.crate/Genomics-4-SE_Variation.cwl']
- more_info_urls: ['https://covid19.galaxyproject.org/genomics/', 'https://workflowhub.eu/workflows/6?version=1', 'https://workflowhub.eu/workflows/7?version=1', 'https://workflowhub.eu/workflows/8?version=1']
- doc_summary: Dating the most recent common ancestor (MRCA) of SARS-CoV-2. The workflow is used to extract full length sequences of SARS-CoV-2, tidy up their names in FASTA files, produce a multiple sequences al...

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
