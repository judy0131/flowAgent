# O2 Typical Badcases (12 cases, 4 per split)

- Experiment: `20260520_230343 / O2`
- Selection rule: from `selected_exact = false` badcases, choose 4 each for `single / chain / dag`; prioritize `oracle-better`, high regret, and representative failure archetypes.
- Each case keeps `gold`, `selected`, `oracle-best`, and all 10 candidate summaries.

## Summary

| Type | CaseId | OracleBetter | Archetype | Selected | Best | Regret | Unique Candidates |
| --- | --- | ---: | --- | --- | --- | ---: | --- |
| single | 11656312 | True | extra_step_recoverable | original/baseline | minimal/fewest_tools | 0.0741 | 3 / 7 |
| single | 13336269 | False | parameter_copy_failure | original/baseline | original/baseline | 0.0000 | 2 / 3 |
| single | 24435782 | False | parameter_normalization_failure | original/baseline | original/baseline | 0.0000 | 1 / 2 |
| single | 96133316 | False | wrong_tool_hallucination | original/baseline | original/baseline | 0.0000 | 1 / 1 |
| chain | 45875119 | True | missing_required_step_recoverable | original/baseline | minimal/fewest_tools | 0.7778 | 2 / 2 |
| chain | 31461277 | True | transformation_order_error | original/baseline | minimal/fewest_transformations | 0.6667 | 2 / 2 |
| chain | 29292224 | True | long_chain_dependency_collapse | original/baseline | action_coverage/step_by_step_decomposition | 0.2290 | 3 / 5 |
| chain | 31788289 | True | extra_terminal_step | original/baseline | minimal/fewest_tools | 0.4903 | 3 / 3 |
| dag | 27258164 | True | edge_direction_error | original/baseline | minimal/fewest_tools | 0.2037 | 2 / 4 |
| dag | 79560754 | True | upstream_binding_error | original/baseline | minimal/fewest_tools | 0.1167 | 2 / 2 |
| dag | 13018270 | False | collapsed_auxiliary_branch | original/baseline | original/baseline | 0.0000 | 1 / 2 |
| dag | 26579656 | False | dag_branch_binding_error | original/baseline | original/baseline | 0.0000 | 1 / 1 |

## SINGLE

### 11656312

- Archetype: `extra_step_recoverable`
- 中文解释: 单工具视频搜索题被扩成两步，平白多出 Video-to-Image；说明模型会把“找视频教程”误解成“搜视频后再抽帧”。
- Oracle better: `True`
- Selection route: `original_dependency_pass`
- Structural / exact unique candidates: `3 / 7`
- Instruction: I have never baked cookies before and I really want to give it a shot. Can you find me a video tutorial on how to bake chocolate chip cookies that are easy to follow?

**Gold**

- Workflow: `Video Search`
- Node args: `[{"task": "Video Search", "arguments": ["beginner-friendly chocolate chip cookies baking tutorial"]}]`

**Selected**

- Candidate: `#1` | `original/baseline`
- Metrics: `quality=0.3704, node_f1=0.6667, edge_f1=, exact=False`
- Workflow: `Video Search -> Video-to-Image`
- Edges: `Video Search -> Video-to-Image`
- Node args: `[{"task": "Video Search", "arguments": ["how to bake chocolate chip cookies"]}, {"task": "Video-to-Image", "arguments": ["example video"]}]`

**Oracle Best**

- Candidate: `#2` | `minimal/fewest_tools`
- Metrics: `quality=0.4444, node_f1=1.0000, edge_f1=, regret=0.0741`
- Workflow: `Video Search`
- Node args: `[{"task": "Video Search", "arguments": ["how to bake chocolate chip cookies tutorial"]}]`

**All 10 Candidates**

| # | Family | Variant | Quality | nF1 | eF1 | Exact | Workflow | Edges |
| ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 1 | original | baseline | 0.3704 | 0.6667 |  | False | Video Search -> Video-to-Image | Video Search -> Video-to-Image |
| 2 | minimal | fewest_tools | 0.4444 | 1.0000 |  | False | Video Search |  |
| 3 | minimal | fewest_transformations | 0.4444 | 1.0000 |  | False | Video Search |  |
| 4 | action_coverage | strict_explicit_action_coverage | 0.3704 | 0.6667 |  | False | Video Search -> Video Search (by Image) | Video Search -> Video Search (by Image) |
| 5 | action_coverage | step_by_step_decomposition | 0.4444 | 1.0000 |  | False | Video Search |  |
| 6 | action_coverage | preserve_every_user_requested_operation | 0.4444 | 1.0000 |  | False | Video Search |  |
| 7 | parallel_dag | preserve_independent_branches | 0.4444 | 1.0000 |  | False | Video Search |  |
| 8 | parallel_dag | avoid_forcing_dags_into_chains | 0.3704 | 0.6667 |  | False | Video Search -> Video Search (by Image) | Video Search -> Video Search (by Image) |
| 9 | dependency_first | semantic_dependency_continuity | 0.4444 | 1.0000 |  | False | Video Search |  |
| 10 | parameter_copy | exact_parameter_copy | 0.4444 | 1.0000 |  | False | Video Search |  |

### 13336269

- Archetype: `parameter_copy_failure`
- 中文解释: 工具选对了，但检索 query 被改写，gold 的 `high-resolution breathtaking sunset` 被弱化成 `a beautiful sunset`，属于参数拷贝失败。
- Oracle better: `False`
- Selection route: `original_dependency_pass`
- Structural / exact unique candidates: `2 / 3`
- Instruction: I'm trying to find a visually striking image for my presentation slide. Can you find me a high-resolution image of a breathtaking sunset?

**Gold**

- Workflow: `Image Search`
- Node args: `[{"task": "Image Search", "arguments": ["high-resolution breathtaking sunset"]}]`

**Selected**

- Candidate: `#1` | `original/baseline`
- Metrics: `quality=0.4444, node_f1=1.0000, edge_f1=, exact=False`
- Workflow: `Image Search`
- Node args: `[{"task": "Image Search", "arguments": ["a beautiful sunset"]}]`

**Oracle Best**

- Candidate: `#1` | `original/baseline`
- Metrics: `quality=0.4444, node_f1=1.0000, edge_f1=, regret=0.0000`
- Workflow: `Image Search`
- Node args: `[{"task": "Image Search", "arguments": ["a beautiful sunset"]}]`

**All 10 Candidates**

| # | Family | Variant | Quality | nF1 | eF1 | Exact | Workflow | Edges |
| ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 1 | original | baseline | 0.4444 | 1.0000 |  | False | Image Search |  |
| 2 | minimal | fewest_tools | 0.4444 | 1.0000 |  | False | Image Search |  |
| 3 | minimal | fewest_transformations | 0.4444 | 1.0000 |  | False | Image Search |  |
| 4 | action_coverage | strict_explicit_action_coverage | 0.4444 | 1.0000 |  | False | Image Search |  |
| 5 | action_coverage | step_by_step_decomposition | 0.4444 | 1.0000 |  | False | Image Search |  |
| 6 | action_coverage | preserve_every_user_requested_operation | 0.4444 | 1.0000 |  | False | Image Search |  |
| 7 | parallel_dag | preserve_independent_branches | 0.4444 | 1.0000 |  | False | Image Search |  |
| 8 | parallel_dag | avoid_forcing_dags_into_chains | 0.3704 | 0.6667 |  | False | Image Search -> Image Downloader | Image Search -> Image Downloader |
| 9 | dependency_first | semantic_dependency_continuity | 0.4444 | 1.0000 |  | False | Image Search |  |
| 10 | parameter_copy | exact_parameter_copy | 0.4444 | 1.0000 |  | False | Image Search |  |

### 24435782

- Archetype: `parameter_normalization_failure`
- 中文解释: 节点正确，但数值参数 `75%` 被改写成长文本说明，属于字面值拷贝 / 参数规范化失败。
- Oracle better: `False`
- Selection route: `original_dependency_pass`
- Structural / exact unique candidates: `1 / 2`
- Instruction: Hey, I was watching this interesting video 'example.mp4', but it's playing a bit too fast for my liking. Can you help me reduce the playback pace to about 75% of its current speed?

