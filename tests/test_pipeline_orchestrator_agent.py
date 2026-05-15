import sys
import unittest
from pathlib import Path
from types import ModuleType
from typing import List
from unittest.mock import patch

from agent.pipeline_orchestrator_agent import (
    LLMRuntimeConfig,
    PipelineOrchestratorAgent,
    SkillRegistry,
    _safe_json_dumps,
)
from agent.pipeline_orchestrator.retrieval import WorkflowMemoryRetriever
from agent.pipeline_orchestrator.workflow_memory import WorkflowMemoryIndex, WorkflowMemoryMotif


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = PROJECT_ROOT / "skills" / "operators"


class TestSkillRegistry(unittest.TestCase):
    def test_discover_skills(self) -> None:
        registry = SkillRegistry(SKILLS_ROOT)
        names = set(registry.skills.keys())
        self.assertIn("load_csv", names)
        self.assertIn("filter_rows", names)
        self.assertIn("aggregate_sum", names)
        self.assertIn("generate_report", names)

    def test_load_skill_executor(self) -> None:
        registry = SkillRegistry(SKILLS_ROOT)
        skill = registry.load_skill("load_csv")
        ctx = {"trace": []}
        out = skill.run({"path": "not_exists.csv"}, ctx)
        self.assertIn("rows", out)
        self.assertGreater(out["rows"], 0)
        self.assertIn("data", ctx)


