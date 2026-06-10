import unittest

from agent.memory_guided_workflow.incremental_planning import (
    IncrementalPlanner,
    complete_taskbench_arguments,
)
from agent.memory_guided_workflow.task_understanding import TaskUnderstanding
from agent.memory_guided_workflow.workflow_coverage_verification import (
    WorkflowCoverageVerifier,
    compute_connected_components,
)
from agent.memory_guided_workflow.models import (
    PlannerDecision,
    PlanningCandidate,
    TaskStep,
    ToolCandidate,
    ToolRetrievalResult,
    ToolSpec,
    UserRequest,
    WorkflowDAG,
    WorkflowEdge,
    WorkflowNode,
)
from agent.memory_guided_workflow.run_miwp_case import (
    _verify_and_repair_workflow,
    build_taskbench_prediction,
)


class TestMIWPTaskBenchArguments(unittest.TestCase):
    def test_complete_and_export_taskbench_arguments(self) -> None:
        steps = [
            (
                "n_t1",
                "Video-to-Audio",
                "Extract the audio track from the 'example.mp4' video.",
                [],
                ["example.mp4"],
            ),
            (
                "n_t2",
                "Audio Splicer",
                "Combine the extracted audio from Step 1 with the 'example.wav' audio file.",
                ["n_t1"],
                ["example.wav"],
            ),
            (
                "n_t3",
                "Audio-to-Text",
                "Transcribe the speech from the combined audio in Step 2 into text.",
                ["n_t2"],
                [],
            ),
            (
                "n_t4",
                "Audio Effects",
                "Apply the 'add reverb' effect to the combined audio from Step 2.",
                ["n_t2"],
                ["add reverb"],
            ),
            (
                "n_t5",
                "Audio-to-Image",
                "Generate a waveform image for the final audio output from Step 4.",
                ["n_t4"],
                [],
            ),
        ]

        dag = WorkflowDAG()
        for index, (node_id, tool_name, description, predecessors, llm_arguments) in enumerate(steps, start=1):
            arguments = complete_taskbench_arguments(
                task=TaskStep(
                    task_id=f"t{index}",
                    description=description,
                    referenced_literals=llm_arguments,
                ),
                tool_name=tool_name,
                predecessor_node_ids=predecessors,
                workflow_nodes=dag.nodes,
            )
            node = WorkflowNode(
                node_id=node_id,
                task_id=f"t{index}",
                task_description=description,
                tool_id=tool_name,
                tool_name=tool_name,
                metadata={"arguments": arguments},
            )
            dag.add_node(node)
            for predecessor in predecessors:
                dag.add_edge(
                    WorkflowEdge(
                        source_node_id=predecessor,
                        target_node_id=node_id,
                    )
                )

        prediction = build_taskbench_prediction(
            case_id="case",
            user_request="request",
            result={"workflow_dag": dag.to_dict()},
        )

        self.assertEqual(
            prediction["result"]["task_nodes"],
            [
                {"task": "Video-to-Audio", "arguments": ["example.mp4"]},
                {"task": "Audio Splicer", "arguments": ["<node-0>", "example.wav"]},
                {"task": "Audio-to-Text", "arguments": ["<node-1>"]},
                {"task": "Audio Effects", "arguments": ["<node-1>", "add reverb"]},
                {"task": "Audio-to-Image", "arguments": ["<node-3>"]},
            ],
        )

    def test_literal_completion_only_when_input_count_is_missing(self) -> None:
        llm = _FakeLiteralLLM()
        planner = IncrementalPlanner(
            tool_knowledge=_FakeToolKnowledge({"Audio Effects": ["audio", "effect"]}),
            tool_transition_graph=None,
            llm_client=llm,
        )
        final_arguments, completion = planner.complete_literal_arguments_if_needed(
            task=TaskStep(
                task_id="t4",
                description="Apply the 'add reverb' effect to the combined audio from Step 2.",
            ),
            selected_candidate=PlanningCandidate(
                task_id="t4",
                task_description="Apply the 'add reverb' effect to the combined audio from Step 2.",
                tool_id="Audio Effects",
                tool_name="Audio Effects",
                retrieval_score=1.0,
            ),
            reference_arguments=["<node-1>"],
        )

        self.assertEqual(final_arguments, ["<node-1>", "add reverb"])
        self.assertEqual(completion["missing_count"], 1)
        self.assertEqual(llm.calls, 1)

        satisfied_llm = _FakeLiteralLLM()
        satisfied_planner = IncrementalPlanner(
            tool_knowledge=_FakeToolKnowledge({"Audio Effects": ["audio"]}),
            tool_transition_graph=None,
            llm_client=satisfied_llm,
        )
        satisfied_arguments, satisfied_completion = satisfied_planner.complete_literal_arguments_if_needed(
            task=TaskStep(
                task_id="t4",
                description="Apply the 'add reverb' effect to the combined audio from Step 2.",
            ),
            selected_candidate=PlanningCandidate(
                task_id="t4",
                task_description="Apply the 'add reverb' effect to the combined audio from Step 2.",
                tool_id="Audio Effects",
                tool_name="Audio Effects",
                retrieval_score=1.0,
            ),
            reference_arguments=["<node-1>"],
        )

        self.assertEqual(satisfied_arguments, ["<node-1>"])
        self.assertEqual(satisfied_completion["missing_count"], 0)
        self.assertEqual(satisfied_llm.calls, 0)

    def test_extra_literal_is_trimmed_when_predecessor_satisfies_input(self) -> None:
        llm = _FakeLiteralLLM()
        planner = IncrementalPlanner(
            tool_knowledge=_FakeToolKnowledge({"Audio Downloader": ["url"]}),
            tool_transition_graph=None,
            llm_client=llm,
        )

        final_arguments, completion = planner.complete_literal_arguments_if_needed(
            task=TaskStep(
                task_id="t2",
                description="Download the audio from the URL.",
                referenced_literals=["https://example.com/podcast/example.wav"],
            ),
            selected_candidate=PlanningCandidate(
                task_id="t2",
                task_description="Download the audio from the URL.",
                tool_id="Audio Downloader",
                tool_name="Audio Downloader",
                retrieval_score=1.0,
            ),
            reference_arguments=["<node-0>", "https://example.com/podcast/example.wav"],
        )

        self.assertEqual(final_arguments, ["<node-0>"])
        self.assertEqual(completion["required_count"], 1)
        self.assertEqual(completion["original_reference_argument_count"], 2)
        self.assertEqual(completion["reference_argument_count"], 1)
        self.assertTrue(completion["trimmed_to_input_count"])
        self.assertEqual(llm.calls, 0)

    def test_prediction_export_trims_arguments_to_tool_input_count(self) -> None:
        dag = WorkflowDAG()
        dag.add_node(
            WorkflowNode(
                node_id="n_t1",
                task_id="t1",
                task_description="Extract the URL from the text.",
                tool_id="URL Extractor",
                tool_name="URL Extractor",
                metadata={
                    "arguments": ["Check out this podcast at https://example.com/podcast/example.wav"],
                    "input_types": ["text"],
                },
            )
        )
        dag.add_node(
            WorkflowNode(
                node_id="n_t2",
                task_id="t2",
                task_description="Download the audio from the URL.",
                tool_id="Audio Downloader",
                tool_name="Audio Downloader",
                metadata={
                    "arguments": ["<node-0>", "https://example.com/podcast/example.wav"],
                    "input_types": ["url"],
                },
            )
        )
        dag.add_edge(WorkflowEdge(source_node_id="n_t1", target_node_id="n_t2"))

        prediction = build_taskbench_prediction(
            case_id="37338668",
            user_request="request",
            result={"workflow_dag": dag.to_dict()},
        )

        self.assertEqual(
            prediction["result"]["task_nodes"][1],
            {"task": "Audio Downloader", "arguments": ["<node-0>"]},
        )

    def test_dense_candidate_intent_is_in_debug_and_context(self) -> None:
        dense_candidates = [
            ToolCandidate(
                tool_id="Topic Generator",
                name="Topic Generator",
                retrieval_score=0.64,
                intent="generate",
                metadata={"intent": "generate"},
            ),
            ToolCandidate(
                tool_id="Article Spinner",
                name="Article Spinner",
                retrieval_score=0.42,
            ),
            ToolCandidate(
                tool_id="Text Summarizer",
                name="Text Summarizer",
                retrieval_score=0.35,
            ),
        ]
        knowledge = _FakeToolKnowledge(
            dense_candidates=dense_candidates,
        )
        planner = IncrementalPlanner(
            tool_knowledge=knowledge,
            tool_transition_graph=_FakeTransitionGraph(),
            top_k=3,
        )

        candidates = planner.build_planning_candidates(
            task=TaskStep(
                task_id="t2",
                description="Expand the generated topics into more detailed versions.",
            ),
            memory=_EmptyMemory(),
        )

        self.assertEqual(knowledge.last_top_k, 10)
        candidate_debug = {
            candidate["tool_id"]: candidate
            for candidate in planner._last_candidate_generation_debug["candidate_tools"]
        }
        self.assertEqual(candidate_debug["Topic Generator"]["intent"], "generate")

        context = planner.build_planning_context(
            task=TaskStep(
                task_id="t2",
                description="Expand the generated topics into more detailed versions.",
            ),
            candidates=candidates,
            memory=_EmptyMemory(),
        )
        context_candidates = {
            candidate["tool_id"]: candidate
            for candidate in context["candidate_tools"]
        }
        self.assertEqual(context_candidates["Topic Generator"]["intent"], "generate")

    def test_referenced_literals_feed_taskbench_arguments(self) -> None:
        original_sentence = (
            "The grandiose edifice exhibiting an eclectic mixture of architectural designs "
            "evokes a sense of awe and admiration among the beholders."
        )
        request_text = (
            f"I have a complex sentence: '{original_sentence}' I would like it to be "
            "paraphrased, simplified, and then find an image related to the simplified sentence."
        )
        raw_payload = {
            "tasks": [
                {
                    "task_id": "t1",
                    "description": "Paraphrase the complex sentence.",
                },
                {
                    "task_id": "t2",
                    "description": "Simplify the paraphrased sentence.",
                },
                {
                    "task_id": "t3",
                    "description": "Find an image related to the simplified sentence.",
                },
            ]
        }

        understanding = object.__new__(TaskUnderstanding)
        result = understanding._normalize_result(UserRequest(text=request_text), raw_payload)

        self.assertEqual(result.steps[0].referenced_literals, [original_sentence])
        self.assertEqual(result.steps[1].referenced_literals, [])
        self.assertEqual(result.steps[2].referenced_literals, [])

        dag = WorkflowDAG()
        task_specs = [
            (result.steps[0], "Text Paraphraser", []),
            (result.steps[1], "Text Simplifier", ["n_t1"]),
            (result.steps[2], "Image Search", ["n_t2"]),
        ]
        for index, (task, tool_name, predecessors) in enumerate(task_specs, start=1):
            arguments = complete_taskbench_arguments(
                task=task,
                tool_name=tool_name,
                predecessor_node_ids=predecessors,
                workflow_nodes=dag.nodes,
            )
            node = WorkflowNode(
                node_id=f"n_t{index}",
                task_id=task.task_id,
                task_description=task.description,
                tool_id=tool_name,
                tool_name=tool_name,
                metadata={"arguments": arguments},
            )
            dag.add_node(node)
            for predecessor in predecessors:
                dag.add_edge(
                    WorkflowEdge(
                        source_node_id=predecessor,
                        target_node_id=node.node_id,
                    )
                )

        prediction = build_taskbench_prediction(
            case_id="literal-case",
            user_request=request_text,
            result={"workflow_dag": dag.to_dict()},
        )

        self.assertEqual(
            prediction["result"]["task_nodes"],
            [
                {"task": "Text Paraphraser", "arguments": [original_sentence]},
                {"task": "Text Simplifier", "arguments": ["<node-0>"]},
                {"task": "Image Search", "arguments": ["<node-1>"]},
            ],
        )

    def test_connected_components_counts_weak_components(self) -> None:
        workflow = {
            "nodes": [
                {"node_id": "n1"},
                {"node_id": "n2"},
                {"node_id": "n3"},
            ],
            "edges": [
                {"source_node_id": "n1", "target_node_id": "n2"},
            ],
        }

        component_info = compute_connected_components(workflow)
        self.assertEqual(component_info["component_count"], 2)
        self.assertEqual(
            {tuple(component) for component in component_info["components"]},
            {("n1", "n2"), ("n3",)},
        )

    def test_workflow_coverage_verifier_normalizes_repair_tasks(self) -> None:
        verifier = WorkflowCoverageVerifier(llm_client=_FakeCoverageLLM())
        report = verifier.verify(
            user_request="Generate a video and combine it with audio.",
            workflow={
                "nodes": [{"node_id": "n1", "tool_name": "Text-to-Video"}],
                "edges": [],
            },
        )

        self.assertFalse(report["is_fully_covered"])
        self.assertEqual(report["component_count"], 1)
        self.assertEqual(
            report["repair_tasks"],
            [
                {
                    "task_id": "repair_1",
                    "description": "Combine the generated video with the audio.",
                    "status": "remaining",
                    "priority": 1.0,
                    "referenced_literals": [],
                    "metadata": {},
                }
            ],
        )

    def test_incompatible_predecessor_is_removed(self) -> None:
        knowledge = _FakeToolKnowledge(
            input_types_by_tool_id={"Text Sentiment Analysis": ["text"]},
            output_types_by_tool_id={
                "Text-to-Image": ["image"],
                "Text Sentiment Analysis": ["text"],
            },
            dense_candidates=[
                ToolCandidate(
                    tool_id="Text Sentiment Analysis",
                    name="Text Sentiment Analysis",
                    retrieval_score=0.9,
                    intent="analyze",
                    metadata={"intent": "analyze"},
                )
            ],
        )
        planner = IncrementalPlanner(
            tool_knowledge=knowledge,
            tool_transition_graph=_FakeTransitionGraph(),
            top_k=5,
        )
        memory = _EmptyMemory()
        memory.workflow_dag.add_node(
            WorkflowNode(
                node_id="n_t1",
                task_id="t1",
                task_description="Generate an image.",
                tool_id="Text-to-Image",
                tool_name="Text-to-Image",
                metadata={"output_types": ["image"]},
            )
        )

        task = TaskStep(task_id="t2", description="Analyze the sentiment of the text.")
        candidates = planner.build_planning_candidates(task, memory)
        context = planner.build_planning_context(task, candidates, memory)
        predecessor = context["candidate_tools"][0]["predecessor_candidates"][0]
        self.assertFalse(predecessor["type_compatible"])

        decision = PlannerDecision(
            task_id="t2",
            selected_tool_id="Text Sentiment Analysis",
            predecessor_node_ids=["n_t1"],
        )
        validated = planner.validate_decision(decision, candidates, context["workflow_so_far"])

        self.assertEqual(validated.predecessor_node_ids, [])
        self.assertEqual(validated.metadata["removed_incompatible_predecessors"], ["n_t1"])

    def test_dynamic_candidate_pool_includes_close_scores_beyond_top_k(self) -> None:
        dense_candidates = [
            ToolCandidate(tool_id=f"Tool {index}", name=f"Tool {index}", retrieval_score=score)
            for index, score in enumerate(
                [0.50, 0.46, 0.44, 0.43, 0.40, 0.385, 0.34],
                start=1,
            )
        ]
        knowledge = _FakeToolKnowledge(dense_candidates=dense_candidates)
        planner = IncrementalPlanner(
            tool_knowledge=knowledge,
            tool_transition_graph=_FakeTransitionGraph(),
            top_k=5,
        )

        candidates = planner.build_planning_candidates(
            task=TaskStep(task_id="t1", description="Do something."),
            memory=_EmptyMemory(),
        )

        self.assertEqual(knowledge.last_top_k, 10)
        self.assertEqual([candidate.tool_id for candidate in candidates], [f"Tool {i}" for i in range(1, 7)])
        policy = planner._last_candidate_generation_debug["candidate_pool_policy"]
        self.assertEqual(policy["min_candidate_k"], 5)
        self.assertEqual(policy["max_candidate_k"], 10)
        self.assertEqual(policy["selected_count"], 6)

    def test_repair_verification_only_runs_for_independent_components(self) -> None:
        connected_memory = _EmptyMemory()
        connected_memory.workflow_dag.add_node(
            WorkflowNode(
                node_id="n1",
                task_id="t1",
                task_description="Download a video.",
                tool_id="Video Downloader",
                tool_name="Video Downloader",
            )
        )
        connected_memory.workflow_dag.add_node(
            WorkflowNode(
                node_id="n2",
                task_id="t2",
                task_description="Extract text.",
                tool_id="Video-to-Text",
                tool_name="Video-to-Text",
            )
        )
        connected_memory.workflow_dag.add_edge(
            WorkflowEdge(source_node_id="n1", target_node_id="n2")
        )
        connected_verifier = _CountingVerifier()
        connected_planner = _TraceOnlyPlanner()

        connected_history = _verify_and_repair_workflow(
            user_request="Download a video and extract text.",
            tasks=[],
            memory=connected_memory,
            planner=connected_planner,
            verifier=connected_verifier,
            max_repair_rounds=1,
        )

        self.assertEqual(connected_verifier.calls, 0)
        self.assertEqual(connected_history[0]["metadata"]["repair_skipped"], "single_connected_component")

        branched_memory = _EmptyMemory()
        branched_memory.workflow_dag.add_node(
            WorkflowNode(
                node_id="n1",
                task_id="t1",
                task_description="Download a video.",
                tool_id="Video Downloader",
                tool_name="Video Downloader",
            )
        )
        branched_memory.workflow_dag.add_node(
            WorkflowNode(
                node_id="n2",
                task_id="t2",
                task_description="Combine audio.",
                tool_id="Audio Splicer",
                tool_name="Audio Splicer",
            )
        )
        branched_verifier = _CountingVerifier()
        branched_planner = _TraceOnlyPlanner()

        branched_history = _verify_and_repair_workflow(
            user_request="Create a video with combined audio.",
            tasks=[],
            memory=branched_memory,
            planner=branched_planner,
            verifier=branched_verifier,
            max_repair_rounds=1,
        )

        self.assertEqual(branched_verifier.calls, 1)
        self.assertEqual(branched_history[0]["metadata"]["repair_trigger"], "independent_components")


