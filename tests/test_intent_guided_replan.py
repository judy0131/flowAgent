import unittest

from agent.memory_guided_workflow.experiments.intent_audited_replan_edge_repair import (
    extract_planner_json_object,
)


class TestIntentGuidedReplanJsonRepair(unittest.TestCase):
    def test_repairs_task_links_inside_task_nodes_array(self) -> None:
        raw = (
            '{"replan_decision":"ADD_MISSING_TOOLS",'
            '"replanned_workflow":{"task_nodes":[{"id":"node-0","task":"Image-to-Text","arguments":["example.jpg"]},'
            '{"id":"node-1","task":"Conversational","arguments":["<node-0>"]},'
            '"task_links":[{"source":"node-0","target":"node-1","target_input_slot":"text"}]},'
            '"reason":"ok"}'
        )

        payload, repaired = extract_planner_json_object(raw)

        self.assertTrue(repaired)
        self.assertEqual(payload["replanned_workflow"]["task_nodes"][1]["task"], "Conversational")
        self.assertEqual(payload["replanned_workflow"]["task_links"][0]["target"], "node-1")

    def test_repairs_unquoted_task_links_key(self) -> None:
        raw = (
            '{"replan_decision":"ADD_MISSING_TOOLS",'
            '"replanned_workflow":{"task_nodes":[{"id":"node-0","task":"Text-to-Image","arguments":["x"]}],'
            'task_links":[{"source":"node-0","target":"node-1"}]},'
            '"reason":"ok"}'
        )

        payload, repaired = extract_planner_json_object(raw)

        self.assertTrue(repaired)
        self.assertEqual(payload["replanned_workflow"]["task_links"][0]["source"], "node-0")

    def test_repairs_node_fields_misplaced_on_workflow_object(self) -> None:
        raw = (
            '{"replan_decision":"ADD_MISSING_TOOLS",'
            '"replanned_workflow":{"task_nodes":[{"id":"node-0","task":"Object Detection","arguments":["example.jpg"]},'
            '{"id":"node-1","task":"Sentence Similarity","arguments":["<node-0>"]}],'
            '"id":"node-2","task":"Visual Question Answering","arguments":["<node-0>","<node-1>"],'
            '"task_links":[]},'
            '"reason":"ok"}'
        )

        payload, repaired = extract_planner_json_object(raw)

        self.assertTrue(repaired)
        self.assertEqual(payload["replanned_workflow"]["task_nodes"][2]["id"], "node-2")
        self.assertNotIn("task", payload["replanned_workflow"])

    def test_repairs_extra_quote_before_node_object(self) -> None:
        raw = (
            '{"replan_decision":"ADD_MISSING_TOOLS",'
            '"replanned_workflow":{"task_nodes":[{"id":"node-0","task":"Image-to-Text","arguments":["example.jpg"]},'
            '"{"id":"node-1","task":"Document Question Answering","arguments":["example.jpg","question"]}],'
            '"task_links":[]},'
            '"reason":"ok"}'
        )

        payload, repaired = extract_planner_json_object(raw)

        self.assertTrue(repaired)
        self.assertEqual(payload["replanned_workflow"]["task_nodes"][1]["task"], "Document Question Answering")


if __name__ == "__main__":
    unittest.main()
