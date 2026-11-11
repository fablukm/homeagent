import structlog

from homeagent.sinks.base import Sink, Topic, register_sink


@register_sink("stdout")
class StdoutSink(Sink):
    name = "stdout"

    def __init__(self) -> None:
        self.log = structlog.get_logger("sink")
        self.lines: list[tuple[Topic, str]] = []

    async def emit(self, topic: Topic, text: str) -> None:
        self.lines.append((topic, text))
        self.log.info("emit", topic=topic.value, text=text)
