"""FORGE — test suite.

These are not smoke tests. Each one pins a claim the submission makes:

  * the organisers' schema validates every response we can generate (fuzz)
  * the seven mechanical gates hold under adversarial input
  * deeplink polarity never flips an enable/disable pair
  * the placeholder can never win the ranking (a bug we actually shipped once)
  * identical input produces byte-identical output
  * one physical interaction per step, including the "between X and Y" case
  * multi-intent decomposition fires only on disjoint problems

Run: python -m pytest tests/ -q
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from forge.catalog import DeeplinkCatalog, path_signature, step_polarity, target_signature
from forge.compile import compile_goal, parse_siis, split_clauses, describe, _titlecase
from forge.contract import (
    ContextDeeplinkResponse,
    Envelope,
    Goal,
    Meta,
    audit_envelope,
    is_title_case,
    leaks_url,
    word_count,
)
from forge.nlp import HashedNgramEncoder, canonical_atoms, intent_signature, make_variations
from forge.serve import Engine, split_intents

DATA = ROOT / "data"
SIIS = DATA / "siis_responses.json"
CATALOG = DATA / "deeplinks.json"
ATLAS = ROOT / "atlas" / "current"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session")
def encoder():
    return HashedNgramEncoder()


@pytest.fixture(scope="session")
def catalog(encoder):
    return DeeplinkCatalog.load(str(CATALOG), encoder)


@pytest.fixture(scope="session")
def rows():
    return json.loads(SIIS.read_text(encoding="utf-8"))["responses"]


@pytest.fixture(scope="session")
def engine(encoder):
    if not Path(ATLAS).exists():
        pytest.skip("atlas not built — run `python -m forge.build` first")
    return Engine(str(ATLAS), str(CATALOG), encoder=encoder)


# --------------------------------------------------------------------------- #
# contract
# --------------------------------------------------------------------------- #

def test_goal_syntax_exact():
    ok = Goal(goal="Follow these steps to perform this Battery Drain Troubleshooting",
              title="Battery drain", actions=[], score=0.5)
    env = Envelope(query="q", response=ContextDeeplinkResponse(contexts=[ok]), meta=Meta())
    assert audit_envelope(env).ok

    bad = Goal(goal="Follow these steps to fix Battery Drain",
               title="Battery drain", actions=[], score=0.5)
    env = Envelope(query="q", response=ContextDeeplinkResponse(contexts=[bad]), meta=Meta())
    assert "GOAL_SYNTAX" in audit_envelope(env).failures


def test_description_window_enforced():
    for n in range(2, 12):
        text = "It will " + " ".join(["a"] * (n - 2))
        goal = Goal(goal="Follow these steps to perform this Battery Drain Troubleshooting",
                    title="Battery drain", score=0.5,
                    actions=[{"actionName": "Do Thing", "description": text,
                              "category": "manual",
                              "stepGroups": [{"steps": ["Tap OK."]}]}])
        env = Envelope(query="q", response=ContextDeeplinkResponse(contexts=[goal]), meta=Meta())
        rep = audit_envelope(env)
        assert (rep.ok if 5 <= n <= 7 else not rep.ok), f"n={n} text={text!r}"


def test_url_leak_detection():
    assert leaks_url("visit samsung.com/support")
    assert leaks_url("see https://example.com")
    assert leaks_url("[click here](http://x.io)")
    assert not leaks_url("Tap on Wi-Fi.")
    assert not leaks_url("deeplink voiceassist://masked/act/abc123")


def test_title_case_allows_minor_words_and_acronyms():
    assert is_title_case("Check Email Access on a PC")
    assert is_title_case("Configure Navigation Bar Settings")
    assert is_title_case("Set Wi-Fi Scanning")
    assert is_title_case("Enable USB Debugging")
    assert not is_title_case("check email access")
    assert not is_title_case("Check EMAIL access")


def test_ordering_is_monotonic(engine):
    for r in engine.atlas.records:
        rank = {"auto": 0, "manual": 1, "critical": 2}
        cats = [a["category"] for a in r["goal"]["actions"]]
        seq = [rank[c] for c in cats]
        assert seq == sorted(seq), f"{r['plan_id']} out of order: {cats}"


# --------------------------------------------------------------------------- #
# catalog grammar + resolution
# --------------------------------------------------------------------------- #

def test_catalog_grammar_parses_all_families(catalog):
    fams = {}
    for e in catalog.entries:
        fams[e.family] = fams.get(e.family, 0) + 1
    assert fams["open"] > 200
    assert fams["enable"] > 100 and fams["disable"] > 100
    assert fams["monitor"] >= 5
    assert sum(1 for e in catalog.entries if e.is_placeholder) == 1


def test_target_extraction():
    assert target_signature("Opens the Wi-Fi scanning settings page in device Settings on the device.")[1] \
        == "Wi-Fi scanning"
    fam, tgt = target_signature("Disables adaptive battery via device Settings on the device.")
    assert fam == "disable" and "adaptive battery" in tgt
    fam, tgt = target_signature("Diagnoses excessive battery drain to identify power-hungry apps.")[0:2]
    assert fam == "monitor" and "battery drain" in tgt


def test_step_polarity():
    assert step_polarity(["Toggle on Wi-Fi scanning."]) == "enable"
    assert step_polarity(["Toggle off Wi-Fi scanning."]) == "disable"
    assert step_polarity(["Tap on Display."]) == "none"


def test_polarity_never_flips(catalog):
    """A step that toggles ON must never resolve to the catalog's OFF entry."""
    on_steps = ["Navigate to and open Settings.", "Tap on Connections.",
                "Toggle on Wi-Fi scanning."]
    res = catalog.resolve(on_steps, "Enable Wi-Fi Scanning")
    if res["entry"] is not None:
        assert res["entry"].family != "disable"

    off_steps = ["Navigate to and open Settings.", "Tap on Connections.",
                 "Toggle off Wi-Fi scanning."]
    res = catalog.resolve(off_steps, "Disable Wi-Fi Scanning")
    if res["entry"] is not None:
        assert res["entry"].family != "enable"