**Gold**

- Workflow: `Video Speed Changer`
- Node args: `[{"task": "Video Speed Changer", "arguments": ["example.mp4", "75%"]}]`

**Selected**

- Candidate: `#1` | `original/baseline`
- Metrics: `quality=0.5000, node_f1=1.0000, edge_f1=, exact=False`
- Workflow: `Video Speed Changer`
- Node args: `[{"task": "Video Speed Changer", "arguments": ["example.mp4", "slow down to 75% of its original speed"]}]`

**Oracle Best**

- Candidate: `#1` | `original/baseline`
- Metrics: `quality=0.5000, node_f1=1.0000, edge_f1=, regret=0.0000`
- Workflow: `Video Speed Changer`
- Node args: `[{"task": "Video Speed Changer", "arguments": ["example.mp4", "slow down to 75% of its original speed"]}]`

**All 10 Candidates**

| # | Family | Variant | Quality | nF1 | eF1 | Exact | Workflow | Edges |
| ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 1 | original | baseline | 0.5000 | 1.0000 |  | False | Video Speed Changer |  |
| 2 | minimal | fewest_tools | 0.5000 | 1.0000 |  | False | Video Speed Changer |  |
| 3 | minimal | fewest_transformations | 0.5000 | 1.0000 |  | False | Video Speed Changer |  |
| 4 | action_coverage | strict_explicit_action_coverage | 0.5000 | 1.0000 |  | False | Video Speed Changer |  |
| 5 | action_coverage | step_by_step_decomposition | 0.5000 | 1.0000 |  | False | Video Speed Changer |  |
| 6 | action_coverage | preserve_every_user_requested_operation | 0.5000 | 1.0000 |  | False | Video Speed Changer |  |
| 7 | parallel_dag | preserve_independent_branches | 0.5000 | 1.0000 |  | False | Video Speed Changer |  |
| 8 | parallel_dag | avoid_forcing_dags_into_chains | 0.5000 | 1.0000 |  | False | Video Speed Changer |  |
| 9 | dependency_first | semantic_dependency_continuity | 0.5000 | 1.0000 |  | False | Video Speed Changer |  |
| 10 | parameter_copy | exact_parameter_copy | 0.5000 | 1.0000 |  | False | Video Speed Changer |  |

### 96133316

- Archetype: `wrong_tool_hallucination`
- 中文解释: 把纯文本搜图误判成 `Image Search (by Image)`，还凭空引入 `example.jpg`；属于模态幻觉。
- Oracle better: `False`
- Selection route: `original_dependency_pass`
- Structural / exact unique candidates: `1 / 1`
- Instruction: I'm working on an art project and I need some inspiration. Could you find an image of a breathtaking landscape for me to use as a reference?

**Gold**

- Workflow: `Image Search`
- Node args: `[{"task": "Image Search", "arguments": ["breathtaking landscape"]}]`

**Selected**

- Candidate: `#1` | `original/baseline`
- Metrics: `quality=0.2222, node_f1=0.0000, edge_f1=, exact=False`
- Workflow: `Image Search (by Image)`
- Node args: `[{"task": "Image Search (by Image)", "arguments": ["example.jpg"]}]`

**Oracle Best**

- Candidate: `#1` | `original/baseline`
- Metrics: `quality=0.2222, node_f1=0.0000, edge_f1=, regret=0.0000`
- Workflow: `Image Search (by Image)`
- Node args: `[{"task": "Image Search (by Image)", "arguments": ["example.jpg"]}]`

**All 10 Candidates**

| # | Family | Variant | Quality | nF1 | eF1 | Exact | Workflow | Edges |
| ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 1 | original | baseline | 0.2222 | 0.0000 |  | False | Image Search (by Image) |  |
| 2 | minimal | fewest_tools | 0.2222 | 0.0000 |  | False | Image Search (by Image) |  |
| 3 | minimal | fewest_transformations | 0.2222 | 0.0000 |  | False | Image Search (by Image) |  |
| 4 | action_coverage | strict_explicit_action_coverage | 0.2222 | 0.0000 |  | False | Image Search (by Image) |  |
| 5 | action_coverage | step_by_step_decomposition | 0.2222 | 0.0000 |  | False | Image Search (by Image) |  |
| 6 | action_coverage | preserve_every_user_requested_operation | 0.2222 | 0.0000 |  | False | Image Search (by Image) |  |
| 7 | parallel_dag | preserve_independent_branches | 0.2222 | 0.0000 |  | False | Image Search (by Image) |  |
| 8 | parallel_dag | avoid_forcing_dags_into_chains | 0.2222 | 0.0000 |  | False | Image Search (by Image) |  |
| 9 | dependency_first | semantic_dependency_continuity | 0.2222 | 0.0000 |  | False | Image Search (by Image) |  |
| 10 | parameter_copy | exact_parameter_copy | 0.2222 | 0.0000 |  | False | Image Search (by Image) |  |

## CHAIN

### 45875119

- Archetype: `missing_required_step_recoverable`
- 中文解释: 漏掉用户显式要求的 `Image Colorizer`，中间步骤缺失；candidate pool 里已有多个满分候选。
- Oracle better: `True`
- Selection route: `original_dependency_pass`
- Structural / exact unique candidates: `2 / 2`
- Instruction: I'm analyzing a certain scene from an archival footage which is in black and white and it's in the file named 'example.mp4'. Can you assist me in isolating a frame from this video, colorize the selected frame, and then help me find a similar image but in color?

**Gold**

- Workflow: `Video-to-Image -> Image Colorizer -> Image Search (by Image)`
- Edges: `Video-to-Image -> Image Colorizer; Image Colorizer -> Image Search (by Image)`
- Node args: `[{"task": "Video-to-Image", "arguments": ["example.mp4"]}, {"task": "Image Colorizer", "arguments": ["<node-0>"]}, {"task": "Image Search (by Image)", "arguments": ["<node-1>"]}]`

**Selected**

- Candidate: `#1` | `original/baseline`
- Metrics: `quality=0.2222, node_f1=0.8000, edge_f1=0.0000, exact=False`
- Workflow: `Video-to-Image -> Image Search (by Image)`
- Edges: `Video-to-Image -> Image Search (by Image)`
- Node args: `[{"task": "Video-to-Image", "arguments": ["example.mp4"]}, {"task": "Image Search (by Image)", "arguments": ["<node-0>"]}]`

**Oracle Best**

- Candidate: `#2` | `minimal/fewest_tools`
- Metrics: `quality=1.0000, node_f1=1.0000, edge_f1=1.0000, regret=0.7778`
- Workflow: `Video-to-Image -> Image Colorizer -> Image Search (by Image)`
- Edges: `Video-to-Image -> Image Colorizer; Image Colorizer -> Image Search (by Image)`
- Node args: `[{"task": "Video-to-Image", "arguments": ["example.mp4"]}, {"task": "Image Colorizer", "arguments": ["<node-0>"]}, {"task": "Image Search (by Image)", "arguments": ["<node-1>"]}]`

**All 10 Candidates**

