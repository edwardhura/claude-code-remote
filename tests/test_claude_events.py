"""Tests for the Pydantic discriminated-union event schema."""

from __future__ import annotations

import json

from pydantic import TypeAdapter

from ccr.claude.events import (
    AssistantTurn,
    ClaudeEvent,
    RateLimitEvent,
    RateLimitInfo,
    ResultEvent,
    SystemInit,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UnknownEvent,
    UserTurn,
    parse_event,
)


def test_round_trip_system_init() -> None:
    line = json.dumps(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "sess-abc",
            "model": "claude-3-5-sonnet",
            "tools": ["Read", "Bash"],
        }
    )
    event = parse_event(line)
    assert isinstance(event, SystemInit)
    assert event.session_id == "sess-abc"
    assert event.model == "claude-3-5-sonnet"
    assert event.tools == ["Read", "Bash"]


def test_round_trip_user_turn_string_content() -> None:
    line = json.dumps(
        {
            "type": "user",
            "message": {"role": "user", "content": "hello"},
        }
    )
    event = parse_event(line)
    assert isinstance(event, UserTurn)
    assert event.message.content == "hello"


def test_round_trip_user_turn_block_content() -> None:
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "hi"}],
            },
        }
    )
    event = parse_event(line)
    assert isinstance(event, UserTurn)
    assert isinstance(event.message.content, list)
    assert isinstance(event.message.content[0], TextBlock)
    assert event.message.content[0].text == "hi"


def test_round_trip_assistant_turn_with_text_thinking_tool_use() -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "let me think"},
                    {"type": "text", "text": "answer"},
                    {
                        "type": "tool_use",
                        "id": "tu-1",
                        "name": "Read",
                        "input": {"path": "/tmp"},
                    },
                ],
            },
        }
    )
    event = parse_event(line)
    assert isinstance(event, AssistantTurn)
    blocks = event.message.content
    assert isinstance(blocks[0], ThinkingBlock)
    assert isinstance(blocks[1], TextBlock)
    assert isinstance(blocks[2], ToolUseBlock)
    assert blocks[2].name == "Read"
    assert blocks[2].input == {"path": "/tmp"}


def test_round_trip_result_success_and_error() -> None:
    success = parse_event(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "duration_ms": 1234,
                "total_cost_usd": 0.04,
            }
        )
    )
    assert isinstance(success, ResultEvent)
    assert success.subtype == "success"
    assert success.duration_ms == 1234

    error = parse_event(json.dumps({"type": "result", "subtype": "error_during_execution"}))
    assert isinstance(error, ResultEvent)
    assert error.subtype == "error_during_execution"


def test_unknown_type_falls_through_to_unknown_event() -> None:
    line = json.dumps({"type": "weather_report", "temp": 72})
    event = parse_event(line)
    assert isinstance(event, UnknownEvent)
    assert event.type == "weather_report"
    assert event.raw == {"type": "weather_report", "temp": 72}


def test_malformed_json_returns_unknown_event_parse_error() -> None:
    event = parse_event("not json at all {")
    assert isinstance(event, UnknownEvent)
    assert event.type == "parse_error"
    assert "line" in event.raw


def test_extra_fields_preserved_in_model_extra() -> None:
    line = json.dumps(
        {
            "type": "system",
            "subtype": "init",
            "extra_unknown": "future-field",
        }
    )
    event = parse_event(line)
    assert isinstance(event, SystemInit)
    assert event.model_extra == {"extra_unknown": "future-field"}


def test_json_schema_is_non_empty() -> None:
    schema = TypeAdapter(ClaudeEvent).json_schema()
    assert isinstance(schema, dict)
    assert len(schema) > 0


def test_known_variant_with_validation_failure_falls_through_to_unknown() -> None:
    """Belt-and-suspenders: forward-compat schema drift on a known type still
    surfaces as UnknownEvent rather than raising ValidationError."""
    line = json.dumps(
        {
            "type": "result",
            "subtype": "totally-new-subtype",  # not in the Literal set
        }
    )
    event = parse_event(line)
    assert isinstance(event, UnknownEvent)
    assert event.type == "result"
    assert event.raw["subtype"] == "totally-new-subtype"


