import structlog

from homeagent.sinks.base import Card, Sink, Topic, register_sink


@register_sink("stdout")
class StdoutSink(Sink):
    name = "stdout"

    def __init__(self) -> None:
        self.log = structlog.get_logger("sink")
        self.lines: list[tuple[Topic, str]] = []
        self.asked: list[tuple[Topic, Card]] = []

    async def emit(self, topic: Topic, text: str) -> None:
        self.lines.append((topic, text))
        self.log.info("emit", topic=topic.value, text=text)

    async def ask(self, topic: Topic, card: Card) -> str:
        self.asked.append((topic, card))
        self.log.info("ask", topic=topic.value, text=card.text, key=card.key)
        return str(len(self.asked))
