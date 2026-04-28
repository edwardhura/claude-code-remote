"""Tests for :mod:`ccr.claude.log`."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ccr.claude.events import SystemInit, TextBlock, UserTurn, parse_event
from ccr.claude.log import JsonlSessionLog, prune


def _make_event(text: str = "hello") -> UserTurn:
    return UserTurn(
        type="user",
        message={  # type: ignore[arg-type]
            "role": "user",
            "content": [TextBlock(type="text", text=text)],
        },
    )


async def test_append_assigns_sequential_seqs_starting_at_zero(tmp_path: Path) -> None:
    log = JsonlSessionLog(tmp_path / "s1.jsonl")
    await log.open()

    seq0 = await log.append(_make_event("first"))
    seq1 = await log.append(_make_event("second"))
    assert seq0 == 0
    assert seq1 == 1
    assert log.seq == 2


async def test_open_resumes_seq_from_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "s2.jsonl"
    pre = "\n".join(
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": f"line-{i}"},
            }
        )
        for i in range(3)
    )
    path.write_text(pre + "\n", encoding="utf-8")

    log = JsonlSessionLog(path)
    await log.open()
    assert log.seq == 3
    seq3 = await log.append(_make_event("after-resume"))
    assert seq3 == 3


async def test_read_from_yields_existing_lines_in_order(tmp_path: Path) -> None:
    log = JsonlSessionLog(tmp_path / "s3.jsonl")
    await log.open()
    for i in range(5):
        await log.append(_make_event(f"msg-{i}"))

    collected: list[tuple[int, str]] = []
    async for seq, event in log.read_from(0):
        assert isinstance(event, UserTurn)
        content = event.message.content
        text_block = content[0] if isinstance(content, list) else None
        text = text_block.text if isinstance(text_block, TextBlock) else ""
        collected.append((seq, text))
    assert collected == [(0, "msg-0"), (1, "msg-1"), (2, "msg-2"), (3, "msg-3"), (4, "msg-4")]


async def test_read_from_at_or_past_eof_yields_nothing(tmp_path: Path) -> None:
    log = JsonlSessionLog(tmp_path / "s4.jsonl")
    await log.open()
    for i in range(2):
        await log.append(_make_event(f"m-{i}"))

    collected: list[tuple[int, object]] = []
    async for pair in log.read_from(2):
        collected.append(pair)
    assert collected == []

    # Far past EOF.
    async for pair in log.read_from(100):
        collected.append(pair)
    assert collected == []


async def test_tail_yields_appends_after_subscribe(tmp_path: Path) -> None:
    log = JsonlSessionLog(tmp_path / "s5.jsonl")
    await log.open()

    received: list[tuple[int, object]] = []

    async def consumer() -> None:
        async for seq, event in log.tail():
            received.append((seq, event))
            if len(received) == 2:
                break

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0)
    await log.append(_make_event("a"))
    await log.append(_make_event("b"))
    await asyncio.wait_for(task, timeout=1)
    assert [seq for seq, _ in received] == [0, 1]


async def test_two_concurrent_tailers_both_see_appends(tmp_path: Path) -> None:
    log = JsonlSessionLog(tmp_path / "s6.jsonl")
    await log.open()

    a: list[int] = []
    b: list[int] = []

    async def consumer(out: list[int]) -> None:
        async for seq, _event in log.tail():
            out.append(seq)
            if len(out) == 3:
                break

    ta = asyncio.create_task(consumer(a))
    tb = asyncio.create_task(consumer(b))
    await asyncio.sleep(0)
    for _ in range(3):
        await log.append(_make_event("x"))
        # Yield so tailers can drain before we clear _notify on the next
        # append. Without this each append might overwrite the _notify edge.
        await asyncio.sleep(0)
    await asyncio.wait_for(asyncio.gather(ta, tb), timeout=2)
    assert a == [0, 1, 2]
    assert b == [0, 1, 2]


async def test_tail_is_cancel_safe(tmp_path: Path) -> None:
    log = JsonlSessionLog(tmp_path / "s7.jsonl")
    await log.open()

    async def consumer() -> None:
        async for _ in log.tail():
            pass

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Subsequent appends still work.
    seq = await log.append(_make_event("post-cancel"))
    assert seq == 0


def test_prune_keeps_last_n_files(tmp_path: Path) -> None:
    files = []
    base = time.time()
    for i in range(5):
        p = tmp_path / f"s{i}.jsonl"
        p.write_text("{}\n", encoding="utf-8")
        # Set distinct mtimes so order is deterministic.
        os.utime(p, (base - (5 - i) * 100, base - (5 - i) * 100))
        files.append(p)

    deleted = prune(tmp_path, retention_count=3, retention_days=365_000)
    assert deleted == 2
    remaining = sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".jsonl")
    # The newest 3 (s2, s3, s4) survive.
    assert remaining == ["s2.jsonl", "s3.jsonl", "s4.jsonl"]


def test_prune_deletes_files_older_than_retention_days(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    fresh = tmp_path / "fresh.jsonl"
    stale = tmp_path / "stale.jsonl"
    fresh.write_text("{}\n", encoding="utf-8")
    stale.write_text("{}\n", encoding="utf-8")

    fresh_ts = now.timestamp()
    stale_ts = now.timestamp() - (40 * 86400)
    os.utime(fresh, (fresh_ts, fresh_ts))
    os.utime(stale, (stale_ts, stale_ts))

    deleted = prune(tmp_path, retention_count=100, retention_days=30, now=now)
    assert deleted == 1
    names = {p.name for p in tmp_path.iterdir()}
    assert names == {"fresh.jsonl"}


def test_prune_handles_missing_dir(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    deleted = prune(missing, retention_count=10, retention_days=10)
    assert deleted == 0


async def test_read_from_parses_unknown_lines_as_unknown_event(tmp_path: Path) -> None:
    """Lines that fail to parse must surface as UnknownEvent so SSE
    replay does not abort mid-stream."""
    path = tmp_path / "bad.jsonl"
    path.write_text("this is not json\n", encoding="utf-8")
    log = JsonlSessionLog(path)
    await log.open()
    items: list[object] = []
    async for _seq, event in log.read_from(0):
        items.append(event)
    assert len(items) == 1
    parsed = parse_event("this is not json")
    assert type(items[0]) is type(parsed)


async def test_open_is_idempotent(tmp_path: Path) -> None:
    log = JsonlSessionLog(tmp_path / "s8.jsonl")
    await log.open()
    await log.append(_make_event("first"))
    # Calling open() again should not reset the seq.
    await log.open()
    seq = await log.append(_make_event("second"))
    assert seq == 1


async def test_append_creates_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "nested"
    log = JsonlSessionLog(nested / "s.jsonl")
    await log.open()
    seq = await log.append(_make_event("nested"))
    assert seq == 0
    assert (nested / "s.jsonl").exists()


# Reference symbol so the import isn't dropped by linters.
_ = SystemInit