| # | Family | Variant | Quality | nF1 | eF1 | Exact | Workflow | Edges |
| ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 1 | original | baseline | 0.2222 | 0.8000 | 0.0000 | False | Video-to-Image -> Image Search (by Image) | Video-to-Image -> Image Search (by Image) |
| 2 | minimal | fewest_tools | 1.0000 | 1.0000 | 1.0000 | True | Video-to-Image -> Image Colorizer -> Image Search (by Image) | Video-to-Image -> Image Colorizer; Image Colorizer -> Image Search (by Image) |
| 3 | minimal | fewest_transformations | 1.0000 | 1.0000 | 1.0000 | True | Video-to-Image -> Image Colorizer -> Image Search (by Image) | Video-to-Image -> Image Colorizer; Image Colorizer -> Image Search (by Image) |
| 4 | action_coverage | strict_explicit_action_coverage | 0.2222 | 0.8000 | 0.0000 | False | Video-to-Image -> Image Search (by Image) | Video-to-Image -> Image Search (by Image) |
| 5 | action_coverage | step_by_step_decomposition | 1.0000 | 1.0000 | 1.0000 | True | Video-to-Image -> Image Colorizer -> Image Search (by Image) | Video-to-Image -> Image Colorizer; Image Colorizer -> Image Search (by Image) |
| 6 | action_coverage | preserve_every_user_requested_operation | 0.2222 | 0.8000 | 0.0000 | False | Video-to-Image -> Image Search (by Image) | Video-to-Image -> Image Search (by Image) |
| 7 | parallel_dag | preserve_independent_branches | 1.0000 | 1.0000 | 1.0000 | True | Video-to-Image -> Image Colorizer -> Image Search (by Image) | Video-to-Image -> Image Colorizer; Image Colorizer -> Image Search (by Image) |
| 8 | parallel_dag | avoid_forcing_dags_into_chains | 0.2222 | 0.8000 | 0.0000 | False | Video-to-Image -> Image Search (by Image) | Video-to-Image -> Image Search (by Image) |
| 9 | dependency_first | semantic_dependency_continuity | 1.0000 | 1.0000 | 1.0000 | True | Video-to-Image -> Image Colorizer -> Image Search (by Image) | Video-to-Image -> Image Colorizer; Image Colorizer -> Image Search (by Image) |
| 10 | parameter_copy | exact_parameter_copy | 1.0000 | 1.0000 | 1.0000 | True | Video-to-Image -> Image Colorizer -> Image Search (by Image) | Video-to-Image -> Image Colorizer; Image Colorizer -> Image Search (by Image) |

### 31461277

- Archetype: `transformation_order_error`
- 中文解释: 节点都对，但把 `Image Style Transfer` 和 `Image Colorizer` 的顺序做反了，属于变换顺序错误。
- Oracle better: `True`
- Selection route: `original_dependency_pass`
- Structural / exact unique candidates: `2 / 2`
- Instruction: I've come across a cool audio file at this URL: 'https://example.com/audio.wav'. I was thinking, wouldn't it be interesting to transform this audio into a spectrum-like image infused with the visual characteristics of a specific image, say 'example.jpg'? Isn't it possible to make the output even more attractive by colorizing it? And oh, if there is any text on that final image, can it be detected and handed over to me?

**Gold**

- Workflow: `Audio Downloader -> Audio-to-Image -> Image Style Transfer -> Image Colorizer -> Image-to-Text`
- Edges: `Audio Downloader -> Audio-to-Image; Audio-to-Image -> Image Style Transfer; Image Style Transfer -> Image Colorizer; Image Colorizer -> Image-to-Text`
- Node args: `[{"task": "Audio Downloader", "arguments": ["https://example.com/audio.wav"]}, {"task": "Audio-to-Image", "arguments": ["<node-0>"]}, {"task": "Image Style Transfer", "arguments": ["<node-1>", "example.jpg"]}, {"task": "Image Colorizer", "arguments": ["<node-2>"]}, {"task": "Image-to-Text", "arguments": ["<node-3>"]}]`

**Selected**

- Candidate: `#1` | `original/baseline`
- Metrics: `quality=0.3333, node_f1=1.0000, edge_f1=0.2500, exact=False`
- Workflow: `Audio Downloader -> Audio-to-Image -> Image Colorizer -> Image Style Transfer -> Image-to-Text`
- Edges: `Audio Downloader -> Audio-to-Image; Audio-to-Image -> Image Colorizer; Image Colorizer -> Image Style Transfer; Image Style Transfer -> Image-to-Text`
- Node args: `[{"task": "Audio Downloader", "arguments": ["https://example.com/audio.wav"]}, {"task": "Audio-to-Image", "arguments": ["<node-0>"]}, {"task": "Image Colorizer", "arguments": ["<node-1>"]}, {"task": "Image Style Transfer", "arguments": ["<node-2>", "example.jpg"]}, {"task": "Image-to-Text", "arguments": ["<node-3>"]}]`

**Oracle Best**

- Candidate: `#3` | `minimal/fewest_transformations`
- Metrics: `quality=1.0000, node_f1=1.0000, edge_f1=1.0000, regret=0.6667`
- Workflow: `Audio Downloader -> Audio-to-Image -> Image Style Transfer -> Image Colorizer -> Image-to-Text`
- Edges: `Audio Downloader -> Audio-to-Image; Audio-to-Image -> Image Style Transfer; Image Style Transfer -> Image Colorizer; Image Colorizer -> Image-to-Text`
- Node args: `[{"task": "Audio Downloader", "arguments": ["https://example.com/audio.wav"]}, {"task": "Audio-to-Image", "arguments": ["<node-0>"]}, {"task": "Image Style Transfer", "arguments": ["<node-1>", "example.jpg"]}, {"task": "Image Colorizer", "arguments": ["<node-2>"]}, {"task": "Image-to-Text", "arguments": ["<node-3>"]}]`

**All 10 Candidates**

| # | Family | Variant | Quality | nF1 | eF1 | Exact | Workflow | Edges |
| ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 1 | original | baseline | 0.3333 | 1.0000 | 0.2500 | False | Audio Downloader -> Audio-to-Image -> Image Colorizer -> Image Style Transfer -> Image-to-Text | Audio Downloader -> Audio-to-Image; Audio-to-Image -> Image Colorizer; Image Colorizer -> Image Style Transfer; Image Style Transfer -> Image-to-Text |
| 2 | minimal | fewest_tools | 0.3333 | 1.0000 | 0.2500 | False | Audio Downloader -> Audio-to-Image -> Image Colorizer -> Image Style Transfer -> Image-to-Text | Audio Downloader -> Audio-to-Image; Audio-to-Image -> Image Colorizer; Image Colorizer -> Image Style Transfer; Image Style Transfer -> Image-to-Text |
| 3 | minimal | fewest_transformations | 1.0000 | 1.0000 | 1.0000 | True | Audio Downloader -> Audio-to-Image -> Image Style Transfer -> Image Colorizer -> Image-to-Text | Audio Downloader -> Audio-to-Image; Audio-to-Image -> Image Style Transfer; Image Style Transfer -> Image Colorizer; Image Colorizer -> Image-to-Text |
| 4 | action_coverage | strict_explicit_action_coverage | 0.3333 | 1.0000 | 0.2500 | False | Audio Downloader -> Audio-to-Image -> Image Colorizer -> Image Style Transfer -> Image-to-Text | Audio Downloader -> Audio-to-Image; Audio-to-Image -> Image Colorizer; Image Colorizer -> Image Style Transfer; Image Style Transfer -> Image-to-Text |
| 5 | action_coverage | step_by_step_decomposition | 0.3333 | 1.0000 | 0.2500 | False | Audio Downloader -> Audio-to-Image -> Image Colorizer -> Image Style Transfer -> Image-to-Text | Audio Downloader -> Audio-to-Image; Audio-to-Image -> Image Colorizer; Image Colorizer -> Image Style Transfer; Image Style Transfer -> Image-to-Text |
| 6 | action_coverage | preserve_every_user_requested_operation | 0.3333 | 1.0000 | 0.2500 | False | Audio Downloader -> Audio-to-Image -> Image Colorizer -> Image Style Transfer -> Image-to-Text | Audio Downloader -> Audio-to-Image; Audio-to-Image -> Image Colorizer; Image Colorizer -> Image Style Transfer; Image Style Transfer -> Image-to-Text |
| 7 | parallel_dag | preserve_independent_branches | 1.0000 | 1.0000 | 1.0000 | True | Audio Downloader -> Audio-to-Image -> Image Style Transfer -> Image Colorizer -> Image-to-Text | Audio Downloader -> Audio-to-Image; Audio-to-Image -> Image Style Transfer; Image Style Transfer -> Image Colorizer; Image Colorizer -> Image-to-Text |
| 8 | parallel_dag | avoid_forcing_dags_into_chains | 1.0000 | 1.0000 | 1.0000 | True | Audio Downloader -> Audio-to-Image -> Image Style Transfer -> Image Colorizer -> Image-to-Text | Audio Downloader -> Audio-to-Image; Audio-to-Image -> Image Style Transfer; Image Style Transfer -> Image Colorizer; Image Colorizer -> Image-to-Text |
| 9 | dependency_first | semantic_dependency_continuity | 0.3333 | 1.0000 | 0.2500 | False | Audio Downloader -> Audio-to-Image -> Image Colorizer -> Image Style Transfer -> Image-to-Text | Audio Downloader -> Audio-to-Image; Audio-to-Image -> Image Colorizer; Image Colorizer -> Image Style Transfer; Image Style Transfer -> Image-to-Text |
| 10 | parameter_copy | exact_parameter_copy | 1.0000 | 1.0000 | 1.0000 | True | Audio Downloader -> Audio-to-Image -> Image Style Transfer -> Image Colorizer -> Image-to-Text | Audio Downloader -> Audio-to-Image; Audio-to-Image -> Image Style Transfer; Image Style Transfer -> Image Colorizer; Image Colorizer -> Image-to-Text |

