import pytest

from slop.quota import parse_rate_limits, until_label, window_label


PAYLOAD = {
    "ordinaryUsageAllowed": False,
    "rateLimitResetCredits": {"availableCount": 3, "credits": None},
    "rateLimits": {
        "limitId": "codex",
        "primary": {
            "usedPercent": 100,
            "windowDurationMins": 10080,
            "resetsAt": 1789819240,
        },
        "secondary": None,
        "credits": {
            "hasCredits": True,
            "unlimited": False,
            "balance": "173.4810950000",
        },
        "planType": "pro",
        "rateLimitReachedType": "rate_limit_reached",
    },
    "rateLimitsByLimitId": {
        "codex": {
            "limitId": "codex",
            "primary": {
                "usedPercent": 100,
                "windowDurationMins": 10080,
                "resetsAt": 1789819240,
            },
            "secondary": None,
            "credits": {
                "hasCredits": True,
                "unlimited": False,
                "balance": "173.4810950000",
            },
            "rateLimitReachedType": "rate_limit_reached",
        },
        "codex_bengalfox": {
            "limitId": "codex_bengalfox",
            "limitName": "GPT-5.3-Codex-Spark",
            "primary": {
                "usedPercent": 0,
                "windowDurationMins": 300,
                "resetsAt": 1789444511,
            },
            "secondary": {
                "usedPercent": 0,
                "windowDurationMins": 10080,
                "resetsAt": 1790031311,
            },
            "credits": None,
            "rateLimitReachedType": None,
        },
    },
}


def test_window_labels():
    assert window_label(300) == "5h"
    assert window_label(10080) == "7d"
    assert window_label(60) == "1h"
    assert window_label(1440) == "1d"


def test_until_past():
    assert until_label(1, now=100) == "now"


def test_parse_rate_limits():
    quota = parse_rate_limits(
        "ted",
        {"type": "chatgpt", "email": "me@tedcharles.net", "planType": "pro"},
        PAYLOAD,
    )
    assert quota.ok
    assert quota.blocked
    assert quota.email == "me@tedcharles.net"
    assert quota.windows[0].label == "7d"
    assert quota.windows[0].remaining_percent == 0
    assert quota.credits == 173.481095
    assert quota.banked_resets == 3
    assert quota.extra[0][0] == "GPT-5.3-Codex-Spark"
    assert quota.extra[0][1][0].label == "5h"
    assert quota.extra[0][1][0].remaining_percent == 100


@pytest.mark.parametrize("count", [0, 1, 7])
@pytest.mark.parametrize("details", [None, [], [{"id": "reset-1", "status": "available"}]])
def test_banked_resets_use_authoritative_count(count, details):
    quota = parse_rate_limits(
        "account",
        None,
        {
            "rateLimits": PAYLOAD["rateLimits"],
            "rateLimitResetCredits": {"availableCount": count, "credits": details},
        },
    )
    assert quota.banked_resets == count
    assert quota.windows[0].remaining_percent == 0


@pytest.mark.parametrize(
    "reset_fields",
    [
        {},
        {"rateLimitResetCredits": None},
        {"rateLimitResetCredits": []},
        {"rateLimitResetCredits": {"credits": [{"status": "available"}]}},
        *[
            {"rateLimitResetCredits": {"availableCount": value}}
            for value in [None, -1, True, False, "3", 1.5, 2.0]
        ],
    ],
)
def test_missing_or_invalid_banked_resets_preserve_usage(reset_fields):
    payload = {key: value for key, value in PAYLOAD.items() if key != "rateLimitResetCredits"}
    quota = parse_rate_limits("account", None, {**payload, **reset_fields})
    assert quota.ok
    assert quota.banked_resets is None
    assert quota.credits == 173.481095
    assert quota.windows[0].remaining_percent == 0
