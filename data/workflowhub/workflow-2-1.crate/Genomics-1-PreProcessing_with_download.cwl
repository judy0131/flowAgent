class: Workflow
cwlVersion: v1.2.0-dev2
doc: 'Preprocessing of raw SARS-CoV-2 reads. More info can be found at '
inputs:
  0_Input Dataset:
    format: data
    type: File
  1_Input Dataset:
    format: data
    type: File
outputs: {}
steps:
  10_MultiQC:
    in:
      results_0|software_cond|output_0|input: 6_FastQC/text_file
    out:
    - stats
    - html_report
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_iuc_multiqc_multiqc_1_7
      inputs:
        results_0|software_cond|output_0|input:
          format: Any
          type: File
      outputs:
        html_report:
          doc: html
          type: File
        stats:
          doc: input
          type: File
  11_Filter SAM or BAM, output SAM or BAM:
    in:
      input1: 7_Map with minimap2/alignment_output
    out:
    - output1
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_devteam_samtool_filter2_samtool_filter2_1_8+galaxy1
      inputs:
        input1:
          format: Any
          type: File
      outputs:
        output1:
          doc: sam
          type: File
  12_Filter SAM or BAM, output SAM or BAM:
    in:
      input1: 9_Map with BWA-MEM/bam_output
    out:
    - output1
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_devteam_samtool_filter2_samtool_filter2_1_8+galaxy1
      inputs:
        input1:
          format: Any
          type: File
      outputs:
        output1:
          doc: sam
          type: File
  13_MergeSamFiles:
    in:
      inputFile: 11_Filter SAM or BAM, output SAM or BAM/output1
    out:
    - outFile
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_devteam_picard_picard_MergeSamFiles_2_18_2_1
      inputs:
        inputFile:
          format: Any
          type: File
      outputs:
        outFile:
          doc: bam
          type: File
  14_MergeSamFiles:
    in:
      inputFile: 12_Filter SAM or BAM, output SAM or BAM/output1
    out:
    - outFile
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_devteam_picard_picard_MergeSamFiles_2_18_2_1
      inputs:
        inputFile:
          format: Any
          type: File
      outputs:
        outFile:
          doc: bam
          type: File
  15_Samtools fastx:
    in:
      input: 13_MergeSamFiles/outFile
    out:
    - nonspecific
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_iuc_samtools_fastx_samtools_fastx_1_9+galaxy1
      inputs:
        input:
          format: Any
          type: File
      outputs:
        nonspecific:
          doc: fasta
          type: File
  16_Samtools fastx:
    in:
      input: 14_MergeSamFiles/outFile
    out:
    - forward
    - reverse
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_iuc_samtools_fastx_samtools_fastx_1_9+galaxy1
      inputs:
        input:
          format: Any
          type: File
      outputs:
        forward:
          doc: fasta
          type: File
        reverse:
          doc: fasta
          type: File
  2_Faster Download and Extract Reads in FASTQ:
    in:
      input|file_list: 0_Input Dataset
    out:
    - list_paired
    - output_collection
    - output_collection_other
    - log
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_iuc_sra_tools_fasterq_dump_2_10_4+galaxy1
      inputs:
        input|file_list:
          format: Any
          type: File
      outputs:
        list_paired:
          doc: input
          type: File
        log:
          doc: txt
          type: File
        output_collection:
          doc: input
          type: File
        output_collection_other:
          doc: input
          type: File
  3_Faster Download and Extract Reads in FASTQ:
    in:
      input|file_list: 1_Input Dataset
    out:
    - list_paired
    - output_collection
    - output_collection_other
    - log
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_iuc_sra_tools_fasterq_dump_2_10_4+galaxy1
      inputs:
        input|file_list:
          format: Any
          type: File
      outputs:
        list_paired:
          doc: input
          type: File
        log:
          doc: txt
          type: File
        output_collection:
          doc: input
          type: File
        output_collection_other:
          doc: input
          type: File
  4_fastp:
    in:
      single_paired|paired_input: 2_Faster Download and Extract Reads in FASTQ/list_paired
    out:
    - output_paired_coll
    - report_html
    - report_json
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_iuc_fastp_fastp_0_19_5+galaxy1
      inputs:
        single_paired|paired_input:
          format: Any
          type: File
      outputs:
        output_paired_coll:
          doc: input
          type: File
        report_html:
          doc: html
          type: File
        report_json:
          doc: json
          type: File
  5_NanoPlot:
    in:
      mode|reads|files: 3_Faster Download and Extract Reads in FASTQ/output_collection
    out:
    - output_html
    - nanostats
    - nanostats_post_filtering
    - read_length
    - log_read_length
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_iuc_nanoplot_nanoplot_1_25_0+galaxy1
      inputs:
        mode|reads|files:
          format: Any
          type: File
      outputs:
        log_read_length:
          doc: png
          type: File
        nanostats:
          doc: txt
          type: File
        nanostats_post_filtering:
          doc: txt
          type: File
        output_html:
          doc: html
          type: File
        read_length:
          doc: png
          type: File
  6_FastQC:
    in:
      input_file: 3_Faster Download and Extract Reads in FASTQ/output_collection
    out:
    - html_file
    - text_file
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_devteam_fastqc_fastqc_0_72+galaxy1
      inputs:
        input_file:
          format: Any
          type: File
      outputs:
        html_file:
          doc: html
          type: File
        text_file:
          doc: txt
          type: File
  7_Map with minimap2:
    in:
      fastq_input|fastq_input1: 3_Faster Download and Extract Reads in FASTQ/output_collection
    out:
    - alignment_output
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_iuc_minimap2_minimap2_2_17+galaxy0
      inputs:
        fastq_input|fastq_input1:
          format: Any
          type: File
      outputs:
        alignment_output:
          doc: bam
          type: File
  8_MultiQC:
    in:
      results_0|software_cond|input: 4_fastp/report_json
    out:
    - stats
    - html_report
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_iuc_multiqc_multiqc_1_7
      inputs:
        results_0|software_cond|input:
          format: Any
          type: File
      outputs:
        html_report:
          doc: html
          type: File
        stats:
          doc: input
          type: File
  9_Map with BWA-MEM:
    in:
      fastq_input|fastq_input1: 4_fastp/output_paired_coll
    out:
    - bam_output
    run:
      class: Operation
      id: toolshed_g2_bx_psu_edu_repos_devteam_bwa_bwa_mem_0_7_17_1
      inputs:
        fastq_input|fastq_input1:
          format: Any
          type: File
      outputs:
        bam_output:
          doc: bam
          type: File

