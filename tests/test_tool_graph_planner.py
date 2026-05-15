import csv
import tempfile
import unittest
from pathlib import Path

from agent.tool_graph_planner import ToolGraphPlanner


class TestToolGraphPlanner(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.csv_path = Path(self.tmp_dir.name) / "edges.csv"
        headers = [
            "source_tool",
            "target_tool",
            "count",
            "workflows",
            "source_tool_id",
            "source_content_id",
            "source_tool_version",
            "source_repo_name",
            "source_repo_owner",
            "source_repo_tool_shed",
            "source_repo_changeset_revision",
            "target_tool_id",
            "target_content_id",
            "target_tool_version",
            "target_repo_name",
            "target_repo_owner",
            "target_repo_tool_shed",
            "target_repo_changeset_revision",
        ]
        rows = [
            ["A", "B", "3", "w1,w2", "a/id", "", "1.0", "repo_a", "owner_a", "shed_a", "rev_a", "b/id", "", "2.0", "repo_b", "owner_b", "shed_b", "rev_b"],
            ["A", "C", "1", "w1", "a/id", "", "1.0", "repo_a", "owner_a", "shed_a", "rev_a", "c/id", "", "1.1", "repo_c", "owner_c", "shed_c", "rev_c"],
            ["B", "D", "2", "w2", "b/id", "", "2.0", "repo_b", "owner_b", "shed_b", "rev_b", "d/id", "", "3.0", "repo_d", "owner_d", "shed_d", "rev_d"],
        ]
        with self.csv_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerows(rows)
        self.planner = ToolGraphPlanner.from_csv(self.csv_path)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_recommend_next_tools_ranked(self) -> None:
        recs = self.planner.recommend_next_tools("A", top_k=2)
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["target_tool"], "B")
        self.assertEqual(recs[1]["target_tool"], "C")

    def test_recommend_next_tools_filters_visited(self) -> None:
        recs = self.planner.recommend_next_tools("A", visited_tools={"B"}, top_k=3)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["target_tool"], "C")

    def test_recommend_start_tools(self) -> None:
        starts = self.planner.recommend_start_tools(top_k=3)
        self.assertEqual(starts[0]["tool"], "A")

    def test_shortest_path(self) -> None:
        path = self.planner.shortest_path("A", "D")
        self.assertEqual(path, ["A", "B", "D"])

    def test_transition_and_explain(self) -> None:
        self.assertTrue(self.planner.is_valid_transition("A", "B"))
        self.assertFalse(self.planner.is_valid_transition("C", "A"))
        detail = self.planner.explain_transition("A", "B")
        self.assertTrue(detail["valid"])
        self.assertEqual(detail["count"], 3)

    def test_parse_node_attributes(self) -> None:
        node = self.planner.get_node("B")
        self.assertIsNotNone(node)
        assert node is not None
        self.assertEqual(node.tool_id, "b/id")
        self.assertEqual(node.repo_owner, "owner_b")


if __name__ == "__main__":
    unittest.main()