def test_dict_input_accepted() -> None:
    obj = {"type": "system", "subtype": "init"}
    event = parse_event(obj)
    assert isinstance(event, SystemInit)


# --------------------------------------------------------------------------- #
# CCR-032: rate_limit_event — typed model surfaces from the discriminated union.
# --------------------------------------------------------------------------- #


def test_parse_rate_limit_event_returns_typed_model() -> None:
    """A probe-shape ``rate_limit_event`` line surfaces as :class:`RateLimitEvent`,
    not :class:`UnknownEvent`. CamelCase wire fields land on snake_case attrs."""
    line = json.dumps(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed",
                "resetsAt": 1777861800,
                "rateLimitType": "five_hour",
                "overageStatus": "rejected",
                "overageDisabledReason": "group_zero_credit_limit",
                "isUsingOverage": False,
            },
            "uuid": "758cb741-0e54-4af1-9c32-0b76648f795f",
            "session_id": "831d0859-e616-4ad0-9ff4-047eb0ae1b81",
        }
    )
    event = parse_event(line)
    assert isinstance(event, RateLimitEvent)
    assert event.session_id == "831d0859-e616-4ad0-9ff4-047eb0ae1b81"
    assert event.uuid == "758cb741-0e54-4af1-9c32-0b76648f795f"
    info = event.rate_limit_info
    assert info is not None
    assert info.status == "allowed"
    assert info.resets_at == 1777861800
    assert info.rate_limit_type == "five_hour"
    assert info.overage_status == "rejected"
    assert info.overage_disabled_reason == "group_zero_credit_limit"
    assert info.is_using_overage is False


def test_parse_rate_limit_event_alias_round_trip() -> None:
    """``RateLimitInfo`` accepts both camelCase wire input and snake_case
    fixture input; both round-trip to the same Python attribute names."""
    camel = RateLimitInfo.model_validate(
        {
            "status": "allowed",
            "resetsAt": 100,
            "rateLimitType": "five_hour",
            "overageStatus": "rejected",
            "overageDisabledReason": "x",
            "isUsingOverage": True,
        }
    )
    snake = RateLimitInfo.model_validate(
        {
            "status": "allowed",
            "resets_at": 100,
            "rate_limit_type": "five_hour",
            "overage_status": "rejected",
            "overage_disabled_reason": "x",
            "is_using_overage": True,
        }
    )
    assert camel.resets_at == snake.resets_at == 100
    assert camel.rate_limit_type == snake.rate_limit_type == "five_hour"
    assert camel.overage_status == snake.overage_status == "rejected"
    assert camel.overage_disabled_reason == snake.overage_disabled_reason == "x"
    assert camel.is_using_overage is snake.is_using_overage is True


def test_parse_rate_limit_event_extra_fields_fall_through_model_extra() -> None:
    """Forward-compat: an unknown sub-field lands in ``model_extra``
    rather than failing validation."""
    line = json.dumps(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed",
                "newField": "future-server-side-thing",
            },
        }
    )
    event = parse_event(line)
    assert isinstance(event, RateLimitEvent)
    info = event.rate_limit_info
    assert info is not None
    assert info.status == "allowed"
    assert info.model_extra == {"newField": "future-server-side-thing"}


def test_parse_rate_limit_event_missing_info_object() -> None:
    """A ``rate_limit_event`` line without ``rate_limit_info`` parses
    cleanly with ``rate_limit_info is None`` (forward-compat)."""
    line = json.dumps({"type": "rate_limit_event"})
    event = parse_event(line)
    assert isinstance(event, RateLimitEvent)
    assert event.rate_limit_info is None


def test_claude_event_union_includes_rate_limit_event() -> None:
    """Sanity: :class:`RateLimitEvent` is part of the public ``ClaudeEvent``
    union so consumers branching on ``isinstance(..., RateLimitEvent)``
    get accurate static typing."""
    # The union includes the type at runtime; this is mostly a guard
    # against an accidental drop from the union in a future refactor.
    assert RateLimitEvent in ClaudeEvent.__args__  # type: ignore[attr-defined]
