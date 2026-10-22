"""Staff /chatstats: how chat suggestions are landing."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.telegram_admin import cmd_chatstats, format_chat_stats
from integrations.telegram_commands import ADMIN_COMMANDS

SAMPLE = {
    "days": 30, "asks": 12, "shown": 10, "questions": 2,
    "outcomes": {"accepted": 6, "rejected": 1, "abandoned": 1, "pending": 4},
    "something_else_taps": 5,
    "opened_pick": 4, "opened_pick_pct": 40, "opened_other": 2,
    "pick_votes": {"up": 3, "meh": 0, "down": 1, "none": 0},
    "fallback": 1,
    "models": {"grok-4.20-0309-non-reasoning": 11, "quick": 1},
    "latency_ms": {"p50": 1400, "p90": 2600},
    "worst": [{
        "id": 9, "household": "Curl <Curl>", "created": "2026-10-20T19:00:00+00:00",
        "sentence": "a white for <fish>", "why": ["opened a different bottle"],
    }],
}


def test_format_shows_the_drink_signal_and_escapes_user_text():
    text = format_chat_stats(SAMPLE)
    assert "The suggested bottle: <b>4</b> (40%)" in text
    assert "That's the one: 6 (60%)" in text
    assert "Median 1.4s, slowest 10% 2.6s" in text
    assert "a white for &lt;fish&gt;" in text
    assert "Curl &lt;Curl&gt;" in text
    assert "<fish>" not in text


def test_format_with_no_asks():
    assert "No asks yet" in format_chat_stats({"days": 7, "asks": 0})


def test_format_with_no_timings_yet():
    data = dict(SAMPLE, latency_ms={"p50": None, "p90": None})
    assert "Median —" in format_chat_stats(data)


@pytest.mark.asyncio
async def test_command_passes_days_and_uses_the_staff_client():
    handler = MagicMock()
    handler.admin_api.chat_stats.return_value = SAMPLE
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["7"]
    await cmd_chatstats(update, context, handler)
    handler.admin_api.chat_stats.assert_called_once_with(7)
    assert update.message.reply_text.await_args.kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_command_failure_never_shows_the_exception():
    handler = MagicMock()
    handler.admin_api.chat_stats.side_effect = RuntimeError("Token abc123 rejected")
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = []
    await cmd_chatstats(update, context, handler)
    body = update.message.reply_text.await_args.args[0]
    assert "abc123" not in body
    assert "Couldn't load chat stats" in body


def test_command_is_in_the_staff_menu():
    assert "chatstats" in [name for name, _ in ADMIN_COMMANDS]
