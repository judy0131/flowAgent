import unittest
from pathlib import Path

from agent.pipeline_orchestrator import TaskBenchPrior, TaskBenchPriorIndex
from agent.pipeline_orchestrator_agent import PipelineOrchestratorAgent, SkillRegistry
from agent.pipeline_orchestrator.workflow_memory import WorkflowMemoryIndex


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = PROJECT_ROOT / "skills" / "operators"


class TestPipelineOrchestratorPriorIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = PipelineOrchestratorAgent.__new__(PipelineOrchestratorAgent)
        self.agent.registry = SkillRegistry(SKILLS_ROOT)
        self.agent._enable_workflow_memory = True
        self.agent._workflow_retrieval_cache = {}
        self.agent._tool_graph_planner = None
        self.agent._skill_to_tool_graph_name = {}
        self.agent._tool_graph_alias_to_skill = {}

        records = [
            {
                "id": "1",
                "instruction": "Load a csv file, filter its rows, and compute an aggregate sum.",
                "tool_nodes": [
                    {"task": "load_csv", "arguments": ["sales.csv"]},
                    {"task": "filter_rows", "arguments": ["<node-0>", "amount > 10"]},
                    {"task": "aggregate_sum", "arguments": ["<node-1>", "amount"]},
                ],
                "tool_links": [
                    {"source": "load_csv", "target": "filter_rows"},
                    {"source": "filter_rows", "target": "aggregate_sum"},
                ],
            },
            {
                "id": "2",
                "instruction": "Load a csv file, filter its rows, and compute an aggregate sum.",
                "tool_nodes": [
                    {"task": "load_csv", "arguments": ["inventory.csv"]},
                    {"task": "filter_rows", "arguments": ["<node-0>", "count > 0"]},
                    {"task": "aggregate_sum", "arguments": ["<node-1>", "count"]},
                ],
                "tool_links": [
                    {"source": "load_csv", "target": "filter_rows"},
                    {"source": "filter_rows", "target": "aggregate_sum"},
                ],
            },
        ]
        memory = WorkflowMemoryIndex.build_from_taskbench_records(records, source_name="unit-test")
        prior_index = TaskBenchPriorIndex(memory)
        prior = TaskBenchPrior(prior_index)
        self.agent._workflow_memory = memory
        self.agent._workflow_retriever = prior.retriever
        self.agent._taskbench_prior_index = prior_index
        self.agent._taskbench_prior = prior

    def test_score_plan_prefers_taskbench_prior_consistent_workflow(self) -> None:
        requirement = "Load a csv file, filter its rows, and compute an aggregate sum."
        good_workflow = {
            "task_steps": ["Step 1", "Step 2", "Step 3"],
            "task_nodes": [
                {"task": "load_csv", "arguments": [{"name": "path", "value": "sales.csv"}]},
                {"task": "filter_rows", "arguments": [{"name": "rows", "value": "<node-0>"}, {"name": "condition", "value": "amount > 10"}]},
                {"task": "aggregate_sum", "arguments": [{"name": "rows", "value": "<node-1>"}, {"name": "column", "value": "amount"}]},
            ],
            "task_links": [
                {"source": "load_csv", "target": "filter_rows"},
                {"source": "filter_rows", "target": "aggregate_sum"},
            ],
        }
        bad_workflow = {
            "task_steps": ["Step 1", "Step 2", "Step 3"],
            "task_nodes": [
                {"task": "load_csv", "arguments": [{"name": "path", "value": "sales.csv"}]},
                {"task": "generate_report", "arguments": [{"name": "rows", "value": "<node-0>"}]},
                {"task": "aggregate_sum", "arguments": [{"name": "rows", "value": "<node-1>"}, {"name": "column", "value": "amount"}]},
            ],
            "task_links": [
                {"source": "load_csv", "target": "generate_report"},
                {"source": "generate_report", "target": "aggregate_sum"},
            ],
        }

        good_score = self.agent._score_plan(good_workflow, user_requirement=requirement)["score"]
        bad_score = self.agent._score_plan(bad_workflow, user_requirement=requirement)["score"]

        self.assertGreater(good_score, bad_score)

    def test_recommend_memory_next_skills_uses_taskbench_prior(self) -> None:
        recs = self.agent.recommend_memory_next_skills(
            "Load a csv file, filter its rows, and compute an aggregate sum.",
            "load_csv",
            top_k=3,
            visited_skills={"load_csv"},
        )
        self.assertTrue(recs)
        self.assertEqual(recs[0]["skill"], "filter_rows")


if __name__ == "__main__":
    unittest.main()