### 29292224

- Archetype: `long_chain_dependency_collapse`
- 中文解释: 长链任务里丢了最终 `Text Search`，同时把多分支依赖压扁到错误上游；`action_coverage` 候选明显更接近 gold。
- Oracle better: `True`
- Selection route: `original_dependency_pass`
- Structural / exact unique candidates: `3 / 5`
- Instruction: I'm examining an extensive article on the impacts of climate change on biodiversity. Could you assist me in deciphering the central themes, feelings, and crucial phrases? Could you also simplify, summarise the document and fetch some relevant topics from the web based on the sentiment analysis and keywords from the article? The article's content is 'Climate change is having significant effects on biodiversity...' (followed by a long text).

**Gold**

- Workflow: `Text Simplifier -> Text Summarizer -> Keyword Extractor -> Text Sentiment Analysis -> Topic Generator -> Text Search`
- Edges: `Text Simplifier -> Text Summarizer; Text Summarizer -> Keyword Extractor; Text Summarizer -> Text Sentiment Analysis; Keyword Extractor -> Topic Generator; Text Sentiment Analysis -> Topic Generator; Topic Generator -> Text Search`
- Node args: `[{"task": "Text Simplifier", "arguments": ["Climate change is having significant effects on biodiversity..."]}, {"task": "Text Summarizer", "arguments": ["<node-1>"]}, {"task": "Keyword Extractor", "arguments": ["<node-2>"]}, {"task": "Text Sentiment Analysis", "arguments": ["<node-2>"]}, {"task": "Topic Generator", "arguments": ["<node-3>"]}, {"task": "Text Search", "arguments": ["<node-5>"]}]`

**Selected**

- Candidate: `#1` | `original/baseline`
- Metrics: `quality=0.2020, node_f1=0.9091, edge_f1=0.0000, exact=False`
- Workflow: `Text Summarizer -> Text Simplifier -> Text Sentiment Analysis -> Keyword Extractor -> Topic Generator`
- Edges: `Text Summarizer -> Text Simplifier; Text Simplifier -> Text Sentiment Analysis; Text Simplifier -> Keyword Extractor; Text Simplifier -> Topic Generator`
- Node args: `[{"task": "Text Summarizer", "arguments": ["Climate change is having significant effects on biodiversity..."]}, {"task": "Text Simplifier", "arguments": ["<node-0>"]}, {"task": "Text Sentiment Analysis", "arguments": ["<node-1>"]}, {"task": "Keyword Extractor", "arguments": ["<node-1>"]}, {"task": "Topic Generator", "arguments": ["<node-1>"]}]`

**Oracle Best**

- Candidate: `#5` | `action_coverage/step_by_step_decomposition`
- Metrics: `quality=0.4310, node_f1=0.9091, edge_f1=0.6667, regret=0.2290`
- Workflow: `Text Simplifier -> Text Summarizer -> Text Sentiment Analysis -> Keyword Extractor -> Topic Generator`
- Edges: `Text Simplifier -> Text Summarizer; Text Summarizer -> Text Sentiment Analysis; Text Summarizer -> Keyword Extractor; Text Summarizer -> Topic Generator`
- Node args: `[{"task": "Text Simplifier", "arguments": ["Climate change is having significant effects on biodiversity..."]}, {"task": "Text Summarizer", "arguments": ["<node-0>"]}, {"task": "Text Sentiment Analysis", "arguments": ["<node-1>"]}, {"task": "Keyword Extractor", "arguments": ["<node-1>"]}, {"task": "Topic Generator", "arguments": ["<node-1>"]}]`

**All 10 Candidates**

| # | Family | Variant | Quality | nF1 | eF1 | Exact | Workflow | Edges |
| ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 1 | original | baseline | 0.2020 | 0.9091 | 0.0000 | False | Text Summarizer -> Text Simplifier -> Text Sentiment Analysis -> Keyword Extractor -> Topic Generator | Text Summarizer -> Text Simplifier; Text Simplifier -> Text Sentiment Analysis; Text Simplifier -> Keyword Extractor; Text Simplifier -> Topic Generator |
| 2 | minimal | fewest_tools | 0.4108 | 0.9091 | 0.6667 | False | Text Simplifier -> Text Summarizer -> Text Sentiment Analysis -> Keyword Extractor -> Topic Generator | Text Simplifier -> Text Summarizer; Text Summarizer -> Text Sentiment Analysis; Text Summarizer -> Keyword Extractor; Text Summarizer -> Topic Generator |
| 3 | minimal | fewest_transformations | 0.4108 | 0.9091 | 0.6667 | False | Text Simplifier -> Text Summarizer -> Text Sentiment Analysis -> Keyword Extractor -> Topic Generator | Text Simplifier -> Text Summarizer; Text Summarizer -> Text Sentiment Analysis; Text Summarizer -> Keyword Extractor; Text Summarizer -> Topic Generator |
| 4 | action_coverage | strict_explicit_action_coverage | 0.4108 | 0.9091 | 0.6667 | False | Text Simplifier -> Text Summarizer -> Text Sentiment Analysis -> Keyword Extractor -> Topic Generator | Text Simplifier -> Text Summarizer; Text Summarizer -> Text Sentiment Analysis; Text Summarizer -> Keyword Extractor; Text Summarizer -> Topic Generator |
| 5 | action_coverage | step_by_step_decomposition | 0.4310 | 0.9091 | 0.6667 | False | Text Simplifier -> Text Summarizer -> Text Sentiment Analysis -> Keyword Extractor -> Topic Generator | Text Simplifier -> Text Summarizer; Text Summarizer -> Text Sentiment Analysis; Text Summarizer -> Keyword Extractor; Text Summarizer -> Topic Generator |
| 6 | action_coverage | preserve_every_user_requested_operation | 0.2020 | 0.9091 | 0.0000 | False | Text Summarizer -> Text Simplifier -> Text Sentiment Analysis -> Keyword Extractor -> Topic Generator | Text Summarizer -> Text Simplifier; Text Simplifier -> Text Sentiment Analysis; Text Simplifier -> Keyword Extractor; Text Simplifier -> Topic Generator |
| 7 | parallel_dag | preserve_independent_branches | 0.2020 | 0.9091 | 0.0000 | False | Text Summarizer -> Text Simplifier -> Text Sentiment Analysis -> Keyword Extractor -> Topic Generator | Text Summarizer -> Text Simplifier; Text Simplifier -> Text Sentiment Analysis; Text Simplifier -> Keyword Extractor; Text Simplifier -> Topic Generator |
| 8 | parallel_dag | avoid_forcing_dags_into_chains | 0.2222 | 0.9091 | 0.0000 | False | Text Summarizer -> Text Simplifier -> Text Sentiment Analysis -> Keyword Extractor -> Topic Generator | Text Summarizer -> Topic Generator |
| 9 | dependency_first | semantic_dependency_continuity | 0.2020 | 0.9091 | 0.0000 | False | Text Summarizer -> Text Simplifier -> Text Sentiment Analysis -> Keyword Extractor -> Topic Generator | Text Summarizer -> Text Simplifier; Text Simplifier -> Text Sentiment Analysis; Text Simplifier -> Keyword Extractor; Text Simplifier -> Topic Generator |
| 10 | parameter_copy | exact_parameter_copy | 0.4108 | 0.9091 | 0.6667 | False | Text Simplifier -> Text Summarizer -> Text Sentiment Analysis -> Keyword Extractor -> Topic Generator | Text Simplifier -> Text Summarizer; Text Summarizer -> Text Sentiment Analysis; Text Summarizer -> Keyword Extractor; Text Summarizer -> Topic Generator |

