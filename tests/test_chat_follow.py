"""Tailing answers.jsonl by byte offset.

This is the same shape as the Monitor loop that watches the ask queue, and it
goes wrong in the same two ways: a line counter replays the whole backlog the
moment the file is pruned, and a naive read splits a line that is still being
written into two broken halves.
"""
import json

from lumbung.chat import follow


def _append(p, text):
    with p.open("a", encoding="utf-8") as fh:
        fh.write(text)
    return p.stat().st_size


def test_missing_file_is_not_an_error(tmp_path):
    lines, off = follow(tmp_path / "nope.jsonl", 0)
    assert lines == []
    assert off == 0


def test_nothing_new_returns_nothing(tmp_path):
    p = tmp_path / "a.jsonl"
    size = _append(p, '{"ts": 1, "text": "one"}\n')
    assert follow(p, size) == ([], size)


def test_new_lines_are_returned_once(tmp_path):
    p = tmp_path / "a.jsonl"
    _append(p, '{"ts": 1, "text": "one"}\n')
    lines, off = follow(p, 0)
    assert [json.loads(x)["text"] for x in lines] == ["one"]

    _append(p, '{"ts": 2, "text": "two"}\n')
    lines, off = follow(p, off)
    assert [json.loads(x)["text"] for x in lines] == ["two"], "must not repeat 'one'"

    assert follow(p, off) == ([], off)


def test_a_half_written_line_is_left_for_next_time(tmp_path):
    """The writer appends without locking, so a read can land mid-line.

    Returning the fragment would emit invalid JSON and lose the message.
    """
    p = tmp_path / "a.jsonl"
    _append(p, '{"ts": 1, "text": "done"}\n{"ts": 2, "te')
    lines, off = follow(p, 0)
    assert [json.loads(x)["text"] for x in lines] == ["done"]

    _append(p, 'xt": "rest"}\n')
    lines, off = follow(p, off)
    assert [json.loads(x)["text"] for x in lines] == ["rest"], "the fragment completes"


def test_no_complete_line_yet_consumes_nothing(tmp_path):
    p = tmp_path / "a.jsonl"
    _append(p, '{"ts": 1, "te')
    assert follow(p, 0) == ([], 0)


def test_truncation_follows_down_instead_of_replaying(tmp_path):
    """Pruning test entries must not spam the chat with the whole backlog."""
    p = tmp_path / "a.jsonl"
    _append(p, '{"ts": 1, "text": "one"}\n{"ts": 2, "text": "two"}\n')
    _, off = follow(p, 0)

    p.write_text('{"ts": 9, "text": "fresh"}\n', encoding="utf-8")
    lines, new_off = follow(p, off)
    assert lines == [], "a smaller file is a rewrite, not new content"
    assert new_off == p.stat().st_size

    _append(p, '{"ts": 10, "text": "after"}\n')
    lines, _ = follow(p, new_off)
    assert [json.loads(x)["text"] for x in lines] == ["after"]


def test_blank_lines_are_skipped(tmp_path):
    p = tmp_path / "a.jsonl"
    _append(p, '\n\n{"ts": 1, "text": "one"}\n\n')
    lines, _ = follow(p, 0)
    assert len(lines) == 1


def test_utf8_survives_the_round_trip(tmp_path):
    """Answers carry em-dashes, bullets and arrows; mangling them is visible."""
    p = tmp_path / "a.jsonl"
    payload = {"ts": 1, "text": "Rp 31.000.000 — committed • 37 days → due"}
    _append(p, json.dumps(payload, ensure_ascii=False) + "\n")
    lines, _ = follow(p, 0)
    assert json.loads(lines[0])["text"] == payload["text"]
