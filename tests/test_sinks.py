from uuid import uuid7

from homeagent.sinks.base import Topic, clip
from homeagent.sinks.stdout import StdoutSink
from homeagent.sinks.telegram import decided_text, parse_verdict, verdict_data

TELEGRAM_CALLBACK_BYTES = 64


def test_clip_keeps_two_lines_and_marks_truncation() -> None:
    assert clip("a\nb\nc", 2) == "a\n…"
    assert clip("a\nb", 2) == "a\nb"


async def test_stdout_sink_records() -> None:
    sink = StdoutSink()
    await sink.emit(Topic.LOG, "hello")
    assert sink.lines == [(Topic.LOG, "hello")]


async def test_stdout_sink_records_questions() -> None:
    sink = StdoutSink()
    ref = await sink.ask(Topic.APPROVAL, "pay", "k1")
    assert sink.asked == [(Topic.APPROVAL, "pay", "k1")]
    assert ref == "1"


def test_verdict_round_trips_and_fits_telegram() -> None:
    key = str(uuid7())
    assert parse_verdict(verdict_data(True, key)) == (True, key)
    assert parse_verdict(verdict_data(False, key)) == (False, key)
    assert len(verdict_data(True, key).encode()) <= TELEGRAM_CALLBACK_BYTES


def test_unknown_verdicts_are_ignored() -> None:
    assert parse_verdict("") is None
    assert parse_verdict("maybe:abc") is None
    assert parse_verdict("y:") is None


def test_decided_text_keeps_the_line_limit() -> None:
    assert decided_text("a\nb\nc\nd", True, 5) == "a\nb\nc\nd\naccepted"
    assert decided_text("a\nb\nc\nd\ne", True, 5) == "a\nb\nc\n…\naccepted"
    assert decided_text("a\nb", False, 5) == "a\nb\ndeclined"