class _FakeToolKnowledge:
    def __init__(
        self,
        input_types_by_tool_id=None,
        output_types_by_tool_id=None,
        tools=None,
        dense_candidates=None,
    ):
        self.input_types_by_tool_id = input_types_by_tool_id or {}
        self.output_types_by_tool_id = output_types_by_tool_id or {}
        self.tools = tools or [
            ToolSpec(
                tool_id=tool_id,
                name=tool_id,
                input_types=input_types,
                output_types=self.output_types_by_tool_id.get(tool_id, []),
            )
            for tool_id, input_types in self.input_types_by_tool_id.items()
        ]
        self.dense_candidates = dense_candidates or []
        self.last_top_k = None

    def get_tool(self, tool_id):
        for tool in self.tools:
            if tool.tool_id == tool_id:
                return tool
        return _FakeTool(
            self.input_types_by_tool_id.get(tool_id, []),
            self.output_types_by_tool_id.get(tool_id, []),
        )

    def get_all_tools(self):
        return list(self.tools)

    def retrieve_tools(self, query, top_k=5):
        self.last_top_k = top_k
        return ToolRetrievalResult(
            task_id="",
            query=query,
            candidates=self.dense_candidates[:top_k],
        )


class _FakeTool:
    def __init__(self, input_types, output_types=None):
        self.input_types = input_types
        self.output_types = output_types or []


