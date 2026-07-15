import json
import tempfile
import unittest
from pathlib import Path

from agent.memory_guided_workflow.tool_transition_graph import ToolTransitionGraph


class ToolTransitionGraphTest(unittest.TestCase):
    def test_exclude_data_removes_matching_record_ids_from_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool_desc = root / "tool_desc.json"
            graph_desc = root / "graph_desc.json"
            data_all = root / "data_all.json"
            data_eval = root / "data.json"

            tool_desc.write_text(
                json.dumps(
                    {
                        "nodes": [
                            {"id": "A", "input-type": [], "output-type": ["text"]},
                            {"id": "B", "input-type": ["text"], "output-type": ["text"]},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            graph_desc.write_text(
                json.dumps({"links": [{"source": "A", "target": "B", "type": "text"}]}),
                encoding="utf-8",
            )
            data_all.write_text(
                "\n".join(
                    [
                        json.dumps({"id": "train-1", "tool_links": [{"source": "A", "target": "B"}]}),
                        json.dumps({"id": "eval-1", "tool_links": [{"source": "A", "target": "B"}]}),
                    ]
                ),
                encoding="utf-8",
            )
            data_eval.write_text(
                json.dumps({"id": "eval-1", "tool_links": [{"source": "A", "target": "B"}]}),
                encoding="utf-8",
            )

            graph = ToolTransitionGraph(
                tool_desc_path=str(tool_desc),
                graph_desc_path=str(graph_desc),
                data_path=str(data_all),
                exclude_data_path=str(data_eval),
            ).build()

            edge = graph.edges[("A", "B")]
            self.assertEqual(edge.count, 1)
            self.assertEqual(graph.metadata["excluded_record_id_count"], 1)
            self.assertEqual(graph.metadata["transition_records_total"], 2)
            self.assertEqual(graph.metadata["transition_records_used"], 1)
            self.assertEqual(graph.metadata["transition_records_excluded"], 1)


if __name__ == "__main__":
    unittest.main()
