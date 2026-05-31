# O2 Family Contribution

- Experiment: `20260520_230343 / O2`
- Total cases: `185`
- Badcases (`selected_exact = false`): `102`
- Oracle-better cases: `17`
- Definition:
  - `OracleBestCount`: 该 family / variant 在该 case 上成为 oracle best 的次数
  - `OracleBetterCount`: 只看 `oracle_better=true` 的 case，这个 family / variant 真正把 selected 拉高的次数
  - `UpperBoundMeanQuality`: 每个 case 里只看这个 family / variant 自己时，能达到的平均质量上界
  - `StructureMatch`: 只比较 workflow + edges，不看 node args

## Family-Level Contribution

| Family | OracleBestCount | OracleBestRate | OracleBetterCount | OracleBetterShare | OracleBetterMeanRegret | UpperBoundMeanQuality | DeltaVsOriginal | OracleBestStructureMatchCount | OracleBetterCases |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| original | 168 | 90.8108% | 1 | 5.8824% | 0.3259 | 0.6552 | 0.0000 | 120 | 21338123 |
| minimal | 14 | 7.5676% | 13 | 76.4706% | 0.2395 | 0.6717 | 0.0165 | 5 | 27258164, 46051366, 24192922, 31893844, 50534924, 54951370, 79466668, 79560754, 31788289, 31461277, 11656312, 45875119, 19565758 |
| action_coverage | 2 | 1.0811% | 2 | 11.7647% | 0.2071 | 0.6668 | 0.0116 | 0 | 29292224, 59923748 |
| parallel_dag | 1 | 0.5405% | 1 | 5.8824% | 0.0171 | 0.6697 | 0.0145 | 0 | 24402294 |
| dependency_first | 0 | 0.0000% | 0 | 0.0000% | 0.0000 | 0.6625 | 0.0073 | 0 |  |
| parameter_copy | 0 | 0.0000% | 0 | 0.0000% | 0.0000 | 0.6674 | 0.0122 | 0 |  |

## Variant-Level Contribution

| Variant | OracleBestCount | OracleBetterCount | OracleBetterShare | OracleBetterMeanRegret | UpperBoundMeanQuality | DeltaVsOriginal | OracleBetterCases |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| original/baseline | 168 | 1 | 5.8824% | 0.3259 | 0.6552 | 0.0000 | 21338123 |
| minimal/fewest_tools | 10 | 9 | 52.9412% | 0.2414 | 0.6646 | 0.0094 | 27258164, 46051366, 24192922, 31893844, 50534924, 79560754, 31788289, 11656312, 45875119 |
| minimal/fewest_transformations | 4 | 4 | 23.5294% | 0.2353 | 0.6676 | 0.0124 | 54951370, 79466668, 31461277, 19565758 |
| action_coverage/strict_explicit_action_coverage | 1 | 1 | 5.8824% | 0.1852 | 0.6514 | -0.0038 | 59923748 |
| action_coverage/step_by_step_decomposition | 1 | 1 | 5.8824% | 0.2290 | 0.6625 | 0.0073 | 29292224 |
| action_coverage/preserve_every_user_requested_operation | 0 | 0 | 0.0000% | 0.0000 | 0.6560 | 0.0008 |  |
| parallel_dag/preserve_independent_branches | 0 | 0 | 0.0000% | 0.0000 | 0.6654 | 0.0102 |  |
| parallel_dag/avoid_forcing_dags_into_chains | 1 | 1 | 5.8824% | 0.0171 | 0.6564 | 0.0012 | 24402294 |
| dependency_first/semantic_dependency_continuity | 0 | 0 | 0.0000% | 0.0000 | 0.6625 | 0.0073 |  |
| parameter_copy/exact_parameter_copy | 0 | 0 | 0.0000% | 0.0000 | 0.6674 | 0.0122 |  |

## Quick Read

- `original` 仍然是绝对主力；如果只看 oracle best 覆盖，它赢了绝大多数 case。
- `minimal` 是最主要的补救 family；如果只看 `oracle_better` 子集，它贡献最大。
- `action_coverage` 和 `parallel_dag` 只在少数 hard cases 上提供增量，但这些增量是结构性的，不是噪声。
- `dependency_first` / `parameter_copy` 在这批实验里没有成为 oracle best，但它们的单 family 上界仍然可比较，说明问题更像是 winning power 不足，而不是完全无效。