### 31788289

- Archetype: `extra_terminal_step`
- 中文解释: 主链基本正确，但末端额外幻觉出 `Text Translator`，属于过度规划 / 额外步骤。
- Oracle better: `True`
- Selection route: `original_dependency_pass`
- Structural / exact unique candidates: `3 / 3`
- Instruction: I am a speaker who has just delivered a speech which was recorded in the 'example.wav' file. Could you help me transcribe the speech, correct any grammar issues, simplify the language, create a distinct and expanded version of the text, and propose a list of related topics that are in English?

**Gold**

- Workflow: `Audio-to-Text -> Text Grammar Checker -> Text Simplifier -> Article Spinner -> Text Expander -> Topic Generator`
- Edges: `Audio-to-Text -> Text Grammar Checker; Text Grammar Checker -> Text Simplifier; Text Simplifier -> Article Spinner; Article Spinner -> Text Expander; Text Expander -> Topic Generator`
- Node args: `[{"task": "Audio-to-Text", "arguments": ["example.wav"]}, {"task": "Text Grammar Checker", "arguments": ["<node-0>"]}, {"task": "Text Simplifier", "arguments": ["<node-1>"]}, {"task": "Article Spinner", "arguments": ["<node-2>"]}, {"task": "Text Expander", "arguments": ["<node-3>"]}, {"task": "Topic Generator", "arguments": ["<node-4>"]}]`

**Selected**

- Candidate: `#1` | `original/baseline`
- Metrics: `quality=0.5097, node_f1=0.9231, edge_f1=0.9091, exact=False`
- Workflow: `Audio-to-Text -> Text Grammar Checker -> Text Simplifier -> Article Spinner -> Text Expander -> Topic Generator -> Text Translator`
- Edges: `Audio-to-Text -> Text Grammar Checker; Text Grammar Checker -> Text Simplifier; Text Simplifier -> Article Spinner; Article Spinner -> Text Expander; Text Expander -> Topic Generator; Topic Generator -> Text Translator`
- Node args: `[{"task": "Audio-to-Text", "arguments": ["example.wav"]}, {"task": "Text Grammar Checker", "arguments": ["<node-0>"]}, {"task": "Text Simplifier", "arguments": ["<node-1>"]}, {"task": "Article Spinner", "arguments": ["<node-2>"]}, {"task": "Text Expander", "arguments": ["<node-3>"]}, {"task": "Topic Generator", "arguments": ["<node-4>"]}, {"task": "Text Translator", "arguments": ["<node-5>"]}]`

**Oracle Best**

- Candidate: `#2` | `minimal/fewest_tools`
- Metrics: `quality=1.0000, node_f1=1.0000, edge_f1=1.0000, regret=0.4903`
- Workflow: `Audio-to-Text -> Text Grammar Checker -> Text Simplifier -> Article Spinner -> Text Expander -> Topic Generator`
- Edges: `Audio-to-Text -> Text Grammar Checker; Text Grammar Checker -> Text Simplifier; Text Simplifier -> Article Spinner; Article Spinner -> Text Expander; Text Expander -> Topic Generator`
- Node args: `[{"task": "Audio-to-Text", "arguments": ["example.wav"]}, {"task": "Text Grammar Checker", "arguments": ["<node-0>"]}, {"task": "Text Simplifier", "arguments": ["<node-1>"]}, {"task": "Article Spinner", "arguments": ["<node-2>"]}, {"task": "Text Expander", "arguments": ["<node-3>"]}, {"task": "Topic Generator", "arguments": ["<node-4>"]}]`

**All 10 Candidates**

| # | Family | Variant | Quality | nF1 | eF1 | Exact | Workflow | Edges |
| ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 1 | original | baseline | 0.5097 | 0.9231 | 0.9091 | False | Audio-to-Text -> Text Grammar Checker -> Text Simplifier -> Article Spinner -> Text Expander -> Topic Generator -> Text Translator | Audio-to-Text -> Text Grammar Checker; Text Grammar Checker -> Text Simplifier; Text Simplifier -> Article Spinner; Article Spinner -> Text Expander; Text Expander -> Topic Generator; Topic Generator -> Text Translator |
| 2 | minimal | fewest_tools | 1.0000 | 1.0000 | 1.0000 | True | Audio-to-Text -> Text Grammar Checker -> Text Simplifier -> Article Spinner -> Text Expander -> Topic Generator | Audio-to-Text -> Text Grammar Checker; Text Grammar Checker -> Text Simplifier; Text Simplifier -> Article Spinner; Article Spinner -> Text Expander; Text Expander -> Topic Generator |
| 3 | minimal | fewest_transformations | 1.0000 | 1.0000 | 1.0000 | True | Audio-to-Text -> Text Grammar Checker -> Text Simplifier -> Article Spinner -> Text Expander -> Topic Generator | Audio-to-Text -> Text Grammar Checker; Text Grammar Checker -> Text Simplifier; Text Simplifier -> Article Spinner; Article Spinner -> Text Expander; Text Expander -> Topic Generator |
| 4 | action_coverage | strict_explicit_action_coverage | 0.5097 | 0.9231 | 0.9091 | False | Audio-to-Text -> Text Grammar Checker -> Text Simplifier -> Article Spinner -> Text Expander -> Topic Generator -> Text Translator | Audio-to-Text -> Text Grammar Checker; Text Grammar Checker -> Text Simplifier; Text Simplifier -> Article Spinner; Article Spinner -> Text Expander; Text Expander -> Topic Generator; Topic Generator -> Text Translator |
| 5 | action_coverage | step_by_step_decomposition | 0.4522 | 0.9231 | 0.7273 | False | Audio-to-Text -> Text Grammar Checker -> Text Simplifier -> Article Spinner -> Text Expander -> Text Translator -> Topic Generator | Audio-to-Text -> Text Grammar Checker; Text Grammar Checker -> Text Simplifier; Text Simplifier -> Article Spinner; Article Spinner -> Text Expander; Text Expander -> Text Translator; Text Translator -> Topic Generator |
| 6 | action_coverage | preserve_every_user_requested_operation | 0.5097 | 0.9231 | 0.9091 | False | Audio-to-Text -> Text Grammar Checker -> Text Simplifier -> Article Spinner -> Text Expander -> Topic Generator -> Text Translator | Audio-to-Text -> Text Grammar Checker; Text Grammar Checker -> Text Simplifier; Text Simplifier -> Article Spinner; Article Spinner -> Text Expander; Text Expander -> Topic Generator; Topic Generator -> Text Translator |
| 7 | parallel_dag | preserve_independent_branches | 0.5097 | 0.9231 | 0.9091 | False | Audio-to-Text -> Text Grammar Checker -> Text Simplifier -> Article Spinner -> Text Expander -> Topic Generator -> Text Translator | Audio-to-Text -> Text Grammar Checker; Text Grammar Checker -> Text Simplifier; Text Simplifier -> Article Spinner; Article Spinner -> Text Expander; Text Expander -> Topic Generator; Topic Generator -> Text Translator |
| 8 | parallel_dag | avoid_forcing_dags_into_chains | 1.0000 | 1.0000 | 1.0000 | True | Audio-to-Text -> Text Grammar Checker -> Text Simplifier -> Article Spinner -> Text Expander -> Topic Generator | Audio-to-Text -> Text Grammar Checker; Text Grammar Checker -> Text Simplifier; Text Simplifier -> Article Spinner; Article Spinner -> Text Expander; Text Expander -> Topic Generator |
| 9 | dependency_first | semantic_dependency_continuity | 0.5097 | 0.9231 | 0.9091 | False | Audio-to-Text -> Text Grammar Checker -> Text Simplifier -> Article Spinner -> Text Expander -> Topic Generator -> Text Translator | Audio-to-Text -> Text Grammar Checker; Text Grammar Checker -> Text Simplifier; Text Simplifier -> Article Spinner; Article Spinner -> Text Expander; Text Expander -> Topic Generator; Topic Generator -> Text Translator |
| 10 | parameter_copy | exact_parameter_copy | 0.4522 | 0.9231 | 0.7273 | False | Audio-to-Text -> Text Grammar Checker -> Text Simplifier -> Article Spinner -> Text Expander -> Text Translator -> Topic Generator | Audio-to-Text -> Text Grammar Checker; Text Grammar Checker -> Text Simplifier; Text Simplifier -> Article Spinner; Article Spinner -> Text Expander; Text Expander -> Text Translator; Text Translator -> Topic Generator |

