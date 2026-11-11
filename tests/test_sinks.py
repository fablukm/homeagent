from homeagent.sinks.base import Topic, clip
from homeagent.sinks.stdout import StdoutSink


def test_clip_keeps_two_lines_and_marks_truncation() -> None:
    assert clip("a\nb\nc", 2) == "a\n…"
    assert clip("a\nb", 2) == "a\nb"


async def test_stdout_sink_records() -> None:
    sink = StdoutSink()
    await sink.emit(Topic.LOG, "hello")
    assert sink.lines == [(Topic.LOG, "hello")]
