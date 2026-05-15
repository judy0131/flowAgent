---
name: tranalign
description: [Genomics - Recombination and selection analysis] WorkflowHub step 'tranalign' generated from toolshed_g2_bx_psu_edu_repos_devteam_emboss_5_EMBOSS_tranalign100_5_0_0. Context: This workflow employs a recombination detection algorithm (GARD) developed by Kosakovsky Pond et al. and implemented in the hyphy package. More info can be found at https://covid19.galaxyproject.or...
---

# tranalign

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: ['mafft']

Workflow context:
- workflow_names: ['Genomics - Analysis of S-protein polymorphism', 'Genomics - Recombination and selection analysis']
- source_cwls: ['workflow-10-1.crate/Genomics-6-RecombinationSelection.cwl', 'workflow-9-1.crate/Genomics-5-S-analysis.cwl']
- more_info_urls: ['https://covid19.galaxyproject.org/genomics/', 'https://workflowhub.eu/workflows/10?version=1', 'https://workflowhub.eu/workflows/9?version=1']
- doc_summary: This workflow employs a recombination detection algorithm (GARD) developed by Kosakovsky Pond et al. and implemented in the hyphy package. More info can be found at https://covid19.galaxyproject.or...

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
