"""Live natural-language eval. Not part of pytest.

    python tests/eval/run.py

Exits 0 when the thresholds hold, 2 when XAI_API_KEY is missing, 1 when a
threshold misses. It spends real API credit: a full run is a handful of calls.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from integrations.chat_models import ChatFailure, ChatModels, compact_cellar  # noqa: E402


def _load_cases(path: Path) -> list[dict]:
    try:
        import yaml
    except ImportError:
        print("pip install pyyaml  (only needed for this eval)")
        raise SystemExit(2)
    return yaml.safe_load(path.read_text())


def _same(got: dict, want: dict) -> bool:
    for key, expected in want.items():
        actual = got.get(key)
        if isinstance(expected, str):
            if str(actual or "").casefold() != expected.casefold():
                return False
        elif actual != expected:
            return False
    return True


def main() -> int:
    key = os.environ.get("XAI_API_KEY", "").strip()
    if not key:
        print("XAI_API_KEY is not set — skipping the live eval")
        return 2
    wines = json.loads((HERE / "cellar.json").read_text())
    cases = _load_cases(HERE / "cases.yaml")
    chat = ChatModels(key)
    intents = facts = picks = 0
    intent_n = fact_n = pick_n = 0
    forbidden = 0
    parse_times: list[float] = []
    rec_times: list[float] = []
    for case in cases:
        started = time.monotonic()
        parsed = chat.parse(case["text"], {})
        parse_times.append(time.monotonic() - started)
        ok_intent = parsed["intent"] == case["intent"]
        intents += ok_intent
        intent_n += 1
        print(f"{case['id']}: intent {parsed['intent']} ({'ok' if ok_intent else 'MISS'})")
        if case["intent"] == "fact" and ok_intent:
            fact_n += 1
            got = parsed["fact"]
            match = _same(got, case["fact"])
            facts += match
            print(f"  fact {got} ({'ok' if match else 'MISS'})")
        if case["intent"] == "price" and ok_intent:
            fact_n += 1
            got = parsed["price"]["amount"]
            match = got == case["price"]["amount"]
            facts += match
        if case["intent"] == "correct" and ok_intent:
            fact_n += 1
            got = parsed["correction"]
            match = _same(got, case["correction"])
            facts += match
            print(f"  correction {got} ({'ok' if match else 'MISS'})")
        if case["intent"] != "recommend" or not ok_intent:
            continue
        pick_n += 1
        block, _lookup = compact_cellar(wines)
        started = time.monotonic()
        try:
            result = chat.recommend(
                case["text"], parsed["taste"], block, "", [],
                allow_question=True, valid_ids={w["id"] for w in wines},
            )
        except ChatFailure as exc:
            rec_times.append(time.monotonic() - started)
            print(f"  pick failed kind={exc.kind}")
            continue
        rec_times.append(time.monotonic() - started)
        chosen = result["pick"]["id"]
        others = [result.get("runner_up", {}).get("id")]
        others += [item.get("id") for item in result.get("alternatives") or []]
        ids = [chosen, *others]
        good = chosen in case["acceptable"]
        banned = [i for i in ids if i in case.get("must_not", [])]
        picks += good
        forbidden += len(banned)
        print(f"  pick {chosen} ({'ok' if good else 'MISS'}) banned={banned}")

    def pct(n, d):
        return 100 * n / d if d else 100

    def percentile(values, p):
        if not values:
            return 0
        ordered = sorted(values)
        index = min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))
        return ordered[index]

    summary = {
        "intent": pct(intents, intent_n),
        "fields": pct(facts, fact_n),
        "picks": pct(picks, pick_n),
        "forbidden": forbidden,
        "parse_p90": percentile(parse_times, 90),
        "recommend_p50": percentile(rec_times, 50),
        "recommend_p90": percentile(rec_times, 90),
    }
    print(json.dumps(summary, indent=2))
    ok = (
        summary["intent"] >= 95
        and summary["fields"] >= 95
        and summary["picks"] >= 85
        and summary["forbidden"] == 0
        and summary["parse_p90"] <= 1.5
        and summary["recommend_p50"] <= 5
        and summary["recommend_p90"] <= 8
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