def test_placeholder_can_never_win(catalog):
    """Regression: a -1 applied to both `rel` and `factor` made the placeholder
    the argmax. The mask belongs on the final score."""
    for steps, name in [
        (["Navigate to Settings.", "Tap Apps."], "Resolve an Unknown Screen"),
        (["Do something entirely unrelated."], "Nonsense Action"),
        ([], ""),
    ]:
        res = catalog.resolve(steps, name)
        assert res["entry"] is None or not res["entry"].is_placeholder


def test_decline_beats_guess(catalog):
    """When the target cannot be justified we must decline, not emit a URI."""
    res = catalog.resolve(
        ["Navigate to Settings.", "Tap on Totally Unrelated Menu.",
         "Toggle on Something Never Indexed."],
        "Configure Something Never Indexed")
    if res["entry"] is not None:
        assert res["accepted"] is True
        assert res["overlap"] >= 0.34


# --------------------------------------------------------------------------- #
# compile: steps, descriptions, titles
# --------------------------------------------------------------------------- #

def test_one_interaction_per_step():
    got = split_clauses("Navigate to Settings. Tap Apps. Select your email app. Tap Storage.")
    assert len(got) == 4, got


def test_between_and_is_one_choice():
    got = split_clauses(
        "Select your preferred navigation type between Buttons and Swipe gestures.")
    assert len(got) == 1, got
    assert "between Buttons and Swipe gestures" in got[0]


def test_protected_collocations_survive():
    got = split_clauses("Press and hold the Power button.")
    assert got == ["Press and hold the Power button."], got


def test_description_always_in_window():
    for name in ["Back Up Phone Data", "Configure Navigation Bar Settings",
                 "Clear the Email App's Cache and Data - Clear the App's Data",
                 "X", "Do It", "Verify Your Phone's Internet Connection",
                 "Restart Your Phone in Safe Mode - Safe Mode 2"]:
        d = describe(name)
        assert d.startswith("It will "), d
        assert 5 <= word_count(d) <= 7, f"{name!r} -> {d!r} ({word_count(d)} words)"


def test_titlecase_preserves_acronyms():
    assert _titlecase("Check Email Access on a PC") == "Check Email Access on a PC"
    assert _titlecase("enable wi-fi scanning") == "Enable Wi-Fi Scanning"


def test_every_compiled_plan_passes_the_gate(catalog, rows):
    for row in rows:
        draft = parse_siis(str(row["id"]), row["siis_response"])
        goal, _ = compile_goal(draft, catalog)
        if goal is None:
            continue
        env = Envelope(query="t", response=ContextDeeplinkResponse(contexts=[Goal(**goal)]),
                       meta=Meta())
        rep = audit_envelope(env, catalog.uris)
        assert rep.ok, f"{row['id']} failed {rep.failures}"


def test_deterministic_build(catalog, rows):
    """Two compiles of the same source must be byte-identical."""
    row = rows[0]
    outs = []
    for _ in range(2):
        g, _ = compile_goal(parse_siis("x", row["siis_response"]), catalog)
        outs.append(json.dumps(g, sort_keys=True))
    assert outs[0] == outs[1]


# --------------------------------------------------------------------------- #
# nlp
# --------------------------------------------------------------------------- #

def test_intent_signature_is_order_insensitive():
    a = intent_signature("screen flickers and goes blank")
    b = intent_signature("goes blank and the screen flickers")
    assert a == b


def test_intent_signature_separates_problems():
    assert intent_signature("battery drains fast") != intent_signature("camera is blurry")