## DAG

### 27258164

- Archetype: `edge_direction_error`
- 中文解释: DAG/chain 边方向错误，先降噪再变声，导致首段依赖倒置；`minimal` 候选能修正。
- Oracle better: `True`
- Selection route: `original_dependency_pass`
- Structural / exact unique candidates: `2 / 4`
- Instruction: I've recorded a podcast and there's a section where I attempted to mimic a female voice, but it doesn't sound quite right on my 'example.wav' file. Could you help me modify that section of the audio to actually sound like a female voice, and while you're at it, could you get rid of all the background noise? Then, I'd like to visualize what the cleaned and edited audio looks like as a waveform image. Better yet, combine this waveform image with my podcast logo 'example.jpg' into a snazzy little slideshow video. Sounds cool?

**Gold**

- Workflow: `Voice Changer -> Audio Noise Reduction -> Audio-to-Image -> Image-to-Video`
- Edges: `Voice Changer -> Audio Noise Reduction; Audio Noise Reduction -> Audio-to-Image; Audio-to-Image -> Image-to-Video`
- Node args: `[{"task": "Voice Changer", "arguments": ["example.wav", "female"]}, {"task": "Audio Noise Reduction", "arguments": ["<node-0>"]}, {"task": "Audio-to-Image", "arguments": ["<node-1>"]}, {"task": "Image-to-Video", "arguments": ["<node-2>", "example.jpg"]}]`

**Selected**

- Candidate: `#1` | `original/baseline`
- Metrics: `quality=0.3333, node_f1=1.0000, edge_f1=0.3333, exact=False`
- Workflow: `Audio Noise Reduction -> Voice Changer -> Audio-to-Image -> Image-to-Video`
- Edges: `Audio Noise Reduction -> Voice Changer; Voice Changer -> Audio-to-Image; Audio-to-Image -> Image-to-Video`
- Node args: `[{"task": "Audio Noise Reduction", "arguments": ["example.wav"]}, {"task": "Voice Changer", "arguments": ["<node-0>", "make it sound like a female voice"]}, {"task": "Audio-to-Image", "arguments": ["<node-1>"]}, {"task": "Image-to-Video", "arguments": ["<node-2>", "example.jpg"]}]`

**Oracle Best**

- Candidate: `#2` | `minimal/fewest_tools`
- Metrics: `quality=0.5370, node_f1=1.0000, edge_f1=1.0000, regret=0.2037`
- Workflow: `Voice Changer -> Audio Noise Reduction -> Audio-to-Image -> Image-to-Video`
- Edges: `Voice Changer -> Audio Noise Reduction; Audio Noise Reduction -> Audio-to-Image; Audio-to-Image -> Image-to-Video`
- Node args: `[{"task": "Voice Changer", "arguments": ["example.wav", "make it sound like a female voice"]}, {"task": "Audio Noise Reduction", "arguments": ["<node-0>"]}, {"task": "Audio-to-Image", "arguments": ["<node-1>"]}, {"task": "Image-to-Video", "arguments": ["<node-2>", "example.jpg"]}]`

**All 10 Candidates**

| # | Family | Variant | Quality | nF1 | eF1 | Exact | Workflow | Edges |
| ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 1 | original | baseline | 0.3333 | 1.0000 | 0.3333 | False | Audio Noise Reduction -> Voice Changer -> Audio-to-Image -> Image-to-Video | Audio Noise Reduction -> Voice Changer; Voice Changer -> Audio-to-Image; Audio-to-Image -> Image-to-Video |
| 2 | minimal | fewest_tools | 0.5370 | 1.0000 | 1.0000 | False | Voice Changer -> Audio Noise Reduction -> Audio-to-Image -> Image-to-Video | Voice Changer -> Audio Noise Reduction; Audio Noise Reduction -> Audio-to-Image; Audio-to-Image -> Image-to-Video |
| 3 | minimal | fewest_transformations | 0.5370 | 1.0000 | 1.0000 | False | Voice Changer -> Audio Noise Reduction -> Audio-to-Image -> Image-to-Video | Voice Changer -> Audio Noise Reduction; Audio Noise Reduction -> Audio-to-Image; Audio-to-Image -> Image-to-Video |
| 4 | action_coverage | strict_explicit_action_coverage | 0.3333 | 1.0000 | 0.3333 | False | Audio Noise Reduction -> Voice Changer -> Audio-to-Image -> Image-to-Video | Audio Noise Reduction -> Voice Changer; Voice Changer -> Audio-to-Image; Audio-to-Image -> Image-to-Video |
| 5 | action_coverage | step_by_step_decomposition | 0.3333 | 1.0000 | 0.3333 | False | Audio Noise Reduction -> Voice Changer -> Audio-to-Image -> Image-to-Video | Audio Noise Reduction -> Voice Changer; Voice Changer -> Audio-to-Image; Audio-to-Image -> Image-to-Video |
| 6 | action_coverage | preserve_every_user_requested_operation | 0.5370 | 1.0000 | 1.0000 | False | Voice Changer -> Audio Noise Reduction -> Audio-to-Image -> Image-to-Video | Voice Changer -> Audio Noise Reduction; Audio Noise Reduction -> Audio-to-Image; Audio-to-Image -> Image-to-Video |
| 7 | parallel_dag | preserve_independent_branches | 0.3333 | 1.0000 | 0.3333 | False | Audio Noise Reduction -> Voice Changer -> Audio-to-Image -> Image-to-Video | Audio Noise Reduction -> Voice Changer; Voice Changer -> Audio-to-Image; Audio-to-Image -> Image-to-Video |
| 8 | parallel_dag | avoid_forcing_dags_into_chains | 0.3333 | 1.0000 | 0.3333 | False | Audio Noise Reduction -> Voice Changer -> Audio-to-Image -> Image-to-Video | Audio Noise Reduction -> Voice Changer; Voice Changer -> Audio-to-Image; Audio-to-Image -> Image-to-Video |
| 9 | dependency_first | semantic_dependency_continuity | 0.5370 | 1.0000 | 1.0000 | False | Voice Changer -> Audio Noise Reduction -> Audio-to-Image -> Image-to-Video | Voice Changer -> Audio Noise Reduction; Audio Noise Reduction -> Audio-to-Image; Audio-to-Image -> Image-to-Video |
| 10 | parameter_copy | exact_parameter_copy | 0.5370 | 1.0000 | 1.0000 | False | Voice Changer -> Audio Noise Reduction -> Audio-to-Image -> Image-to-Video | Voice Changer -> Audio Noise Reduction; Audio Noise Reduction -> Audio-to-Image; Audio-to-Image -> Image-to-Video |

### 79560754

- Archetype: `upstream_binding_error`
- 中文解释: 节点都对，但 `Topic Generator` 和 `Text-to-Video` 都绑到了 grammar 输出，而不是 summary/topic 分支；属于上游绑定错误。
- Oracle better: `True`
- Selection route: `original_dependency_pass`
- Structural / exact unique candidates: `2 / 2`
- Instruction: I've written a piece on environmental conservation: 'Environmntal conservation is verry important to save our planet. Ther are many ways to protect the natur world, like recycling, reducin water waste, nd planting trees.' I'd like to bring my thoughts out more vividly, could you refine its grammar, summarize it, brainstorm some related topical ideas and eventually create a video to bring the message to life?

**Gold**

