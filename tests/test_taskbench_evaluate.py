import unittest

from taskbench.evaluate import (
    _build_incoming_source_map,
    _materialize_resource_graph,
    _resolve_node_reference,
)


class TestTaskbenchEvaluate(unittest.TestCase):
    def test_resolve_node_reference_uses_link_hint_for_one_based_refs(self) -> None:
        node_names = ["Video-to-Audio", "Audio Noise Reduction", "Audio Effects"]
        incoming_source_map = _build_incoming_source_map(
            [
                {"source": "Video-to-Audio", "target": "Audio Noise Reduction"},
                {"source": "Audio Noise Reduction", "target": "Audio Effects"},
            ]
        )

        self.assertEqual(
            _resolve_node_reference(
                "<node-1>",
                1,
                node_names,
                current_task="Audio Noise Reduction",
                incoming_source_map=incoming_source_map,
            ),
            0,
        )
        self.assertEqual(
            _resolve_node_reference(
                "<node-2>",
                2,
                node_names,
                current_task="Audio Effects",
                incoming_source_map=incoming_source_map,
            ),
            1,
        )

    def test_materialize_resource_graph_handles_one_based_refs(self) -> None:
        node_names, links, node_arguments = _materialize_resource_graph(
            [
                {"task": "Video-to-Audio", "arguments": ["example.mp4"]},
                {"task": "Audio Noise Reduction", "arguments": ["<node-1>"]},
                {"task": "Audio Effects", "arguments": ["<node-2>", "reverb"]},
            ],
            [
                {"source": "Video-to-Audio", "target": "Audio Noise Reduction"},
                {"source": "Audio Noise Reduction", "target": "Audio Effects"},
            ],
            {
                "Video-to-Audio": "audio",
                "Audio Noise Reduction": "audio",
                "Audio Effects": "audio",
            },
        )

        self.assertEqual(node_names, ["Video-to-Audio", "Audio Noise Reduction", "Audio Effects"])
        self.assertEqual(
            links,
            [
                {"source": "Video-to-Audio", "target": "Audio Noise Reduction"},
                {"source": "Audio Noise Reduction", "target": "Audio Effects"},
            ],
        )
        self.assertEqual(node_arguments[0], [{"name": "video", "value": "example.mp4"}])
        self.assertEqual(node_arguments[1], [{"name": "audio", "value": "Video-to-Audio"}])
        self.assertEqual(
            node_arguments[2],
            [
                {"name": "audio", "value": "Audio Noise Reduction"},
                {"name": "text", "value": "reverb"},
            ],
        )

    def test_materialize_resource_graph_preserves_zero_based_refs(self) -> None:
        _, links, node_arguments = _materialize_resource_graph(
            [
                {"task": "Video-to-Audio", "arguments": ["example.mp4"]},
                {"task": "Audio Noise Reduction", "arguments": ["<node-0>"]},
                {"task": "Audio Effects", "arguments": ["reverb", "<node-1>"]},
            ],
            [
                {"source": "Video-to-Audio", "target": "Audio Noise Reduction"},
                {"source": "Audio Noise Reduction", "target": "Audio Effects"},
            ],
            {
                "Video-to-Audio": "audio",
                "Audio Noise Reduction": "audio",
                "Audio Effects": "audio",
            },
        )

        self.assertEqual(
            links,
            [
                {"source": "Video-to-Audio", "target": "Audio Noise Reduction"},
                {"source": "Audio Noise Reduction", "target": "Audio Effects"},
            ],
        )
        self.assertEqual(node_arguments[1], [{"name": "audio", "value": "Video-to-Audio"}])
        self.assertEqual(
            node_arguments[2],
            [
                {"name": "text", "value": "reverb"},
                {"name": "audio", "value": "Audio Noise Reduction"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