def test_device_names_are_noise():
    assert canonical_atoms("My Nexa Fold X1 screen is blank") == \
           canonical_atoms("tablet screen is blank")


def test_variations_count_and_uniqueness():
    vs, kw = make_variations("screen flickers after app install", 9)
    assert 8 <= len(vs) <= 10
    assert len(set(vs)) == len(vs)
    assert kw


def test_multi_intent_only_on_disjoint_problems():
    assert len(split_intents("screen flickers and battery dies fast")) >= 1
    got = split_intents("screen flickers. battery drains fast. wifi keeps dropping.")
    assert len(got) == 3, got
    # a single symptom must not be fragmented
    assert len(split_intents("my phone screen is blank black dark")) == 1


# --------------------------------------------------------------------------- #
# serving
# --------------------------------------------------------------------------- #

def _fuzz_queries(rows, n=40):
    out = []
    for row in rows:
        q = row.get("original_query") or ""
        if q:
            out.append(q)
        out.extend(make_variations(q, 5, seed=hash(q) % 9999)[0][:5])
    return out[:n]


def test_engine_conformance_fuzz(engine):
    bad = []
    for q in _fuzz_queries(list(engine.atlas.records) and json.loads(SIIS.read_text())["responses"]):
        env = engine.troubleshoot(q)
        rep = audit_envelope(env, engine.catalog.uris)
        if not rep.ok:
            bad.append((q, rep.failures))
    assert not bad, f"{len(bad)} non-conformant responses, first: {bad[0]}"


def test_engine_no_url_leaks(engine):
    for q in _fuzz_queries(json.loads(SIIS.read_text())["responses"]):
        env = engine.troubleshoot(q)
        assert not leaks_url(env.model_dump_json())


def test_no_match_fallback_shape(engine):
    env = engine.troubleshoot("the kettle will not stop beeping after i set a timer")
    if not env.response.contexts:
        assert env.response.fallback == "no_match"
        assert env.model_dump()["response"]["fallback"] == "no_match"


def test_empty_query_is_safe(engine):
    env = engine.troubleshoot("")
    assert env.response.contexts == []


def test_latency_and_cost_are_reported(engine):
    env = engine.troubleshoot(json.loads(SIIS.read_text())["responses"][0]["original_query"])
    assert env.meta.cost_usd == 0.0
    assert env.meta.model == "none(rules)"
    assert env.meta.tokens_in == 0 and env.meta.tokens_out == 0


def test_fanout_never_repeats_a_goal(engine):
    """A multi-clause query must not return the same goal twice.

    Two distinct corpus records can compile to plans with different plan ids but
    identical goal text. Before this was pinned, a two-clause fan-out returned
    the same troubleshooting steps twice, which looks like a bug to any user
    reading the response and silently inflates the contract's contexts list.
    """
    q = "screen flickers and goes blank. email server not responding."
    env = engine.troubleshoot(q)
    goals = [g.goal for g in env.response.contexts]
    assert len(goals) == len(set(goals)), f"duplicate goals in fan-out: {goals}"
    ids = env.meta.plan_ids
    assert len(ids) == len(set(ids)), f"duplicate plan ids in fan-out: {ids}"
    if len(goals) > 1:
        scores = [g.score for g in env.response.contexts]
        assert scores == sorted(scores, reverse=True), "contexts must be score-ranked"


def test_topic_verbatim_query_resolves(engine):
    """A user typing the corpus topic verbatim must hit that plan.

    `topic` / `source_title` / `keywords` were originally left out of the alias
    index, so the most natural possible query — the topic string itself — missed
    a plan that was sitting in the atlas.
    """
    env = engine.troubleshoot("email server not responding")
    assert env.meta.cache_hit, "topic-verbatim query missed the atlas"
    assert env.response.contexts, "topic-verbatim query returned no goal"


def test_resolution_is_punctuation_invariant(engine):
    """A full stop must not change the answer.

    `split_intents` yields clauses that keep their terminal period. The character
    n-gram encoder penalised that ("email server not responding" scored 0.729 but
    "...responding." scored 0.689, below the 0.72 floor), so the same question got
    a different answer — and every multi-clause fan-out silently dropped its last
    clause. All four forms below must resolve identically.
    """
    forms = [
        "email server not responding",
        "email server not responding.",
        "Email server not responding!",
        "  email server not responding  ",
    ]
    results = [engine.index.lookup(f) for f in forms]
    tiers = {r["tier"] for r in results}
    plans = {r["plan_id"] for r in results}
    sims = [round(float(r["sim"]), 3) for r in results]
    assert len(tiers) == 1, f"tier varied with punctuation: {tiers}"
    assert len(plans) == 1, f"plan varied with punctuation: {plans}"
    assert results[0]["plan_id"], "punctuation-invariant query should still resolve"
    assert max(sims) - min(sims) < 0.01, f"similarity varied with punctuation: {sims}"
