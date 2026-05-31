# Oracle Strategy Gain Summary

Scope: chain and DAG cases only.

Metrics: node-F1 and edge-F1. No quality score is used.

Oracle setting: for each strategy, compare cached baseline with that strategy's candidate and take the better one per metric. This measures the improvement space that a reranker could recover.

## Chain + DAG Overall

| Strategy | Node-F1 Oracle Gain | Node Wins | Edge-F1 Oracle Gain | Edge Wins |
|---|---:|---:|---:|---:|
| action_coverage | +0.0130 | 228 | +0.0230 | 207 |
| minimal | +0.0125 | 227 | +0.0212 | 200 |
| parallel_dag | +0.0119 | 207 | +0.0216 | 193 |
| typed_dependency | +0.0110 | 201 | +0.0201 | 186 |
| materialization | +0.0104 | 188 | +0.0192 | 176 |

`action_coverage` is the strongest overall strategy. `minimal` is close behind. `parallel_dag` is especially useful for edge recovery.

## Chain

Baseline:

| Metric | Value |
|---|---:|
| node-F1 | 0.8444 |
| edge-F1 | 0.6538 |

| Strategy | Node-F1 Oracle Gain | Node Wins | Edge-F1 Oracle Gain | Edge Wins |
|---|---:|---:|---:|---:|
| action_coverage | +0.0125 | 187 | +0.0227 | 170 |
| minimal | +0.0121 | 183 | +0.0213 | 164 |
| parallel_dag | +0.0112 | 168 | +0.0205 | 154 |
| typed_dependency | +0.0106 | 161 | +0.0199 | 154 |
| materialization | +0.0095 | 144 | +0.0185 | 143 |

For chain cases, `action_coverage` is the best single strategy. `minimal` is very close and should also be kept if the candidate budget allows.

## DAG

Baseline:

| Metric | Value |
|---|---:|
| node-F1 | 0.8385 |
| edge-F1 | 0.6131 |

| Strategy | Node-F1 Oracle Gain | Node Wins | Edge-F1 Oracle Gain | Edge Wins |
|---|---:|---:|---:|---:|
| action_coverage | +0.0159 | 41 | +0.0248 | 37 |
| materialization | +0.0156 | 44 | +0.0231 | 33 |
| parallel_dag | +0.0155 | 39 | +0.0279 | 39 |
| minimal | +0.0148 | 44 | +0.0209 | 36 |
| typed_dependency | +0.0136 | 40 | +0.0212 | 32 |

For DAG cases, `parallel_dag` has the strongest edge-F1 oracle gain. `action_coverage`, `materialization`, and `parallel_dag` are close on node-F1.

## Interpretation

`wins` means the strategy candidate is better than cached baseline on that metric for that many cases.

These gains are oracle gains, not final reranker results. They show that the candidate pool contains better alternatives, but a selector or reranker is still needed to choose them without hurting cases where baseline is better.

## Recommendation

Use cached baseline for single cases.

For chain cases:

```text
baseline + action_coverage + minimal
```

For DAG cases:

```text
baseline + action_coverage + parallel_dag + minimal
```

If keeping all five strategies is affordable, keep them for now. If reducing candidate count, `typed_dependency` and `materialization` have lower marginal value overall, although `materialization` still has some DAG node-F1 value.
