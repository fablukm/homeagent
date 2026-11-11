import structlog

from homeagent.sinks.base import Sink, Topic, register_sink


@register_sink("stdout")
class StdoutSink(Sink):
    name = "stdout"

    def __init__(self) -> None:
        self.log = structlog.get_logger("sink")
        self.lines: list[tuple[Topic, str]] = []
        self.asked: list[tuple[Topic, str, str]] = []

    async def emit(self, topic: Topic, text: str) -> None:
        self.lines.append((topic, text))
        self.log.info("emit", topic=topic.value, text=text)

    async def ask(self, topic: Topic, text: str, key: str) -> str:
        self.asked.append((topic, text, key))
        self.log.info("ask", topic=topic.value, text=text, key=key)
        return str(len(self.asked))
