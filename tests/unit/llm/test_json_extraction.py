"""JSON extraction from prose, fences, and malformed responses (spec section 32)."""

from __future__ import annotations

import pytest

from scout_llm.errors import LLMResponseFormatError
from scout_llm.json_extraction import (
    extract_json_object,
    parse_first_json_value,
    strip_code_fences,
    try_parse_json_object,
)


def test_plain_object_parses() -> None:
    assert extract_json_object('{"verdict": "pass"}') == {"verdict": "pass"}


def test_fenced_json_parses() -> None:
    text = 'Here you go:\n```json\n{"findings": [{"id": "f1"}]}\n```\nHope that helps!'
    assert extract_json_object(text) == {"findings": [{"id": "f1"}]}


def test_fence_without_info_string_parses() -> None:
    assert extract_json_object('```\n{"a": 1}\n```') == {"a": 1}


def test_fence_with_payload_on_the_fence_line_parses() -> None:
    assert extract_json_object('```{"a": 1}```') == {"a": 1}


def test_unterminated_fence_still_parses() -> None:
    assert extract_json_object('```json\n{"a": 1}') == {"a": 1}


def test_prose_wrapped_json_parses() -> None:
    text = 'Sure! The result is:\n{"confidence": 0.5}\nLet me know if you need more.'
    assert extract_json_object(text) == {"confidence": 0.5}


def test_nested_objects_and_arrays_parse() -> None:
    text = 'Result: {"a": {"b": [1, 2, {"c": 3}]}} trailing'
    assert extract_json_object(text) == {"a": {"b": [1, 2, {"c": 3}]}}


def test_braces_inside_strings_do_not_unbalance_the_scan() -> None:
    text = 'Prefix {"pattern": "a } b { c", "escaped": "quote \\" and brace }"} suffix'
    assert extract_json_object(text) == {"pattern": "a } b { c", "escaped": 'quote " and brace }'}


def test_non_json_prose_braces_are_skipped() -> None:
    """A naive "first brace to last brace" scan would fail here."""
    text = 'Set {x} to 1. The answer is {"ok": true}.'
    assert extract_json_object(text) == {"ok": True}


def test_array_is_rejected_with_the_json_type_attached() -> None:
    with pytest.raises(LLMResponseFormatError) as excinfo:
        extract_json_object("[1, 2, 3]")

    assert excinfo.value.context["json_type"] == "list"
    assert excinfo.value.raw_text == "[1, 2, 3]"


def test_missing_json_raises_with_the_raw_text_attached() -> None:
    with pytest.raises(LLMResponseFormatError) as excinfo:
        extract_json_object("I could not complete this task.")

    assert excinfo.value.raw_text == "I could not complete this task."
    assert "no JSON value" in str(excinfo.value)


def test_malformed_json_raises() -> None:
    with pytest.raises(LLMResponseFormatError):
        extract_json_object('{"unclosed": ')


def test_try_parse_returns_none_instead_of_raising() -> None:
    assert try_parse_json_object("no json here") is None
    assert try_parse_json_object("[1, 2]") is None
    assert try_parse_json_object('{"ok": 1}') == {"ok": 1}


def test_parse_first_json_value_reports_found_and_value() -> None:
    assert parse_first_json_value("[1]") == (True, [1])
    assert parse_first_json_value("nothing") == (False, None)


def test_strip_code_fences_is_identity_without_a_fence() -> None:
    assert strip_code_fences("plain {text}") == "plain {text}"


def test_strip_code_fences_prefers_the_first_fenced_block() -> None:
    text = 'prose {"wrong": true}\n```json\n{"right": true}\n```'
    assert extract_json_object(text) == {"right": True}


def test_brace_only_text_does_not_parse() -> None:
    assert try_parse_json_object("{") is None
    assert try_parse_json_object("}") is None
    assert try_parse_json_object("{}") == {}