- Workflow: `Text Grammar Checker -> Text Summarizer -> Topic Generator -> Text-to-Video`
- Edges: `Text Grammar Checker -> Text Summarizer; Text Summarizer -> Text-to-Video; Topic Generator -> Text-to-Video`
- Node args: `[{"task": "Text Grammar Checker", "arguments": ["Environmntal conservation is verry important to save our planet. Ther are many ways to protect the natur world, like recycling, reducin water waste, nd planting trees."]}, {"task": "Text Summarizer", "arguments": ["<node-0>"]}, {"task": "Topic Generator", "arguments": ["environmental conservation"]}, {"task": "Text-to-Video", "arguments": ["<node-1>"]}]`

**Selected**

- Candidate: `#1` | `original/baseline`
- Metrics: `quality=0.3667, node_f1=1.0000, edge_f1=0.4000, exact=False`
- Workflow: `Text Grammar Checker -> Text Summarizer -> Topic Generator -> Text-to-Video`
- Edges: `Text Grammar Checker -> Text Summarizer; Text Grammar Checker -> Topic Generator; Text Grammar Checker -> Text-to-Video`
- Node args: `[{"task": "Text Grammar Checker", "arguments": ["Environmntal conservation is verry important to save our planet. Ther are many ways to protect the natur world, like recycling, reducin water waste, nd planting trees."]}, {"task": "Text Summarizer", "arguments": ["<node-0>"]}, {"task": "Topic Generator", "arguments": ["<node-0>"]}, {"task": "Text-to-Video", "arguments": ["<node-0>"]}]`

**Oracle Best**

- Candidate: `#2` | `minimal/fewest_tools`
- Metrics: `quality=0.4833, node_f1=1.0000, edge_f1=0.8000, regret=0.1167`
- Workflow: `Text Grammar Checker -> Text Summarizer -> Topic Generator -> Text-to-Video`
- Edges: `Text Grammar Checker -> Text Summarizer; Text Grammar Checker -> Topic Generator; Text Summarizer -> Text-to-Video`
- Node args: `[{"task": "Text Grammar Checker", "arguments": ["Environmntal conservation is verry important to save our planet. Ther are many ways to protect the natur world, like recycling, reducin water waste, nd planting trees."]}, {"task": "Text Summarizer", "arguments": ["<node-0>"]}, {"task": "Topic Generator", "arguments": ["<node-0>"]}, {"task": "Text-to-Video", "arguments": ["<node-1>"]}]`

**All 10 Candidates**

| # | Family | Variant | Quality | nF1 | eF1 | Exact | Workflow | Edges |
| ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 1 | original | baseline | 0.3667 | 1.0000 | 0.4000 | False | Text Grammar Checker -> Text Summarizer -> Topic Generator -> Text-to-Video | Text Grammar Checker -> Text Summarizer; Text Grammar Checker -> Topic Generator; Text Grammar Checker -> Text-to-Video |
| 2 | minimal | fewest_tools | 0.4833 | 1.0000 | 0.8000 | False | Text Grammar Checker -> Text Summarizer -> Topic Generator -> Text-to-Video | Text Grammar Checker -> Text Summarizer; Text Grammar Checker -> Topic Generator; Text Summarizer -> Text-to-Video |
| 3 | minimal | fewest_transformations | 0.4833 | 1.0000 | 0.8000 | False | Text Grammar Checker -> Text Summarizer -> Topic Generator -> Text-to-Video | Text Grammar Checker -> Text Summarizer; Text Grammar Checker -> Topic Generator; Text Summarizer -> Text-to-Video |
| 4 | action_coverage | strict_explicit_action_coverage | 0.3667 | 1.0000 | 0.4000 | False | Text Grammar Checker -> Text Summarizer -> Topic Generator -> Text-to-Video | Text Grammar Checker -> Text Summarizer; Text Grammar Checker -> Topic Generator; Text Grammar Checker -> Text-to-Video |
| 5 | action_coverage | step_by_step_decomposition | 0.3667 | 1.0000 | 0.4000 | False | Text Grammar Checker -> Text Summarizer -> Topic Generator -> Text-to-Video | Text Grammar Checker -> Text Summarizer; Text Grammar Checker -> Topic Generator; Text Grammar Checker -> Text-to-Video |
| 6 | action_coverage | preserve_every_user_requested_operation | 0.3667 | 1.0000 | 0.4000 | False | Text Grammar Checker -> Text Summarizer -> Topic Generator -> Text-to-Video | Text Grammar Checker -> Text Summarizer; Text Grammar Checker -> Topic Generator; Text Grammar Checker -> Text-to-Video |
| 7 | parallel_dag | preserve_independent_branches | 0.3667 | 1.0000 | 0.4000 | False | Text Grammar Checker -> Text Summarizer -> Topic Generator -> Text-to-Video | Text Grammar Checker -> Text Summarizer; Text Grammar Checker -> Topic Generator; Text Grammar Checker -> Text-to-Video |
| 8 | parallel_dag | avoid_forcing_dags_into_chains | 0.3667 | 1.0000 | 0.4000 | False | Text Grammar Checker -> Text Summarizer -> Topic Generator -> Text-to-Video | Text Grammar Checker -> Text Summarizer; Text Grammar Checker -> Topic Generator; Text Grammar Checker -> Text-to-Video |
| 9 | dependency_first | semantic_dependency_continuity | 0.3667 | 1.0000 | 0.4000 | False | Text Grammar Checker -> Text Summarizer -> Topic Generator -> Text-to-Video | Text Grammar Checker -> Text Summarizer; Text Grammar Checker -> Topic Generator; Text Grammar Checker -> Text-to-Video |
| 10 | parameter_copy | exact_parameter_copy | 0.3667 | 1.0000 | 0.4000 | False | Text Grammar Checker -> Text Summarizer -> Topic Generator -> Text-to-Video | Text Grammar Checker -> Text Summarizer; Text Grammar Checker -> Topic Generator; Text Grammar Checker -> Text-to-Video |

### 13018270

- Archetype: `collapsed_auxiliary_branch`
- 中文解释: 把 effect 描述直接塞进 `Audio Effects`，漏掉 gold 中的 `Text Simplifier` 支路；说明模型会压缩辅助文本分支。
- Oracle better: `False`
- Selection route: `original_dependency_pass`
- Structural / exact unique candidates: `1 / 2`
- Instruction: I'm working on a personal project and I've recorded two separate audio clips, 'example.wav' and 'example2.wav'. To create a smooth sequence, I'd like to merge them. Additionally, to spice things up, can we enhance the resulting audio by adding a reverb effect with a 2-second decay and use an equalizer to amplify the bass frequencies by 3dB? Could you make sure the instructions are understandable enough for my software tools?

**Gold**

- Workflow: `Text Simplifier -> Audio Splicer -> Audio Effects`
- Edges: `Text Simplifier -> Audio Effects; Audio Splicer -> Audio Effects`
- Node args: `[{"task": "Text Simplifier", "arguments": ["Apply a reverb effect with a 2-second decay, and use equalizer to boost the bass frequencies by 3dB."]}, {"task": "Audio Splicer", "arguments": ["example.wav", "example2.wav"]}, {"task": "Audio Effects", "arguments": ["<node-2>", "<node-1>"]}]`

**Selected**

- Candidate: `#1` | `original/baseline`
- Metrics: `quality=0.4741, node_f1=0.8000, edge_f1=1.0000, exact=False`
- Workflow: `Audio Splicer -> Audio Effects`
- Edges: `Audio Splicer -> Audio Effects`
- Node args: `[{"task": "Audio Splicer", "arguments": ["example.wav", "example2.wav"]}, {"task": "Audio Effects", "arguments": ["<node-0>", "Add a reverb effect with a 2-second decay, followed by an equalization boosting the bass frequencies by 3dB"]}]`

**Oracle Best**

- Candidate: `#1` | `original/baseline`
- Metrics: `quality=0.4741, node_f1=0.8000, edge_f1=1.0000, regret=0.0000`
- Workflow: `Audio Splicer -> Audio Effects`
- Edges: `Audio Splicer -> Audio Effects`
- Node args: `[{"task": "Audio Splicer", "arguments": ["example.wav", "example2.wav"]}, {"task": "Audio Effects", "arguments": ["<node-0>", "Add a reverb effect with a 2-second decay, followed by an equalization boosting the bass frequencies by 3dB"]}]`

**All 10 Candidates**

