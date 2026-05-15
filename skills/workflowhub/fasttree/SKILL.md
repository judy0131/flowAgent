---
name: fasttree
description: [Genomics - Recombination and selection analysis] WorkflowHub step 'FASTTREE' generated from toolshed_g2_bx_psu_edu_repos_iuc_fasttree_fasttree_2_1_10+galaxy1. Context: This workflow employs a recombination detection algorithm (GARD) developed by Kosakovsky Pond et al. and implemented in the hyphy package. More info can be found at https://covid19.galaxyproject.or...
---

# fasttree

Inputs:
- source_ref: Upstream artifact key or external input reference
- output_key: Output artifact key to write in context

Dependencies:
- depends_on_any: ['mafft', 'tranalign']

Workflow context:
- workflow_names: ['Genomics - MRCA analysis', 'Genomics - Recombination and selection analysis']
- source_cwls: ['workflow-10-1.crate/Genomics-6-RecombinationSelection.cwl', 'workflow-6-1.crate/Genomics-3-MRCA.cwl']
- more_info_urls: ['https://covid19.galaxyproject.org/genomics/', 'https://workflowhub.eu/workflows/10?version=1', 'https://workflowhub.eu/workflows/6?version=1']
- doc_summary: This workflow employs a recombination detection algorithm (GARD) developed by Kosakovsky Pond et al. and implemented in the hyphy package. More info can be found at https://covid19.galaxyproject.or...

Runtime contract:
- Read from `ctx["artifacts"][source_ref]` when present.
- Write produced artifact to `ctx["artifacts"][output_key]`.
