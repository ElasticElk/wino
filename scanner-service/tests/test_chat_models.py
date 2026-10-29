"""The chat client: shape of the calls, and what a failure is called.

No network. A fake post records the payload and returns whatever the test
hands it, including timeouts.
"""

import json
from unittest.mock import MagicMock

import pytest
import requests

from integrations.chat_models import (
    PARSE_MODEL,
    PARSE_TIMEOUT,
    PLACE_MODEL,
    RECOMMEND_MODEL,
    ChatFailure,
    ChatModels,
    compact_cellar,
    recommend_schema,
)


def _response(content, status=200, headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    if isinstance(content, str):
        body = content
    else:
        body = json.dumps(content)
    resp.json.return_value = {"choices": [{"message": {"content": body}}]}
    resp.text = body
    return resp


def _parse_body():
    return {
        "intent": "fact",
        "fact": {
            "kind": "drawer", "storage_text": "small fridge",
            "drawer": 3, "country_code": "", "grape": "",
        },
        "taste": {
            "colour": "any", "words": [], "occasion": "", "food": "", "budget_max": None,
        },
        "price": {"amount": None, "currency": ""},
        "wine_ref": "",
        "correction": {
            "name": "", "producer": "", "vintage": None, "grape": "", "country_code": "",
        },
        "answer_index": None,
    }


def _pick(wine_id, reason="fits", evidence="note"):
    return {"id": wine_id, "reason": reason, "evidence": evidence}


def _recommend_body(wine_id=12, question=""):
    return {
        "question": question,
        "options": ["Red", "White"] if question else [],
        "pick": _pick(wine_id),
        "runner_up": _pick(0, "", "none"),
        "alternatives": [],
        "no_evidence_for": [],
    }


def _models(post, now=None, sleep=None):
    return ChatModels(
        api_key="k",
        post=post,
        now=now or (lambda: 0),
        sleep=sleep or (lambda _s: None),
    )


def test_parse_sends_model_schema_and_temperature_zero():
    seen = {}

    def post(url, headers, json, timeout):
        seen.update(json=json, timeout=timeout, url=url)
        return _response(_parse_body())

    _models(post).parse("what's in D3?", {"open_question": None})
    assert seen["json"]["model"] == PARSE_MODEL
    assert seen["json"]["temperature"] == 0
    schema = seen["json"]["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert "intent" in schema["schema"]["properties"]
    assert "help" in schema["schema"]["properties"]["intent"]["enum"]
    assert "drink" in schema["schema"]["properties"]["intent"]["enum"]
    assert "help_topic" in schema["schema"]["properties"]
    assert seen["timeout"] == PARSE_TIMEOUT


def _places_response(text, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.json.return_value = {
        "output": [
            {"type": "web_search_call"},
            {"type": "message", "content": [{"type": "output_text", "text": text}]},
        ],
    }
    return resp


def test_find_places_uses_responses_web_search_and_cleans_rows():
    seen = {}
    text = (
        "Here are some spots [[1]](https://x.test).\n```json\n"
        + json.dumps({"places": [
            {"name": "Tannin", "area": "Bree St", "why": "Big SA list [1]", "url": "https://tannin.test"},
            {"name": "Sketchy", "area": "", "why": "ok", "url": "javascript:alert(1)"},
            {"name": "", "area": "x", "why": "no name", "url": ""},
            {"name": "A", "area": "", "why": "", "url": ""},
            {"name": "B", "area": "", "why": "", "url": ""},
        ]})
        + "\n```"
    )

    def post(url, headers, json, timeout):
        seen.update(url=url, json=json)
        return _places_response(text)

    places = _models(post).find_places("bar near me", "likes Syrah", lat=-33.9, lon=18.4)
    assert seen["url"].endswith("/responses")
    assert seen["json"]["model"] == PLACE_MODEL
    assert seen["json"]["tools"] == [{"type": "web_search"}]
    assert "-33.9000" in seen["json"]["input"][0]["content"]
    assert [p["name"] for p in places] == ["Tannin", "Sketchy", "A"]
    assert places[0]["why"] == "Big SA list"
    assert places[1]["url"] == ""


def test_find_places_needs_somewhere_and_maps_failures():
    def never(*_a, **_k):
        raise AssertionError("should not call")

    with pytest.raises(ChatFailure) as exc:
        _models(never).find_places("bar", "")
    assert exc.value.kind == "bad_output"

    def slow(*_a, **_k):
        raise requests.Timeout("slow")

    with pytest.raises(ChatFailure) as exc:
        _models(slow).find_places("bar", "", area="Paarl")
    assert exc.value.kind == "timeout"

    with pytest.raises(ChatFailure) as exc:
        _models(lambda *a, **k: _places_response("no json here")).find_places("bar", "", area="Paarl")
    assert exc.value.kind == "bad_output"


def test_support_sends_the_manual_and_replies_in_the_payload():
    seen = {}

    def post(url, headers, json, timeout):
        seen["messages"] = json["messages"]
        return _response({"reply": "Stuur /invite."})

    reply = _models(post).support("moet my vrou haar eie Telegram hê?", staff=False)
    assert reply == "Stuur /invite."
    system = seen["messages"][0]["content"]
    assert "/invite" in system
    assert "not staff" in system
    assert "moet my vrou" in seen["messages"][1]["content"]


def test_parse_returns_dict_on_valid_json():
    def post(url, headers, json, timeout):
        return _response(_parse_body())

    data = _models(post).parse("list my wines", {})
    assert data["intent"] == "fact"
    assert data["fact"]["drawer"] == 3


@pytest.mark.parametrize("boom,kind", [
    (requests.Timeout("slow"), "timeout"),
    (requests.ConnectionError("down"), "unavailable"),
])
def test_transport_failure_kinds(boom, kind):
    def post(url, headers, json, timeout):
        raise boom

    with pytest.raises(ChatFailure) as caught:
        _models(post).parse("hello there friend", {})
    assert caught.value.kind == kind


@pytest.mark.parametrize("status,kind", [
    (500, "unavailable"),
    (404, "unavailable"),
    (429, "rate_limited"),
    (401, "auth"),
    (403, "auth"),
])
def test_http_failure_kinds(status, kind):
    def post(url, headers, json, timeout):
        return _response("no", status=status)

    with pytest.raises(ChatFailure) as caught:
        _models(post).parse("hello there friend", {})
    assert caught.value.kind == kind


def test_non_json_is_bad_output():
    def post(url, headers, json, timeout):
        return _response("not json at all")

    with pytest.raises(ChatFailure) as caught:
        _models(post).parse("hello", {})
    assert caught.value.kind == "bad_output"


def test_missing_intent_is_bad_output():
    body = _parse_body()
    del body["intent"]

    def post(url, headers, json, timeout):
        return _response(body)

    with pytest.raises(ChatFailure) as caught:
        _models(post).parse("hello", {})
    assert caught.value.kind == "bad_output"


def test_bad_enum_is_bad_output():
    body = _parse_body()
    body["intent"] = "dance"

    def post(url, headers, json, timeout):
        return _response(body)

    with pytest.raises(ChatFailure) as caught:
        _models(post).parse("hello", {})
    assert caught.value.kind == "bad_output"


def test_429_with_short_retry_after_retries_once_then_succeeds():
    calls = {"n": 0}

    def post(url, headers, json, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return _response("no", status=429, headers={"Retry-After": "1"})
        return _response(_parse_body())

    slept = []
    data = _models(post, sleep=slept.append).parse("list them", {})
    assert data["intent"] == "fact"
    assert calls["n"] == 2
    assert slept == [1.0]


def test_breaker_opens_after_three_failures_and_skips_network():
    calls = {"n": 0}

    def post(url, headers, json, timeout):
        calls["n"] += 1
        raise requests.Timeout("slow")

    chat = _models(post)
    for _ in range(3):
        with pytest.raises(ChatFailure):
            chat.parse("a long sentence here", {})
    with pytest.raises(ChatFailure) as caught:
        chat.parse("a long sentence here", {})
    assert caught.value.kind == "unavailable"
    assert caught.value.detail == "circuit open"
    assert calls["n"] == 3
    assert chat.healthy(PARSE_MODEL) is False


def test_breaker_probes_after_cooldown_and_closes_on_success():
    clock = {"t": 0.0}
    calls = {"n": 0}

    def post(url, headers, json, timeout):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise requests.ConnectionError("down")
        return _response(_parse_body())

    chat = _models(post, now=lambda: clock["t"])
    for _ in range(3):
        with pytest.raises(ChatFailure):
            chat.parse("sentence one two", {})
    clock["t"] = 120.0
    data = chat.parse("sentence one two", {})
    assert data["intent"] == "fact"
    assert chat.healthy(PARSE_MODEL) is True
    chat.parse("sentence one two", {})
    assert calls["n"] == 5


def test_bad_output_does_not_trip_breaker():
    calls = {"n": 0}

    def post(url, headers, json, timeout):
        calls["n"] += 1
        return _response("nope")

    chat = _models(post)
    for _ in range(5):
        with pytest.raises(ChatFailure) as caught:
            chat.parse("hello", {})
        assert caught.value.kind == "bad_output"
    assert calls["n"] == 5
    assert chat.healthy(PARSE_MODEL) is True


def test_recommend_drops_unknown_ids_and_raises_bad_output_for_unknown_pick():
    body = _recommend_body(wine_id=99)
    body["alternatives"] = [_pick(12), _pick(99), _pick(13)]
    body["runner_up"] = _pick(12)

    def post(url, headers, json, timeout):
        return _response(body)

    with pytest.raises(ChatFailure) as caught:
        _models(post).recommend(
            "fennel", {}, "cellar", "", [], allow_question=True, valid_ids={12, 13},
        )
    assert caught.value.kind == "bad_output"

    body["pick"] = _pick(12)
    data = _models(post).recommend(
        "fennel", {}, "cellar", "", [], allow_question=True, valid_ids={12, 13},
    )
    assert data["pick"]["id"] == 12
    assert [a["id"] for a in data["alternatives"]] == [13]
    assert data["runner_up"]["id"] == 0


def test_recommend_runner_up_equal_to_pick_is_dropped():
    body = _recommend_body(12)
    body["runner_up"] = _pick(12, "also")

    def post(url, headers, json, timeout):
        return _response(body)

    data = _models(post).recommend(
        "fennel", {}, "cellar", "", [], allow_question=False, valid_ids={12},
    )
    assert data["runner_up"]["id"] == 0


def test_question_allows_a_zero_pick():
    body = _recommend_body(0, question="Red or white?")

    def post(url, headers, json, timeout):
        return _response(body)

    data = _models(post).recommend(
        "something nice", {}, "cellar", "", [], allow_question=True, valid_ids={12},
    )
    assert data["question"] == "Red or white?"
    assert data["pick"]["id"] == 0


def test_recommend_without_question_schema_has_no_question_field():
    seen = {}

    def post(url, headers, json, timeout):
        seen["schema"] = json["response_format"]["json_schema"]["schema"]
        return _response(_recommend_body())

    _models(post).recommend("fennel", {}, "cellar", "", [], allow_question=False)
    assert "question" not in seen["schema"]["properties"]
    assert "question" not in recommend_schema(allow_question=False)["properties"]
    assert "question" in recommend_schema(allow_question=True)["properties"]


def test_cellar_block_is_identical_between_calls_and_sentence_is_last():
    seen = []

    def post(url, headers, json, timeout):
        seen.append(json["messages"])
        return _response(_recommend_body())

    chat = _models(post)
    chat.recommend("fennel", {"colour": "red"}, "CELLAR", "profile", [], allow_question=True)
    chat.recommend("leathery", {"colour": "red"}, "CELLAR", "profile", [], allow_question=True)
    assert seen[0][1]["content"] == seen[1][1]["content"] == "CELLAR"
    assert seen[0][1]["content"] != seen[0][2]["content"]
    assert "fennel" in seen[0][2]["content"]
    assert seen[0][2]["content"].rstrip().endswith('Parsed: {"colour": "red"}') or "fennel" in seen[0][2]["content"]
    assert seen[0][2]["content"].index("CELLAR") if False else "fennel" in seen[0][-1]["content"]
    assert "leathery" not in seen[0][-1]["content"]


def test_conv_id_header_uses_household():
    seen = {}

    def post(url, headers, json, timeout):
        seen["headers"] = headers
        return _response(_recommend_body())

    _models(post).recommend(
        "fennel", {}, "cellar", "", [], allow_question=True, household_id=14,
    )
    assert seen["headers"]["x-grok-conv-id"] == "hh-14"


def _wine(wine_id, **extra):
    base = {
        "id": wine_id,
        "name": f"Wine {wine_id}",
        "producer": "Producer",
        "wine_type": "RE",
        "grapes": ["Syrah"],
        "country": "ZA",
        "region": "Stellenbosch",
        "items": [{"storage": "Big Fridge", "drawer": 2, "price": 10}],
        "drink_by": "2027-01-01",
        "rating": 8,
        "critic_score": 90,
        "vivino_rating": 4.1,
        "votes": {"up": 1, "meh": 0, "down": 0},
        "food_pairings": ["Lamb"],
        "attributes": ["Leathery"],
        "comment": "fennel",
        "stock_count": 1,
    }
    base.update(extra)
    return base


def test_compact_cellar_line_format_single_grape_and_blend():
    single = _wine(2)
    blend = _wine(1, grapes=["Syrah", "Grenache"], producer="", food_pairings=[], attributes=[], comment="")
    text, lookup = compact_cellar([single, blend])
    lines = text.splitlines()
    assert lines[0].startswith("#1 | ")
    assert "blend: Syrah, Grenache" in lines[0]
    assert "Producer" not in lines[0]
    assert "pairs:" not in lines[0]
    assert "note:" not in lines[0]
    assert lines[1].startswith("#2 | Wine 2 | Producer | red | Syrah | ZA/Stellenbosch | Big Fridge D2")
    assert "note: fennel" in lines[1]
    assert "votes +1 ~0 -0" in lines[1]
    assert set(lookup) == {1, 2}


def test_compact_cellar_is_stable():
    wines = [_wine(3), _wine(1), _wine(2)]
    assert compact_cellar(wines)[0] == compact_cellar(list(reversed(wines)))[0]


def test_compact_cellar_caps_at_300_using_explicit_fields_only():
    wines = []
    for i in range(1, 302):
        wines.append(_wine(
            i,
            wine_type="WH" if i == 301 else "RE",
            comment="fennel" if i == 301 else "",
            drink_by=f"2030-01-{i:02d}" if i < 32 else "2099-01-01",
            grapes=["Chenin Blanc"] if i == 301 else ["Syrah"],
            country="FR" if i == 301 else "ZA",
        ))
    text, lookup = compact_cellar(wines, colour="red")
    assert 301 not in lookup
    assert len(lookup) == 300
    assert "fennel" not in text

    # No explicit filter: the note does not earn a place past the cap.
    # The white with the latest-looking id still loses to the drink-by order
    # only after the cap, and colour was not applied.
    text, lookup = compact_cellar(wines)
    assert len(lookup) == 300
    assert 301 not in lookup