class TestPipelineOrchestratorCore(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Bypass __init__ to avoid requiring LLM/API key in unit tests.
        self.agent = PipelineOrchestratorAgent.__new__(PipelineOrchestratorAgent)
        self.agent.registry = SkillRegistry(SKILLS_ROOT)
        self.agent.llm_config = LLMRuntimeConfig(provider="openai", model_name="gpt-4.1", temperature=0.0)
        self.agent.llm = object()
        self.agent._candidate_llm_cache = {0.0: self.agent.llm}
        self.agent._enable_workflow_memory = False
        self.agent._workflow_memory = None
        self.agent._workflow_retriever = None
        self.agent._workflow_retrieval_cache = {}

    def _attach_test_workflow_memory(self, memory: WorkflowMemoryIndex) -> None:
        self.agent._enable_workflow_memory = True
        self.agent._workflow_memory = memory
        self.agent._workflow_retriever = WorkflowMemoryRetriever(memory)
        self.agent._workflow_retrieval_cache = {}

    def test_validate_plan_unknown_skill(self) -> None:
        plan = {"task_steps": ["Step 1"], "task_nodes": [{"task": "unknown_skill", "arguments": []}], "task_links": []}
        with self.assertRaises(ValueError):
            self.agent.validate_plan(plan)

    def test_validate_plan_missing_args(self) -> None:
        plan = {"task_steps": ["Step 1"], "task_nodes": [{"task": "load_csv", "arguments": []}], "task_links": []}
        with self.assertRaises(ValueError):
            self.agent.validate_plan(plan)

    def test_validate_plan_dependency_order(self) -> None:
        # map_with_bwa_mem depends on fastp in generated workflowhub skills.
        if "map_with_bwa_mem" not in self.agent.registry.skills:
            self.skipTest("workflowhub-generated skills not found")
        plan = [
            {"id": 1, "skill": "map_with_bwa_mem", "args": {"source_ref": "x", "output_key": "bam"}},
            {"id": 2, "skill": "fastp", "args": {"source_ref": "x", "output_key": "clean"}},
        ]
        with self.assertRaises(ValueError):
            self.agent.validate_plan(self.agent._build_workflow_view(plan))

    def test_compile_workflow_resolves_upstream_refs_and_output_keys(self) -> None:
        workflow = {
            "task_steps": [
                "Step 1: Call Video-to-Audio with arg1=example.mp4.",
                "Step 2: Call Audio Noise Reduction with arg1=<node-0>.",
                "Step 3: Call Audio Effects with arg2=reverb, arg1=<node-1>.",
            ],
            "task_nodes": [
                {"task": "Video-to-Audio", "arguments": ["example.mp4"]},
                {"task": "Audio Noise Reduction", "arguments": ["<node-0>"]},
                {"task": "Audio Effects", "arguments": ["reverb", "<node-1>"]},
            ],
            "task_links": [
                {"source": "Video-to-Audio", "target": "Audio Noise Reduction"},
                {"source": "Audio Noise Reduction", "target": "Audio Effects"},
            ],
        }

        compiled = self.agent._compile_workflow(workflow)

        self.assertEqual(compiled[0]["task"], "Video-to-Audio")
        self.assertEqual(compiled[0]["args"], {"arg1": "example.mp4"})
        self.assertEqual(compiled[0]["upstream_inputs"], {})
        self.assertEqual(compiled[1]["task"], "Audio Noise Reduction")
        self.assertEqual(compiled[1]["args"], {})
        self.assertEqual(compiled[1]["upstream_inputs"], {"arg1": 0})
        self.assertEqual(compiled[2]["task"], "Audio Effects")
        self.assertEqual(compiled[2]["args"], {"arg2": "reverb"})
        self.assertEqual(compiled[2]["upstream_inputs"], {"arg1": 1})
        self.assertTrue(compiled[0]["output_key"].endswith("_out"))

    def test_normalize_workflow_payload_canonicalizes_one_based_node_refs(self) -> None:
        workflow = {
            "task_steps": [
                "Step 1: Call Video-to-Audio with arg1=example.mp4.",
                "Step 2: Call Audio Noise Reduction with arg1=<node-1>.",
                "Step 3: Call Audio Effects with arg1=<node-2>, arg2=reverb.",
            ],
            "task_nodes": [
                {"task": "Video-to-Audio", "arguments": ["example.mp4"]},
                {"task": "Audio Noise Reduction", "arguments": ["<node-1>"]},
                {"task": "Audio Effects", "arguments": ["<node-2>", "reverb"]},
            ],
            "task_links": [
                {"source": "Video-to-Audio", "target": "Audio Noise Reduction"},
                {"source": "Audio Noise Reduction", "target": "Audio Effects"},
            ],
        }

        normalized = self.agent._normalize_workflow_payload(workflow)
        self.assertEqual(normalized["task_nodes"][1]["arguments"], ["<node-0>"])
        self.assertEqual(normalized["task_nodes"][2]["arguments"], ["<node-1>", "reverb"])

        compiled = self.agent._compile_workflow(workflow)
        self.assertEqual(compiled[1]["upstream_inputs"], {"arg1": 0})
        self.assertEqual(compiled[2]["upstream_inputs"], {"arg1": 1})

    def test_normalize_workflow_payload_keeps_valid_node_ref_when_task_links_conflict(self) -> None:
        workflow = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Text Search", "arguments": ["Climate change and its effect on polar bears"]},
                {"task": "Text Simplifier", "arguments": ["<node-0>"]},
                {"task": "Text Grammar Checker", "arguments": ["<node-1>"]},
                {"task": "Topic Generator", "arguments": ["<node-2>"]},
                {"task": "Text-to-Image", "arguments": ["<node-3>"]},
            ],
            "task_links": [
                {"source": "Text Search", "target": "Text Simplifier"},
                {"source": "Text Simplifier", "target": "Text Grammar Checker"},
                {"source": "Text Grammar Checker", "target": "Topic Generator"},
                {"source": "Text Grammar Checker", "target": "Text-to-Image"},
            ],
        }

        normalized = self.agent._normalize_workflow_payload(workflow)
        self.assertEqual(normalized["task_nodes"][4]["arguments"], ["<node-3>"])

    def test_canonicalize_compiled_workflow_view_reorders_generic_arguments(self) -> None:
        self.agent.registry._meta_by_name.update(
            {
                "Video-to-Audio": type(
                    "Skill",
                    (),
                    {"input_schema": {"arg1": "str"}}
                )(),
                "Audio Splicer": type(
                    "Skill",
                    (),
                    {"input_schema": {"arg1": "str", "arg2": "str"}}
                )(),
                "Audio-to-Text": type(
                    "Skill",
                    (),
                    {"input_schema": {"arg1": "str"}}
                )(),
                "Audio Effects": type(
                    "Skill",
                    (),
                    {"input_schema": {"arg1": "str", "arg2": "str"}}
                )(),
                "Audio-to-Image": type(
                    "Skill",
                    (),
                    {"input_schema": {"arg1": "str"}}
                )(),
            }
        )
        workflow = {
            "task_steps": [
                "Step 1: Call Video-to-Audio with arg1=example.mp4.",
                "Step 2: Call Audio Splicer with arg1=<node-0>, arg2=example.wav.",
                "Step 3: Call Audio-to-Text with arg1=<node-1>.",
                "Step 4: Call Audio Effects with arg2=add reverb, arg1=<node-1>.",
                "Step 5: Call Audio-to-Image with arg1=<node-3>.",
            ],
            "task_nodes": [
                {"task": "Video-to-Audio", "arguments": ["example.mp4"]},
                {"task": "Audio Splicer", "arguments": ["<node-0>", "example.wav"]},
                {"task": "Audio-to-Text", "arguments": ["<node-1>"]},
                {"task": "Audio Effects", "arguments": ["add reverb", "<node-1>"]},
                {"task": "Audio-to-Image", "arguments": ["<node-3>"]},
            ],
            "task_links": [
                {"source": "Video-to-Audio", "target": "Audio Splicer"},
                {"source": "Audio Splicer", "target": "Audio-to-Text"},
                {"source": "Audio Splicer", "target": "Audio Effects"},
                {"source": "Audio Effects", "target": "Audio-to-Image"},
            ],
        }

        normalized = self.agent._normalize_workflow_payload(workflow)
        compiled = self.agent._compile_task_nodes(normalized["task_nodes"])
        canonical = self.agent._canonicalize_compiled_workflow_view(compiled)

        self.assertEqual(canonical["task_nodes"][3]["arguments"], ["<node-1>", "add reverb"])
        self.assertEqual(
            canonical["task_steps"][3],
            "Step 4: Call Audio Effects with arg1=<node-1>, arg2=add reverb.",
        )

    def test_match_requirement_actions_detects_retrieval_chain(self) -> None:
        requirement = (
            "I want to study the impact of climate change on polar bears. "
            "Can you help me find some easy-to-understand information, check its grammar, "
            "generate related topic ideas, and create an image to represent the main topic?"
        )

        actions = self.agent._match_requirement_actions(requirement)
        self.assertEqual(actions, ["retrieval", "simplify", "grammar", "topic", "image"])

    def test_match_requirement_actions_detects_analysis_chain(self) -> None:
        requirement = (
            "I have a long article about the effects of climate change on biodiversity. "
            "I need help in understanding the main ideas, sentiment, and important keywords. "
            "Simplify and summarize the text, then find some related topics on the internet. "
            "The article's content: 'Climate change is having significant effects on biodiversity...'"
        )

        actions = self.agent._match_requirement_actions(requirement)
        self.assertEqual(actions, ["retrieval", "simplify", "summarize", "sentiment", "keywords", "topic"])

    def test_match_requirement_actions_does_not_treat_voice_tone_as_sentiment(self) -> None:
        requirement = (
            "Provide a detailed and grammatically correct explanation in English audio "
            "with a female tone for the phrase 'Economic globalization' sourced from example.wav."
        )

        actions = self.agent._match_requirement_actions(requirement)
        self.assertEqual(actions, ["transcribe", "expand", "grammar", "voice_change"])
        self.assertNotIn("sentiment", actions)

    def test_match_requirement_actions_treats_plain_english_translate_as_rewrite_not_language_translation(self) -> None:
        requirement = (
            "I'm preparing for a presentation on 'Economic Globalization' and I'd like to understand it better in plain English. "
            "Could you translate the content from my example.wav file into a detailed explanation with female narration?"
        )

        actions = self.agent._match_requirement_actions(requirement)
        self.assertIn("transcribe", actions)
        self.assertIn("expand", actions)
        self.assertIn("voice_change", actions)
        self.assertNotIn("translate", actions)

    def test_match_requirement_actions_detects_instruction_simplification_branch(self) -> None:
        requirement = (
            "I'm working on a personal project and I've recorded two separate audio clips, 'example.wav' and 'example2.wav'. "
            "To create a smooth sequence, I'd like to merge them. Additionally, to spice things up, can we enhance the resulting "
            "audio by adding a reverb effect with a 2-second decay and use an equalizer to amplify the bass frequencies by 3dB? "
            "Could you make sure the instructions are understandable enough for my software tools?"
        )

        actions = self.agent._match_requirement_actions(requirement)
        self.assertIn("simplify", actions)
        self.assertIn("combine", actions)
        self.assertIn("audio_effect", actions)
        self.assertNotIn("transcribe", actions)
        self.assertNotIn("translate", actions)

    def test_match_requirement_actions_detects_direct_url_download_as_retrieval(self) -> None:
        requirement = (
            "I've come across this intriguing article online, and I felt it would make a compelling voiceover for my upcoming "
            "video project. Could you create an audio voiceover using the article at https://example-article.com and integrate "
            "it into my video file named 'example.mp4'?"
        )

        actions = self.agent._match_requirement_actions(requirement)
        self.assertIn("retrieval", actions)
        self.assertIn("combine", actions)

    def test_match_requirement_actions_detects_brainstorming_topic_request(self) -> None:
        requirement = (
            "I want to create a blog post about green living, but I only have a vague idea of what I want to write. "
            "I need help with brainstorming and finding relevant images. "
            "Here's my initial idea: 'Sustainable lifestyle, eco-friendly practices, and tips for going green.'"
        )

        actions = self.agent._match_requirement_actions(requirement)
        self.assertIn("topic", actions)

    def test_match_requirement_actions_detects_content_based_audio_effects_need_transcription(self) -> None:
        requirement = (
            "I have an audio file 'example.wav' that I want to enhance with some effects based on the content of the speech. "
            "Then, I'd like to create a waveform or spectrogram image representing the enhanced audio."
        )

        actions = self.agent._match_requirement_actions(requirement)
        self.assertIn("transcribe", actions)
        self.assertIn("audio_effect", actions)
        self.assertIn("waveform", actions)

    def test_requirement_coverage_penalizes_missing_search_step(self) -> None:
        requirement = (
            "I want to study the impact of climate change on polar bears. "
            "Can you help me find some easy-to-understand information, check its grammar, "
            "generate related topic ideas, and create an image to represent the main topic?"
        )

        incomplete = self.agent._score_requirement_action_coverage(
            requirement,
            ["Text Simplifier", "Text Grammar Checker", "Topic Generator", "Text-to-Image"],
        )
        complete = self.agent._score_requirement_action_coverage(
            requirement,
            ["Text Simplifier", "Text Search", "Text Grammar Checker", "Topic Generator", "Text-to-Image"],
        )

        self.assertIn("retrieval", incomplete["missing_actions"])
        self.assertNotIn("retrieval", complete["missing_actions"])
        self.assertGreater(complete["bonus"] + complete["penalty"], incomplete["bonus"] + incomplete["penalty"])

    def test_workflow_covers_image_action_for_audio_to_image(self) -> None:
        self.assertTrue(self.agent._workflow_covers_action(["Audio-to-Image"], "image"))

    def test_workflow_action_tags_infer_multimedia_actions(self) -> None:
        tags = self.agent._workflow_action_tags(
            ["Audio Noise Reduction", "Audio-to-Image", "Image-to-Video"]
        )

        self.assertIn("denoise", tags)
        self.assertIn("waveform", tags)
        self.assertIn("image", tags)
        self.assertIn("video", tags)

    def test_workflow_action_tags_infer_expansion_and_voiceover_actions(self) -> None:
        tags = self.agent._workflow_action_tags(["Text Expander", "Video Voiceover"])

        self.assertIn("expand", tags)
        self.assertIn("combine", tags)
        self.assertIn("video", tags)

    def test_requirement_coverage_penalizes_missing_expand_step(self) -> None:
        requirement = (
            "Provide a detailed and grammatically correct explanation in English audio "
            "with a female tone for the phrase 'Economic globalization' sourced from example.wav."
        )

        incomplete = self.agent._score_requirement_action_coverage(
            requirement,
            ["Audio-to-Text", "Text Grammar Checker", "Text-to-Audio", "Voice Changer"],
        )
        complete = self.agent._score_requirement_action_coverage(
            requirement,
            ["Audio-to-Text", "Text Expander", "Text Grammar Checker", "Text-to-Audio", "Voice Changer"],
        )

        self.assertIn("expand", incomplete["missing_actions"])
        self.assertNotIn("expand", complete["missing_actions"])
        self.assertGreater(complete["bonus"] + complete["penalty"], incomplete["bonus"] + incomplete["penalty"])

    def test_requirement_coverage_penalizes_missing_analysis_steps(self) -> None:
        requirement = (
            "I have a long article about the effects of climate change on biodiversity. "
            "I need help in understanding the main ideas, sentiment, and important keywords. "
            "Simplify and summarize the text, then find some related topics on the internet."
        )

        incomplete = self.agent._score_requirement_action_coverage(
            requirement,
            ["Text Simplifier", "Text Summarizer", "Text Search", "Topic Generator"],
        )
        complete = self.agent._score_requirement_action_coverage(
            requirement,
            [
                "Text Simplifier",
                "Text Summarizer",
                "Text Sentiment Analysis",
                "Keyword Extractor",
                "Text Search",
                "Topic Generator",
            ],
        )

        self.assertIn("sentiment", incomplete["missing_actions"])
        self.assertIn("keywords", incomplete["missing_actions"])
        self.assertNotIn("sentiment", complete["missing_actions"])
        self.assertNotIn("keywords", complete["missing_actions"])
        self.assertGreater(complete["bonus"] + complete["penalty"], incomplete["bonus"] + incomplete["penalty"])

    def test_build_plan_prompt_prefers_exact_user_wording_for_arguments(self) -> None:
        prompt = self.agent._build_plan_prompt(
            "Generate an image based on the theme 'Climate change and its impact on polar bears'."
        )

        self.assertIn("prefer the exact span copied from the user requirement", prompt)
        self.assertIn('changing "impact" to "effect"', prompt)
        self.assertIn("Climate change and its impact on polar bears", prompt)
        self.assertNotIn("Climate change and its effect on polar bears", prompt)

    def test_build_plan_prompt_includes_dependency_self_check(self) -> None:
        prompt = self.agent._build_plan_prompt(
            "Extract audio, transcribe it, add reverb, and generate a waveform image."
        )

        dependency_idx = prompt.index("Dependency and wiring rules:")
        direct_need_idx = prompt.index("specific earlier node whose output is directly needed by that step")
        validation_idx = prompt.index("Validation rules:")

        self.assertLess(dependency_idx, direct_need_idx)
        self.assertLess(direct_need_idx, validation_idx)
        self.assertIn("specific earlier node whose output is directly needed by that step", prompt)
        self.assertIn("keep it on that artifact branch", prompt)
        self.assertIn("connect both of them to that shared earlier node", prompt)
        self.assertIn("soft heuristic to infer likely input/output modality", prompt)
        self.assertIn("X-to-Y often suggest input X and output Y", prompt)
        self.assertIn("Treat skill-name-based modality inference as a hint, not a hard rule", prompt)
        self.assertIn("follow the schema and the user request", prompt)
        self.assertIn("starting point", prompt)
        self.assertIn("Topic Generator is immediately followed by Text-to-Image", prompt)
        self.assertNotIn("run a wiring self-check over every node argument", prompt)

    def test_infer_skill_name_modalities_uses_conversion_pattern(self) -> None:
        self.assertEqual(
            self.agent._infer_skill_name_modalities("audio-to-text"),
            {"input": "audio", "output": "text"},
        )
        self.assertEqual(
            self.agent._infer_skill_name_modalities("Audio Effects"),
            {"input": "audio", "output": "audio"},
        )

    def test_infer_name_based_link_compatibility_detects_modality_conflict(self) -> None:
        self.assertFalse(
            self.agent._infer_name_based_link_compatibility("audio-to-text", "audio effects")
        )
        self.assertTrue(
            self.agent._infer_name_based_link_compatibility("audio splicer", "audio effects")
        )
        self.assertIsNone(
            self.agent._infer_name_based_link_compatibility("fastp", "generate_report")
        )

    def test_resolve_llm_runtime_config_from_profile(self) -> None:
        cfg = PipelineOrchestratorAgent._resolve_llm_runtime_config(llm_profile="gpt4")

        self.assertEqual(cfg.provider, "openai")
        self.assertEqual(cfg.model_name, "gpt-4o")
        self.assertEqual(cfg.api_key_envs, ["OPENAI_API_KEY"])
        self.assertEqual(cfg.base_url_env, "OPENAI_BASE_URL")
        self.assertEqual(cfg.base_url_envs, ["OPENAI_BASE_URL"])

    def test_resolve_llm_runtime_config_from_custom_config(self) -> None:
        cfg = PipelineOrchestratorAgent._resolve_llm_runtime_config(
            model_name="qwen-max",
            provider="tongyi",
            llm_profile="gpt4-exp",
            llm_config={
                "default_profile": "qwen-local",
                "profiles": {
                    "gpt4-exp": {
                        "provider": "openai",
                        "model_name": "gpt-4.1",
                        "temperature": 0.2,
                        "api_key_envs": ["OPENAI_API_KEY"],
                        "base_url_env": "OPENAI_BASE_URL",
                    }
                },
            },
        )

        self.assertEqual(cfg.provider, "openai")
        self.assertEqual(cfg.model_name, "gpt-4.1")
        self.assertEqual(cfg.temperature, 0.2)
        self.assertEqual(cfg.api_key_envs, ["OPENAI_API_KEY"])

    def test_resolve_llm_runtime_config_reads_env_profile(self) -> None:
        with patch.dict("os.environ", {"PIPELINE_ORCHESTRATOR_LLM_PROFILE": "gpt-4o-mini"}, clear=False):
            cfg = PipelineOrchestratorAgent._resolve_llm_runtime_config()

        self.assertEqual(cfg.provider, "openai")
        self.assertEqual(cfg.model_name, "gpt-4o-mini")

    def test_resolve_llm_runtime_config_keeps_gemini_base_url(self) -> None:
        cfg = PipelineOrchestratorAgent._resolve_llm_runtime_config(
            llm_config={
                "provider": "gemini",
                "model_name": "gemini-2.5-flash",
                "base_url": "https://www.packyapi.com/v1",
            }
        )

        self.assertEqual(cfg.provider, "gemini")
        self.assertEqual(cfg.base_url, "https://www.packyapi.com/v1")

    def test_resolve_llm_runtime_config_reads_gemini_base_url_env_list(self) -> None:
        cfg = PipelineOrchestratorAgent._resolve_llm_runtime_config(
            llm_config_path="configs/gemini.json"
        )

        self.assertEqual(cfg.provider, "gemini")
        self.assertEqual(cfg.base_url_env, "GEMINI_BASE_URL")
        self.assertEqual(cfg.base_url_envs, ["GEMINI_BASE_URL", "GOOGLE_BASE_URL"])

    def test_load_llm_config_payload_resolves_repo_relative_path_from_nested_cwd(self) -> None:
        nested_cwd = PROJECT_ROOT / "taskbench" / "pipelineOrchastration"
        with patch.object(Path, "cwd", return_value=nested_cwd):
            payload = PipelineOrchestratorAgent._load_llm_config_payload(
                llm_config_path="configs/openai.json"
            )

        self.assertEqual(payload["provider"], "openai")
        self.assertEqual(payload["model_name"], "gpt-4.1")

    def test_resolve_api_key_accepts_literal_key_in_api_key_envs_for_backward_compat(self) -> None:
        config = LLMRuntimeConfig(
            provider="openai",
            model_name="gpt-4.1",
            api_key_envs=["sk-test-direct-key"],
        )

        self.assertEqual(
            PipelineOrchestratorAgent._resolve_api_key(config),
            "sk-test-direct-key",
        )

    def test_build_llm_client_uses_openai_compatible_client_for_gemini_base_url(self) -> None:
        fake_module = ModuleType("langchain_openai")
        captured: dict[str, object] = {}

        class FakeChatOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        fake_module.ChatOpenAI = FakeChatOpenAI
        config = LLMRuntimeConfig(
            provider="gemini",
            model_name="gemini-2.5-flash",
            api_key="sk-packy-test",
            base_url="https://www.packyapi.com/v1",
        )

        with patch.dict(sys.modules, {"langchain_openai": fake_module}):
            client = PipelineOrchestratorAgent._build_llm_client(config)

        self.assertIsInstance(client, FakeChatOpenAI)
        self.assertEqual(captured["model"], "gemini-2.5-flash")
        self.assertEqual(captured["api_key"], "sk-packy-test")
        self.assertEqual(captured["base_url"], "https://www.packyapi.com/v1")

    def test_build_llm_client_uses_first_available_gemini_base_url_env(self) -> None:
        fake_module = ModuleType("langchain_openai")
        captured: dict[str, object] = {}

        class FakeChatOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        fake_module.ChatOpenAI = FakeChatOpenAI
        config = LLMRuntimeConfig(
            provider="gemini",
            model_name="gemini-2.5-flash",
            api_key="sk-packy-test",
            base_url_envs=["PRIMARY_TEST_BASE_URL", "SECONDARY_TEST_BASE_URL"],
        )

        with patch.dict(
            "os.environ",
            {"SECONDARY_TEST_BASE_URL": "https://gateway.example.com/v1"},
            clear=False,
        ):
            with patch.dict(sys.modules, {"langchain_openai": fake_module}):
                client = PipelineOrchestratorAgent._build_llm_client(config)

        self.assertIsInstance(client, FakeChatOpenAI)
        self.assertEqual(captured["base_url"], "https://gateway.example.com/v1")

    def test_repair_normalized_workflow_prefers_simplify_then_search_for_starting_text(self) -> None:
        workflow = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Text Search", "arguments": ["Climate change and its effect on polar bears"]},
                {"task": "Text Simplifier", "arguments": ["<node-0>"]},
                {"task": "Text Grammar Checker", "arguments": ["<node-1>"]},
                {"task": "Topic Generator", "arguments": ["<node-2>"]},
                {"task": "Text-to-Image", "arguments": ["<node-2>"]},
            ],
            "task_links": [],
        }
        requirement = (
            "I want to study the impact of climate change on polar bears. "
            "Can you help me find some easy-to-understand information, check its grammar, "
            "generate related topic ideas, and create an image to represent the main topic? "
            "Please use the text 'Climate change and its effect on polar bears' as the starting point."
        )

        repaired = self.agent._repair_normalized_workflow(workflow, requirement)

        self.assertEqual(repaired["task_nodes"][0]["task"], "Text Simplifier")
        self.assertEqual(
            repaired["task_nodes"][0]["arguments"],
            ["Climate change and its effect on polar bears"],
        )
        self.assertEqual(repaired["task_nodes"][1]["task"], "Text Search")
        self.assertEqual(repaired["task_nodes"][1]["arguments"], ["<node-0>"])
        self.assertEqual(repaired["task_nodes"][4]["arguments"], ["<node-3>"])

    def test_score_compiled_workflow_prefers_repaired_polar_bear_chain(self) -> None:
        fake_skill = lambda: type(
            "Skill",
            (),
            {"input_schema": {"arg1": "str"}, "depends_on_all": [], "depends_on_any": []},
        )()
        self.agent.registry._meta_by_name.update(
            {
                "Text Search": fake_skill(),
                "Text Simplifier": fake_skill(),
                "Text Grammar Checker": fake_skill(),
                "Topic Generator": fake_skill(),
                "Text-to-Image": fake_skill(),
            }
        )
        self.agent._tool_graph_planner = None
        self.agent._tool_graph_alias_to_skill = {}
        self.agent._skill_to_tool_graph_name = {}

        requirement = (
            "I want to study the impact of climate change on polar bears. "
            "Can you help me find some easy-to-understand information, check its grammar, "
            "generate related topic ideas, and create an image to represent the main topic? "
            "Please use the text 'Climate change and its effect on polar bears' as the starting point."
        )
        gold_like = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Text Simplifier", "arguments": ["Climate change and its effect on polar bears"]},
                {"task": "Text Search", "arguments": ["<node-0>"]},
                {"task": "Text Grammar Checker", "arguments": ["<node-1>"]},
                {"task": "Topic Generator", "arguments": ["<node-2>"]},
                {"task": "Text-to-Image", "arguments": ["<node-3>"]},
            ],
            "task_links": [],
        }
        pred_like = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Text Search", "arguments": ["Climate change and its effect on polar bears"]},
                {"task": "Text Simplifier", "arguments": ["<node-0>"]},
                {"task": "Text Grammar Checker", "arguments": ["<node-1>"]},
                {"task": "Topic Generator", "arguments": ["<node-2>"]},
                {"task": "Text-to-Image", "arguments": ["<node-2>"]},
            ],
            "task_links": [],
        }

        _, gold_compiled = self.agent._prepare_workflow(gold_like)
        _, pred_compiled = self.agent._prepare_workflow(pred_like)
        gold_score = self.agent._score_compiled_workflow(
            gold_like,
            gold_compiled,
            user_requirement=requirement,
        )
        pred_score = self.agent._score_compiled_workflow(
            pred_like,
            pred_compiled,
            user_requirement=requirement,
        )

        self.assertGreater(gold_score["score"], pred_score["score"])

    def test_score_compiled_workflow_prefers_search_from_summary_when_summary_requested(self) -> None:
        fake_skill = lambda: type(
            "Skill",
            (),
            {"input_schema": {"arg1": "str"}, "depends_on_all": [], "depends_on_any": []},
        )()
        self.agent.registry._meta_by_name.update(
            {
                "Text Simplifier": fake_skill(),
                "Text Summarizer": fake_skill(),
                "Text Sentiment Analysis": fake_skill(),
                "Keyword Extractor": fake_skill(),
                "Text Search": fake_skill(),
                "Topic Generator": fake_skill(),
            }
        )
        self.agent._tool_graph_planner = None
        self.agent._tool_graph_alias_to_skill = {}
        self.agent._skill_to_tool_graph_name = {}

        requirement = (
            "I have a long article about the effects of climate change on biodiversity. "
            "I need help in understanding the main ideas, sentiment, and important keywords. "
            "Simplify and summarize the text, then find some related topics on the internet. "
            "The article's content: 'Climate change is having significant effects on biodiversity...'"
        )
        better = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Text Simplifier", "arguments": ["Climate change is having significant effects on biodiversity..."]},
                {"task": "Text Summarizer", "arguments": ["<node-0>"]},
                {"task": "Text Sentiment Analysis", "arguments": ["<node-1>"]},
                {"task": "Keyword Extractor", "arguments": ["<node-1>"]},
                {"task": "Text Search", "arguments": ["<node-1>"]},
                {"task": "Topic Generator", "arguments": ["<node-4>"]},
            ],
            "task_links": [],
        }
        worse = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Text Simplifier", "arguments": ["Climate change is having significant effects on biodiversity..."]},
                {"task": "Text Summarizer", "arguments": ["<node-0>"]},
                {"task": "Text Sentiment Analysis", "arguments": ["<node-1>"]},
                {"task": "Keyword Extractor", "arguments": ["<node-1>"]},
                {"task": "Text Search", "arguments": ["<node-0>"]},
                {"task": "Topic Generator", "arguments": ["<node-4>"]},
            ],
            "task_links": [],
        }

        _, better_compiled = self.agent._prepare_workflow(better)
        _, worse_compiled = self.agent._prepare_workflow(worse)
        better_score = self.agent._score_compiled_workflow(
            better,
            better_compiled,
            user_requirement=requirement,
        )
        worse_score = self.agent._score_compiled_workflow(
            worse,
            worse_compiled,
            user_requirement=requirement,
        )

        self.assertGreater(better_score["score"], worse_score["score"])

    def test_score_compiled_workflow_penalizes_unrequested_translation_step(self) -> None:
        fake_skill = lambda: type(
            "Skill",
            (),
            {"input_schema": {"arg1": "str"}, "depends_on_all": [], "depends_on_any": []},
        )()
        self.agent.registry._meta_by_name.update(
            {
                "Audio-to-Text": fake_skill(),
                "Text Expander": fake_skill(),
                "Text Grammar Checker": fake_skill(),
                "Text Translator": fake_skill(),
                "Text-to-Audio": fake_skill(),
                "Voice Changer": fake_skill(),
            }
        )
        self.agent._tool_graph_planner = None
        self.agent._tool_graph_alias_to_skill = {}
        self.agent._skill_to_tool_graph_name = {}

        requirement = (
            "Provide a detailed and grammatically correct explanation in English audio "
            "with a female tone for the phrase 'Economic globalization' sourced from example.wav."
        )
        better = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Audio-to-Text", "arguments": ["example.wav"]},
                {"task": "Text Expander", "arguments": ["<node-0>"]},
                {"task": "Text Grammar Checker", "arguments": ["<node-1>"]},
                {"task": "Text-to-Audio", "arguments": ["<node-2>"]},
                {"task": "Voice Changer", "arguments": ["<node-3>", "female tone"]},
            ],
            "task_links": [],
        }
        worse = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Audio-to-Text", "arguments": ["example.wav"]},
                {"task": "Text Expander", "arguments": ["<node-0>"]},
                {"task": "Text Grammar Checker", "arguments": ["<node-1>"]},
                {"task": "Text Translator", "arguments": ["<node-2>"]},
                {"task": "Text-to-Audio", "arguments": ["<node-3>"]},
                {"task": "Voice Changer", "arguments": ["<node-4>", "female tone"]},
            ],
            "task_links": [],
        }

        _, better_compiled = self.agent._prepare_workflow(better)
        _, worse_compiled = self.agent._prepare_workflow(worse)
        better_score = self.agent._score_compiled_workflow(
            better,
            better_compiled,
            user_requirement=requirement,
        )
        worse_score = self.agent._score_compiled_workflow(
            worse,
            worse_compiled,
            user_requirement=requirement,
        )

        self.assertGreater(better_score["score"], worse_score["score"])

    def test_score_compiled_workflow_penalizes_unrequested_video_downloader_between_search_and_edit(self) -> None:
        fake_skill = lambda: type(
            "Skill",
            (),
            {"input_schema": {"arg1": "str"}, "depends_on_all": [], "depends_on_any": []},
        )()
        self.agent.registry._meta_by_name.update(
            {
                "Audio Effects": fake_skill(),
                "Audio-to-Text": fake_skill(),
                "Text Expander": fake_skill(),
                "Video Search": fake_skill(),
                "Video Downloader": fake_skill(),
                "Video Speed Changer": fake_skill(),
            }
        )
        self.agent._tool_graph_planner = None
        self.agent._tool_graph_alias_to_skill = {}
        self.agent._skill_to_tool_graph_name = {}

        requirement = (
            "I have an audio file, example.wav, and I want to apply some audio effects such as reverb and equalization to it. "
            "After applying the effects, I want the speech in the audio to be transcribed into text. "
            "Then, I would like to expand the transcribed text into a more detailed and descriptive version. "
            "Based on this detailed text, I want to find a relevant video on the internet. "
            "Finally, please speed up the video to 1.5x speed."
        )
        better = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Audio Effects", "arguments": ["example.wav", "reverb and equalization"]},
                {"task": "Audio-to-Text", "arguments": ["<node-0>"]},
                {"task": "Text Expander", "arguments": ["<node-1>"]},
                {"task": "Video Search", "arguments": ["<node-2>"]},
                {"task": "Video Speed Changer", "arguments": ["<node-3>", "1.5x speed"]},
            ],
            "task_links": [],
        }
        worse = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Audio Effects", "arguments": ["example.wav", "reverb and equalization"]},
                {"task": "Audio-to-Text", "arguments": ["<node-0>"]},
                {"task": "Text Expander", "arguments": ["<node-1>"]},
                {"task": "Video Search", "arguments": ["<node-2>"]},
                {"task": "Video Downloader", "arguments": ["<node-3>"]},
                {"task": "Video Speed Changer", "arguments": ["<node-4>", "1.5x speed"]},
            ],
            "task_links": [],
        }

        _, better_compiled = self.agent._prepare_workflow(better)
        _, worse_compiled = self.agent._prepare_workflow(worse)
        better_score = self.agent._score_compiled_workflow(
            better,
            better_compiled,
            user_requirement=requirement,
        )
        worse_score = self.agent._score_compiled_workflow(
            worse,
            worse_compiled,
            user_requirement=requirement,
        )

        self.assertGreater(better_score["score"], worse_score["score"])

    def test_score_compiled_workflow_penalizes_unrequested_simplifier_before_brainstorming(self) -> None:
        fake_skill = lambda: type(
            "Skill",
            (),
            {"input_schema": {"arg1": "str"}, "depends_on_all": [], "depends_on_any": []},
        )()
        self.agent.registry._meta_by_name.update(
            {
                "Text Simplifier": fake_skill(),
                "Topic Generator": fake_skill(),
                "Image Search": fake_skill(),
            }
        )
        self.agent._tool_graph_planner = None
        self.agent._tool_graph_alias_to_skill = {}
        self.agent._skill_to_tool_graph_name = {}

        requirement = (
            "I want to create a blog post about green living, but I only have a vague idea of what I want to write. "
            "I need help with brainstorming and finding relevant images. "
            "Here's my initial idea: 'Sustainable lifestyle, eco-friendly practices, and tips for going green.'"
        )
        better = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Topic Generator", "arguments": ["Sustainable lifestyle, eco-friendly practices, and tips for going green."]},
                {"task": "Image Search", "arguments": ["<node-0>"]},
            ],
            "task_links": [],
        }
        worse = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Text Simplifier", "arguments": ["Sustainable lifestyle, eco-friendly practices, and tips for going green."]},
                {"task": "Topic Generator", "arguments": ["<node-0>"]},
                {"task": "Image Search", "arguments": ["<node-1>"]},
            ],
            "task_links": [],
        }

        _, better_compiled = self.agent._prepare_workflow(better)
        _, worse_compiled = self.agent._prepare_workflow(worse)
        better_score = self.agent._score_compiled_workflow(
            better,
            better_compiled,
            user_requirement=requirement,
        )
        worse_score = self.agent._score_compiled_workflow(
            worse,
            worse_compiled,
            user_requirement=requirement,
        )

        self.assertGreater(better_score["score"], worse_score["score"])

    def test_candidate_selection_meta_marks_missing_actions_as_fail(self) -> None:
        requirement = (
            "I want to study the impact of climate change on polar bears. "
            "Can you help me find some easy-to-understand information, check its grammar, "
            "generate related topic ideas, and create an image to represent the main topic?"
        )
        workflow = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Text Simplifier", "arguments": ["climate change and polar bears"]},
                {"task": "Text Grammar Checker", "arguments": ["<node-0>"]},
                {"task": "Topic Generator", "arguments": ["<node-1>"]},
            ],
            "task_links": [],
        }

        normalized, compiled = self.agent._prepare_workflow(workflow)
        score_meta = self.agent._score_compiled_workflow(normalized, compiled, user_requirement=requirement)
        selection_meta = self.agent._candidate_selection_meta(
            normalized,
            compiled,
            user_requirement=requirement,
            score_details=score_meta["details"],
        )

        self.assertEqual(selection_meta["hard_filter_tier"], 0)
        self.assertFalse(selection_meta["coverage_complete"])
        self.assertTrue(any(item.startswith("missing_action:retrieval") for item in selection_meta["failures"]))
        self.assertFalse(selection_meta["repairable"])

    def test_candidate_selection_meta_marks_root_input_modality_mismatch_as_failure(self) -> None:
        fake_skill = lambda action_tags=None, schema=None: type(
            "Skill",
            (),
            {
                "input_schema": dict(schema or {"arg1": "str"}),
                "depends_on_all": [],
                "depends_on_any": [],
                "action_tags": list(action_tags or []),
            },
        )()
        self.agent.registry._meta_by_name.update(
            {
                "Video-to-Audio": fake_skill(),
                "Audio-to-Text": fake_skill(["transcribe"]),
            }
        )

        requirement = "Transcribe the speech from example.wav into text."
        workflow = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Video-to-Audio", "arguments": ["example.wav"]},
                {"task": "Audio-to-Text", "arguments": ["<node-0>"]},
            ],
            "task_links": [
                {"source": "Video-to-Audio", "target": "Audio-to-Text"},
            ],
        }

        normalized, compiled = self.agent._prepare_workflow(workflow)
        selection_meta = self.agent._candidate_selection_meta(
            normalized,
            compiled,
            user_requirement=requirement,
            score_details={},
        )

        self.assertEqual(selection_meta["hard_filter_tier"], 0)
        self.assertFalse(selection_meta["modality_ok"])
        self.assertTrue(
            any(item.startswith("root_input_modality_mismatch:0:Video-to-Audio") for item in selection_meta["failures"])
        )

    def test_candidate_selection_meta_marks_disconnected_candidate_dag_as_failure(self) -> None:
        fake_skill = lambda action_tags=None, schema=None: type(
            "Skill",
            (),
            {
                "input_schema": dict(schema or {"arg1": "str"}),
                "depends_on_all": [],
                "depends_on_any": [],
                "action_tags": list(action_tags or []),
            },
        )()
        self.agent.registry._meta_by_name.update(
            {
                "Text Simplifier": fake_skill(["simplify"]),
                "Text Search": fake_skill(["retrieval"]),
            }
        )

        requirement = "Simplify this article and then search online for related topics."
        workflow = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Text Simplifier", "arguments": ["article text"]},
                {"task": "Text Search", "arguments": ["related climate topics"]},
            ],
            "task_links": [],
        }

        normalized, compiled = self.agent._prepare_workflow(workflow)
        selection_meta = self.agent._candidate_selection_meta(
            normalized,
            compiled,
            user_requirement=requirement,
            score_details={},
        )

        self.assertEqual(selection_meta["hard_filter_tier"], 0)
        self.assertFalse(selection_meta["dag_ok"])
        self.assertTrue(any(item.startswith("isolated_nodes:") for item in selection_meta["failures"]))
        self.assertTrue(any(item.startswith("disconnected_components:") for item in selection_meta["failures"]))

    def test_candidate_selection_meta_marks_late_search_source_as_suspicious(self) -> None:
        requirement = (
            "I have a long article about the effects of climate change on biodiversity. "
            "I need help in understanding the main ideas, sentiment, and important keywords. "
            "Simplify and summarize the text, then find some related topics on the internet. "
            "The article's content: 'Climate change is having significant effects on biodiversity...'"
        )
        workflow = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Text Simplifier", "arguments": ["Climate change is having significant effects on biodiversity..."]},
                {"task": "Text Summarizer", "arguments": ["<node-0>"]},
                {"task": "Text Sentiment Analysis", "arguments": ["<node-1>"]},
                {"task": "Keyword Extractor", "arguments": ["<node-1>"]},
                {"task": "Text Search", "arguments": ["<node-3>"]},
                {"task": "Topic Generator", "arguments": ["<node-4>"]},
            ],
            "task_links": [],
        }

        normalized, compiled = self.agent._prepare_workflow(workflow)
        score_meta = self.agent._score_compiled_workflow(normalized, compiled, user_requirement=requirement)
        selection_meta = self.agent._candidate_selection_meta(
            normalized,
            compiled,
            user_requirement=requirement,
            score_details=score_meta["details"],
        )

        self.assertEqual(selection_meta["hard_filter_tier"], 1)
        self.assertFalse(selection_meta["search_source_ok"])
        self.assertTrue(any(item.startswith("search_uses_late_analysis_source") for item in selection_meta["warnings"]))
        self.assertTrue(selection_meta["repairable"])

    def test_candidate_selection_meta_marks_unrequested_image_downloader_bridge_as_warning(self) -> None:
        fake_skill = lambda action_tags=None, schema=None: type(
            "Skill",
            (),
            {
                "input_schema": dict(schema or {"arg1": "str"}),
                "depends_on_all": [],
                "depends_on_any": [],
                "action_tags": list(action_tags or []),
            },
        )()
        self.agent.registry._meta_by_name.update(
            {
                "Image Stitcher": fake_skill(["image"], {"arg1": "str", "arg2": "str"}),
                "Image Colorizer": fake_skill(["image"]),
                "Image Search (by Image)": fake_skill(["image"]),
                "Image Downloader": fake_skill(),
                "Image-to-Video": fake_skill(["video"], {"arg1": "str", "arg2": "str"}),
            }
        )

        requirement = (
            "I have two black and white images, 'example1.jpg' and 'example2.jpg', and I want to create "
            "a panoramic collage of the two images, colorize the panorama, find a similar color image, "
            "and then create a slideshow video using the original panorama and the found similar image."
        )
        workflow = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Image Stitcher", "arguments": ["example1.jpg", "example2.jpg"]},
                {"task": "Image Colorizer", "arguments": ["<node-0>"]},
                {"task": "Image Search (by Image)", "arguments": ["<node-1>"]},
                {"task": "Image Downloader", "arguments": ["<node-2>"]},
                {"task": "Image-to-Video", "arguments": ["<node-0>", "<node-3>"]},
            ],
            "task_links": [
                {"source": "Image Stitcher", "target": "Image Colorizer"},
                {"source": "Image Colorizer", "target": "Image Search (by Image)"},
                {"source": "Image Search (by Image)", "target": "Image Downloader"},
                {"source": "Image Stitcher", "target": "Image-to-Video"},
                {"source": "Image Downloader", "target": "Image-to-Video"},
            ],
        }

        normalized, compiled = self.agent._prepare_workflow(workflow)
        score_meta = self.agent._score_compiled_workflow(normalized, compiled, user_requirement=requirement)
        selection_meta = self.agent._candidate_selection_meta(
            normalized,
            compiled,
            user_requirement=requirement,
            score_details=score_meta["details"],
        )

        self.assertEqual(selection_meta["hard_filter_tier"], 1)
        self.assertFalse(selection_meta["bridge_tool_ok"])
        self.assertEqual(selection_meta["unrequested_bridge_tool_count"], 1)
        self.assertIn("unrequested_bridge_tool:image downloader", selection_meta["warnings"])
        self.assertTrue(selection_meta["repairable"])

    def test_candidate_selection_meta_marks_unrequested_article_spinner_bridge_as_warning(self) -> None:
        fake_skill = lambda action_tags=None, schema=None: type(
            "Skill",
            (),
            {
                "input_schema": dict(schema or {"arg1": "str"}),
                "depends_on_all": [],
                "depends_on_any": [],
                "action_tags": list(action_tags or []),
            },
        )()
        self.agent.registry._meta_by_name.update(
            {
                "Text Downloader": fake_skill(["retrieval"]),
                "Article Spinner": fake_skill(["simplify"]),
                "Text-to-Audio": fake_skill(),
                "Video Voiceover": fake_skill(["combine"], {"arg1": "str", "arg2": "str"}),
            }
        )

        requirement = (
            "I need to create an audio file of a voiceover for a given online article, "
            "and add it to my example.mp4 video. The URL of the article is "
            "https://example-article.com"
        )
        workflow = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Text Downloader", "arguments": ["https://example-article.com"]},
                {"task": "Article Spinner", "arguments": ["<node-0>"]},
                {"task": "Text-to-Audio", "arguments": ["<node-1>"]},
                {"task": "Video Voiceover", "arguments": ["<node-2>", "example.mp4"]},
            ],
            "task_links": [
                {"source": "Text Downloader", "target": "Article Spinner"},
                {"source": "Article Spinner", "target": "Text-to-Audio"},
                {"source": "Text-to-Audio", "target": "Video Voiceover"},
            ],
        }

        normalized, compiled = self.agent._prepare_workflow(workflow)
        score_meta = self.agent._score_compiled_workflow(normalized, compiled, user_requirement=requirement)
        selection_meta = self.agent._candidate_selection_meta(
            normalized,
            compiled,
            user_requirement=requirement,
            score_details=score_meta["details"],
        )

        self.assertFalse(selection_meta["bridge_tool_ok"])
        self.assertIn("unrequested_bridge_tool:article spinner", selection_meta["warnings"])
        self.assertTrue(selection_meta["repairable"])

    def test_candidate_selection_meta_does_not_flag_requested_video_synchronization(self) -> None:
        fake_skill = lambda action_tags=None, schema=None: type(
            "Skill",
            (),
            {
                "input_schema": dict(schema or {"arg1": "str"}),
                "depends_on_all": [],
                "depends_on_any": [],
                "action_tags": list(action_tags or []),
            },
        )()
        self.agent.registry._meta_by_name.update(
            {
                "Text Translator": fake_skill(["translate"]),
                "Video Search": fake_skill(),
                "Video Synchronization": fake_skill(["combine"], {"arg1": "str", "arg2": "str"}),
            }
        )

        requirement = (
            "I have a Spanish text: 'Hola, me gustaria aprender a cocinar paella.' "
            "Please translate this into English, find a video tutorial that teaches how to cook paella, "
            "and synchronize the timing of example.wav with the visuals of the video."
        )
        workflow = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Text Translator", "arguments": ["Hola, me gustaria aprender a cocinar paella."]},
                {"task": "Video Search", "arguments": ["how to cook paella video tutorial"]},
                {"task": "Video Synchronization", "arguments": ["<node-1>", "example.wav"]},
            ],
            "task_links": [],
        }

        normalized, compiled = self.agent._prepare_workflow(workflow)
        score_meta = self.agent._score_compiled_workflow(normalized, compiled, user_requirement=requirement)
        selection_meta = self.agent._candidate_selection_meta(
            normalized,
            compiled,
            user_requirement=requirement,
            score_details=score_meta["details"],
        )

        self.assertTrue(selection_meta["bridge_tool_ok"])
        self.assertEqual(selection_meta["unrequested_bridge_tool_count"], 0)
        self.assertFalse(
            any(item == "unrequested_bridge_tool:video synchronization" for item in selection_meta["warnings"])
        )

    def test_score_compiled_workflow_does_not_over_penalize_complete_longer_plan(self) -> None:
        fake_skill = lambda: type(
            "Skill",
            (),
            {"input_schema": {"arg1": "str"}, "depends_on_all": [], "depends_on_any": [], "action_tags": []},
        )()
        self.agent.registry._meta_by_name.update(
            {
                "Text Simplifier": fake_skill(),
                "Text Summarizer": fake_skill(),
                "Text Sentiment Analysis": fake_skill(),
                "Keyword Extractor": fake_skill(),
                "Text Search": fake_skill(),
                "Topic Generator": fake_skill(),
            }
        )

        requirement = (
            "I have a long article about the effects of climate change on biodiversity. "
            "I need help in understanding the main ideas, sentiment, and important keywords. "
            "Simplify and summarize the text, then find some related topics on the internet."
        )
        complete = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Text Simplifier", "arguments": ["article text"]},
                {"task": "Text Summarizer", "arguments": ["<node-0>"]},
                {"task": "Text Sentiment Analysis", "arguments": ["<node-0>"]},
                {"task": "Keyword Extractor", "arguments": ["<node-0>"]},
                {"task": "Text Search", "arguments": ["<node-0>"]},
                {"task": "Topic Generator", "arguments": ["<node-4>"]},
            ],
            "task_links": [],
        }
        incomplete = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Text Simplifier", "arguments": ["article text"]},
                {"task": "Text Summarizer", "arguments": ["<node-0>"]},
                {"task": "Text Search", "arguments": ["<node-0>"]},
                {"task": "Topic Generator", "arguments": ["<node-2>"]},
            ],
            "task_links": [],
        }

        _, complete_compiled = self.agent._prepare_workflow(complete)
        _, incomplete_compiled = self.agent._prepare_workflow(incomplete)
        complete_score = self.agent._score_compiled_workflow(complete, complete_compiled, user_requirement=requirement)
        incomplete_score = self.agent._score_compiled_workflow(incomplete, incomplete_compiled, user_requirement=requirement)

        self.assertGreater(complete_score["score"], incomplete_score["score"])

    def test_score_compiled_workflow_reports_composite_prior_and_executability_scores(self) -> None:
        fake_skill = lambda: type(
            "Skill",
            (),
            {"input_schema": {"arg1": "str"}, "depends_on_all": [], "depends_on_any": [], "action_tags": []},
        )()
        self.agent.registry._meta_by_name.update(
            {
                "Text Simplifier": fake_skill(),
                "Text Search": fake_skill(),
                "Text Grammar Checker": fake_skill(),
            }
        )
        memory = WorkflowMemoryIndex(
            motifs=[
                WorkflowMemoryMotif(
                    motif_id="Text Simplifier -> Text Search -> Text Grammar Checker",
                    tasks=("Text Simplifier", "Text Search", "Text Grammar Checker"),
                    links=(
                        ("Text Simplifier", "Text Search"),
                        ("Text Search", "Text Grammar Checker"),
                    ),
                    action_tags=("simplify", "retrieval", "grammar"),
                    support=14,
                ),
            ],
            transition_counts={
                ("Text Simplifier", "Text Search"): 14,
                ("Text Search", "Text Grammar Checker"): 11,
            },
            start_counts={"Text Simplifier": 11},
            end_counts={"Text Grammar Checker": 11},
        )
        self._attach_test_workflow_memory(memory)

        requirement = "Make this easy to understand, search online, and check the grammar."
        workflow = {
            "task_steps": [
                "Step 1: Call Text Simplifier with arg1=climate change article.",
                "Step 2: Call Text Search with arg1=<node-0>.",
                "Step 3: Call Text Grammar Checker with arg1=<node-1>.",
            ],
            "task_nodes": [
                {"task": "Text Simplifier", "arguments": ["climate change article"]},
                {"task": "Text Search", "arguments": ["<node-0>"]},
                {"task": "Text Grammar Checker", "arguments": ["<node-1>"]},
            ],
            "task_links": [
                {"source": "Text Simplifier", "target": "Text Search"},
                {"source": "Text Search", "target": "Text Grammar Checker"},
            ],
        }

        normalized, compiled = self.agent._prepare_workflow(workflow)
        verification = self.agent._verify_candidate_workflow(normalized, compiled, requirement)
        score_meta = self.agent._score_compiled_workflow(
            normalized,
            compiled,
            user_requirement=requirement,
            verification_meta=verification,
        )
        details = score_meta["details"]

        self.assertGreater(details["action_coverage_score"], 0.0)
        self.assertGreater(details["schema_validity_score"], 0.0)
        self.assertGreater(details["dependency_correctness_score"], 0.0)
        self.assertGreater(details["modality_consistency_score"], 0.0)
        self.assertGreater(details["dag_validity_score"], 0.0)
        self.assertGreater(details["executability_score"], 0.0)
        self.assertGreater(details["start_prior_score"], 0.0)
        self.assertGreater(details["transition_prior_score"], 0.0)
        self.assertGreater(details["motif_match_score"], 0.0)
        self.assertGreater(details["memory_prior_score"], 0.0)

    def test_validate_plan_accepts_workflow(self) -> None:
        workflow = {
            "task_steps": [
                "Step 1: Call load_csv with path=not_exists.csv.",
                "Step 2: Call filter_rows with arg1=<node-0>, field=region, op=eq, value=east.",
            ],
            "task_nodes": [
                {"task": "load_csv", "arguments": [{"name": "path", "value": "not_exists.csv"}]},
                {
                    "task": "filter_rows",
                    "arguments": [
                        {"name": "arg1", "value": "<node-0>"},
                        {"name": "field", "value": "region"},
                        {"name": "op", "value": "eq"},
                        {"name": "value", "value": "east"},
                    ],
                },
            ],
            "task_links": [{"source": "load_csv", "target": "filter_rows"}],
        }

        self.agent.validate_plan(workflow)

    async def test_execute_plan_accepts_workflow(self) -> None:
        workflow = {
            "task_steps": [
                "Step 1: Call load_csv with path=not_exists.csv.",
                "Step 2: Call filter_rows with arg1=<node-0>, field=region, op=eq, value=east.",
                "Step 3: Call aggregate_sum with arg1=<node-1>, field=sales.",
            ],
            "task_nodes": [
                {"task": "load_csv", "arguments": [{"name": "path", "value": "not_exists.csv"}]},
                {
                    "task": "filter_rows",
                    "arguments": [
                        {"name": "arg1", "value": "<node-0>"},
                        {"name": "field", "value": "region"},
                        {"name": "op", "value": "eq"},
                        {"name": "value", "value": "east"},
                    ],
                },
                {
                    "task": "aggregate_sum",
                    "arguments": [
                        {"name": "arg1", "value": "<node-1>"},
                        {"name": "field", "value": "sales"},
                    ],
                },
            ],
            "task_links": [
                {"source": "load_csv", "target": "filter_rows"},
                {"source": "filter_rows", "target": "aggregate_sum"},
            ],
        }

        execution = await self.agent.execute_plan(workflow)

        self.assertTrue(all(step["ok"] for step in execution["results"]))
        self.assertEqual(execution["context"]["last_total"], 180.0)

    async def test_execute_plan_success(self) -> None:
        plan = {
            "task_steps": [
                "Step 1: Call load_csv with path=not_exists.csv.",
                "Step 2: Call filter_rows with field=region, op=eq, value=east.",
                "Step 3: Call aggregate_sum with field=sales.",
                "Step 4: Call generate_report with title=east sales.",
            ],
            "task_nodes": [
                {"task": "load_csv", "arguments": [{"name": "path", "value": "not_exists.csv"}]},
                {
                    "task": "filter_rows",
                    "arguments": [
                        {"name": "field", "value": "region"},
                        {"name": "op", "value": "eq"},
                        {"name": "value", "value": "east"},
                    ],
                },
                {"task": "aggregate_sum", "arguments": [{"name": "field", "value": "sales"}]},
                {"task": "generate_report", "arguments": [{"name": "title", "value": "east sales"}]},
            ],
            "task_links": [],
        }
        self.agent.validate_plan(plan)
        execution = await self.agent.execute_plan(plan)

        self.assertEqual(len(execution["results"]), 4)
        self.assertTrue(all(step["ok"] for step in execution["results"]))
        self.assertEqual(execution["context"]["last_total"], 180.0)
        self.assertEqual(execution["context"]["report"]["title"], "east sales")

    async def test_execute_plan_operator_alias_eq(self) -> None:
        plan = {
            "task_steps": [
                "Step 1: Call load_csv with path=not_exists.csv.",
                "Step 2: Call filter_rows with field=region, op===, value=east.",
                "Step 3: Call aggregate_sum with field=sales.",
            ],
            "task_nodes": [
                {"task": "load_csv", "arguments": [{"name": "path", "value": "not_exists.csv"}]},
                {
                    "task": "filter_rows",
                    "arguments": [
                        {"name": "field", "value": "region"},
                        {"name": "op", "value": "=="},
                        {"name": "value", "value": "east"},
                    ],
                },
                {"task": "aggregate_sum", "arguments": [{"name": "field", "value": "sales"}]},
            ],
            "task_links": [],
        }
        execution = await self.agent.execute_plan(plan)
        self.assertTrue(all(step["ok"] for step in execution["results"]))
        self.assertEqual(execution["context"]["last_total"], 180.0)

    async def test_execute_plan_handles_operator_error(self) -> None:
        plan = {
            "task_steps": [
                "Step 1: Call load_csv with path=not_exists.csv.",
                "Step 2: Call filter_rows with field=region, op=bad_op, value=east.",
            ],
            "task_nodes": [
                {"task": "load_csv", "arguments": [{"name": "path", "value": "not_exists.csv"}]},
                {
                    "task": "filter_rows",
                    "arguments": [
                        {"name": "field", "value": "region"},
                        {"name": "op", "value": "bad_op"},
                        {"name": "value", "value": "east"},
                    ],
                },
            ],
            "task_links": [],
        }
        execution = await self.agent.execute_plan(plan)
        self.assertTrue(execution["results"][0]["ok"])
        self.assertFalse(execution["results"][1]["ok"])
        self.assertIn("unsupported op", execution["results"][1]["error"])

    def test_recommend_memory_start_and_next_skills_follow_query_conditioned_graph(self) -> None:
        memory = WorkflowMemoryIndex(
            motifs=[
                WorkflowMemoryMotif(
                    motif_id="Text Simplifier -> Text Search",
                    tasks=("Text Simplifier", "Text Search"),
                    links=(("Text Simplifier", "Text Search"),),
                    action_tags=("simplify", "retrieval"),
                    support=12,
                ),
                WorkflowMemoryMotif(
                    motif_id="Text Search -> Text Grammar Checker",
                    tasks=("Text Search", "Text Grammar Checker"),
                    links=(("Text Search", "Text Grammar Checker"),),
                    action_tags=("retrieval", "grammar"),
                    support=9,
                ),
            ],
            transition_counts={
                ("Text Simplifier", "Text Search"): 12,
                ("Text Simplifier", "Topic Generator"): 2,
                ("Text Search", "Text Grammar Checker"): 9,
            },
            start_counts={"Text Simplifier": 10, "Audio-to-Text": 2},
            end_counts={"Text Grammar Checker": 9, "Topic Generator": 2},
        )
        self._attach_test_workflow_memory(memory)

        requirement = (
            "I want easy-to-understand information about climate change, then search online "
            "for details and check the grammar."
        )

        start_recs = self.agent.recommend_memory_start_skills(requirement, top_k=2)
        self.assertTrue(start_recs)
        self.assertEqual(start_recs[0]["skill"], "Text Simplifier")

        next_recs = self.agent.recommend_memory_next_skills(
            requirement,
            "Text Simplifier",
            top_k=2,
            visited_skills={"Text Simplifier"},
        )
        self.assertTrue(next_recs)
        self.assertEqual(next_recs[0]["skill"], "Text Search")

    def test_recommend_memory_graph_penalizes_unrequested_article_spinner_for_url_voiceover_query(self) -> None:
        memory = WorkflowMemoryIndex(
            motifs=[
                WorkflowMemoryMotif(
                    motif_id="Text Downloader -> Article Spinner -> Text-to-Audio",
                    tasks=("Text Downloader", "Article Spinner", "Text-to-Audio"),
                    links=(
                        ("Text Downloader", "Article Spinner"),
                        ("Article Spinner", "Text-to-Audio"),
                    ),
                    action_tags=("simplify",),
                    support=15,
                ),
                WorkflowMemoryMotif(
                    motif_id="Text Downloader -> Text-to-Audio -> Video Voiceover",
                    tasks=("Text Downloader", "Text-to-Audio", "Video Voiceover"),
                    links=(
                        ("Text Downloader", "Text-to-Audio"),
                        ("Text-to-Audio", "Video Voiceover"),
                    ),
                    action_tags=("combine",),
                    support=8,
                ),
            ],
            transition_counts={
                ("Text Downloader", "Article Spinner"): 18,
                ("Article Spinner", "Text-to-Audio"): 14,
                ("Text Downloader", "Text-to-Audio"): 7,
                ("Text-to-Audio", "Video Voiceover"): 9,
            },
            start_counts={
                "Article Spinner": 16,
                "Text Downloader": 6,
                "Video Voiceover": 5,
            },
            end_counts={"Video Voiceover": 9, "Text-to-Audio": 14},
        )
        self._attach_test_workflow_memory(memory)

        requirement = (
            "I need to create an audio file of a voiceover for a given online article, "
            "and add it to my example.mp4 video. The URL of the article is "
            "https://example-article.com"
        )

        start_recs = self.agent.recommend_memory_start_skills(requirement, top_k=3)
        self.assertTrue(start_recs)
        self.assertEqual(start_recs[0]["skill"], "Text Downloader")
        self.assertNotEqual(start_recs[0]["skill"], "Article Spinner")

        next_recs = self.agent.recommend_memory_next_skills(
            requirement,
            "Text Downloader",
            top_k=3,
            visited_skills={"Text Downloader"},
        )
        self.assertTrue(next_recs)
        self.assertEqual(next_recs[0]["skill"], "Text-to-Audio")
        self.assertNotIn("Article Spinner", [str(item.get("skill", "")) for item in next_recs[:2]])

    def test_build_candidate_strategy_specs_does_not_include_memory_graph_guidance(self) -> None:
        memory = WorkflowMemoryIndex(
            motifs=[
                WorkflowMemoryMotif(
                    motif_id="Text Simplifier -> Text Search -> Text Grammar Checker",
                    tasks=("Text Simplifier", "Text Search", "Text Grammar Checker"),
                    links=(
                        ("Text Simplifier", "Text Search"),
                        ("Text Search", "Text Grammar Checker"),
                    ),
                    action_tags=("simplify", "retrieval", "grammar"),
                    support=14,
                ),
            ],
            transition_counts={
                ("Text Simplifier", "Text Search"): 14,
                ("Text Search", "Text Grammar Checker"): 11,
            },
            start_counts={"Text Simplifier": 11},
            end_counts={"Text Grammar Checker": 11},
        )
        self._attach_test_workflow_memory(memory)

        requirement = (
            "Make this easy to understand, search for information online, and check the grammar."
        )
        specs = self.agent._build_candidate_strategy_specs(requirement)

        names = [str(spec.get("name", "")) for spec in specs]
        self.assertNotIn("memory_graph_guided", names)
        self.assertFalse(any(name.startswith("memory_graph_start_") for name in names))
        self.assertFalse(any(name.startswith("memory_graph_motif_") for name in names))

    def test_build_candidate_strategy_specs_adds_source_media_clause_coverage(self) -> None:
        requirement = (
            "Provide a detailed and grammatically correct explanation in English audio "
            "with a female tone for the phrase 'Economic globalization' sourced from example.wav."
        )

        specs = self.agent._build_candidate_strategy_specs(requirement)
        names = [str(spec.get("name", "")) for spec in specs]
        self.assertIn("source_media_clause_coverage", names)

    def test_build_candidate_strategy_specs_adds_instruction_branch_preserving(self) -> None:
        requirement = (
            "I've recorded two separate audio clips, 'example.wav' and 'example2.wav'. "
            "Please merge them, add a reverb effect, and make sure the instructions are understandable enough for my software tools."
        )

        specs = self.agent._build_candidate_strategy_specs(requirement)
        names = [str(spec.get("name", "")) for spec in specs]
        self.assertIn("instruction_branch_preserving", names)

    async def test_plan_candidates_does_not_pass_memory_graph_hint_to_generator(self) -> None:
        memory = WorkflowMemoryIndex(
            motifs=[
                WorkflowMemoryMotif(
                    motif_id="load_csv -> filter_rows -> aggregate_sum",
                    tasks=("load_csv", "filter_rows", "aggregate_sum"),
                    links=(
                        ("load_csv", "filter_rows"),
                        ("filter_rows", "aggregate_sum"),
                    ),
                    action_tags=(),
                    support=6,
                ),
            ],
            transition_counts={
                ("load_csv", "filter_rows"): 6,
                ("filter_rows", "aggregate_sum"): 6,
            },
            start_counts={"load_csv": 6},
            end_counts={"aggregate_sum": 6},
        )
        self._attach_test_workflow_memory(memory)

        captured_hints: List[str] = []

        async def fake_plan(_req: str, strategy_hint: str | None = None, llm_client=None):
            _ = llm_client
            captured_hints.append(str(strategy_hint or ""))
            return {
                "task_steps": [
                    "Step 1: Call load_csv with path=not_exists.csv.",
                    "Step 2: Call aggregate_sum with arg1=<node-0>, field=sales.",
                ],
                "task_nodes": [
                    {"task": "load_csv", "arguments": [{"name": "path", "value": "not_exists.csv"}]},
                    {
                        "task": "aggregate_sum",
                        "arguments": [
                            {"name": "arg1", "value": "<node-0>"},
                            {"name": "field", "value": "sales"},
                        ],
                    },
                ],
                "task_links": [{"source": "load_csv", "target": "aggregate_sum"}],
            }

        self.agent._plan_with_client = fake_plan  # type: ignore[method-assign]
        self.agent._get_candidate_llm = lambda _temp: object()  # type: ignore[method-assign]

        await self.agent.plan_candidates("Load not_exists.csv and sum the sales field.", candidate_count=1)

        self.assertTrue(captured_hints)
        self.assertNotIn("query-conditioned workflow memory priors", captured_hints[0])
        self.assertFalse(any("query-conditioned workflow memory priors" in hint for hint in captured_hints))
        self.assertFalse(any("Try a valid workflow that starts from" in hint for hint in captured_hints))

    async def test_plan_candidates_deduplicates(self) -> None:
        plans = [
            {
                "task_steps": [
                    "Step 1: Call load_csv with path=not_exists.csv.",
                    "Step 2: Call aggregate_sum with arg1=<node-0>, field=sales.",
                ],
                "task_nodes": [
                    {"task": "load_csv", "arguments": [{"name": "path", "value": "not_exists.csv"}]},
                    {
                        "task": "aggregate_sum",
                        "arguments": [
                            {"name": "arg1", "value": "<node-0>"},
                            {"name": "field", "value": "sales"},
                        ],
                    },
                ],
                "task_links": [{"source": "load_csv", "target": "aggregate_sum"}],
            },
            {
                "task_steps": [
                    "Step 1: Call load_csv with path=not_exists.csv.",
                    "Step 2: Call aggregate_sum with arg1=<node-0>, field=sales.",
                ],
                "task_nodes": [
                    {"task": "load_csv", "arguments": [{"name": "path", "value": "not_exists.csv"}]},
                    {
                        "task": "aggregate_sum",
                        "arguments": [
                            {"name": "arg1", "value": "<node-0>"},
                            {"name": "field", "value": "sales"},
                        ],
                    },
                ],
                "task_links": [{"source": "load_csv", "target": "aggregate_sum"}],
            },
            {
                "task_steps": [
                    "Step 1: Call load_csv with path=not_exists.csv.",
                    "Step 2: Call filter_rows with arg1=<node-0>, field=region, op=eq, value=east.",
                    "Step 3: Call aggregate_sum with arg1=<node-1>, field=sales.",
                ],
                "task_nodes": [
                    {"task": "load_csv", "arguments": [{"name": "path", "value": "not_exists.csv"}]},
                    {
                        "task": "filter_rows",
                        "arguments": [
                            {"name": "arg1", "value": "<node-0>"},
                            {"name": "field", "value": "region"},
                            {"name": "op", "value": "eq"},
                            {"name": "value", "value": "east"},
                        ],
                    },
                    {
                        "task": "aggregate_sum",
                        "arguments": [
                            {"name": "arg1", "value": "<node-1>"},
                            {"name": "field", "value": "sales"},
                        ],
                    },
                ],
                "task_links": [
                    {"source": "load_csv", "target": "filter_rows"},
                    {"source": "filter_rows", "target": "aggregate_sum"},
                ],
            },
        ]
        state = {"idx": 0}

        async def fake_plan(_req: str, strategy_hint: str | None = None, llm_client=None):
            _ = (strategy_hint, llm_client)
            i = state["idx"]
            state["idx"] = min(i + 1, len(plans) - 1)
            return plans[i]

        self.agent._plan_with_client = fake_plan  # type: ignore[method-assign]
        self.agent._get_candidate_llm = lambda _temp: object()  # type: ignore[method-assign]
        candidates = await self.agent.plan_candidates("test", candidate_count=2)
        self.assertEqual(len(candidates), 2)
        self.assertTrue(all(candidate.get("strategy_name") for candidate in candidates))

    async def test_plan_candidates_skips_failed_attempts(self) -> None:
        state = {"calls": 0}

        async def flaky_plan(_req: str, strategy_hint: str | None = None, llm_client=None):
            _ = (strategy_hint, llm_client)
            state["calls"] += 1
            if state["calls"] == 1:
                raise RuntimeError("transient gateway error")
            return {
                "task_steps": [
                    "Step 1: Call load_csv with path=not_exists.csv.",
                    "Step 2: Call aggregate_sum with arg1=<node-0>, field=sales.",
                ],
                "task_nodes": [
                    {"task": "load_csv", "arguments": [{"name": "path", "value": "not_exists.csv"}]},
                    {
                        "task": "aggregate_sum",
                        "arguments": [
                            {"name": "arg1", "value": "<node-0>"},
                            {"name": "field", "value": "sales"},
                        ],
                    },
                ],
                "task_links": [{"source": "load_csv", "target": "aggregate_sum"}],
            }

        self.agent._plan_with_client = flaky_plan  # type: ignore[method-assign]
        self.agent._get_candidate_llm = lambda _temp: object()  # type: ignore[method-assign]
        candidates = await self.agent.plan_candidates("test", candidate_count=1)

        self.assertEqual(len(candidates), 1)
        self.assertGreaterEqual(state["calls"], 2)

    async def test_plan_candidates_applies_repaired_candidate_when_verifier_flags_repairable_issue(self) -> None:
        requirement = "Load not_exists.csv and sum the sales field."
        raw_plan = {
            "task_steps": [
                "Step 1: Call load_csv with path=not_exists.csv.",
                "Step 2: Call aggregate_sum with arg1=<node-0>, field=sales.",
            ],
            "task_nodes": [
                {"task": "load_csv", "arguments": [{"name": "path", "value": "not_exists.csv"}]},
                {
                    "task": "aggregate_sum",
                    "arguments": [
                        {"name": "arg1", "value": "<node-0>"},
                        {"name": "field", "value": "sales"},
                    ],
                },
            ],
            "task_links": [],
        }
        repaired_plan = {
            "task_steps": [
                "Step 1: Call load_csv with path=not_exists.csv.",
                "Step 2: Call aggregate_sum with arg1=<node-0>, field=sales.",
            ],
            "task_nodes": [
                {"task": "load_csv", "arguments": [{"name": "path", "value": "not_exists.csv"}]},
                {
                    "task": "aggregate_sum",
                    "arguments": [
                        {"name": "arg1", "value": "<node-0>"},
                        {"name": "field", "value": "sales"},
                    ],
                },
            ],
            "task_links": [{"source": "load_csv", "target": "aggregate_sum"}],
        }

        async def fake_plan(_req: str, strategy_hint: str | None = None, llm_client=None):
            _ = (strategy_hint, llm_client)
            return raw_plan

        async def fake_repair(
            _req: str,
            workflow: dict[str, object],
            verification_meta: dict[str, object],
            llm_client=None,
        ):
            _ = (workflow, verification_meta, llm_client)
            return repaired_plan

        self.agent._plan_with_client = fake_plan  # type: ignore[method-assign]
        self.agent._repair_candidate_with_llm = fake_repair  # type: ignore[method-assign]
        self.agent._get_candidate_llm = lambda _temp: object()  # type: ignore[method-assign]

        candidates = await self.agent.plan_candidates(requirement, candidate_count=1)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["workflow"]["task_nodes"][0]["task"], "load_csv")
        self.assertEqual(candidates[0]["workflow"]["task_links"], [{"source": "load_csv", "target": "aggregate_sum"}])
        self.assertTrue(candidates[0]["repair_meta"]["attempted"])
        self.assertTrue(candidates[0]["repair_meta"]["applied"])
        self.assertEqual(candidates[0]["selection_meta"]["hard_filter_tier"], 2)

    async def test_plan_candidates_filters_hard_fail_candidates_when_executable_candidate_exists(self) -> None:
        bad_plan = {
            "task_steps": [
                "Step 1: Call load_csv with path=not_exists.csv.",
                "Step 2: Call aggregate_sum with arg1=<node-3>, field=sales.",
            ],
            "task_nodes": [
                {"task": "load_csv", "arguments": [{"name": "path", "value": "not_exists.csv"}]},
                {
                    "task": "aggregate_sum",
                    "arguments": [
                        {"name": "arg1", "value": "<node-3>"},
                        {"name": "field", "value": "sales"},
                    ],
                },
            ],
            "task_links": [{"source": "load_csv", "target": "aggregate_sum"}],
        }
        good_plan = {
            "task_steps": [
                "Step 1: Call load_csv with path=not_exists.csv.",
                "Step 2: Call aggregate_sum with arg1=<node-0>, field=sales.",
            ],
            "task_nodes": [
                {"task": "load_csv", "arguments": [{"name": "path", "value": "not_exists.csv"}]},
                {
                    "task": "aggregate_sum",
                    "arguments": [
                        {"name": "arg1", "value": "<node-0>"},
                        {"name": "field", "value": "sales"},
                    ],
                },
            ],
            "task_links": [{"source": "load_csv", "target": "aggregate_sum"}],
        }
        state = {"calls": 0}

        async def fake_plan(_req: str, strategy_hint: str | None = None, llm_client=None):
            _ = (strategy_hint, llm_client)
            state["calls"] += 1
            return bad_plan if state["calls"] == 1 else good_plan

        self.agent._plan_with_client = fake_plan  # type: ignore[method-assign]
        self.agent._get_candidate_llm = lambda _temp: object()  # type: ignore[method-assign]

        candidates = await self.agent.plan_candidates("Load not_exists.csv and sum the sales field.", candidate_count=2)

        self.assertEqual(len(candidates), 1)
        self.assertTrue(all(candidate["selection_meta"]["hard_filter_tier"] > 0 for candidate in candidates))
        self.assertEqual(candidates[0]["workflow"]["task_nodes"][0]["task"], "load_csv")

    async def test_select_best_candidate_prefers_shorter_plan(self) -> None:
        long_plan = {
            "task_steps": [
                "Step 1: Call load_csv with path=not_exists.csv.",
                "Step 2: Call filter_rows with field=region, op=eq, value=east.",
                "Step 3: Call aggregate_sum with field=sales.",
            ],
            "task_nodes": [
                {"task": "load_csv", "arguments": [{"name": "path", "value": "not_exists.csv"}]},
                {
                    "task": "filter_rows",
                    "arguments": [
                        {"name": "field", "value": "region"},
                        {"name": "op", "value": "eq"},
                        {"name": "value", "value": "east"},
                    ],
                },
                {"task": "aggregate_sum", "arguments": [{"name": "field", "value": "sales"}]},
            ],
            "task_links": [],
        }
        short_plan = {
            "task_steps": [
                "Step 1: Call load_csv with path=not_exists.csv.",
                "Step 2: Call aggregate_sum with field=sales.",
            ],
            "task_nodes": [
                {"task": "load_csv", "arguments": [{"name": "path", "value": "not_exists.csv"}]},
                {"task": "aggregate_sum", "arguments": [{"name": "field", "value": "sales"}]},
            ],
            "task_links": [],
        }
        candidates = [
            {"id": 1, "workflow": long_plan, "score": self.agent._score_plan(long_plan)["score"]},
            {"id": 2, "workflow": short_plan, "score": self.agent._score_plan(short_plan)["score"]},
        ]
        selected = self.agent._select_best_candidate(candidates)
        self.assertEqual(selected["id"], 2)

    async def test_select_best_candidate_prefers_hard_filter_pass_candidate(self) -> None:
        candidates = [
            {
                "id": 1,
                "workflow": {"task_steps": [], "task_nodes": [], "task_links": []},
                "score": 140.0,
                "score_details": {"missing_actions": ["retrieval"], "length_penalty": 0.0},
                "selection_meta": {"hard_filter_tier": 0, "coverage_complete": False, "warning_count": 0},
            },
            {
                "id": 2,
                "workflow": {"task_steps": [], "task_nodes": [], "task_links": []},
                "score": 130.0,
                "score_details": {"missing_actions": [], "length_penalty": -2.0},
                "selection_meta": {
                    "hard_filter_tier": 2,
                    "coverage_complete": True,
                    "search_source_ok": True,
                    "retrieval_before_topic_ok": True,
                    "video_after_waveform_ok": True,
                    "warning_count": 0,
                },
            },
        ]

        selected = self.agent._select_best_candidate(candidates)
        self.assertEqual(selected["id"], 2)

    async def test_select_best_candidate_breaks_ties_with_text_preference(self) -> None:
        candidates = [
            {
                "id": 1,
                "workflow": {"task_steps": [], "task_nodes": [], "task_links": []},
                "score": 120.0,
                "score_details": {
                    "action_coverage_bonus": 24.0,
                    "action_coverage_penalty": 0.0,
                    "text_preference_bonus": 0.0,
                    "text_preference_penalty": 0.0,
                    "graph_transition_bonus": 0.0,
                    "graph_transition_penalty": 0.0,
                    "name_modality_bonus": 0.0,
                    "name_modality_penalty": 0.0,
                    "transition_bonus": 0.0,
                    "transition_penalty": 0.0,
                    "length_penalty": -20.0,
                    "missing_actions": [],
                },
            },
            {
                "id": 2,
                "workflow": {"task_steps": [], "task_nodes": [], "task_links": []},
                "score": 120.0,
                "score_details": {
                    "action_coverage_bonus": 24.0,
                    "action_coverage_penalty": 0.0,
                    "text_preference_bonus": 4.0,
                    "text_preference_penalty": 0.0,
                    "graph_transition_bonus": 0.0,
                    "graph_transition_penalty": 0.0,
                    "name_modality_bonus": 0.0,
                    "name_modality_penalty": 0.0,
                    "transition_bonus": 0.0,
                    "transition_penalty": 0.0,
                    "length_penalty": -20.0,
                    "missing_actions": [],
                },
            },
        ]

        selected = self.agent._select_best_candidate(candidates)
        self.assertEqual(selected["id"], 2)

    async def test_select_best_candidate_breaks_ties_with_memory_prior_score(self) -> None:
        candidates = [
            {
                "id": 1,
                "workflow": {"task_steps": [], "task_nodes": [], "task_links": []},
                "score": 120.0,
                "score_details": {
                    "action_coverage_score": 24.0,
                    "executability_score": 22.0,
                    "memory_prior_score": 1.0,
                    "text_preference_bonus": 0.0,
                    "text_preference_penalty": 0.0,
                    "name_modality_bonus": 0.0,
                    "name_modality_penalty": 0.0,
                    "transition_bonus": 0.0,
                    "transition_penalty": 0.0,
                    "redundancy_penalty": 0.0,
                    "extra_action_penalty": 0.0,
                    "length_penalty": -6.0,
                    "missing_actions": [],
                },
                "selection_meta": {
                    "hard_filter_tier": 2,
                    "coverage_complete": True,
                    "dag_ok": True,
                    "schema_ok": True,
                    "dependency_ok": True,
                    "modality_ok": True,
                    "bridge_tool_ok": True,
                    "unrequested_bridge_tool_count": 0,
                    "search_source_ok": True,
                    "retrieval_before_topic_ok": True,
                    "grammar_after_retrieval_ok": True,
                    "video_after_waveform_ok": True,
                    "warning_count": 0,
                    "extra_actions": [],
                },
            },
            {
                "id": 2,
                "workflow": {"task_steps": [], "task_nodes": [], "task_links": []},
                "score": 120.0,
                "score_details": {
                    "action_coverage_score": 24.0,
                    "executability_score": 22.0,
                    "memory_prior_score": 5.0,
                    "text_preference_bonus": 0.0,
                    "text_preference_penalty": 0.0,
                    "name_modality_bonus": 0.0,
                    "name_modality_penalty": 0.0,
                    "transition_bonus": 0.0,
                    "transition_penalty": 0.0,
                    "redundancy_penalty": 0.0,
                    "extra_action_penalty": 0.0,
                    "length_penalty": -6.0,
                    "missing_actions": [],
                },
                "selection_meta": {
                    "hard_filter_tier": 2,
                    "coverage_complete": True,
                    "dag_ok": True,
                    "schema_ok": True,
                    "dependency_ok": True,
                    "modality_ok": True,
                    "bridge_tool_ok": True,
                    "unrequested_bridge_tool_count": 0,
                    "search_source_ok": True,
                    "retrieval_before_topic_ok": True,
                    "grammar_after_retrieval_ok": True,
                    "video_after_waveform_ok": True,
                    "warning_count": 0,
                    "extra_actions": [],
                },
            },
        ]

        selected = self.agent._select_best_candidate(candidates)
        self.assertEqual(selected["id"], 2)

    async def test_select_best_candidate_prefers_fewer_unrequested_bridge_warnings_over_higher_score(self) -> None:
        candidates = [
            {
                "id": 1,
                "workflow": {"task_steps": [], "task_nodes": [], "task_links": []},
                "score": 140.0,
                "score_details": {"missing_actions": [], "length_penalty": -2.0},
                "selection_meta": {
                    "hard_filter_tier": 1,
                    "coverage_complete": True,
                    "bridge_tool_ok": False,
                    "unrequested_bridge_tool_count": 1,
                    "search_source_ok": True,
                    "retrieval_before_topic_ok": True,
                    "video_after_waveform_ok": True,
                    "warning_count": 1,
                },
            },
            {
                "id": 2,
                "workflow": {"task_steps": [], "task_nodes": [], "task_links": []},
                "score": 130.0,
                "score_details": {"missing_actions": [], "length_penalty": -2.0},
                "selection_meta": {
                    "hard_filter_tier": 1,
                    "coverage_complete": True,
                    "bridge_tool_ok": True,
                    "unrequested_bridge_tool_count": 0,
                    "search_source_ok": True,
                    "retrieval_before_topic_ok": True,
                    "video_after_waveform_ok": True,
                    "warning_count": 0,
                },
            },
        ]

        selected = self.agent._select_best_candidate(candidates)
        self.assertEqual(selected["id"], 2)

    def test_build_candidate_strategy_specs_uses_varied_sampling(self) -> None:
        requirement = (
            "I have a long article about the effects of climate change on biodiversity. "
            "I need help in understanding the main ideas, sentiment, and important keywords. "
            "Simplify and summarize the text, then find some related topics on the internet."
        )

        specs = self.agent._build_candidate_strategy_specs(requirement)
        temperatures = {round(float(spec["temperature"]), 2) for spec in specs}

        self.assertGreater(len(temperatures), 2)
        self.assertTrue(all(spec.get("name") for spec in specs))
        self.assertTrue(any("structurally distinct" in spec["hint"] for spec in specs))
        self.assertTrue(any("analysis coverage" in spec["hint"] for spec in specs))


class TestSafeJson(unittest.TestCase):
    def test_safe_json_dumps_with_circular_ref(self) -> None:
        data = {"a": 1}
        data["self"] = data
        text = _safe_json_dumps(data)
        self.assertIn("\"a\": 1", text)
        self.assertIn("<circular_ref>", text)


if __name__ == "__main__":
    unittest.main()
