"""Rate-limit message parsing used by the Account & Usage panel."""

import datetime as dt

from omnicouncil.accounts import parse_limit_message

NOW = dt.datetime(2026, 10, 2, 10, 0)


def test_codex_try_again_at():
    t = parse_limit_message("ERROR: You've hit your usage limit. ... try again at 8:51 PM.", NOW)
    assert dt.datetime.fromtimestamp(t) == dt.datetime(2026, 10, 2, 20, 51)


def test_agy_quota_resets_in_hours_minutes():
    t = parse_limit_message("Individual quota reached. Please upgrade your subscription. Resets in 149h25m35s.", NOW)
    assert dt.datetime.fromtimestamp(t) == NOW + dt.timedelta(hours=149, minutes=25)


def test_unrelated_errors_are_ignored():
    assert parse_limit_message("connection reset by peer", NOW) is None
    assert parse_limit_message("invalid model selection", NOW) is None
