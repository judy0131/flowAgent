import unittest
from pathlib import Path

from agent.pipeline_orchestrator_agent import PipelineOrchestratorAgent, SkillRegistry


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = PROJECT_ROOT / "skills" / "operators"


class _FakePlanner:
    def __init__(self) -> None:
        self._valid = {
            ("fastp", "Map with BWA-MEM"): {"count": 3, "workflows": ["w1"]},
            ("Map with BWA-MEM", "MultiQC"): {"count": 1, "workflows": ["w1"]},
        }

    def explain_transition(self, source_tool: str, target_tool: str):
        data = self._valid.get((source_tool, target_tool))
        if data:
            return {"valid": True, "count": data["count"], "workflows": data["workflows"]}
        return {"valid": False}

    def recommend_next_tools(self, current_tool: str, visited_tools=None, top_k: int = 5):
        _ = visited_tools
        _ = top_k
        if current_tool == "fastp":
            return [
                {"target_tool": "Map with BWA-MEM", "score": 3.0, "edge_count": 3, "workflows": ["w1"], "reason": "ok"},
                {"target_tool": "Unknown Tool", "score": 1.0, "edge_count": 1, "workflows": ["w2"], "reason": "skip"},
            ]
        return []

    def recommend_start_tools(self, top_k: int = 5):
        _ = top_k
        return [{"tool": "fastp", "score": 4.0, "reason": "start"}]


class TestPipelineOrchestratorGraphIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = PipelineOrchestratorAgent.__new__(PipelineOrchestratorAgent)
        self.agent.registry = SkillRegistry(SKILLS_ROOT)
        self.agent._tool_graph_planner = _FakePlanner()
        self.agent._skill_to_tool_graph_name = {
            "fastp": "fastp",
            "map_with_bwa_mem": "Map with BWA-MEM",
            "multiqc": "MultiQC",
        }
        self.agent._tool_graph_alias_to_skill = {
            "fastp": "fastp",
            "Map with BWA-MEM": "map_with_bwa_mem",
            "MultiQC": "multiqc",
        }

    def test_score_plan_includes_graph_bonus_and_penalty(self) -> None:
        good_workflow = {
            "task_steps": ["Step 1", "Step 2"],
            "task_nodes": [
                {"task": "fastp", "arguments": [{"name": "source_ref", "value": "external_input"}]},
                {"task": "map_with_bwa_mem", "arguments": [{"name": "source_ref", "value": "<node-0>"}]},
            ],
            "task_links": [{"source": "fastp", "target": "map_with_bwa_mem"}],
        }
        bad_workflow = {
            "task_steps": ["Step 1", "Step 2"],
            "task_nodes": [
                {"task": "fastp", "arguments": [{"name": "source_ref", "value": "external_input"}]},
                {"task": "multiqc", "arguments": [{"name": "source_ref", "value": "<node-0>"}]},
            ],
            "task_links": [{"source": "fastp", "target": "multiqc"}],
        }
        good_score = self.agent._score_plan(good_workflow)["score"]
        bad_score = self.agent._score_plan(bad_workflow)["score"]
        self.assertGreater(good_score, bad_score)

    def test_recommend_graph_next_skills_maps_back_to_skill(self) -> None:
        recs = self.agent.recommend_graph_next_skills("fastp", top_k=3)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["skill"], "map_with_bwa_mem")


if __name__ == "__main__":
    unittest.main()
