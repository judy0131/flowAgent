class: Workflow
cwlVersion: v1.2.0-dev2
doc: 'This workflow uses Illumina and Oxford Nanopore reads that were pre-processed to remove human-derived sequences. Two assembly tools are used: spades and unicycler. In addition to assemblies (actual sequences) the two tools produce assembly graphs that can be used for visualization of assembly with bandage. More info can be found at https://covid19.galaxyproject.org/genomics/'
inputs:
  0_Input Dataset:
    format: data
    type: File
  1_Input Dataset:
    format: data
    type: File
  2_Input Dataset:
    format: data
    type: File
outputs: {}
steps:
  3_Create assemblies with Unicycler:
    in:
      long: 2_Input Dataset
      paired_unpaired|fastq_input1: 0_Input Dataset
      paired_unpaired|fastq_input2: 1_Input Dataset
    out:
    - assembly_graph
    - assembly
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_iuc_unicycler_unicycler_0_4_6_0
      inputs:
        long:
          format: Any
          type: File
        paired_unpaired|fastq_input1:
          format: Any
          type: File
        paired_unpaired|fastq_input2:
          format: Any
          type: File
      outputs:
        assembly:
          doc: fasta
          type: File
        assembly_graph:
          doc: tabular
          type: File
  4_SPAdes:
    in:
      libraries_0|files_0|file_type|fwd_reads: 0_Input Dataset
      libraries_0|files_0|file_type|rev_reads: 1_Input Dataset
      nanopore_reads: 2_Input Dataset
    out:
    - out_contig_stats
    - out_scaffold_stats
    - out_contigs
    - out_scaffolds
    - out_log
    - contig_graph
    - scaffold_graph
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_nml_spades_spades_3_12_0+galaxy1
      inputs:
        libraries_0|files_0|file_type|fwd_reads:
          format: Any
          type: File
        libraries_0|files_0|file_type|rev_reads:
          format: Any
          type: File
        nanopore_reads:
          format: Any
          type: File
      outputs:
        contig_graph:
          doc: txt
          type: File
        out_contig_stats:
          doc: tabular
          type: File
        out_contigs:
          doc: fasta
          type: File
        out_log:
          doc: txt
          type: File
        out_scaffold_stats:
          doc: tabular
          type: File
        out_scaffolds:
          doc: fasta
          type: File
        scaffold_graph:
          doc: txt
          type: File
  5_Bandage Info:
    in:
      input_file: 3_Create assemblies with Unicycler/assembly_graph
    out:
    - outfile
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_iuc_bandage_bandage_info_0_8_1+galaxy1
      inputs:
        input_file:
          format: Any
          type: File
      outputs:
        outfile:
          doc: txt
          type: File
  6_Bandage Image:
    in:
      input_file: 3_Create assemblies with Unicycler/assembly_graph
    out:
    - outfile
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_iuc_bandage_bandage_image_0_8_1+galaxy2
      inputs:
        input_file:
          format: Any
          type: File
      outputs:
        outfile:
          doc: jpg
          type: File
  7_Bandage Image:
    in:
      input_file: 4_SPAdes/contig_graph
    out:
    - outfile
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_iuc_bandage_bandage_image_0_8_1+galaxy2
      inputs:
        input_file:
          format: Any
          type: File
      outputs:
        outfile:
          doc: jpg
          type: File
  8_Bandage Info:
    in:
      input_file: 4_SPAdes/contig_graph
    out:
    - outfile
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_iuc_bandage_bandage_info_0_8_1+galaxy1
      inputs:
        input_file:
          format: Any
          type: File
      outputs:
        outfile:
          doc: txt
          type: File

