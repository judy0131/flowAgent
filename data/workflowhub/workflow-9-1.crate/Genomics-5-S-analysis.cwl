class: Workflow
cwlVersion: v1.2.0-dev2
doc: 'Analysis of S-protein polymorphism. This workflow includes: obtaining coding sequences of S proteins from a diverse group of coronaviruses and generating amino acid alignments to assess conservation of the polymorphic location. More info can be found at https://covid19.galaxyproject.org/genomics/'
inputs:
  0_Input Dataset:
    format: data
    type: File
outputs: {}
steps:
  1_transeq:
    in:
      input1: 0_Input Dataset
    out:
    - out_file1
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_devteam_emboss_5_EMBOSS_transeq101_5_0_0
      inputs:
        input1:
          format: Any
          type: File
      outputs:
        out_file1:
          doc: fasta
          type: File
  2_MAFFT:
    in:
      inputSequences: 1_transeq/out_file1
    out:
    - outputAlignment
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_rnateam_mafft_rbc_mafft_7_221_3
      inputs:
        inputSequences:
          format: Any
          type: File
      outputs:
        outputAlignment:
          doc: fasta
          type: File
  3_tranalign:
    in:
      input1: 0_Input Dataset
      input2: 2_MAFFT/outputAlignment
    out:
    - out_file1
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_devteam_emboss_5_EMBOSS_tranalign100_5_0_0
      inputs:
        input1:
          format: Any
          type: File
        input2:
          format: Any
          type: File
      outputs:
        out_file1:
          doc: fasta
          type: File

