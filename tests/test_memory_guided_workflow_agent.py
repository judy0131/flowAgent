import unittest
from unittest.mock import patch

from agent.memory_guided_workflow_agent import (
    MemoryGuidedWorkflowAgent,
    OpenAICompatibleLLMClient,
    TaskStep,
    TaskUnderstanding,
    TaskUnderstandingResult,
    ToolSpec,
    UserRequest,
    WorkflowDAG,
)


class FakeTaskUnderstanding(TaskUnderstanding):
    def _call_llm(self, request: UserRequest) -> str:
        _ = request
        return """
{
  "steps": [
    {
      "step_id": "s1",
      "description": "find climate datasets about the Yellow River Basin",
      "priority": 1
    },
    {
      "step_id": "s2",
      "description": "translate metadata into English",
      "priority": 2
    },
    {
      "step_id": "s3",
      "description": "generate a summary report",
      "priority": 3
    }
  ]
}
"""


class TestTaskUnderstanding(unittest.TestCase):
    def test_parse_structured_llm_output(self) -> None:
        parser = FakeTaskUnderstanding()

        result = parser.parse(
            UserRequest(
                text=(
                    "Find climate datasets about the Yellow River Basin, "
                    "translate their metadata into English, "
                    "and generate a summary report."
                )
            )
        )

        self.assertEqual(
            [step.description for step in result.steps],
            [
                "find climate datasets about the Yellow River Basin",
                "translate metadata into English",
                "generate a summary report",
            ],
        )
        self.assertEqual([step.step_id for step in result.steps], ["s1", "s2", "s3"])
        self.assertEqual(result.to_dict()["steps"][0]["step_id"], "s1")
        self.assertNotIn("Image Search", [step.description for step in result.steps])
        self.assertNotIn("Translator", [step.description for step in result.steps])
        self.assertNotIn("Report Generator", [step.description for step in result.steps])

    def test_parse_fallback_when_llm_fails(self) -> None:
        class BrokenTaskUnderstanding(TaskUnderstanding):
            def _call_llm(self, request: UserRequest) -> str:
                _ = request
                raise RuntimeError("LLM unavailable")

        request = UserRequest(
            text="Summarize this file.",
            constraints=["keep it short"],
            preferences=["English"],
        )

        result = BrokenTaskUnderstanding().parse(request)

        self.assertEqual(len(result.steps), 1)
        self.assertEqual(result.steps[0].step_id, "s1")
        self.assertEqual(result.steps[0].description, "Summarize this file.")
        self.assertEqual(result.input_artifacts, [])
        self.assertEqual(result.expected_output_artifacts, [])

    def test_initialize_memory(self) -> None:
        parser = FakeTaskUnderstanding()
        result = TaskUnderstandingResult(
            request=UserRequest(text="Use input and produce output."),
            steps=[
                TaskStep(step_id="s1", description="understand input", priority=1),
                TaskStep(step_id="s2", description="produce output", priority=2),
            ],
            raw_llm_output={},
        )

        memory = parser.initialize_memory(result)

        self.assertEqual(memory.covered_goal_ids, [])
        self.assertEqual(memory.remaining_goal_ids, ["s1", "s2"])
        self.assertEqual(memory.available_artifacts, {})
        self.assertEqual(memory.used_tool_ids, [])
        self.assertIsInstance(memory.workflow_so_far, WorkflowDAG)
        self.assertEqual(memory.workflow_so_far.nodes, [])
        self.assertEqual(memory.workflow_so_far.edges, [])
        self.assertEqual(memory.current_step, 0)

    def test_llm_config_is_loaded_from_pipeline_style_config_file(self) -> None:
        client = OpenAICompatibleLLMClient(llm_config_path="configs/openai.json")

        config = client.resolve_config()

        self.assertEqual(config["provider"], "openai")
        self.assertEqual(config["model_name"], "gpt-4.1")
        self.assertEqual(config["api_key_envs"], ["OPENAI_API_KEY"])
        self.assertEqual(config["base_url_env"], "OPENAI_BASE_URL")

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-key", "OPENAI_BASE_URL": "https://example.test/v1"}):
            self.assertEqual(client.resolve_api_key(config), "sk-test-key")
            self.assertEqual(client.resolve_base_url(config), "https://example.test/v1")

    def test_result_round_trip(self) -> None:
        result = FakeTaskUnderstanding().parse(UserRequest(text="x"))

        restored = TaskUnderstandingResult.from_dict(result.to_dict())

        self.assertEqual(restored.request.text, result.request.text)
        self.assertEqual(restored.steps[0].step_id, "s1")


class TestMemoryGuidedWorkflowAgent(unittest.TestCase):
    def test_agent_can_use_new_task_understanding_class(self) -> None:
        agent = MemoryGuidedWorkflowAgent(
            task_understanding=FakeTaskUnderstanding(),
            tools=[ToolSpec(name="summary_tool", description="summary report")],
        )

        result = agent.run("Find climate datasets and generate a summary report.")

        self.assertEqual(len(result.understanding.steps), 3)
        self.assertGreaterEqual(len(result.candidate_pool), 1)


if __name__ == "__main__":
    unittest.main()
