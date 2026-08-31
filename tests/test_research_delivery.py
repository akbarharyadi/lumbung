"""Research findings must reach the chat, and the backlog must stay visible.

The morning job queued questions into a file for weeks and nothing read it back.
The bug was not in any function -- every function worked -- it was that no
function existed. These tests exist so the path cannot quietly disappear again.

There is one delivery channel now: `answers.jsonl`, which the in-app chat
streams. `research_answers.jsonl` is the ledger that keeps `pending_questions`
honest, and the two must not be confused -- writing to only the ledger delivers
nothing, writing to only the chat answers the same question forever.
"""

from __future__ import annotations

import json

import pytest

from lumbung.research import (
    Finding,
    Question,
    deliver_findings,
    pending_questions,
    question_key,
    queue_questions,
)


@pytest.fixture
def data(tmp_path):
    """Never the real data directory: an earlier suite wrote live queue entries
    and the running app announced questions nobody had asked."""
    return tmp_path


def q(topic="rates", text="did the BI rate move?") -> Question:
    return Question(topic=topic, text=text, why="because")


# -- identity ---------------------------------------------------------------
def test_questions_queued_together_are_still_distinct():
    """One `lumbung research` run stamps every question with the same ts, so a
    ts-only key would mark all five answered the moment one was."""
    rows = [q("rates").as_dict(), q("news").as_dict()]
    rows[1]["ts"] = rows[0]["ts"]
    assert question_key(rows[0]) != question_key(rows[1])


def test_the_same_question_asked_again_is_a_new_question():
    """"Any material news in the last 24 hours" is meant to recur. Hashing the
    text alone would suppress it forever after the first answer."""
    a = q().as_dict()
    b = dict(a, ts=a["ts"] + 86_400)
    assert question_key(a) != question_key(b)


# -- backlog ----------------------------------------------------------------
def test_everything_is_pending_before_any_answer(data):
    queue_questions(data / "research_queue.jsonl", [q("rates"), q("news")])
    open_q = pending_questions(
        data / "research_queue.jsonl", data / "research_answers.jsonl"
    )
    assert len(open_q) == 2


def test_answering_one_leaves_the_others_pending(data):
    queue_questions(data / "research_queue.jsonl", [q("rates"), q("news")])
    rows = [json.loads(x) for x in
            (data / "research_queue.jsonl").read_text(encoding="utf-8").splitlines()]

    deliver_findings(
        [Finding(topic="rates", text="BI held at 5.75%.", key=question_key(rows[0]))],
        data_dir=data,
    )
    open_q = pending_questions(
        data / "research_queue.jsonl", data / "research_answers.jsonl"
    )
    assert [r["topic"] for r in open_q] == ["news"]


def test_a_missing_answers_file_means_everything_is_pending(data):
    queue_questions(data / "research_queue.jsonl", [q()])
    assert len(pending_questions(data / "research_queue.jsonl", data / "nope.jsonl")) == 1


def test_a_half_written_line_does_not_lose_the_rest(data):
    queue_questions(data / "research_queue.jsonl", [q("rates"), q("news")])
    with open(data / "research_queue.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"topic": "truncated"\n')
    assert len(pending_questions(
        data / "research_queue.jsonl", data / "research_answers.jsonl")) == 2


# -- delivery ---------------------------------------------------------------
def test_findings_reach_the_chat(data):
    out = deliver_findings([Finding(topic="rates", text="BI held at 5.75%.")],
                           data_dir=data)
    assert out["written"] == 1

    answers = (data / "answers.jsonl").read_text(encoding="utf-8").strip()
    assert "BI held at 5.75%." in answers
    assert json.loads(answers)["source"] == "morning-research"


def test_the_ledger_keeps_the_question_the_chat_does_not(data):
    """Two files, two jobs.

    The chat shows a conversation; the ledger has to be able to say which
    question was answered, or `pending_questions` cannot tell.
    """
    deliver_findings(
        [Finding(topic="rates", text="BI held.", question="did the BI rate move?",
                 key="k1")],
        data_dir=data,
    )
    chat = json.loads((data / "answers.jsonl").read_text(encoding="utf-8").strip())
    ledger = json.loads((data / "research_answers.jsonl").read_text(encoding="utf-8").strip())
    assert "question" not in chat
    assert ledger["question"] == "did the BI rate move?"
    assert ledger["key"] == "k1"


def test_nothing_is_written_for_an_empty_list(data):
    assert deliver_findings([], data_dir=data) == {"written": 0}
    assert not (data / "answers.jsonl").exists()


def test_a_long_finding_is_delivered_whole(data):
    """No length cap any more.

    The old one existed because Telegram rejects a message over 4096 bytes. A
    file has no such limit, and splitting an answer into numbered fragments was
    never anything but a workaround.
    """
    deliver_findings([Finding(topic="huge", text="y" * 9_000)], data_dir=data)
    row = json.loads((data / "answers.jsonl").read_text(encoding="utf-8").strip())
    assert row["text"].count("y") == 9_000, "no text may be dropped"


def test_every_finding_gets_its_own_line(data):
    deliver_findings(
        [Finding(topic=f"t{i}", text=f"finding {i}") for i in range(6)],
        data_dir=data,
    )
    lines = (data / "answers.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 6
    assert [json.loads(x)["topic"] for x in lines] == [f"t{i}" for i in range(6)]
