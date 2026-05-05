"""Tests for ``ccr.bot.formatting`` — chunking + per-event rendering."""

from __future__ import annotations

import uuid

from ccr.bot.formatting import (
    SAFE_CHUNK,
    TELEGRAM_HARD_LIMIT,
    chunk_text,
    event_to_messages,
)
from ccr.claude.events import (
    AssistantTurn,
    McpPermissionRequest,
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


# --------------------------------------------------------------------------- #
# CCR-026: AskUserQuestion rendering inside an AssistantTurn.
# --------------------------------------------------------------------------- #


def test_assistant_turn_with_ask_user_question_options_renders_keyboard_sentinel() -> None:
    """Probed schema with options → _PendingKeyboard(kind='auq', request_id=id8, options=labels)."""
    from ccr.bot.formatting import _PendingKeyboard

    full_id = "toolu_aaaabbbbccccdddd"
    block = ToolUseBlock(
        type="tool_use",
        id=full_id,
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "question": "Pick a colour",
                    "header": "colour",
                    "multiSelect": False,
                    "options": [
                        {"label": "red", "description": "rouge"},
                        {"label": "blue", "description": "blue"},
                    ],
                },
            ],
        },
    )
    event = _assistant_with([block])
    out = event_to_messages(event)
    assert len(out) == 1
    text, kb = out[0]
    assert "Pick a colour" in text
    assert isinstance(kb, _PendingKeyboard)
    assert kb.kind == "auq"
    assert kb.request_id == full_id[:8]
    assert kb.options == ["red", "blue"]


def test_assistant_turn_with_ask_user_question_no_options_renders_text_only() -> None:
    """Free-text question (empty options) → text + /answer hint, no sentinel."""
    block = ToolUseBlock(
        type="tool_use",
        id="toolu_freetext_aaaabbbb",
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "question": "What name should I use?",
                    "options": [],
                },
            ],
        },
    )
    event = _assistant_with([block])
    out = event_to_messages(event)
    assert len(out) == 1
    text, kb = out[0]
    assert kb is None
    assert "What name should I use?" in text
    assert "/answer" in text
    assert "toolu_fr" in text  # the 8-hex prefix appears in the hint


def test_assistant_turn_with_ask_user_question_html_escapes_question_and_options() -> None:
    """HTML-escape the question text and ensure options arrive verbatim in the sentinel."""
    from ccr.bot.formatting import _PendingKeyboard

    block = ToolUseBlock(
        type="tool_use",
        id="toolu_evilevil00000000",
        name="AskUserQuestion",
        input={
            "questions": [
                {
                    "question": "<script>alert(1)</script>",
                    "options": [{"label": "A & B"}, {"label": "C"}],
                },
            ],
        },
    )
    event = _assistant_with([block])
    out = event_to_messages(event)
    assert len(out) == 1
    text, kb = out[0]
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    # Options are kept as plain strings inside the sentinel; the
    # broadcast loop hands them to the keyboard builder which uses them
    # as button labels (Telegram does not run HTML on button labels).
    assert isinstance(kb, _PendingKeyboard)
    assert kb.options == ["A & B", "C"]


def test_assistant_turn_with_ask_user_question_empty_input_falls_back() -> None:
    """tool_input={} → fallback text and free-text path."""
    block = ToolUseBlock(
        type="tool_use",
        id="toolu_emptyempty0000",
        name="AskUserQuestion",
        input={},
    )
    event = _assistant_with([block])
    out = event_to_messages(event)
    assert len(out) == 1
    text, kb = out[0]
    assert kb is None
    assert "(no question text)" in text


def test_assistant_turn_mixed_blocks_keeps_other_tool_uses_unchanged() -> None:
    """Text + AskUserQuestion + Bash tool_use → 3 outbound entries in order."""
    from ccr.bot.formatting import _PendingKeyboard

    blocks = [
        TextBlock(type="text", text="thinking..."),
        ToolUseBlock(
            type="tool_use",
            id="toolu_q_aaaabbbb",
            name="AskUserQuestion",
            input={"questions": [{"question": "?", "options": [{"label": "x"}]}]},
        ),
        ToolUseBlock(
            type="tool_use",
            id="toolu_bash_001",
            name="Bash",
            input={"command": "ls"},
        ),
    ]
    event = _assistant_with(blocks)
    out = event_to_messages(event)
    assert len(out) == 3
    assert out[0][0] == "thinking..."
    assert out[0][1] is None
    assert isinstance(out[1][1], _PendingKeyboard)
    assert out[1][1].kind == "auq"
    assert out[2][0].startswith("\U0001f527 Bash")
    assert out[2][1] is None


# --------------------------------------------------------------------------- #
# CCR-028: McpPermissionRequest formatter — defensive guard.
# --------------------------------------------------------------------------- #


_MCP_TEST_SESSION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def test_event_to_messages_drops_mcp_permission_request_for_ask_user_question() -> None:
    """A stray AUQ McpPermissionRequest is silently dropped at the formatter."""
    event = McpPermissionRequest(
        request_id="abcdef01",
        session_id=_MCP_TEST_SESSION_ID,
        tool_name="AskUserQuestion",
        tool_input={"questions": []},
    )
    assert event_to_messages(event) == []


def test_event_to_messages_keeps_mcp_permission_request_for_other_tools() -> None:
    """Non-AUQ McpPermissionRequest envelopes still render the keyboard message."""
    from ccr.bot.formatting import _PendingKeyboard

    event = McpPermissionRequest(
        request_id="cafebabe",
        session_id=_MCP_TEST_SESSION_ID,
        tool_name="Bash",
        tool_input={"cmd": "ls"},
    )
    out = event_to_messages(event)
    assert len(out) == 1
    text, kb = out[0]
    assert "Permission requested" in text
    assert "Bash" in text
    assert isinstance(kb, _PendingKeyboard)
    assert kb.kind == "perm"
    assert kb.request_id == "cafebabe"