class _FakeLiteralLLM:
    def __init__(self):
        self.calls = 0

    def chat(self, messages):
        self.calls += 1
        return '{"literal_arguments": ["add reverb"]}'


class _FakeTransitionGraph:
    def get_transition_probability(self, source_tool_id, target_tool_id):
        return 0.0


class _FakeCoverageLLM:
    def chat(self, messages):
        return (
            '{"is_fully_covered": false, '
            '"missing_requirements": ["audio/video composition is missing"], '
            '"repair_tasks": ['
            '{"description": "Combine the generated video with the audio.", "referenced_literals": []}, '
            '{"description": "Provide the result.", "referenced_literals": []}'
            ']}'
        )


class _CountingVerifier:
    def __init__(self):
        self.calls = 0

    def verify(self, user_request, workflow, component_info=None):
        self.calls += 1
        return {
            "component_count": (component_info or {}).get("component_count", 0),
            "components": (component_info or {}).get("components", []),
            "is_fully_covered": True,
            "missing_requirements": [],
            "repair_tasks": [],
        }


class _TraceOnlyPlanner:
    def __init__(self):
        self.debug_history = []

    def plan_next(self, task, memory):
        raise AssertionError("plan_next should not be called when verifier returns covered")


class _EmptyMemory:
    def __init__(self):
        self.workflow_dag = WorkflowDAG()

    def get_workflow_dag(self):
        return self.workflow_dag


if __name__ == "__main__":
    unittest.main()
