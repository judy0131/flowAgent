import ast
import json
from typing import Any


#input = """{"id": "25866928", "seed": 416960, "n_tools": 5, "type": "chain", "sampled_nodes": "[{\\"input-type\\": [\\"text\\"], \\"output-type\\": [\\"text\\"], \\"task\\": \\"Text Grammar Checker\\"}, {\\"input-type\\": [\\"text\\"], \\"output-type\\": [\\"text\\"], \\"task\\": \\"Text Search\\"}, {\\"input-type\\": [\\"text\\"], \\"output-type\\": [\\"text\\"], \\"task\\": \\"Text Simplifier\\"}, {\\"input-type\\": [\\"text\\"], \\"output-type\\": [\\"image\\"], \\"task\\": \\"Text-to-Image\\"}, {\\"input-type\\": [\\"text\\"], \\"output-type\\": [\\"text\\"], \\"task\\": \\"Topic Generator\\"}]", "sampled_links": "[{\\"source\\": \\"Text Simplifier\\", \\"target\\": \\"Text Search\\"}, {\\"source\\": \\"Text Search\\", \\"target\\": \\"Text Grammar Checker\\"}, {\\"source\\": \\"Text Grammar Checker\\", \\"target\\": \\"Topic Generator\\"}, {\\"source\\": \\"Topic Generator\\", \\"target\\": \\"Text-to-Image\\"}]", "instruction": "I'm doing a research project about the effects of climate change on polar bears. Could you help me find simplified and grammatically correct information on this topic, generate some related sub-topics, and produce an illustrative image based on the theme 'Climate change and its impact on polar bears'?", "tool_steps": "[\\"Step 1: Make the project theme easier to understand\\", \\"Step 2: Search for internet content using the simplified text\\", \\"Step 3: Perform a grammar check on the found information\\", \\"Step 4: Generate some related sub-topics from the grammatically correct text\\", \\"Step 5: Generate an image representing the main topic\\"]", "tool_nodes": "[{\\"task\\": \\"Text Simplifier\\", \\"arguments\\": [\\"Climate change and its impact on polar bears\\"]}, {\\"task\\": \\"Text Search\\", \\"arguments\\": [\\"<node-0>\\"]}, {\\"task\\": \\"Text Grammar Checker\\", \\"arguments\\": [\\"<node-1>\\"]}, {\\"task\\": \\"Topic Generator\\", \\"arguments\\": [\\"<node-2>\\"]}, {\\"task\\": \\"Text-to-Image\\", \\"arguments\\": [\\"<node-3>\\"]}]", "tool_links": "[{\\"source\\": \\"Text Simplifier\\", \\"target\\": \\"Text Search\\"}, {\\"source\\": \\"Text Search\\", \\"target\\": \\"Text Grammar Checker\\"}, {\\"source\\": \\"Text Grammar Checker\\", \\"target\\": \\"Topic Generator\\"}, {\\"source\\": \\"Topic Generator\\", \\"target\\": \\"Text-to-Image\\"}]"}"""
input="""
{"id": "16097613", "instruction": "I have a video file example.mp4, and I want to extract its audio track, reduce background noise, and then add a reverb effect. Please provide the processed audio file.", "n_tools": 3, "tool_steps": "[\"Step 1: Call Video-to-Audio with arg1=example.mp4.\", \"Step 2: Call Audio Noise Reduction with arg1=<node-0>.\", \"Step 3: Call Audio Effects with arg2=reverb, arg1=<node-1>.\"]", "tool_nodes": "[{\"task\": \"Video-to-Audio\", \"arguments\": [\"example.mp4\"]}, {\"task\": \"Audio Noise Reduction\", \"arguments\": [\"<node-0>\"]}, {\"task\": \"Audio Effects\", \"arguments\": [\"reverb\", \"<node-1>\"]}]", "tool_links": "[{\"source\": \"Video-to-Audio\", \"target\": \"Audio Noise Reduction\"}, {\"source\": \"Audio Noise Reduction\", \"target\": \"Audio Effects\"}]", "result": {"task_steps": ["Step 1: Call Video-to-Audio with arg1=example.mp4.", "Step 2: Call Audio Noise Reduction with arg1=<node-0>.", "Step 3: Call Audio Effects with arg2=reverb, arg1=<node-1>."], "task_nodes": [{"task": "Video-to-Audio", "arguments": ["example.mp4"]}, {"task": "Audio Noise Reduction", "arguments": ["<node-0>"]}, {"task": "Audio Effects", "arguments": ["reverb", "<node-1>"]}], "task_links": [{"source": "Video-to-Audio", "target": "Audio Noise Reduction"}, {"source": "Audio Noise Reduction", "target": "Audio Effects"}]}}
"""

def _candidate_payloads(text: str) -> list[str]:
    stripped = text.strip().lstrip("\ufeff")
    candidates = [stripped]

    try:
        repaired = stripped.encode("gbk").decode("utf-8").lstrip("\ufeff").strip()
        candidates.append(repaired)
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass

    for marker in ('{', '[', '"', "'"):
        for source in list(candidates):
            idx = source.find(marker)
            if idx > 0:
                candidates.append(source[idx:])

    unique: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if item and item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _loads_maybe_json(text: str) -> Any:
    candidates = _candidate_payloads(text)
    if not candidates:
        return text

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

        try:
            return ast.literal_eval(candidate)
        except (SyntaxError, ValueError):
            pass

    return text


def _expand_nested_json(value: Any) -> Any:
    if isinstance(value, str):
        parsed = _loads_maybe_json(value)
        if parsed is value:
            return value
        return _expand_nested_json(parsed)

    if isinstance(value, list):
        return [_expand_nested_json(item) for item in value]

    if isinstance(value, dict):
        return {key: _expand_nested_json(item) for key, item in value.items()}

    return value


def parse_structured_json(raw_text: str) -> dict[str, Any]:
    parsed = _loads_maybe_json(raw_text)
    if isinstance(parsed, str):
        reparsed = _loads_maybe_json(parsed)
        if reparsed is not parsed:
            parsed = reparsed
    if not isinstance(parsed, dict):
        raise ValueError("input must be a JSON object string or Python dict string")
    return _expand_nested_json(parsed)


def print_structured_json(raw_text: str) -> None:
    structured = parse_structured_json(raw_text)
    print(json.dumps(structured, ensure_ascii=False, indent=2))


def main() -> None:
    raw_text = input
    if not raw_text.strip():
        raise SystemExit("input is empty")

    print_structured_json(raw_text)


if __name__ == "__main__":
    main()
