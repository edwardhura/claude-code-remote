"""Tests for ``ccr.bot.formatting`` — chunking + per-event rendering."""

from __future__ import annotations

from ccr.bot.formatting import (
    SAFE_CHUNK,
    TELEGRAM_HARD_LIMIT,
    _PendingKeyboard,
    chunk_text,
    event_to_messages,
)
from ccr.claude.events import (
    AssistantTurn,
    PermissionRequest,
    ResultEvent,
    ResultUsage,
    SystemInit,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UnknownEvent,
)

# --------------------------------------------------------------------------- #
# chunk_text
# --------------------------------------------------------------------------- #


def test_chunk_text_short_returns_single_chunk() -> None:
    assert chunk_text("hello") == ["hello"]


def test_chunk_text_empty_returns_empty_list() -> None:
    assert chunk_text("") == []


def test_chunk_text_paragraph_split_on_double_newline() -> None:
    paragraph_a = "A" * 2000
    paragraph_b = "B" * 2000
    text = f"{paragraph_a}\n\n{paragraph_b}"
    chunks = chunk_text(text)
    assert len(chunks) == 2
    assert chunks[0].endswith("\n\n")
    assert chunks[1] == paragraph_b
    assert all(len(c) <= TELEGRAM_HARD_LIMIT for c in chunks)


def test_chunk_text_10000_chars_paragraphed_no_mid_sentence_cut() -> None:
    """A 10 000-char text with paragraph breaks → ≥3 chunks, no hard mid-sentence cuts."""
    paragraph = "Sentence one. Sentence two. Sentence three. " * 50  # ~2200 chars
    text = "\n\n".join([paragraph] * 5)  # ~11k chars total with separators
    assert len(text) >= 10_000
    chunks = chunk_text(text)
    assert len(chunks) >= 3
    assert all(len(c) <= TELEGRAM_HARD_LIMIT for c in chunks)
    # When paragraph breaks exist, every split happens on \n\n — no chunk ends
    # mid-sentence (i.e. with an alphabetic char as the last visible char).
    for chunk in chunks[:-1]:
        # Trailing whitespace is fine; the actual split should be on a
        # boundary, so the chunk should end with the boundary string.
        assert chunk.endswith(("\n\n", "\n", ". "))


def test_chunk_text_fallback_hard_cut_when_no_boundaries() -> None:
    text = "x" * (SAFE_CHUNK * 2 + 5)
    chunks = chunk_text(text)
    assert all(len(c) <= TELEGRAM_HARD_LIMIT for c in chunks)
    assert sum(len(c) for c in chunks) == len(text)


# --------------------------------------------------------------------------- #
# event_to_messages
# --------------------------------------------------------------------------- #


def _assistant_with(blocks: list[object]) -> AssistantTurn:
    return AssistantTurn.model_validate(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [b.model_dump() if hasattr(b, "model_dump") else b for b in blocks],
            },
        }
    )


def test_text_block_renders_as_plain_string() -> None:
    event = _assistant_with([TextBlock(type="text", text="hello")])
    assert event_to_messages(event) == [("hello", None)]


def test_text_block_html_escaped() -> None:
    event = _assistant_with([TextBlock(type="text", text="<script>x</script>")])
    out = event_to_messages(event)
    assert out == [("&lt;script&gt;x&lt;/script&gt;", None)]


def test_thinking_block_renders_as_plain_string() -> None:
    event = _assistant_with([ThinkingBlock(type="thinking", thinking="deep thoughts")])
    assert event_to_messages(event) == [("deep thoughts", None)]


def test_tool_use_one_liner_truncates_args() -> None:
    big_input = {"path": "/foo", "data": "x" * 1000}
    event = _assistant_with(
        [ToolUseBlock(type="tool_use", id="t1", name="read_file", input=big_input)],
    )
    out = event_to_messages(event)
    assert len(out) == 1
    text, kb = out[0]
    assert kb is None
    assert text.startswith("\U0001f527 read_file ")
    # The full dict (over 1000 chars when repr'd) must NOT appear in the
    # outbound message — only the first 200 repr chars (HTML-escaped, which
    # may add a few entity expansions) should make it through.
    assert "x" * 200 not in text  # the full data block is suppressed
    # The repr-of-the-input portion derives from `repr(input)[:200]`; allowing
    # for HTML entity expansion of quote characters, the args section stays
    # well under twice the truncation bound.
    args_section = text.split(" ", 2)[2]
    assert len(args_section) <= 400


def test_tool_result_non_error_yields_no_message() -> None:
    event = _assistant_with(
        [ToolResultBlock(type="tool_result", tool_use_id="t1", content="ok", is_error=False)],
    )
    assert event_to_messages(event) == []


def test_tool_result_error_renders_with_id_and_truncated_content() -> None:
    event = _assistant_with(
        [
            ToolResultBlock(
                type="tool_result",
                tool_use_id="t1",
                content="boom! " + "x" * 500,
                is_error=True,
            ),
        ],
    )
    out = event_to_messages(event)
    assert len(out) == 1
    text, kb = out[0]
    assert kb is None
    assert text.startswith("❌ t1: ")
    body = text.removeprefix("❌ t1: ")
    # 500 'x's would mean the full content was passed through; truncation to
    # 200 keeps the runaway content out of the outbound message.
    assert "x" * 300 not in body


def test_result_success_sub_minute_renders_seconds_one_decimal() -> None:
    event = ResultEvent(
        type="result",
        subtype="success",
        duration_ms=2200,
        total_cost_usd=0.0012,
    )
    out = event_to_messages(event)
    assert len(out) == 1
    text, kb = out[0]
    assert kb is None
    assert "✅ done" in text
    assert "2.2s" in text
    assert "ms" not in text
    assert "$" not in text


