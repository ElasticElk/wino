"""Typed-correction lookup talks to /responses, not /chat/completions."""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from integrations.grok_vision import GrokVisionAPI


def _wine():
    return {
        "type": "label",
        "name": "KANONKOP PINOTAGE",
        "producer": "Kanonkop",
        "vintage": 2022,
        "barcode": None,
    }


def _responses(text, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {
        "output": [
            {"type": "web_search_call"},
            {"type": "message", "content": [{"type": "output_text", "text": text}]},
        ],
    }
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    return resp


def _completions(body, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {"choices": [{"message": {"content": json.dumps(body)}}]}
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    return resp


@patch("integrations.grok_vision.requests.post")
def test_identify_uses_responses_web_search_and_cleans_the_row(post):
    text = (
        "Found it [[1]](https://x.test).\n```json\n"
        + json.dumps(_wine())
        + "\n```"
    )
    post.return_value = _responses(text)

    data = GrokVisionAPI("k").identify_wine_from_hint(
        "Kanonkop Pinotage 2022", wrong_name="Kanonkop Pinotage 2021",
    )

    url, = [c.kwargs.get("url") or c.args[0] for c in post.call_args_list]
    payload = post.call_args.kwargs["json"]
    assert url.endswith("/responses")
    assert payload["tools"] == [{"type": "web_search"}]
    assert payload["input"][0]["content"].startswith("The previous identification")
    assert data["name"] == "Kanonkop Pinotage"
    assert data["vintage"] == 2022
    assert post.call_count == 1


@patch("integrations.grok_vision.requests.post")
def test_identify_falls_back_to_completions_without_the_search_tool(post):
    post.side_effect = [
        _responses("no", status=422),
        _completions(_wine()),
    ]

    data = GrokVisionAPI("k").identify_wine_from_hint("Kanonkop Pinotage 2022")

    first, second = post.call_args_list
    assert first.args[0].endswith("/responses")
    assert second.args[0].endswith("/chat/completions")
    assert "tools" not in second.kwargs["json"]
    assert data["name"] == "Kanonkop Pinotage"


@patch("integrations.grok_vision.requests.post")
def test_identify_returns_none_when_both_calls_fail(post):
    post.side_effect = requests.Timeout("slow")
    assert GrokVisionAPI("k").identify_wine_from_hint("x") is None
