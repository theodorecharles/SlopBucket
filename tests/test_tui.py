from slop.tui import _split_bar, _status_markup
from slop.quota import Quota, Window


def test_empty_bar_is_all_track():
    filled, empty = _split_bar(0, 20)
    assert filled == 0
    assert empty == 20


def test_full_bar_is_all_fill():
    filled, empty = _split_bar(100, 20)
    assert filled == 20
    assert empty == 0


def test_status_hides_internals():
    q = Quota(
        name="ted",
        ok=True,
        ordinary_allowed=False,
        rate_limit_reached=True,
        windows=[Window("7d", 100, 0, 10080, 1)],
        credits=173.48,
    )
    markup = _status_markup(q, False)
    assert "ordinaryUsageAllowed" not in markup
    assert "empty" in markup
    assert "173 credits" in markup