| # | Family | Variant | Quality | nF1 | eF1 | Exact | Workflow | Edges |
| ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 1 | original | baseline | 0.4741 | 0.8000 | 1.0000 | False | Audio Splicer -> Audio Effects | Audio Splicer -> Audio Effects |
| 2 | minimal | fewest_tools | 0.4741 | 0.8000 | 1.0000 | False | Audio Splicer -> Audio Effects | Audio Splicer -> Audio Effects |
| 3 | minimal | fewest_transformations | 0.4741 | 0.8000 | 1.0000 | False | Audio Splicer -> Audio Effects | Audio Splicer -> Audio Effects |
| 4 | action_coverage | strict_explicit_action_coverage | 0.4741 | 0.8000 | 1.0000 | False | Audio Splicer -> Audio Effects | Audio Splicer -> Audio Effects |
| 5 | action_coverage | step_by_step_decomposition | 0.4741 | 0.8000 | 1.0000 | False | Audio Splicer -> Audio Effects | Audio Splicer -> Audio Effects |
| 6 | action_coverage | preserve_every_user_requested_operation | 0.4741 | 0.8000 | 1.0000 | False | Audio Splicer -> Audio Effects | Audio Splicer -> Audio Effects |
| 7 | parallel_dag | preserve_independent_branches | 0.4741 | 0.8000 | 1.0000 | False | Audio Splicer -> Audio Effects | Audio Splicer -> Audio Effects |
| 8 | parallel_dag | avoid_forcing_dags_into_chains | 0.4741 | 0.8000 | 1.0000 | False | Audio Splicer -> Audio Effects | Audio Splicer -> Audio Effects |
| 9 | dependency_first | semantic_dependency_continuity | 0.4741 | 0.8000 | 1.0000 | False | Audio Splicer -> Audio Effects | Audio Splicer -> Audio Effects |
| 10 | parameter_copy | exact_parameter_copy | 0.4741 | 0.8000 | 1.0000 | False | Audio Splicer -> Audio Effects | Audio Splicer -> Audio Effects |

### 26579656

- Archetype: `dag_branch_binding_error`
- 中文解释: 节点都对，但 `Video-to-Image` 接成了 stabilized video，而不是 original download 分支；是典型 DAG 分支绑定错误。
- Oracle better: `False`
- Selection route: `original_dependency_pass`
- Structural / exact unique candidates: `1 / 1`
- Instruction: I have recently stumbled upon an interesting video online with URL 'example.mp4' that I'd like to use for my project. Could you assist me in downloading this and ensure any unstable elements are stabilized? I would also require an audio file from this video and a single still image captured at a crucial moment.

**Gold**

- Workflow: `Video Downloader -> Video Stabilizer -> Video-to-Audio -> Video-to-Image`
- Edges: `Video Downloader -> Video Stabilizer; Video Stabilizer -> Video-to-Audio; Video Downloader -> Video-to-Image`
- Node args: `[{"task": "Video Downloader", "arguments": ["example.mp4"]}, {"task": "Video Stabilizer", "arguments": ["<node-0>"]}, {"task": "Video-to-Audio", "arguments": ["<node-1>"]}, {"task": "Video-to-Image", "arguments": ["<node-0>"]}]`

**Selected**

- Candidate: `#1` | `original/baseline`
- Metrics: `quality=0.4537, node_f1=1.0000, edge_f1=0.6667, exact=False`
- Workflow: `Video Downloader -> Video Stabilizer -> Video-to-Audio -> Video-to-Image`
- Edges: `Video Downloader -> Video Stabilizer; Video Stabilizer -> Video-to-Audio; Video Stabilizer -> Video-to-Image`
- Node args: `[{"task": "Video Downloader", "arguments": ["example.mp4"]}, {"task": "Video Stabilizer", "arguments": ["<node-0>"]}, {"task": "Video-to-Audio", "arguments": ["<node-1>"]}, {"task": "Video-to-Image", "arguments": ["<node-1>"]}]`

**Oracle Best**

- Candidate: `#1` | `original/baseline`
- Metrics: `quality=0.4537, node_f1=1.0000, edge_f1=0.6667, regret=0.0000`
- Workflow: `Video Downloader -> Video Stabilizer -> Video-to-Audio -> Video-to-Image`
- Edges: `Video Downloader -> Video Stabilizer; Video Stabilizer -> Video-to-Audio; Video Stabilizer -> Video-to-Image`
- Node args: `[{"task": "Video Downloader", "arguments": ["example.mp4"]}, {"task": "Video Stabilizer", "arguments": ["<node-0>"]}, {"task": "Video-to-Audio", "arguments": ["<node-1>"]}, {"task": "Video-to-Image", "arguments": ["<node-1>"]}]`

**All 10 Candidates**

| # | Family | Variant | Quality | nF1 | eF1 | Exact | Workflow | Edges |
| ---: | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| 1 | original | baseline | 0.4537 | 1.0000 | 0.6667 | False | Video Downloader -> Video Stabilizer -> Video-to-Audio -> Video-to-Image | Video Downloader -> Video Stabilizer; Video Stabilizer -> Video-to-Audio; Video Stabilizer -> Video-to-Image |
| 2 | minimal | fewest_tools | 0.4537 | 1.0000 | 0.6667 | False | Video Downloader -> Video Stabilizer -> Video-to-Audio -> Video-to-Image | Video Downloader -> Video Stabilizer; Video Stabilizer -> Video-to-Audio; Video Stabilizer -> Video-to-Image |
| 3 | minimal | fewest_transformations | 0.4537 | 1.0000 | 0.6667 | False | Video Downloader -> Video Stabilizer -> Video-to-Audio -> Video-to-Image | Video Downloader -> Video Stabilizer; Video Stabilizer -> Video-to-Audio; Video Stabilizer -> Video-to-Image |
| 4 | action_coverage | strict_explicit_action_coverage | 0.4537 | 1.0000 | 0.6667 | False | Video Downloader -> Video Stabilizer -> Video-to-Audio -> Video-to-Image | Video Downloader -> Video Stabilizer; Video Stabilizer -> Video-to-Audio; Video Stabilizer -> Video-to-Image |
| 5 | action_coverage | step_by_step_decomposition | 0.4537 | 1.0000 | 0.6667 | False | Video Downloader -> Video Stabilizer -> Video-to-Audio -> Video-to-Image | Video Downloader -> Video Stabilizer; Video Stabilizer -> Video-to-Audio; Video Stabilizer -> Video-to-Image |
| 6 | action_coverage | preserve_every_user_requested_operation | 0.4537 | 1.0000 | 0.6667 | False | Video Downloader -> Video Stabilizer -> Video-to-Audio -> Video-to-Image | Video Downloader -> Video Stabilizer; Video Stabilizer -> Video-to-Audio; Video Stabilizer -> Video-to-Image |
| 7 | parallel_dag | preserve_independent_branches | 0.4537 | 1.0000 | 0.6667 | False | Video Downloader -> Video Stabilizer -> Video-to-Audio -> Video-to-Image | Video Downloader -> Video Stabilizer; Video Stabilizer -> Video-to-Audio; Video Stabilizer -> Video-to-Image |
| 8 | parallel_dag | avoid_forcing_dags_into_chains | 0.4537 | 1.0000 | 0.6667 | False | Video Downloader -> Video Stabilizer -> Video-to-Audio -> Video-to-Image | Video Downloader -> Video Stabilizer; Video Stabilizer -> Video-to-Audio; Video Stabilizer -> Video-to-Image |
| 9 | dependency_first | semantic_dependency_continuity | 0.4537 | 1.0000 | 0.6667 | False | Video Downloader -> Video Stabilizer -> Video-to-Audio -> Video-to-Image | Video Downloader -> Video Stabilizer; Video Stabilizer -> Video-to-Audio; Video Stabilizer -> Video-to-Image |
| 10 | parameter_copy | exact_parameter_copy | 0.4537 | 1.0000 | 0.6667 | False | Video Downloader -> Video Stabilizer -> Video-to-Audio -> Video-to-Image | Video Downloader -> Video Stabilizer; Video Stabilizer -> Video-to-Audio; Video Stabilizer -> Video-to-Image |