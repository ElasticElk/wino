"""Eval-style typed-message pipeline: mocked classifier, real execute path.

Each case is a realistic English or Afrikaans line with an expected intent
and outcome. The model is a fake so the tests stay deterministic.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from integrations.chat_intent import (
    CLASSIFY_INTENTS,
    LOW_CONFIDENCE,
    SCHEMA_INTENTS,
    SLOT_KEYS,
    canonical_parse,
    gated_flag,
    needs_clarification,
    slot_summary,
)
from integrations.chat_models import PARSE_PROMPT, parse_prompt, parse_schema
from integrations.copy import t
from test_chat_router import FakeChat, _api, _context, _handler, _labels, _tap, _text, _wine


def _slots(**kwargs):
    body = {
        "wine": "", "producer": "", "appellation": "", "vintage": None,
        "colour": "", "grape": "", "person": "", "club": "",
        "location": "", "quantity": None,
    }
    body.update(kwargs)
    return body


def _club(**kwargs):
    body = {
        "kind": "none", "member_name": "", "club_name": "", "wine_text": "",
        "grape": "", "vintage": None, "period": "", "when": "",
    }
    body.update(kwargs)
    return body


def _parse(intent, **kwargs):
    body = {
        "intent": intent,
        "confidence": kwargs.pop("confidence", 0.9),
        "slots": kwargs.pop("slots", _slots()),
        "fact": kwargs.pop("fact", {
            "kind": "none", "storage_text": "", "drawer": None,
            "country_code": "", "grape": "",
        }),
        "taste": kwargs.pop("taste", {
            "colour": "any", "words": [], "occasion": "", "food": "", "budget_max": None,
        }),
        "price": kwargs.pop("price", {"amount": None, "currency": ""}),
        "wine_ref": kwargs.pop("wine_ref", ""),
        "correction": kwargs.pop("correction", {
            "name": "", "producer": "", "vintage": None, "grape": "", "country_code": "",
        }),
        "answer_index": kwargs.pop("answer_index", None),
        "area": kwargs.pop("area", ""),
        "help_topic": kwargs.pop("help_topic", "other"),
        "club": kwargs.pop("club", _club()),
        "list_op": kwargs.pop("list_op", "none"),
        "unknown_kind": kwargs.pop("unknown_kind", "none"),
    }
    body.update(kwargs)
    return body


CASES = [
    # --- cellar_query EN/AF ---
    dict(id="en_drawer", text="what's in D3 of the big fridge?", locale="en",
         parse=_parse("cellar_query", fact={"kind": "drawer", "storage_text": "big fridge", "drawer": 3, "country_code": "", "grape": ""},
                      slots=_slots(location="big fridge")),
         expect_intent="cellar_query", expect_outcome="answered", must=["Kanonkop"]),
    dict(id="af_drawer", text="wat is in D3 van die klein yskas?", locale="af",
         parse=_parse("cellar_query", fact={"kind": "drawer", "storage_text": "klein yskas", "drawer": 3, "country_code": "", "grape": ""},
                      slots=_slots(location="klein yskas")),
         expect_intent="cellar_query", expect_outcome="answered"),
    dict(id="en_grape", text="any shiraz in the fridge?", locale="en",
         parse=_parse("cellar_query", fact={"kind": "grape", "storage_text": "", "drawer": None, "country_code": "", "grape": "shiraz"},
                      slots=_slots(grape="shiraz")),
         expect_intent="cellar_query", expect_outcome="answered", must=["Kanonkop"]),
    dict(id="af_grape", text="het ek enige shiraz?", locale="af",
         parse=_parse("cellar_query", fact={"kind": "grape", "storage_text": "", "drawer": None, "country_code": "", "grape": "shiraz"},
                      slots=_slots(grape="shiraz")),
         expect_intent="cellar_query", expect_outcome="answered"),
    dict(id="en_country", text="what wines do I have from South Africa?", locale="en",
         parse=_parse("cellar_query", fact={"kind": "country", "storage_text": "", "drawer": None, "country_code": "ZA", "grape": ""},
                      slots=_slots()),
         expect_intent="cellar_query", expect_outcome="answered"),
    dict(id="af_country", text="Watter wyne het ek van Frankryk?", locale="af",
         parse=_parse("cellar_query", fact={"kind": "country", "storage_text": "", "drawer": None, "country_code": "FR", "grape": ""},
                      slots=_slots()),
         expect_intent="cellar_query", expect_outcome="answered"),
    dict(id="en_list_all", text="list all my wines", locale="en",
         parse=_parse("cellar_query", fact={"kind": "list_all", "storage_text": "", "drawer": None, "country_code": "", "grape": ""}),
         expect_intent="cellar_query", expect_outcome="answered"),
    dict(id="en_appellation", text="anything from Barolo?", locale="en",
         parse=_parse("cellar_query", fact={"kind": "grape", "storage_text": "", "drawer": None, "country_code": "", "grape": ""},
                      slots=_slots(appellation="Barolo", wine="Barolo")),
         wines=[_wine(name="Barolo Cannubi 2018", grapes=["Nebbiolo"])],
         expect_intent="cellar_query", expect_outcome="answered", must=["Barolo Cannubi"]),
    dict(id="en_appellation_none", text="anything from Barolo?", locale="en",
         parse=_parse("cellar_query", fact={"kind": "none", "storage_text": "", "drawer": None, "country_code": "", "grape": ""},
                      slots=_slots(appellation="Barolo")),
         expect_intent="cellar_query", expect_outcome="answered",
         must=["Nothing matching", "Barolo"], must_not=["You only drink"]),
    # --- club_query flags on ---
    dict(id="en_my_clubs", text="What clubs am I in?", locale="en", flags={"feature_clubs": "on"},
         parse=_parse("club_query", club=_club(kind="my_clubs")),
         expect_intent="club_query", expect_outcome="answered", club_ask=True,
         club_result={"status": "ok", "kind": "my_clubs",
                      "clubs": [{"id": 1, "name": "Thursday reds", "is_last_used": True}]},
         must=["Thursday reds"], must_not=["You only drink"]),
    dict(id="af_my_clubs", text="In watter klubs is ek?", locale="af", flags={"feature_clubs": "on"},
         parse=_parse("club_query", club=_club(kind="my_clubs")),
         expect_intent="club_query", expect_outcome="answered", club_ask=True,
         club_result={"status": "ok", "kind": "my_clubs",
                      "clubs": [{"id": 1, "name": "Thursday reds", "is_last_used": True}]},
         must=["Thursday reds"]),
    dict(id="en_members", text="who's in my club?", locale="en", flags={"feature_clubs": "on"},
         parse=_parse("club_query", club=_club(kind="members")),
         expect_intent="club_query", expect_outcome="answered", club_ask=True,
         club_result={"status": "ok", "kind": "members",
                      "club": {"id": 1, "name": "Thursday reds"},
                      "members": [{"display_name": "Riley", "role": "owner"},
                                  {"display_name": "Sam", "role": "member"}]},
         must=["Riley", "Sam"]),
    dict(id="af_members", text="wie is in my klub?", locale="af", flags={"feature_clubs": "on"},
         parse=_parse("club_query", club=_club(kind="members")),
         expect_intent="club_query", expect_outcome="answered", club_ask=True,
         club_result={"status": "ok", "kind": "members",
                      "club": {"id": 1, "name": "Thursday reds"},
                      "members": [{"display_name": "Riley", "role": "owner"}]},
         must=["Riley", "eienaar"]),
    dict(id="en_sam_merlot", text="how many Merlots does Sam have?", locale="en", flags={"feature_clubs": "on"},
         parse=_parse("club_query", club=_club(kind="cellar", member_name="Sam", grape="Merlot"),
                      slots=_slots(person="Sam", grape="Merlot")),
         expect_intent="club_query", expect_outcome="answered", club_ask=True,
         club_result={"status": "ok", "kind": "cellar", "visibility": "full",
                      "club": {"id": 1, "name": "Thursday reds"}, "member": "Sam",
                      "count": 2, "wines": [{"name": "Hillside Merlot", "vintage": 2018, "stock_count": 2}]},
         must=["Sam", "Merlot"]),
    dict(id="en_who_has_barolo", text="who in my club has a 2015 Barolo?", locale="en", flags={"feature_clubs": "on"},
         parse=_parse("club_query", club=_club(kind="who_has", wine_text="Barolo", vintage=2015),
                      slots=_slots(wine="Barolo", vintage=2015)),
         expect_intent="club_query", expect_outcome="answered", club_ask=True,
         club_result={"status": "ok", "kind": "who_has", "club": {"id": 1, "name": "Thursday reds"},
                      "query": "2015 Barolo", "matches": []}),
    dict(id="en_oldest", text="what's the oldest bottle in the club?", locale="en", flags={"feature_clubs": "on"},
         parse=_parse("club_query", club=_club(kind="oldest")),
         expect_intent="club_query", expect_outcome="answered", club_ask=True,
         club_result={"status": "ok", "kind": "oldest", "club": {"id": 1, "name": "Thursday reds"},
                      "vintage": 2001, "wines": []}),
    dict(id="en_feed", text="what did Riley drink this week?", locale="en", flags={"feature_clubs": "on"},
         parse=_parse("club_query", club=_club(kind="feed", member_name="Riley", period="week"),
                      slots=_slots(person="Riley")),
         expect_intent="club_query", expect_outcome="answered", club_ask=True,
         club_result={"status": "ok", "kind": "feed", "club": {"id": 1, "name": "Thursday reds"},
                      "member": "Riley", "events": []}),
    dict(id="en_who_drinks", text="who drinks the most?", locale="en", flags={"feature_clubs": "on"},
         parse=_parse("club_query", club=_club(kind="who_drinks")),
         expect_intent="club_query", expect_outcome="answered", club_ask=True,
         club_result={"status": "ok", "kind": "who_drinks", "club": {"id": 1, "name": "Thursday reds"},
                      "ranking": []}),
    dict(id="en_private_list", text="what's on Riley's to-drink list?", locale="en",
         flags={"feature_clubs": "on", "feature_lists": "on"},
         parse=_parse("club_query", club=_club(kind="list", member_name="Riley"),
                      slots=_slots(person="Riley")),
         expect_intent="club_query", expect_outcome="answered", club_ask=True,
         club_result={"status": "private", "kind": "list", "member": "Riley",
                      "club": {"id": 4, "name": "Thursday reds"}},
         must=["private"], must_not=["Pinotage"]),
    # --- flag-off honesty ---
    dict(id="en_clubs_off", text="What clubs am I in?", locale="en", flags={},
         parse=_parse("club_query", club=_club(kind="my_clubs")),
         expect_intent="club_query", expect_outcome="flag_off",
         must=["can't help with that yet", "own cellar"],
         must_not=["You only drink", "club", "Thursday"]),
    dict(id="af_clubs_off", text="In watter klubs is ek?", locale="af", flags={},
         parse=_parse("club_query", club=_club(kind="my_clubs")),
         expect_intent="club_query", expect_outcome="flag_off",
         must=["eie kelder"], must_not=["klub", "You only drink"]),
    dict(id="en_tasteoff_off", text="when is the next taste-off?", locale="en",
         flags={"feature_clubs": "on"},
         parse=_parse("club_query", club=_club(kind="tasteoff", when="next")),
         expect_intent="club_query", expect_outcome="flag_off",
         must=["can't help with that yet"], must_not=["taste-off", "aren't on"]),
    dict(id="af_tasteoff_off", text="wanneer is die volgende proe?", locale="af",
         flags={"feature_clubs": "on"},
         parse=_parse("club_query", club=_club(kind="tasteoff", when="next")),
         expect_intent="club_query", expect_outcome="flag_off",
         must_not=["proe", "aan"]),
    dict(id="en_comments_off", text="what did people say about the Pinotage?", locale="en",
         flags={"feature_clubs": "on"},
         parse=_parse("club_query", club=_club(kind="comments", wine_text="Pinotage"),
                      slots=_slots(wine="Pinotage")),
         expect_intent="club_query", expect_outcome="flag_off",
         must_not=["comment", "Pinotage"]),
    dict(id="en_lists_off_club", text="what's on the club list?", locale="en",
         flags={"feature_clubs": "on"},
         parse=_parse("club_query", club=_club(kind="list")),
         expect_intent="club_query", expect_outcome="flag_off",
         must_not=["To-drink", "list isn't"]),
    dict(id="en_lists_off_personal", text="what's on my to-drink list?", locale="en", flags={},
         parse=_parse("list_action", list_op="show"),
         expect_intent="list_action", expect_outcome="flag_off",
         must=["own cellar"], must_not=["To-drink lists aren't"]),
    dict(id="af_lists_off", text="wat is op my lys?", locale="af", flags={},
         parse=_parse("list_action", list_op="show"),
         expect_intent="list_action", expect_outcome="flag_off",
         must=["eie kelder"], must_not=["lys is nog nie"]),
    dict(id="en_global_off", text="are there clubs for everyone?", locale="en", flags={},
         parse=_parse("club_query", club=_club(kind="my_clubs")),
         expect_intent="club_query", expect_outcome="flag_off",
         must_not=["discover", "global"]),
    # --- drink_remove ---
    dict(id="en_drink_named", text="I drank one of my barolos from the big fridge", locale="en",
         parse=_parse("drink_remove", wine_ref="barolos",
                      fact={"kind": "none", "storage_text": "big fridge", "drawer": None, "country_code": "", "grape": ""},
                      slots=_slots(wine="barolos", location="big fridge")),
         expect_intent="drink_remove", expect_outcome="answered"),
    dict(id="af_drink_named", text="ek het een van my barolos uit die yskas gedrink", locale="af",
         parse=_parse("drink_remove", wine_ref="barolos",
                      fact={"kind": "none", "storage_text": "yskas", "drawer": None, "country_code": "", "grape": ""},
                      slots=_slots(wine="barolos", location="yskas")),
         expect_intent="drink_remove", expect_outcome="answered"),
    dict(id="en_drink_this", text="drank this", locale="en",
         parse=_parse("drink_remove", wine_ref="this", slots=_slots(wine="this")),
         expect_intent="drink_remove", expect_outcome="answered"),
    # --- add_wine / search / recommend ---
    dict(id="en_add", text="add Kanonkop Paul Sauer 2019 to the cellar", locale="en",
         parse=_parse("add_wine", slots=_slots(wine="Kanonkop Paul Sauer", vintage=2019, producer="Kanonkop")),
         expect_intent="add_wine", expect_outcome="answered", must=["photo", "Kanonkop"]),
    dict(id="af_add", text="voeg die Kanonkop by die kelder", locale="af",
         parse=_parse("add_wine", slots=_slots(wine="Kanonkop", producer="Kanonkop")),
         expect_intent="add_wine", expect_outcome="answered", must=["foto"]),
    dict(id="en_search", text="Kanonkop Paul Sauer 2019", locale="en",
         parse=_parse("search", slots=_slots(wine="Kanonkop Paul Sauer", vintage=2019, producer="Kanonkop")),
         expect_intent="search", expect_outcome="answered", handled=False),
    dict(id="af_search", text="Kanonkop Paul Sauer", locale="af",
         parse=_parse("search", slots=_slots(wine="Kanonkop Paul Sauer", producer="Kanonkop")),
         expect_intent="search", expect_outcome="answered", handled=False),
    dict(id="en_recommend", text="what should I drink tonight?", locale="en",
         parse=_parse("recommend", taste={"colour": "any", "words": [], "occasion": "tonight", "food": "", "budget_max": None}),
         expect_intent="recommend", expect_outcome="answered"),
    dict(id="af_recommend", text="wat moet ek vanaand drink?", locale="af",
         parse=_parse("recommend", taste={"colour": "any", "words": [], "occasion": "vanaand", "food": "", "budget_max": None}),
         expect_intent="recommend", expect_outcome="answered"),
    dict(id="en_recommend_fennel", text="something leathery for lamb", locale="en",
         parse=_parse("recommend", taste={"colour": "any", "words": ["leathery"], "occasion": "", "food": "lamb", "budget_max": None}),
         expect_intent="recommend", expect_outcome="answered"),
    # --- list_action flag on ---
    dict(id="en_list_show", text="what's on my to-drink list?", locale="en", flags={"feature_lists": "on"},
         parse=_parse("list_action", list_op="show"),
         expect_intent="list_action", expect_outcome="answered"),
    dict(id="af_list_show", text="wat is op my lys?", locale="af", flags={"feature_lists": "on"},
         parse=_parse("list_action", list_op="show"),
         expect_intent="list_action", expect_outcome="answered"),
    dict(id="en_list_add_need_wine", text="add this to my list", locale="en", flags={"feature_lists": "on"},
         parse=_parse("list_action", list_op="add"),
         expect_intent="list_action", expect_outcome="answered", must=["label"]),
    # --- smalltalk_help / wine_knowledge / place ---
    dict(id="en_help_invite", text="how do I add someone to my house?", locale="en",
         parse=_parse("smalltalk_help", help_topic="invite"),
         expect_intent="smalltalk_help", expect_outcome="answered"),
    dict(id="af_help_invite", text="hoe nooi ek iemand?", locale="af",
         parse=_parse("smalltalk_help", help_topic="invite"),
         expect_intent="smalltalk_help", expect_outcome="answered"),
    dict(id="en_help_photo", text="how do I add a wine?", locale="en",
         parse=_parse("smalltalk_help", help_topic="photo"),
         expect_intent="smalltalk_help", expect_outcome="answered"),
    dict(id="en_wine_knowledge", text="I'm at a steakhouse, what should I order?", locale="en",
         parse=_parse("wine_knowledge"),
         expect_intent="wine_knowledge", expect_outcome="answered", wine_chat=True,
         must_not=["Kanonkop"]),
    dict(id="af_wine_knowledge", text="Watter wyn pas by bobotie?", locale="af",
         parse=_parse("wine_knowledge"),
         expect_intent="wine_knowledge", expect_outcome="answered", wine_chat=True),
    dict(id="en_decant", text="should I decant a 2015 Rioja?", locale="en",
         parse=_parse("wine_knowledge"),
         expect_intent="wine_knowledge", expect_outcome="answered", wine_chat=True),
    dict(id="en_place_near", text="a restaurant near me with a good wine list", locale="en",
         parse=_parse("place", area=""),
         expect_intent="place", expect_outcome="answered", must=["share where you are"]),
    dict(id="en_place_area", text="wine bars in Stellenbosch", locale="en",
         parse=_parse("place", area="Stellenbosch", slots=_slots(location="Stellenbosch")),
         expect_intent="place", expect_outcome="answered"),
    dict(id="af_place", text="Ken jy 'n goeie restaurant naby my?", locale="af",
         parse=_parse("place", area=""),
         expect_intent="place", expect_outcome="answered"),
    dict(id="en_price", text="I paid 18.50 for this", locale="en",
         parse=_parse("price", price={"amount": 18.5, "currency": ""}, wine_ref="this"),
         expect_intent="price", expect_outcome="answered"),
    dict(id="en_correct", text="vintage is 2019", locale="en",
         parse=_parse("correct", correction={"name": "", "producer": "", "vintage": 2019, "grape": "", "country_code": ""}),
         expect_intent="correct", expect_outcome="answered"),
    # --- unknown / offtopic / low confidence ---
    dict(id="en_offtopic_plane", text="how high do planes fly?", locale="en",
         parse=_parse("unknown", unknown_kind="offtopic", confidence=0.95),
         expect_intent="unknown", expect_outcome="unknown",
         must=["wine"], must_not=["Kanonkop", "You only drink"]),
    dict(id="af_offtopic", text="wie het die rugby gewen?", locale="af",
         parse=_parse("unknown", unknown_kind="offtopic", confidence=0.9),
         expect_intent="unknown", expect_outcome="unknown",
         must=["wyn"], must_not=["You only drink", "Kanonkop"]),
    dict(id="en_unclear", text="maybe that one or wait", locale="en",
         parse=_parse("unknown", unknown_kind="unclear", confidence=0.2),
         expect_intent="unknown", expect_outcome="clarified",
         buttons=["What's in stock", "What should I drink"]),
    dict(id="af_unclear", text="mm nie seker nie", locale="af",
         parse=_parse("unknown", unknown_kind="unclear", confidence=0.1),
         expect_intent="unknown", expect_outcome="clarified"),
    dict(id="en_low_conf_club", text="that group thing", locale="en", flags={},
         parse=_parse("club_query", confidence=0.4, club=_club(kind="my_clubs")),
         expect_intent="club_query", expect_outcome="flag_off"),
    dict(id="en_low_conf_cellar", text="the usual?", locale="en",
         parse=_parse("cellar_query", confidence=0.2),
         expect_intent="cellar_query", expect_outcome="clarified",
         buttons=["What's in stock"]),
    dict(id="en_short_unknown_searches", text="aux moines", locale="en",
         parse=_parse("unknown", unknown_kind="unclear", confidence=0.3),
         expect_intent="unknown", expect_outcome="answered", handled=False),
    # --- outliers ---
    dict(id="en_emoji", text="🍷 tonight???", locale="en",
         parse=_parse("recommend", confidence=0.8),
         expect_intent="recommend", expect_outcome="answered"),
    dict(id="en_mixed", text="Het ek any Pinotage left", locale="en",
         parse=_parse("cellar_query", fact={"kind": "grape", "storage_text": "", "drawer": None, "country_code": "", "grape": "Pinotage"},
                      slots=_slots(grape="Pinotage")),
         expect_intent="cellar_query", expect_outcome="answered"),
    dict(id="en_person_only", text="what about Sam?", locale="en", flags={"feature_clubs": "on"},
         parse=_parse("club_query", confidence=0.7, club=_club(kind="cellar", member_name="Sam"),
                      slots=_slots(person="Sam")),
         expect_intent="club_query", expect_outcome="answered", club_ask=True,
         club_result={"status": "need_club", "kind": "cellar", "choices": [
             {"id": 1, "label": "Thursday reds", "kind": "club"},
         ]},
         buttons=["Thursday reds"]),
    dict(id="en_multi_member", text="how many Merlots does Sam have?", locale="en", flags={"feature_clubs": "on"},
         parse=_parse("club_query", club=_club(kind="cellar", member_name="Sam", grape="Merlot"),
                      slots=_slots(person="Sam", grape="Merlot")),
         expect_intent="club_query", expect_outcome="answered", club_ask=True,
         club_result={"status": "need_member", "kind": "cellar",
                      "club": {"id": 1, "name": "Thursday reds"},
                      "choices": [
                          {"id": 2, "label": "Sam", "kind": "member", "club_id": 1},
                          {"id": 3, "label": "Samantha", "kind": "member", "club_id": 1},
                      ]},
         buttons=["Sam", "Samantha"]),
    dict(id="en_no_match_club", text="is Pat in my club?", locale="en", flags={"feature_clubs": "on"},
         parse=_parse("club_query", club=_club(kind="members", member_name="Pat"),
                      slots=_slots(person="Pat")),
         expect_intent="club_query", expect_outcome="answered", club_ask=True,
         club_result={"status": "not_found", "kind": "cellar", "target": "member", "query": "Pat",
                      "club": {"id": 1, "name": "Thursday reds"},
                      "choices": [
                          {"id": 2, "label": "Riley", "kind": "member", "club_id": 1},
                          {"id": 3, "label": "Sam", "kind": "member", "club_id": 1},
                      ]},
         buttons=["Riley", "Sam", "List members"]),
    dict(id="en_offtopic_poem", text="write me a poem about cars", locale="en",
         parse=_parse("unknown", unknown_kind="offtopic"),
         expect_intent="unknown", expect_outcome="unknown", must_not=["Kanonkop"]),
    dict(id="en_help_switch", text="how do I switch cellars?", locale="en",
         parse=_parse("smalltalk_help", help_topic="switch"),
         expect_intent="smalltalk_help", expect_outcome="answered"),
    dict(id="en_help_take_out", text="I moved things around in the fridge", locale="en",
         parse=_parse("smalltalk_help", help_topic="take_out"),
         expect_intent="smalltalk_help", expect_outcome="answered"),
    dict(id="af_recommend_red", text="iets rooi en diep", locale="af",
         parse=_parse("recommend", taste={"colour": "red", "words": ["diep"], "occasion": "", "food": "", "budget_max": None},
                      slots=_slots(colour="red")),
         expect_intent="recommend", expect_outcome="answered"),
    dict(id="en_quantity", text="remove two bottles of chenin", locale="en",
         parse=_parse("drink_remove", wine_ref="chenin", slots=_slots(wine="chenin", quantity=2)),
         expect_intent="drink_remove", expect_outcome="answered"),
    dict(id="en_producer_search", text="Sadie Family", locale="en",
         parse=_parse("search", slots=_slots(producer="Sadie Family", wine="Sadie Family")),
         expect_intent="search", expect_outcome="answered", handled=False),
    dict(id="en_badges", text="what badges does Sam have?", locale="en", flags={"feature_clubs": "on"},
         parse=_parse("club_query", club=_club(kind="badges", member_name="Sam"), slots=_slots(person="Sam")),
         expect_intent="club_query", expect_outcome="answered", club_ask=True,
         club_result={"status": "ok", "kind": "badges", "member": "Sam", "achievements": []}),
    dict(id="en_avg_rating", text="average rating Sam gives", locale="en", flags={"feature_clubs": "on"},
         parse=_parse("club_query", club=_club(kind="avg_rating", member_name="Sam"), slots=_slots(person="Sam")),
         expect_intent="club_query", expect_outcome="answered", club_ask=True,
         club_result={"status": "ok", "kind": "avg_rating", "member": "Sam",
                      "vote_count": 2, "votes": {"up": 2, "meh": 0, "down": 0}}),
    # --- extra UX eval: typos, slang, mixed, vague, follow-ups, flags ---
    dict(id="en_typo_shiraz", text="any shirazz in the fridge??", locale="en",
         parse=_parse("cellar_query", fact={"kind": "grape", "storage_text": "", "drawer": None, "country_code": "", "grape": "shiraz"},
                      slots=_slots(grape="shiraz")),
         expect_intent="cellar_query", expect_outcome="answered", must=["Kanonkop"]),
    dict(id="en_slang_open", text="what can I crack tonight", locale="en",
         parse=_parse("recommend", taste={"colour": "any", "words": [], "occasion": "tonight", "food": "", "budget_max": None}),
         expect_intent="recommend", expect_outcome="answered"),
    dict(id="af_slang", text="wat kan ek vanaand oopmaak", locale="af",
         parse=_parse("recommend", language="af", taste={"colour": "any", "words": [], "occasion": "vanaand", "food": "", "budget_max": None}),
         expect_intent="recommend", expect_outcome="answered"),
    dict(id="en_mixed_af_en", text="Het ek any Pinotage left in die yskas", locale="af",
         parse=_parse("cellar_query", language="af", fact={"kind": "grape", "storage_text": "yskas", "drawer": None, "country_code": "", "grape": "Pinotage"},
                      slots=_slots(grape="Pinotage", location="yskas")),
         expect_intent="cellar_query", expect_outcome="answered"),
    dict(id="en_vague_followup", text="and the white?", locale="en",
         parse=_parse("recommend", taste={"colour": "white", "words": [], "occasion": "", "food": "", "budget_max": None},
                      slots=_slots(colour="white")),
         expect_intent="recommend", expect_outcome="answered"),
    dict(id="en_no_match_grape", text="any malbec?", locale="en",
         parse=_parse("cellar_query", fact={"kind": "grape", "storage_text": "", "drawer": None, "country_code": "", "grape": "malbec"},
                      slots=_slots(grape="malbec")),
         expect_intent="cellar_query", expect_outcome="answered",
         must=["don't have"], must_not=["Kanonkop Shiraz"]),
    dict(id="af_no_match_grape", text="het ek malbec?", locale="af",
         parse=_parse("cellar_query", language="af", fact={"kind": "grape", "storage_text": "", "drawer": None, "country_code": "", "grape": "malbec"},
                      slots=_slots(grape="malbec")),
         expect_intent="cellar_query", expect_outcome="answered", must=["nie 'n"]),
    dict(id="en_multi_storage", text="what's in the fridge?", locale="en",
         parse=_parse("cellar_query", fact={"kind": "storage", "storage_text": "fridge", "drawer": None, "country_code": "", "grape": ""},
                      slots=_slots(location="fridge")),
         expect_intent="cellar_query", expect_outcome="answered"),
    dict(id="en_list_add_named", text="add Sadie Columella to my list", locale="en",
         flags={"feature_lists": "on"},
         parse=_parse("list_action", list_op="add", slots=_slots(wine="Sadie Columella", producer="Sadie")),
         expect_intent="list_action", expect_outcome="answered",
         must=["Sadie Columella"], must_not=["Which bottle"]),
    dict(id="af_list_add_named", text="sit Kanonkop op my lys", locale="af",
         flags={"feature_lists": "on"},
         parse=_parse("list_action", language="af", list_op="add", slots=_slots(wine="Kanonkop")),
         expect_intent="list_action", expect_outcome="answered", must=["Kanonkop"]),
    dict(id="en_list_remove", text="take that off my list", locale="en",
         flags={"feature_lists": "on"},
         parse=_parse("list_action", list_op="remove"),
         expect_intent="list_action", expect_outcome="answered"),
    dict(id="en_club_who_has_hit", text="who has Barolo?", locale="en", flags={"feature_clubs": "on"},
         parse=_parse("club_query", club=_club(kind="who_has", wine_text="Barolo"), slots=_slots(wine="Barolo")),
         expect_intent="club_query", expect_outcome="answered", club_ask=True,
         club_result={"status": "ok", "kind": "who_has", "query": "Barolo",
                      "club": {"id": 1, "name": "Thursday reds"},
                      "matches": [{"display_name": "Sam", "count": 1,
                                   "wines": [{"name": "Barolo Cannubi", "vintage": 2015, "stock_count": 1}]}]},
         must=["Sam", "Barolo"], must_not=["Nobody"]),
    dict(id="en_club_oldest_hit", text="oldest in the club?", locale="en", flags={"feature_clubs": "on"},
         parse=_parse("club_query", club=_club(kind="oldest")),
         expect_intent="club_query", expect_outcome="answered", club_ask=True,
         club_result={"status": "ok", "kind": "oldest", "club": {"id": 1, "name": "Thursday reds"},
                      "vintage": 2001,
                      "wines": [{"name": "Old Vine", "vintage": 2001, "owner": "Riley"}]},
         must=["2001", "Old Vine"]),
    dict(id="en_club_drinks_rank", text="who drinks the most?", locale="en", flags={"feature_clubs": "on"},
         parse=_parse("club_query", club=_club(kind="who_drinks")),
         expect_intent="club_query", expect_outcome="answered", club_ask=True,
         club_result={"status": "ok", "kind": "who_drinks", "club": {"id": 1, "name": "Thursday reds"},
                      "ranking": [{"display_name": "Riley", "count": 4}, {"display_name": "Sam", "count": 1}]},
         must=["Riley", "4"]),
    dict(id="en_drink_typo", text="i drnk the kanonkop", locale="en",
         parse=_parse("drink_remove", wine_ref="kanonkop", slots=_slots(wine="kanonkop")),
         expect_intent="drink_remove", expect_outcome="answered"),
    dict(id="af_drink_this", text="ek het hierdie gedrink", locale="af",
         parse=_parse("drink_remove", language="af", wine_ref="this", slots=_slots(wine="this")),
         expect_intent="drink_remove", expect_outcome="answered"),
    dict(id="en_add_this", text="add this", locale="en",
         parse=_parse("add_wine", slots=_slots(wine="this")),
         expect_intent="add_wine", expect_outcome="answered", must=["photo"]),
    dict(id="en_price_af_amount", text="dit was 120", locale="af",
         parse=_parse("price", language="af", price={"amount": 120, "currency": ""}, wine_ref="this"),
         expect_intent="price", expect_outcome="answered"),
    dict(id="en_correct_producer", text="it's Sadie Family actually", locale="en",
         parse=_parse("correct", correction={"name": "", "producer": "Sadie Family", "vintage": None, "grape": "", "country_code": ""}),
         expect_intent="correct", expect_outcome="answered"),
    dict(id="en_place_typo", text="wine bar near stellnbosch", locale="en",
         parse=_parse("place", area="Stellenbosch", slots=_slots(location="Stellenbosch")),
         expect_intent="place", expect_outcome="answered"),
    dict(id="af_help_photo", text="hoe voeg ek 'n wyn by", locale="af",
         parse=_parse("smalltalk_help", language="af", help_topic="photo"),
         expect_intent="smalltalk_help", expect_outcome="answered"),
    dict(id="en_offtopic_weather", text="what's the weather in Cape Town", locale="en",
         parse=_parse("unknown", unknown_kind="offtopic"),
         expect_intent="unknown", expect_outcome="unknown",
         must=["wine"], must_not=["Kanonkop"]),
    dict(id="en_low_conf_drink_named", text="maybe take the barolo out?", locale="en",
         parse=_parse("drink_remove", confidence=0.4, wine_ref="barolo", slots=_slots(wine="barolo")),
         expect_intent="drink_remove", expect_outcome="answered"),
    dict(id="en_empty_recommend", text="what should I drink", locale="en",
         parse=_parse("recommend"), wines=[],
         expect_intent="recommend", expect_outcome="answered"),
    dict(id="en_html_search", text="Foo <script> bar", locale="en",
         parse=_parse("search", slots=_slots(wine="Foo")),
         expect_intent="search", expect_outcome="answered", handled=False),
    dict(id="en_followup_sam", text="and Sam?", locale="en", flags={"feature_clubs": "on"},
         parse=_parse("club_query", club=_club(kind="cellar", member_name="Sam"), slots=_slots(person="Sam")),
         expect_intent="club_query", expect_outcome="answered", club_ask=True,
         club_result={"status": "ok", "kind": "cellar", "visibility": "full",
                      "club": {"id": 1, "name": "Thursday reds"}, "member": "Sam",
                      "count": 0, "wines": []},
         must=["Sam"]),
    dict(id="af_flag_lists_off", text="voeg Sadie by my lys", locale="af", flags={},
         parse=_parse("list_action", language="af", list_op="add", slots=_slots(wine="Sadie")),
         expect_intent="list_action", expect_outcome="flag_off",
         must=["eie kelder"], must_not=["Sadie"]),
    dict(id="en_wine_chat_decant_af", text="moet ek 'n Barolo dekanteer", locale="af",
         parse=_parse("wine_knowledge", language="af"),
         expect_intent="wine_knowledge", expect_outcome="answered", wine_chat=True),
    dict(id="af_empty_recommend", text="wat moet ek vanaand drink?", locale="af",
         parse=_parse("recommend", language="af"), wines=[],
         expect_intent="recommend", expect_outcome="answered",
         must=["leeg", "foto"], must_not=["scan"]),
    dict(id="en_list_remove_named", text="take Sadie off my list", locale="en",
         flags={"feature_lists": "on"},
         parse=_parse("list_action", list_op="remove", slots=_slots(wine="Sadie")),
         expect_intent="list_action", expect_outcome="answered",
         must=["Sadie"], list_items=[{"id": 3, "name": "Sadie Columella"}]),
    dict(id="en_list_remove_missing", text="take Malbec off my list", locale="en",
         flags={"feature_lists": "on"},
         parse=_parse("list_action", list_op="remove", slots=_slots(wine="Malbec")),
         expect_intent="list_action", expect_outcome="answered",
         must=["isn't on"], list_items=[{"id": 3, "name": "Sadie Columella"}]),
    dict(id="en_list_remove_multi", text="take Sadie off my list", locale="en",
         flags={"feature_lists": "on"},
         parse=_parse("list_action", list_op="remove", slots=_slots(wine="Sadie")),
         expect_intent="list_action", expect_outcome="answered",
         must=["Which one"],
         buttons=["Sadie Columella", "Sadie Pofadder"],
         list_items=[{"id": 3, "name": "Sadie Columella"}, {"id": 4, "name": "Sadie Pofadder"}]),
    dict(id="af_list_remove_named", text="haal Sadie van my lys af", locale="af",
         flags={"feature_lists": "on"},
         parse=_parse("list_action", language="af", list_op="remove", slots=_slots(wine="Sadie")),
         expect_intent="list_action", expect_outcome="answered",
         must=["Sadie"], list_items=[{"id": 3, "name": "Sadie Columella"}]),
    dict(id="af_drink_confirm", text="ek het die Kanonkop gedrink", locale="af",
         parse=_parse("drink_remove", language="af", wine_ref="Kanonkop",
                      slots=_slots(wine="Kanonkop")),
         expect_intent="drink_remove", expect_outcome="answered",
         must=["gedrink", "Kanonkop"], must_not=["Remove 1"]),
    dict(id="af_haal_list", text="haal Sadie van my lys af", locale="af",
         flags={"feature_lists": "on"},
         parse=_parse("list_action", language="af", list_op="remove", slots=_slots(wine="Sadie")),
         expect_intent="list_action", expect_outcome="answered",
         must=["Sadie"], list_items=[{"id": 3, "name": "Sadie Columella"}]),
    dict(id="en_drink_no_match", text="I drank the malbec", locale="en",
         parse=_parse("drink_remove", wine_ref="malbec", slots=_slots(wine="malbec")),
         expect_intent="drink_remove", expect_outcome="answered",
         must=["Nothing matching", "malbec"]),
    dict(id="en_empty_fact", text="any shiraz?", locale="en",
         parse=_parse("cellar_query", fact={"kind": "grape", "storage_text": "", "drawer": None, "country_code": "", "grape": "shiraz"},
                      slots=_slots(grape="shiraz")),
         wines=[], expect_intent="cellar_query", expect_outcome="answered",
         must=["photo"], must_not=["scan"]),
    dict(id="af_empty_fact", text="het ek enige shiraz?", locale="af",
         parse=_parse("cellar_query", language="af",
                      fact={"kind": "grape", "storage_text": "", "drawer": None, "country_code": "", "grape": "shiraz"},
                      slots=_slots(grape="shiraz")),
         wines=[], expect_intent="cellar_query", expect_outcome="answered",
         must=["leeg", "foto"], must_not=["scan"]),
    dict(id="en_list_already", text="add Sadie Columella to my list", locale="en",
         flags={"feature_lists": "on"},
         parse=_parse("list_action", list_op="add", slots=_slots(wine="Sadie Columella", producer="Sadie")),
         expect_intent="list_action", expect_outcome="answered",
         must=["already"], list_already=True),
    dict(id="en_where_kanonkop", text="where is the Kanonkop?", locale="en",
         parse=_parse("cellar_query", slots=_slots(wine="Kanonkop")),
         expect_intent="cellar_query", expect_outcome="answered",
         must=["Kanonkop"]),
    dict(id="af_typo_recommend", text="wat moet ek vanaand drnk", locale="af",
         parse=_parse("recommend", language="af"),
         expect_intent="recommend", expect_outcome="answered"),
    dict(id="en_mixed_recommend", text="wat moet ek drink tonight", locale="af",
         parse=_parse("recommend", language="af"),
         expect_intent="recommend", expect_outcome="answered"),
    dict(id="en_thanks", text="thanks", locale="en",
         parse=_parse("smalltalk_help", help_topic="other"),
         expect_intent="smalltalk_help", expect_outcome="answered"),
    dict(id="af_add_named", text="voeg Sadie Columella by die kelder", locale="af",
         parse=_parse("add_wine", language="af", slots=_slots(wine="Sadie Columella", producer="Sadie")),
         expect_intent="add_wine", expect_outcome="answered",
         must=["foto", "Sadie Columella"]),
    dict(id="en_drink_multi", text="I drank the Kanonkop", locale="en",
         parse=_parse("drink_remove", wine_ref="Kanonkop", slots=_slots(wine="Kanonkop")),
         wines=[_wine(), _wine(wine_id=2, name="Kanonkop Kadette")],
         expect_intent="drink_remove", expect_outcome="answered",
         must=["Which one"]),
]


assert len(CASES) >= 80, len(CASES)


def _run_handler(api, chat, flags=None, locale="en"):
    handler = _handler(api, chat)
    if flags:
        handler.set_flags(flags)
    if locale and locale != "en":
        handler.set_language("4242", locale)
    api.list_lists.return_value = {"lists": [{"id": 9, "kind": "personal", "name": "To drink"}]}
    api.get_list.return_value = {
        "id": 9, "items": [], "count": 0, "offset": 0, "limit": 5, "name": "To drink",
    }
    api.add_list_item.return_value = {"id": 1, "already": False}
    api.delete_list_item.return_value = {"ok": True}
    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
async def test_pipeline_case(case):
    api = _api(case["wines"] if "wines" in case else [_wine()])
    if case.get("club_result") is not None:
        api.club_ask.return_value = case["club_result"]
    chat = FakeChat()
    chat.parse_result = case["parse"]
    chat.wine_chat_reply = "A Northern Rhône Syrah would suit a steak."
    chat.support_reply = "Send /invite and share the link."
    handler = _run_handler(api, chat, case.get("flags"), case.get("locale", "en"))
    if case.get("list_items") is not None:
        api.get_list.return_value = {
            "id": 9, "items": case["list_items"], "count": len(case["list_items"]),
            "offset": 0, "limit": 50, "name": "To drink",
        }
    if case.get("list_already"):
        api.add_list_item.return_value = {"id": 1, "already": True}
    handler._router._sleep = _fast_sleep
    update, sent = _text(case["text"])
    handled = await handler._router.handle_text(update, _context(), case["text"])
    meta = handler._text_meta
    assert meta.get("intent") == case["expect_intent"]
    assert meta.get("outcome") == case["expect_outcome"]
    if case.get("handled") is False:
        assert handled is False
        assert chat.wine_chat_calls == []
        return
    assert handled is True
    if case.get("wine_chat"):
        assert chat.wine_chat_calls
    else:
        assert chat.wine_chat_calls == []
    if case.get("club_ask"):
        api.club_ask.assert_called()
    elif case["expect_intent"] == "club_query" and case["expect_outcome"] == "flag_off":
        api.club_ask.assert_not_called()
    body = ""
    if update.message.reply_text.await_count:
        body = update.message.reply_text.await_args.args[0]
    elif sent.edit_text.await_count:
        body = sent.edit_text.await_args.args[0]
    low = body.lower()
    for needle in case.get("must") or []:
        assert needle.lower() in low, f"{case['id']} missing {needle!r} in {body!r}"
    for needle in case.get("must_not") or []:
        assert needle.lower() not in low, f"{case['id']} leaked {needle!r} in {body!r}"
    if case.get("buttons"):
        markup = None
        if update.message.reply_text.await_count:
            markup = update.message.reply_text.await_args.kwargs.get("reply_markup")
        labels = _labels(markup)
        for label in case["buttons"]:
            assert label in labels, f"{case['id']} missing button {label!r} in {labels}"


async def _fast_sleep(_delay):
    return None


def test_classifier_prompt_contract():
    prompt = parse_prompt({})
    assert prompt == PARSE_PROMPT
    for name in CLASSIFY_INTENTS:
        assert name in prompt
    for slot in SLOT_KEYS:
        assert slot in prompt
    assert "Never guess a wine, person, or club" in prompt
    assert "Do not invent" in prompt
    assert "wine_knowledge" in prompt
    schema = parse_schema()
    intents = schema["properties"]["intent"]["enum"]
    for name in CLASSIFY_INTENTS:
        assert name in intents
    for name in SCHEMA_INTENTS:
        assert name in intents
    slot_props = schema["properties"]["slots"]["properties"]
    for slot in SLOT_KEYS:
        assert slot in slot_props
    assert schema["properties"]["confidence"]["type"] == "number"
    assert schema["properties"]["language"]["enum"]
    assert "Do not invent bottles, people, clubs" in PARSE_PROMPT
    assert "Never guess a wine, person, or club" in PARSE_PROMPT
    assert "language" in PARSE_PROMPT


def test_canonical_parse_aliases_and_slots():
    parsed = canonical_parse({
        "intent": "club",
        "club": {"kind": "my_clubs", "member_name": "Sam", "club_name": "Thursday reds",
                 "wine_text": "", "grape": "", "vintage": None, "period": "", "when": ""},
        "wine_ref": "",
    })
    assert parsed["intent"] == "club_query"
    assert parsed["slots"]["person"] == "Sam"
    assert parsed["slots"]["club"] == "Thursday reds"
    assert "person=Sam" in slot_summary(parsed["slots"])
    offtopic = canonical_parse({"intent": "offtopic"})
    assert offtopic["intent"] == "unknown"
    assert offtopic["unknown_kind"] == "offtopic"
    rewritten = canonical_parse({"intent": "unknown", "raw_intent": "offtopic"})
    assert rewritten["unknown_kind"] == "offtopic"


def test_gated_flag_and_clarification_helpers():
    parsed = canonical_parse({"intent": "club_query", "club": {"kind": "tasteoff"}})
    assert gated_flag(parsed, {}) == "feature_clubs"
    assert gated_flag(parsed, {"clubs": True}) == "feature_tasteoffs"
    assert gated_flag(parsed, {"clubs": True, "tasteoffs": True}) is None
    low = canonical_parse({"intent": "cellar_query", "confidence": 0.2})
    assert needs_clarification(low)
    high = canonical_parse({"intent": "cellar_query", "confidence": 0.9})
    assert not needs_clarification(high)
    assert LOW_CONFIDENCE == 0.55


def test_flag_off_copy_does_not_name_features():
    for loc in ("en", "af"):
        text = t(loc, "chat.flag_off").lower()
        for banned in ("club", "klub", "taste-off", "proe", "comment", "list", "lys", "discover"):
            assert banned not in text
        assert "cellar" in text or "kelder" in text


@pytest.mark.asyncio
async def test_pacer_holds_fast_replies_and_skips_slow_ones():
    slept = []

    async def sleep(delay):
        slept.append(delay)

    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = _parse("unknown", unknown_kind="offtopic")
    handler = _handler(api, chat)
    handler._min_reply_ms = 1000
    handler._router._sleep = sleep
    clock = {"t": 0.0}
    handler._router._now = lambda: clock["t"]
    from integrations.chat_intent import ReplyPacer
    handler._text_pacer = ReplyPacer(lambda: clock["t"], sleep, 1000)
    update, _sent = _text("how high do planes fly?")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert slept == [1.0]
    slept.clear()
    clock["t"] = 0.0
    handler._text_pacer = ReplyPacer(lambda: clock["t"], sleep, 1000)
    chat.parse_result = _parse("wine_knowledge")
    # Simulate a slow model by advancing the clock during wine_chat
    orig = chat.wine_chat

    def slow_wine_chat(*a, **k):
        clock["t"] = 1.4
        return orig(*a, **k)

    chat.wine_chat = slow_wine_chat
    update, _sent = _text("is 2019 a good Barolo year?")
    await handler._router.handle_text(update, _context(), update.message.text)
    assert slept == []


@pytest.mark.asyncio
async def test_typed_log_includes_intent_slots_outcome(caplog):
    import logging
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = _parse("club_query", club=_club(kind="my_clubs"),
                               slots=_slots(), confidence=0.88)
    handler = _run_handler(api, chat, {})
    update, _sent = _text("What clubs am I in?")
    with caplog.at_level(logging.INFO, logger="integrations.telegram_bot"):
        await handler._unknown_text(update, _context())
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "intent=club_query" in logged
    assert "outcome=flag_off" in logged
    assert "handler=chat" in logged
    assert "What clubs am I in?" not in logged
    assert "You only drink" not in logged
    assert "4242" not in logged


@pytest.mark.asyncio
async def test_flag_off_buttons_stay_english_in_afrikaans():
    api = _api([_wine()])
    chat = FakeChat()
    chat.parse_result = _parse("club_query", club=_club(kind="my_clubs"))
    handler = _run_handler(api, chat, {}, locale="af")
    update, _sent = _text("In watter klubs is ek?")
    await handler._router.handle_text(update, _context(), "In watter klubs is ek?")
    body = update.message.reply_text.await_args.args[0]
    assert "eie kelder" in body
    labels = _labels(update.message.reply_text.await_args.kwargs.get("reply_markup"))
    assert labels == ["What's in stock", "What should I drink", "How Whiney works"]


@pytest.mark.asyncio
async def test_clarify_buttons_are_dispatched_by_the_bot():
    from integrations.telegram_bot import _cb
    api = _api([_wine()])
    handler = _handler(api, FakeChat())
    handler._router.on_callback = AsyncMock()
    for action in ("ct", "cp", "cl"):
        query = _tap()
        query.data = _cb(action)
        update = MagicMock()
        update.callback_query = query
        handler._router.on_callback.reset_mock()
        await handler._dispatch_callback(update, _context())
        handler._router.on_callback.assert_awaited_once()
