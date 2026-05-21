| Tag | Label | SuccessfulPredictions | ValidationFailures | Single_nF1 | Single_eF1 | Single_Exact | Chain_nF1 | Chain_eF1 | Chain_Exact | DAG_nF1 | DAG_eF1 | DAG_Exact | Overall_nF1 | Overall_eF1 | Overall_Exact |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | original_single | 182 | 18 | 0.9621 | 0.0000 | 0.6364 | 0.8593 | 0.6601 | 0.3086 | 0.9223 | 0.7372 | 0.1538 | 0.9135 | 0.6711 | 0.4560 |
| B | multi_first_original | 183 | 17 | 0.9621 | 0.0000 | 0.6364 | 0.8578 | 0.6595 | 0.3171 | 0.9223 | 0.7167 | 0.1538 | 0.9126 | 0.6676 | 0.4590 |
| O2 | orthogonal_prompt_candidates_v2 | 185 | 15 | 0.9583 | 0.0000 | 0.6364 | 0.8491 | 0.6296 | 0.2976 | 0.9223 | 0.6859 | 0.1538 | 0.9062 | 0.6374 | 0.4486 |
| O2_oracle | oracle_best_candidate_from_O2_pool |  |  | 0.9621 | 0.0000 | 0.6364 | 0.8666 | 0.7041 | 0.3333 | 0.9223 | 0.7679 | 0.1538 | 0.9160 | 0.7129 | 0.4649 |
| GPT-rerank(O2) | gpt_pairwise_rerank_on_O2_pool |  |  | 0.9621 | 0.0000 | 0.6364 | 0.8631 | 0.6768 | 0.3214 | 0.9223 | 0.7167 | 0.1538 | 0.9144 | 0.6823 | 0.4595 |
