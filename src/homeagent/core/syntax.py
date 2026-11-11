import re
from dataclasses import dataclass, field

TAG = re.compile(r"(?<!\w)#([a-z][a-z0-9_-]*)")
SIZE = re.compile(r"(?<!\w)~(S|M|L|XL|\d+[mhd])\b")


@dataclass(frozen=True)
class ParsedTitle:
    text: str
    blocker: bool = False
    tentative: bool = False
    urgent: bool = False
    tags: frozenset[str] = field(default_factory=frozenset[str])
    size: str | None = None

    @property
    def tokens(self) -> frozenset[str]:
        flags = {
            name
            for name, on in (
                ("blocker", self.blocker),
                ("tentative", self.tentative),
                ("urgent", self.urgent),
            )
            if on
        }
        return frozenset(flags | self.tags)


def parse_title(raw: str, *, task: bool = False) -> ParsedTitle:
    text = raw.strip()
    blocker = tentative = urgent = False
    if text.startswith("!"):
        if task:
            urgent = True
        else:
            blocker = True
        text = text[1:].lstrip()
    elif text.startswith("?"):
        tentative = True
        text = text[1:].lstrip()
    tags = frozenset(m.lower() for m in TAG.findall(text))
    size_match = SIZE.search(text)
    size = size_match.group(1) if size_match else None
    clean = SIZE.sub("", TAG.sub("", text)).strip()
    clean = re.sub(r"\s{2,}", " ", clean)
    return ParsedTitle(clean, blocker, tentative, urgent, tags, size)