def test_result_success_over_minute_renders_minutes_and_seconds() -> None:
    event = ResultEvent(
        type="result",
        subtype="success",
        duration_ms=65_000,
    )
    out = event_to_messages(event)
    assert len(out) == 1
    text, kb = out[0]
    assert kb is None
    assert "1m 5s" in text
    assert "ms" not in text
    assert "$" not in text


def test_result_success_missing_duration_renders_question_mark() -> None:
    event = ResultEvent(type="result", subtype="success", duration_ms=None)
    out = event_to_messages(event)
    assert out == [("✅ done · ?", None)]
    assert "$" not in out[0][0]
    assert "ms" not in out[0][0]


def test_result_success_with_usage_under_10k_renders_raw_count() -> None:
    event = ResultEvent(
        type="result",
        subtype="success",
        duration_ms=2200,
        usage=ResultUsage(input_tokens=3000, output_tokens=1200),
    )
    out = event_to_messages(event)
    assert len(out) == 1
    text, kb = out[0]
    assert kb is None
    assert "4200 tokens" in text
    assert "$" not in text
    assert "k tokens" not in text


def test_result_success_with_usage_at_or_above_10k_renders_k_suffix() -> None:
    event = ResultEvent(
        type="result",
        subtype="success",
        duration_ms=2200,
        usage=ResultUsage(input_tokens=30_000, output_tokens=12_000),
    )
    out = event_to_messages(event)
    assert len(out) == 1
    text, kb = out[0]
    assert kb is None
    assert "42k tokens" in text
    assert "$" not in text


def test_result_success_no_usage_omits_token_segment() -> None:
    event = ResultEvent(type="result", subtype="success", duration_ms=2200)
    out = event_to_messages(event)
    assert out == [("✅ done · 2.2s", None)]
    assert "tokens" not in out[0][0]
    assert "$" not in out[0][0]


def test_result_success_zero_usage_omits_token_segment() -> None:
    event = ResultEvent(
        type="result",
        subtype="success",
        duration_ms=2200,
        usage=ResultUsage(input_tokens=0, output_tokens=0),
    )
    out = event_to_messages(event)
    assert out == [("✅ done · 2.2s", None)]
    assert "tokens" not in out[0][0]


def test_result_failure_renders_subtype_and_no_dollar_sign() -> None:
    event = ResultEvent(type="result", subtype="error_during_execution")
    out = event_to_messages(event)
    assert out == [("❌ failed: error_during_execution", None)]
    assert "$" not in out[0][0]


def test_result_never_contains_dollar_sign_across_inputs() -> None:
    """Belt-and-suspenders: no ``$`` character escapes from any result rendering."""
    cases = [
        ResultEvent(type="result", subtype="success", duration_ms=2200, total_cost_usd=0.0012),
        ResultEvent(type="result", subtype="success", duration_ms=65_000, total_cost_usd=1.23),
        ResultEvent(type="result", subtype="success", duration_ms=None, total_cost_usd=None),
        ResultEvent(
            type="result",
            subtype="success",
            duration_ms=2200,
            total_cost_usd=0.5,
            usage=ResultUsage(input_tokens=3000, output_tokens=1200),
        ),
        ResultEvent(
            type="result",
            subtype="success",
            duration_ms=200,
            usage=ResultUsage(input_tokens=30_000, output_tokens=12_000),
        ),
        ResultEvent(type="result", subtype="error_during_execution"),
    ]
    for event in cases:
        for text, _kb in event_to_messages(event):
            assert "$" not in text, f"unexpected $ in {text!r}"


def test_permission_request_returns_message_with_keyboard_sentinel() -> None:
    """A permission_request event renders as one tuple with a sentinel keyboard."""
    event = PermissionRequest(
        type="permission_request",
        request_id="r1",
        tool_name="bash",
        input={"cmd": "ls"},
        options=["approve", "skip", "abort"],
    )
    out = event_to_messages(event)
    assert len(out) == 1
    text, kb = out[0]
    assert "Permission requested" in text
    assert "bash" in text
    assert "ls" in text
    assert isinstance(kb, _PendingKeyboard)
    assert kb.request_id == "r1"
    assert kb.options == ["approve", "skip", "abort"]


def test_permission_request_html_escapes_tool_name_and_input() -> None:
    """Tool name and input repr go through ``html.escape``."""
    event = PermissionRequest(
        type="permission_request",
        request_id="r2",
        tool_name="<bash>",
        input={"cmd": "<rm -rf>"},
        options=["approve"],
    )
    out = event_to_messages(event)
    assert len(out) == 1
    text, _kb = out[0]
    assert "<bash>" not in text
    assert "&lt;bash&gt;" in text
    # repr() wraps strings in quotes; the html-escape applies to the repr.
    assert "<rm -rf>" not in text


def test_system_init_returns_empty_list() -> None:
    event = SystemInit(type="system", subtype="init")
    assert event_to_messages(event) == []


def test_unknown_event_returns_empty_list() -> None:
    event = UnknownEvent(type="weird", raw={"x": 1})
    assert event_to_messages(event) == []


def test_long_text_event_yields_multiple_chunks_under_limit() -> None:
    """A 10 000-char single text block yields ≥3 messages, none over 4096."""
    long = "Sentence one. Sentence two. Sentence three. " * 250  # ~11k chars
    event = _assistant_with([TextBlock(type="text", text=long)])
    out = event_to_messages(event)
    assert len(out) >= 3
    for text, kb in out:
        assert kb is None
        assert len(text) <= TELEGRAM_HARD_LIMIT
