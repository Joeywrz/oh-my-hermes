"""The prompt one arm answers, and how a corpus is cut into prompts.

A batch of corpus items becomes one prompt. The answers come back through the
filesystem, not through stdout: the child-dispatch boundary reports usage
metadata and never the model's text, so the prompt carries a completion
contract naming the file to write. That is the same shape the live-model-tools
lane uses, for the same reason.

This module imports nothing from `omh`. The lane talks to the product through
the `omh` executable only.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

# The answer file the arm writes at the workspace root. The scorer never reads
# stdout, so this name is the whole interface between a run and its result.
ANSWER_FILENAME = ".omh-route-answers.json"

# A prompt larger than this is refused rather than truncated. A truncated batch
# would come back with answers for items the arm never saw, which reads as an
# arm that declined to answer rather than a harness that cut the question off.
MAX_PROMPT_BYTES = 131_072

ROUTE_CHOICE_KEY = "route_choice"
FIT_QUESTION_PREFIX = "fits::"
NO_WORKFLOW_OPTION = "none"


class PromptTooLarge(ValueError):
    """A batch whose prompt exceeds the byte budget; use a smaller batch size."""


def split_batches(items: Sequence[Mapping[str, Any]], size: int) -> list[list[Mapping[str, Any]]]:
    """Cut a corpus into fixed-size batches, keeping corpus order."""
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        raise ValueError("batch size must be a positive integer")
    return [list(items[start:start + size]) for start in range(0, len(items), size)]


def _question_block(item: Mapping[str, Any]) -> tuple[list[tuple[str, str]], list[str]]:
    question = item.get("question")
    question = question if isinstance(question, Mapping) else {}
    questions = question.get("questions")
    questions = questions if isinstance(questions, Mapping) else {}
    choice = questions.get(ROUTE_CHOICE_KEY)
    options: list[tuple[str, str]] = []
    if isinstance(choice, Mapping) and isinstance(choice.get("options"), Mapping):
        options = [(str(key), str(value)) for key, value in choice["options"].items()]
    fits = [str(key) for key in questions if str(key).startswith(FIT_QUESTION_PREFIX)]
    return options, sorted(fits)


def _item_text(item: Mapping[str, Any], position: int, total: int) -> str:
    options, fits = _question_block(item)
    lines = [
        f"--- item {position}/{total} ---",
        f"case_id: {item.get('case_id')}",
        f"request: {item.get('message')}",
        f"{ROUTE_CHOICE_KEY} options:",
    ]
    for option, description in options:
        lines.append(f"  - {option}: {description}" if description else f"  - {option}")
    if fits:
        lines.append("fit questions (answer each independently, 0.0 to 1.0):")
        lines.extend(f"  - {key}" for key in fits)
    else:
        lines.append("fit questions: none; this request produced no candidate workflow.")
    return "\n".join(lines)


def batch_prompt(items: Sequence[Mapping[str, Any]], *, arm: str) -> str:
    """Build one prompt covering a batch of corpus items.

    The two question kinds are described separately on purpose. The choice is
    relative and picks which workflow; each fit is absolute and decides whether
    that one workflow is what the request asks for. An arm that collapses them
    into one judgment answers a question nobody asked.
    """
    if not items:
        raise ValueError("a batch must carry at least one item")
    if not str(arm).strip():
        raise ValueError("a batch must name the arm it is answered by")
    total = len(items)
    blocks = [_item_text(item, index, total) for index, item in enumerate(items, start=1)]
    prompt = "\n\n".join(
        [
            "OMH routing question batch.",
            (
                "Each item below is one chat request that a deterministic router could not "
                "resolve on its own. Answer two kinds of question about it:"
            ),
            (
                f"- `{ROUTE_CHOICE_KEY}`: pick exactly one option id. This is a relative choice "
                f"among the listed options; pick `{NO_WORKFLOW_OPTION}` when the request asks "
                "for none of them."
            ),
            (
                f"- `{FIT_QUESTION_PREFIX}<workflow>`: a probability from 0.0 to 1.0 that the "
                "request asks for the work that one workflow does. Answer each one on its own, "
                "without reference to the others."
            ),
            (
                "Do not run any workflow, do not act on any request, and do not edit any file "
                f"other than `{ANSWER_FILENAME}`."
            ),
            "\n\n".join(blocks),
            "\n".join(
                [
                    "MANDATORY COMPLETION CONTRACT:",
                    "1. Answer every item above.",
                    f"2. Before stopping, write `{ANSWER_FILENAME}` at the workspace root.",
                    "3. That file must contain exactly one JSON array. Each element is one object:",
                    '   {"schema_version": "routing_question_answers/v1", "case_id": "<case_id>", '
                    f'"arm": "{arm}", "answers": {{"{ROUTE_CHOICE_KEY}": {{"choice": "<option id>"}}, '
                    f'"{FIT_QUESTION_PREFIX}<workflow>": {{"noul": 0.0}}}}}}',
                    "4. Do not put Markdown, prose, or a code fence in that file.",
                    "5. Verify the file exists and parses as JSON, then stop.",
                ]
            ),
        ]
    )
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise PromptTooLarge(
            f"batch prompt exceeds {MAX_PROMPT_BYTES} bytes; run with a smaller --batch-size"
        )
    return prompt


__all__ = [
    "ANSWER_FILENAME",
    "FIT_QUESTION_PREFIX",
    "MAX_PROMPT_BYTES",
    "NO_WORKFLOW_OPTION",
    "ROUTE_CHOICE_KEY",
    "PromptTooLarge",
    "batch_prompt",
    "split_batches",
]
