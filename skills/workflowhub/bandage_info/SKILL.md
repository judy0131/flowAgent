---
name: bandage_info
description: [Genomics - Assembly of the genome sequence] WorkflowHub step 'Bandage Info' generated from toolshed_g2_bx_psu_edu_repos_iuc_bandage_bandage_info_0_8_1+galaxy1. Context: This workflow uses Illumina and Oxford Nanopore reads that were pre-processed to remove human-derived sequences. Two assembly tools are used: spades and unicycler. In addition to assemblies (actual...
---

# bandage_info

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: ['create_assemblies_with_unicycler', 'spades']

Workflow context:
- workflow_names: ['Genomics - Assembly of the genome sequence']
- source_cwls: ['workflow-5-1.crate/Genomics-2-Assembly.cwl']
- more_info_urls: ['https://covid19.galaxyproject.org/genomics/', 'https://workflowhub.eu/workflows/5?version=1']
- doc_summary: This workflow uses Illumina and Oxford Nanopore reads that were pre-processed to remove human-derived sequences. Two assembly tools are used: spades and unicycler. In addition to assemblies (actual...

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
