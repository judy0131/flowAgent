import re
from typing import Dict, List, Optional, Set, Tuple


ACTION_CANONICAL_ORDER: Tuple[str, ...] = (
    "retrieval",
    "simplify",
    "summarize",
    "sentiment",
    "keywords",
    "grammar",
    "topic",
    "image",
    "denoise",
    "voice_change",
    "transcribe",
    "translate",
    "combine",
    "audio_effect",
    "waveform",
    "video",
)


def _ordered_action_tags(tags: Set[str]) -> List[str]:
    ordered = [action for action in ACTION_CANONICAL_ORDER if action in tags]
    extras = sorted(tag for tag in tags if tag not in ACTION_CANONICAL_ORDER)
    return ordered + extras


def _normalize_skill_text(skill_name: str, description: str = "") -> str:
    text = f"{skill_name} {description}".strip().lower()
    text = re.sub(r"[\(\)\[\]\{\}/]+", " ", text)
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _infer_skill_action_tags(
    skill_name: str,
    description: str = "",
    input_schema: Optional[Dict[str, str]] = None,
) -> List[str]:
    text = _normalize_skill_text(skill_name, description)
    tags: Set[str] = set()

    if re.search(r"\b(search|finder|research)\b", text):
        tags.add("retrieval")
    if (
        "text simplifier" in text
        or re.search(r"\bsimplif(?:y|ier|ication)?\b", text)
        or re.search(r"\b(paraphras(?:e|er)|rewrit(?:e|er)|spinner)\b", text)
    ):
        tags.add("simplify")
    if re.search(r"\b(summar(?:y|ies|ize|izer|ization)|main ideas?)\b", text):
        tags.add("summarize")
    if "sentiment" in text:
        tags.add("sentiment")
    if "keyword" in text or "key phrase" in text or "keyphrase" in text:
        tags.add("keywords")
    if "grammar" in text or "proofread" in text:
        tags.add("grammar")
    if "topic" in text:
        tags.add("topic")
    if re.search(r"\b(audio|sound)\s+to\s+(image|spectrogram|waveform)\b", text) or "waveform" in text or "spectrogram" in text:
        tags.update({"image", "waveform"})
    elif (
        re.search(r"\bto\s+image\b", text)
        or (
            "image" in text
            and re.search(r"\b(generate|generator|creator|render|create|visualizer?)\b", text)
        )
    ):
        tags.add("image")
    if "noise reduction" in text or "denois" in text or "background noise" in text:
        tags.add("denoise")
    if re.search(r"\bvoice\s+(changer|change|modifier|modification|modulator|alteration)\b", text):
        tags.add("voice_change")
    if (
        re.search(r"\b(audio|speech|video|image|ocr)\s+to\s+text\b", text)
        or re.search(r"\btext\s+extract(?:ion|or)?\b", text)
        or re.search(r"\bextract\s+text\b", text)
        or re.search(r"\b(optical character recognition|ocr)\b", text)
        or "transcrib" in text
        or "caption" in text
        or "recognition" in text
    ):
        tags.add("transcribe")
    if "translat" in text:
        tags.add("translate")
    if (
        "splicer" in text
        or "combiner" in text
        or "combine" in text
        or "merge" in text
        or "mix" in text
        or "synchronization" in text
    ):
        tags.add("combine")
    if "effect" in text and "audio" in text:
        tags.add("audio_effect")
    if (
        re.search(r"\b(image|text)\s+to\s+video\b", text)
        or re.search(r"\b(video\s+(generator|creator)|slideshow)\b", text)
        or re.search(r"\bcreate\s+slideshow\b", text)
    ):
        tags.add("video")

    schema_keys = {str(k).strip().lower() for k in (input_schema or {}).keys()}
    if "source_ref" in schema_keys and "search" in text:
        tags.add("retrieval")

    return _ordered_action_tags(tags)
