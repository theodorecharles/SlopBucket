import json

from slop.stats import collect_stats, fmt_tokens


def _line(ts, type_, payload, ordinal=None):
    row = {"timestamp": ts, "type": type_, "payload": payload}
    if ordinal is not None:
        row["ordinal"] = ordinal
    return json.dumps(row)


def _usage(input_, cached, output, reasoning, total, cache_write=0):
    return {
        "input_tokens": input_,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": total,
    }


def _token(ts, total, last, ordinal=None):
    return _line(
        ts,
        "event_msg",
        {"type": "token_count", "info": {"total_token_usage": total, "last_token_usage": last}},
        ordinal,
    )


def _write_session(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n")


def _seed(root):
    day1 = "2026-11-01T12:00:00.000Z"
    day2 = "2026-11-02T12:00:00.000Z"
    first = _usage(100, 40, 10, 3, 110)
    second = _usage(300, 240, 30, 9, 330)
    second_last = _usage(200, 200, 20, 6, 220)
    a = root / "2026" / "11" / "01" / "rollout-a.jsonl"
    _write_session(
        a,
        [
            _line(day1, "session_meta", {"id": "a"}),
            _line(day1, "turn_context", {"model": "gpt-m1"}),
            _token(day1, first, first),
            # Duplicate cumulative event (rate-limit style repeat): counts once.
            _token(day1, first, first),
            _token(day1, second, second_last),
            _line(day1, "event_msg", {"type": "token_count", "info": None}),
        ],
    )
    baseline = _usage(990, 0, 10, 0, 1000)
    after_fork = _usage(1090, 50, 20, 5, 1110)
    b = root / "2026" / "11" / "02" / "rollout-b.jsonl"
    _write_session(
        b,
        [
            _line(day2, "session_meta", {"id": "b", "subagent_history_start_ordinal": 10}),
            # Copied parent history: baseline only, never counted.
            _token(day2, baseline, baseline, ordinal=5),
            _line(day2, "turn_context", {"model": "gpt-m2"}),
            _token(day2, after_fork, _usage(100, 50, 10, 5, 110), ordinal=12),
        ],
    )
    return a


def test_collect_stats_counts_cumulative_deltas_once(tmp_path):
    root = tmp_path / "sessions"
    _seed(root)
    stats = collect_stats(session_dirs=[root], cache_path=tmp_path / "cache.json")
    assert stats.total["total"] == 440
    assert stats.total["input"] == 400
    assert stats.total["cached"] == 290
    assert stats.total["output"] == 40
    assert stats.total["reasoning"] == 14
    assert stats.requests == 3
    assert stats.sessions == 2
    assert stats.days["2026-11-01"]["total"] == 330
    assert stats.days["2026-11-02"]["total"] == 110
    assert stats.models["gpt-m1"]["total"] == 330
    assert stats.models["gpt-m2"]["total"] == 110
    assert stats.first_day == "2026-11-01"
    assert stats.last_day == "2026-11-02"


def test_collect_stats_incremental_append_does_not_double_count(tmp_path):
    root = tmp_path / "sessions"
    a = _seed(root)
    cache = tmp_path / "cache.json"
    first = collect_stats(session_dirs=[root], cache_path=cache)
    assert first.total["total"] == 440
    day1 = "2026-11-01T13:00:00.000Z"
    with a.open("a") as fh:
        fh.write(_token(day1, _usage(400, 240, 50, 9, 450), _usage(100, 0, 20, 0, 120)) + "\n")
    again = collect_stats(session_dirs=[root], cache_path=cache)
    assert again.total["total"] == 560
    assert again.days["2026-11-01"]["total"] == 450
    assert again.requests == 4
    # Unchanged files come from cache and stay stable.
    cached = collect_stats(session_dirs=[root], cache_path=cache)
    assert cached.total["total"] == 560


def test_collect_stats_empty_dirs(tmp_path):
    stats = collect_stats(session_dirs=[tmp_path / "missing"], cache_path=tmp_path / "c.json")
    assert stats.total["total"] == 0
    assert stats.sessions == 0
    assert stats.days == {}


def test_fmt_tokens():
    assert fmt_tokens(0) == "0"
    assert fmt_tokens(999) == "999"
    assert fmt_tokens(1234) == "1,234"
    assert fmt_tokens(45_600_000) == "45.6M"
    assert fmt_tokens(1_234_567_890) == "1.23B"
    assert fmt_tokens(2_500_000_000_000) == "2.50T"
