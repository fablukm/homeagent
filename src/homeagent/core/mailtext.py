import re
from dataclasses import dataclass
from datetime import date, timedelta

from homeagent.config.settings import MailTextCfg

ISO_DATE = re.compile(r"(?<![\d.])(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
NUMERIC_DATE = re.compile(
    r"(?<![\d.:/])(\d{1,2})\s?[./]\s?(\d{1,2})\s?[./]\s?(\d{4}|\d{2})(?![\d:])"
)
SHORT_DATE = re.compile(r"(?<![\d.:/])(\d{1,2})\.\s?(\d{1,2})\.(?!\s?\d)")
DAY_MONTH = r"(?<!\d)(\d{{1,2}})(\.|er|°|st|nd|rd|th)?\s+({months})\.?(\s+(\d{{4}}))?\b"
MONTH_DAY = r"\b({months})\.?\s+(\d{{1,2}})(st|nd|rd|th)?(,?\s+(\d{{4}}))?\b"
PERIOD = r"\b(\d{{1,3}}|{numbers})\s?(-\s?)?({units})\b"
SHORT_PERIOD = re.compile(r"\b(\d{1,3})\s?d\b")
WORDS = r"\b({words})\b"
WORD = re.compile(r"\w")
CENTURY = 2000
TWO_DIGITS = 100
MONTHS_PER_YEAR = 12
WEEK = 7
SUNDAY = 6


@dataclass(frozen=True)
class Found:
    day: date
    quote: str
    kind: str


def _alternatives(words: dict[str, int]) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


def _make(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _upcoming(day: int, month: int, received: date) -> date | None:
    for year in (received.year, received.year + 1):
        found = _make(year, month, day)
        if found is None:
            return None
        if found >= received - timedelta(days=1):
            return found
    return None


def _full_year(text: str) -> int:
    year = int(text)
    return year + CENTURY if year < TWO_DIGITS else year


def following_sunday(day: date) -> date:
    return day + timedelta(days=(SUNDAY - day.weekday()) % WEEK or WEEK)


class MailText:
    def __init__(self, cfg: MailTextCfg, body_chars: int) -> None:
        self.cfg = cfg
        self.body_chars = body_chars
        self.thread = (re.compile(cfg.reply_markers), re.compile(cfg.outlook_header))
        self.forward = re.compile(cfg.forward)
        self.footer = re.compile(cfg.footer)
        self.markdown_link = re.compile(cfg.markdown_link)
        self.url = re.compile(cfg.url)
        self.signature = re.compile(cfg.signature)
        months = _alternatives(cfg.months)
        self.day_month = re.compile(DAY_MONTH.format(months=months))
        self.month_day = re.compile(MONTH_DAY.format(months=months))
        self.period = re.compile(
            PERIOD.format(numbers=_alternatives(cfg.number_words), units=_alternatives(cfg.units))
        )
        self.weekday = re.compile(WORDS.format(words=_alternatives(cfg.weekdays)))
        self.relative = re.compile(WORDS.format(words=_alternatives(cfg.relative)))
        self.next_week = re.compile(cfg.next_week)
        self.month_end = re.compile(cfg.month_end)

    def latest(self, body: str) -> str:
        cut = len(body)
        for pattern in self.thread:
            for match in pattern.finditer(body):
                before = body[max(0, match.start() - self.cfg.forward_lookback) : match.start()]
                if self.forward.search(before):
                    continue
                if body[: match.start()].strip():
                    cut = min(cut, match.start())
                break
        lines = body[:cut].splitlines()
        return "\n".join(ln for ln in lines if not ln.lstrip().startswith(self.cfg.quote_prefix))

    def clean(self, body: str) -> str:
        text = self.url.sub("", self.markdown_link.sub(r"\1", self.latest(body)))
        kept: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if self.signature.match(stripped):
                break
            if stripped and WORD.search(stripped) and not self.footer.search(stripped):
                kept.append(stripped)
        return "\n".join(kept)[: self.body_chars]

    @staticmethod
    def _named(year: str | None, month: int, day: int, received: date) -> date | None:
        return _make(int(year), month, day) if year else _upcoming(day, month, received)

    def dates(self, body: str, received: date) -> list[Found]:
        text = self.latest(body)
        low = text.lower()
        found: list[tuple[int, Found]] = []

        def add(match: re.Match[str], day: date | None, kind: str) -> None:
            if day is not None and day >= received:
                left = max(0, match.start() - self.cfg.quote_chars)
                quote = " ".join(text[left : match.end() + self.cfg.quote_chars].split())
                found.append((match.start(), Found(day, quote, kind)))

        for m in ISO_DATE.finditer(low):
            add(m, _make(int(m[1]), int(m[2]), int(m[3])), "date")
        for m in NUMERIC_DATE.finditer(low):
            add(m, _make(_full_year(m[3]), int(m[2]), int(m[1])), "date")
        for m in SHORT_DATE.finditer(low):
            if 1 <= int(m[2]) <= MONTHS_PER_YEAR:
                add(m, _upcoming(int(m[1]), int(m[2]), received), "date")
        for m in self.day_month.finditer(low):
            add(m, self._named(m[5], self.cfg.months[m[3]], int(m[1]), received), "date")
        for m in self.month_day.finditer(low):
            add(m, self._named(m[5], self.cfg.months[m[1]], int(m[2]), received), "date")
        for m in self.period.finditer(low):
            count = int(m[1]) if m[1].isdigit() else self.cfg.number_words[m[1]]
            add(m, received + timedelta(days=count * self.cfg.units[m[3]]), "period")
        for m in SHORT_PERIOD.finditer(low):
            add(m, received + timedelta(days=int(m[1])), "period")
        for m in self.weekday.finditer(low):
            ahead = (self.cfg.weekdays[m[1]] - received.weekday()) % WEEK or WEEK
            add(m, received + timedelta(days=ahead), "weekday")
        for m in self.relative.finditer(low):
            add(m, received + timedelta(days=self.cfg.relative[m[1]]), "relative")
        for m in self.next_week.finditer(low):
            add(m, received + timedelta(days=(SUNDAY - received.weekday()) + WEEK), "relative")
        for m in self.month_end.finditer(low):
            first = date(
                received.year + received.month // MONTHS_PER_YEAR,
                received.month % MONTHS_PER_YEAR + 1,
                1,
            )
            add(m, first - timedelta(days=1), "relative")
        seen: dict[date, Found] = {}
        for _, item in sorted(found, key=lambda pair: pair[0]):
            seen.setdefault(item.day, item)
        return list(seen.values())
