"""Extracting a JSON object from model prose (spec section 32).

Models wrap JSON in prose, in Markdown fences, or in both. Two rules govern how Scout copes
with that:

* **No greedy regex.** ``re.search(r"\\{.*\\}", text, re.DOTALL)`` happily spans from the
  first brace to the last one in a multi-part answer and produces a string that is not
  valid JSON, or worse, one that is valid but wrong. A balanced scan that understands
  strings and escapes is used instead.
* **Strict about the outcome.** The first JSON *value* found must be an object. An array or
  a bare string is a contract failure with the raw text attached, not something to coerce.
"""

from __future__ import annotations

import json
from typing import Any, Final

from scout_llm.errors import LLMResponseFormatError

__all__ = [
    "extract_json_object",
    "json_object_failure_reason",
    "parse_first_json_value",
    "strip_code_fences",
    "try_parse_json_object",
]

_OPENING: Final = "{"
_ARRAY_OPENING: Final = "["
_PAIRS: Final[frozenset[tuple[str, str]]] = frozenset({("{", "}"), ("[", "]")})


def strip_code_fences(text: str) -> str:
    """Return the contents of the first Markdown fenced block, if there is one.

    Handles `````json````, a bare ````` ``` ```` fence, and a fence whose info string is
    missing but whose body starts on the same line. Text with no fence is returned
    unchanged, so callers can always feed the result to the scanner.
    """
    opening = text.find("```")
    if opening == -1:
        return text

    after_fence = opening + 3
    line_end = text.find("\n", after_fence)
    info_end = len(text) if line_end == -1 else line_end
    info = text[after_fence:info_end].strip()

    if "{" in info or "[" in info:
        # No info string: the fence and the payload are on the same line.
        body_start = after_fence
    elif line_end == -1:
        body_start = info_end
    else:
        body_start = line_end + 1

    closing = text.find("```", body_start)
    if closing == -1:
        return text[body_start:]
    return text[body_start:closing]


def parse_first_json_value(text: str) -> tuple[bool, Any]:
    """Return ``(found, value)`` for the first parseable JSON value in ``text``.

    The search skips candidate braces that do not delimit valid JSON (prose such as
    "set {x} to 1") and keeps scanning, so a sentence containing braces does not mask a real
    payload later in the answer.
    """
    candidate_text = strip_code_fences(text)
    index = 0
    length = len(candidate_text)

    while index < length:
        char = candidate_text[index]
        if char not in (_OPENING, _ARRAY_OPENING):
            index += 1
            continue

        end = _balanced_end(candidate_text, index)
        if end is None:
            index += 1
            continue

        try:
            return True, json.loads(candidate_text[index : end + 1])
        except json.JSONDecodeError:
            # A balanced-brace slice that is not valid JSON: keep looking. This is what
            # makes prose like "the config {a: 1} is wrong" harmless.
            index += 1

    return False, None


def try_parse_json_object(text: str) -> dict[str, Any] | None:
    """Return the first JSON object in ``text``, or ``None`` when there is not one."""
    found, value = parse_first_json_value(text)
    if not found or not isinstance(value, dict):
        return None
    return value


def extract_json_object(text: str) -> dict[str, Any]:
    """Return the first JSON object in ``text``.

    Raises:
        LLMResponseFormatError: no JSON value was found, or the first one found was not an
            object. The raw text is attached so the caller can log or persist it without
            re-running the model.
    """
    found, value = parse_first_json_value(text)
    if not found:
        raise LLMResponseFormatError(
            "model response contained no JSON value",
            raw_text=text,
        )
    if not isinstance(value, dict):
        raise LLMResponseFormatError(
            f"model response contained a JSON {type(value).__name__}, but an object was required",
            raw_text=text,
            json_type=type(value).__name__,
        )
    return value


def json_object_failure_reason(text: str) -> str | None:
    """Explain why ``text`` holds no JSON object, or ``None`` when it does.

    Used by callers that have already attempted a lenient parse and want the strict
    diagnosis for the error message rather than a second, coarser guess.
    """
    found, value = parse_first_json_value(text)
    if not found:
        return "the response contained no JSON value"
    if not isinstance(value, dict):
        return f"the response contained a JSON {type(value).__name__}, not an object"
    return None


def _balanced_end(text: str, start: int) -> int | None:
    """Return the index of the bracket matching ``text[start]``, or ``None``.

    String literals are honoured (including backslash escapes) so that a brace *inside* a
    JSON string does not unbalance the scan — the classic failure of a naive depth counter.
    """
    stack: list[str] = []
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char in (_OPENING, _ARRAY_OPENING):
            stack.append(char)
        elif char in ("}", "]"):
            if not stack:
                return None
            opening = stack.pop()
            if (opening, char) not in _PAIRS:
                return None
            if not stack:
                return index

    return None
