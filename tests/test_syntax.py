from homeagent.core.syntax import parse_title


def test_blocker_with_tag() -> None:
    parsed = parse_title("! Kinderarzt #kids")
    assert parsed.blocker and parsed.tags == {"kids"} and parsed.text == "Kinderarzt"
    assert parsed.tokens == {"blocker", "kids"}


def test_task_urgent_and_size() -> None:
    parsed = parse_title("!Pay Mietvertrag ~L #bill", task=True)
    assert parsed.urgent and parsed.size == "L" and parsed.tags == {"bill"}
    assert parsed.text == "Pay Mietvertrag"


def test_plain_title_has_no_tokens() -> None:
    assert parse_title("Lunch with Anna").tokens == frozenset()
