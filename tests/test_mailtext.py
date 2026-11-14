from datetime import date

from homeagent.config.settings import Settings
from homeagent.core.mailtext import MailText, following_sunday

MONDAY = date(2026, 12, 28)


def _text(settings: Settings) -> MailText:
    return MailText(settings.triage.text, settings.triage.body_chars)


def test_latest_drops_the_quoted_thread_but_keeps_a_forward(settings: Settings) -> None:
    text = _text(settings)
    reply = "Danke!\n\nOn Mon, 3 Nov 2026 at 10:00, Anna <a@example.ch> wrote:\n> old text"
    quoted = "Passt.\n> Geht es am Freitag?"
    forward = (
        "FYI\n\n---------- Forwarded message ---------\nFrom: Shop <s@example.ch>\n"
        "Date: Mon, 3 Nov 2026\nSubject: Abholung\n\nIhre Bestellung ist abholbereit."
    )

    assert text.latest(reply).strip() == "Danke!"
    assert text.latest(quoted) == "Passt."
    assert "abholbereit" in text.latest(forward)


def test_clean_keeps_the_words_and_drops_links_footers_and_signature(
    settings: Settings,
) -> None:
    body = (
        "Hallo\n[Hier klicken](https://x.example/a) für Details\nhttps://t.example/abc\n"
        "Abmelden vom Newsletter\n--\nAnna Muster"
    )

    assert _text(settings).clean(body) == "Hallo\nHier klicken für Details"
    assert _text(settings).clean("z" * (settings.triage.body_chars + 9)) == "z" * (
        settings.triage.body_chars
    )


def test_dates_resolve_written_relative_and_numeric_dates(settings: Settings) -> None:
    body = (
        "Rechnung vom 01.12.2026. Termin am 5.1. um 10 Uhr, zahlbar bis 15.01.2027, "
        "Link gültig 30 Tage. Antwort bis Freitag, morgen Abholung, sonst Ende des Monats, "
        "spätestens 18. Februar."
    )

    found = _text(settings).dates(body, MONDAY)

    assert [f.day for f in found] == [
        date(2027, 1, 5),
        date(2027, 1, 15),
        date(2027, 1, 27),
        date(2027, 1, 1),
        date(2026, 12, 29),
        date(2026, 12, 31),
        date(2027, 2, 18),
    ]
    assert "zahlbar bis 15.01.2027" in found[1].quote
    assert [f.kind for f in found][2:5] == ["period", "weekday", "relative"]


def test_dates_ignore_the_quoted_thread(settings: Settings) -> None:
    body = "Gerne.\n\nAm 3. Dez. 2026 schrieb Anna <a@example.ch>:\n> bis 20.01.2027 bitte"

    assert _text(settings).dates(body, MONDAY) == []


def test_following_sunday_is_never_the_same_day() -> None:
    assert following_sunday(date(2026, 11, 11)) == date(2026, 11, 15)
    assert following_sunday(date(2026, 11, 15)) == date(2026, 11, 22)
