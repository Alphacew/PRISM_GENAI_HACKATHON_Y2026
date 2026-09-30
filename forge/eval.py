"""FORGE — measurement harness.

Everything the guide asks to report (S6, Appendix C) is produced here from real
runs, not from a spreadsheet:

  * schema / rule compliance over every emitted response
  * zero-URL-leak and catalog-validity gates
  * screen-resolution rate and auto-action deeplink coverage
  * latency percentiles per execution path (N >= 60 each)
  * semantic cache hit rate on *freshly generated unseen paraphrases*
  * a four-row architecture ablation

Run:  python -m forge.eval --siis data/siis_responses.json --catalog data/deeplinks.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import numpy as np

from .catalog import DeeplinkCatalog
from .compile import compile_goal, parse_siis
from .contract import ContextDeeplinkResponse, Envelope, Goal, Meta, audit_envelope
from .nlp import (
    HashedNgramEncoder,
    canonical_atoms,
    domain_of,
    intent_signature,
    make_encoder,
    make_variations,
)
from .serve import Engine, build_atlas, split_intents


def _pct(xs: Sequence[float], p: float) -> float:
    return float(np.percentile(np.asarray(xs, dtype=float), p)) if len(xs) else 0.0


# --------------------------------------------------------------------------- #
# 1. Compliance over emitted responses
# --------------------------------------------------------------------------- #

def compliance(eng: Engine, queries: Sequence[str]) -> Dict[str, object]:
    n = ok = 0
    failures: Dict[str, int] = {}
    n_actions = n_auto = n_auto_dl = 0
    leaf_agree = n_leaf = 0
    for q in queries:
        env = eng.troubleshoot(q)
        rep = audit_envelope(env, eng.catalog.uris)
        n += 1
        ok += int(rep.ok)
        for f in set(rep.failures):
            failures[f] = failures.get(f, 0) + 1
        n_actions += rep.checks.get("action", 0)
        n_auto += rep.checks.get("auto_actions", 0)
        n_auto_dl += rep.checks.get("auto_with_deeplink", 0)
        # screen resolution: does the picked catalog entry's leaf appear in the
        # action's own step path?
        for goal in env.response.contexts:
            for a in goal.actions:
                if a.category.value != "auto":
                    continue
                dl = a.stepGroups[0].actionableDeeplink if a.stepGroups else None
                if dl is None or dl.deeplink.endswith("dummy_positive"):
                    continue
                entry = next((e for e in eng.catalog.entries if e.deeplink == dl.deeplink), None)
                if entry is None or not entry.leaf:
                    continue
                from .nlp import content_tokens as _ct
                path_txt = " ".join(
                    s for sg in a.stepGroups for s in sg.steps
                )
                n_leaf += 1
                lt = set(_ct(entry.leaf))
                leaf_agree += int(bool(lt) and len(lt & set(_ct(path_txt))) / max(len(lt), 1) >= 0.5)
    return {
        "queries": n,
        "schema_conformant": ok,
        "schema_conformance_pct": round(100.0 * ok / max(n, 1), 2),
        "gate_failures": failures,
        "url_leaks": failures.get("URL_LEAK", 0) + failures.get("STEP_URL_LEAK", 0),
        "actions_total": n_actions,
        "auto_actions": n_auto,
        "auto_with_valid_deeplink": n_auto_dl,
        "auto_deeplink_coverage_pct": round(100.0 * n_auto_dl / max(n_auto, 1), 2),
        "screen_resolution_checked": n_leaf,
        "screen_resolution_rate_pct": round(100.0 * leaf_agree / max(n_leaf, 1), 2),
    }


# --------------------------------------------------------------------------- #
# 2. Latency
# --------------------------------------------------------------------------- #

def latency(eng: Engine, queries: Sequence[str], n_per_path: int = 60) -> Dict[str, object]:
    """In-process engine latency per execution path, measured with perf_counter.

    Three genuinely different paths: exact tier, unseen-paraphrase tier, and the
    cold path (novel intent with reference text supplied -> compile-on-miss).
    """
    for q in queries:                      # pre-warm the atlas tier
        eng.troubleshoot(q)

    def timed(q: str, siis: Optional[str] = None) -> Tuple[float, str, bool]:
        t0 = time.perf_counter()
        env = eng.troubleshoot(q, siis)
        ms = (time.perf_counter() - t0) * 1000.0
        return ms, env.meta.cache_tier, bool(env.response.contexts)

    exact: List[float] = []
    semantic: List[float] = []
    cold: List[float] = []
    rng = np.random.default_rng(7)
    i = 0
    while len(semantic) < n_per_path and i < n_per_path * 40:
        q = queries[i % len(queries)]
        ms, tier, _ = timed(q)
        if tier == "exact":
            exact.append(ms)
        var = make_variations(q, 9, seed=1000 + i)[0][1 + int(rng.integers(0, 8))]
        ms2, tier2, _ = timed(var)
        if tier2 in {"exact", "semantic"}:
            semantic.append(ms2)
        i += 1

    novel = [
        "the kettle will not stop beeping after i set a timer",
        "my doorbell chime replays the wrong melody at midnight",
        "the air purifier filter light blinks orange twice a day",
        "coffee machine grinds too coarse since the firmware update",
        "robot vacuum refuses to dock and spins in a small circle",
    ]
    ref = ("## Step 1: Check App Permissions\nNavigate to Settings.\nTap Apps.\n"
           "Toggle on the permission for the affected app.\n")
    for j in range(n_per_path):
        ms, tier, ok = timed(novel[j % len(novel)], ref)
        if tier == "miss":
            cold.append(ms)

    return {
        "n": {"exact": len(exact), "paraphrase": len(semantic), "cold": len(cold)},
        "cache_hit_exact": {"p50_ms": round(_pct(exact, 50), 2), "p95_ms": round(_pct(exact, 95), 2),
                            "min_ms": round(min(exact), 2) if exact else None},
        "cache_hit_paraphrase": {"p50_ms": round(_pct(semantic, 50), 2),
                                 "p95_ms": round(_pct(semantic, 95), 2)},
        "cold_path_compile_on_miss": {"p50_ms": round(_pct(cold, 50), 2),
                                      "p95_ms": round(_pct(cold, 95), 2)},
    }


# --------------------------------------------------------------------------- #
# 3. Cache efficacy on unseen paraphrases
# --------------------------------------------------------------------------- #

def cache_efficacy(eng: Engine, seeds: Sequence[str], k: int = 3) -> Dict[str, object]:
    """Fresh paraphrase sets, generated with registers the build never saw.

    Split into two populations, because conflating them is how a cache metric
    flatters itself:

    * `keyword_form` variations normalise onto the seed's intent signature, so
      they hit tier 1 by construction. They measure the normaliser, not the
      semantic tier.
    * `novel_wording` variations produce a *different* intent signature but mean
      the same thing. Those are the real test of a semantic cache.

    `wrong_serve` counts serves whose plan answers a different domain, or shares
    no problem atom with the question at all — the failure a semantic cache has
    to avoid, and the one the margin gate exists to prevent.
    """
    hits = wrong = 0
    n = n_novel = hits_novel = wrong_novel = 0
    by_tier: Dict[str, int] = {}
    margins: List[float] = []
    for idx, s in enumerate(seeds):
        seed_sig = intent_signature(s)
        for j in range(k):
            for v in make_variations(s, 9, seed=5000 + idx * 17 + j)[0][1:]:
                env = eng.troubleshoot(v)
                n += 1
                tier = env.meta.cache_tier
                by_tier[tier] = by_tier.get(tier, 0) + 1
                novel = intent_signature(v) != seed_sig
                if novel:
                    n_novel += 1
                if not env.meta.cache_hit:
                    continue
                hits += 1
                if novel:
                    hits_novel += 1
                if env.meta.margin is not None:
                    margins.append(float(env.meta.margin))

                q_atoms = set(canonical_atoms(v)) - {"generic"}
                q_dom = domain_of(sorted(q_atoms))
                ok = False
                for pid in env.meta.plan_ids:
                    rec = eng.by_id.get(pid)
                    if rec is None:
                        continue
                    parts = str(rec.get("intent_signature", "")).split("::")
                    p_dom = parts[0] if parts else ""
                    p_atoms = set(parts[1].split("+")) if len(parts) > 1 else set()
                    if p_dom == q_dom and (q_atoms & p_atoms):
                        ok = True
                if not ok:
                    wrong += 1
                    if novel:
                        wrong_novel += 1
    return {
        "paraphrases_tested": n,
        "cache_hit": hits,
        "cache_hit_rate_pct": round(100.0 * hits / max(n, 1), 2),
        "novel_wording_tested": n_novel,
        "novel_wording_hit": hits_novel,
        "novel_wording_recall_pct": round(100.0 * hits_novel / max(n_novel, 1), 2),
        "wrong_plan_served": wrong,
        "wrong_serve_rate_pct": round(100.0 * wrong / max(n, 1), 2),
        "wrong_serve_novel_pct": round(100.0 * wrong_novel / max(n_novel, 1), 2),
        "by_tier": by_tier,
        "semantic_margin_p50": round(float(np.percentile(margins, 50)), 4) if margins else None,
        "semantic_margin_p05": round(float(np.percentile(margins, 5)), 4) if margins else None,
    }


# --------------------------------------------------------------------------- #
# 4. Ablation
# --------------------------------------------------------------------------- #

def ablation(siis_path: str, catalog_path: str, encoder_name: str = "lexical") -> Dict[str, object]:
    """Four architectures, measured on the same corpus and the same rules.

    The rows isolate two independent design decisions: *where* the compiler runs
    (build time vs query time) and *how* a step trajectory is mapped to a
    catalog entry (lexical / dense / hybrid / hybrid+path).
    """
    enc = HashedNgramEncoder() if encoder_name == "lexical" else make_encoder()
    cat = DeeplinkCatalog.load(catalog_path, enc)
    raw = json.loads(Path(siis_path).read_text(encoding="utf-8"))
    rows = raw["responses"] if isinstance(raw, dict) else raw

    def run(mode: str) -> Dict[str, object]:
        t0 = time.perf_counter()
        n_actions = n_resolved = n_gate_fail = 0
        overlaps: List[float] = []
        pol_ok = pol_n = 0
        for i, row in enumerate(rows):
            draft = parse_siis(str(row.get("id") or i), row.get("siis_response") or {})
            goal, build = compile_goal(draft, cat, mode=mode)
            if goal is None:
                continue
            b = build["build"]
            n_actions += b["n_actions"]
            n_resolved += b["n_resolved"]
            if b.get("mean_target_overlap"):
                overlaps.append(float(b["mean_target_overlap"]))
            if b.get("polarity_agreement") is not None:
                pol_ok += round(float(b["polarity_agreement"]) * float(b["n_polarity_considered"]))
                pol_n += int(b["n_polarity_considered"])
            env = Envelope(query="ablation", response=ContextDeeplinkResponse(contexts=[Goal(**goal)]),
                           meta=Meta(stage="compile"))
            if not audit_envelope(env, cat.uris).ok:
                n_gate_fail += 1
        build_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "mode": mode,
            "actions": n_actions,
            "resolved": n_resolved,
            "accept_rate_pct": round(100.0 * n_resolved / max(n_actions, 1), 2),
            "mean_target_overlap_of_accepted": round(
                sum(overlaps) / len(overlaps), 4) if overlaps else 0.0,
            "polarity_agreement_pct": round(100.0 * pol_ok / pol_n, 2) if pol_n else None,
            "generic_placeholder_rate_pct": round(
                100.0 * (n_actions - n_resolved) / max(n_actions, 1), 2),
            "plans_failing_gates": n_gate_fail,
            "full_corpus_compile_ms": round(build_ms, 1),
            "per_source_compile_ms": round(build_ms / max(len(rows), 1), 2),
        }

    # query-time placement: compile one source per request, repeatedly
    def run_compile_at_query() -> Dict[str, object]:
        row = rows[0]
        draft = parse_siis("q", row.get("siis_response") or {})
        ts: List[float] = []
        for _ in range(30):
            t0 = time.perf_counter()
            parse_siis("q", row.get("siis_response") or {})
            compile_goal(draft, cat, mode="hybrid")
            ts.append((time.perf_counter() - t0) * 1000.0)
        return {
            "mode": "compile_at_query (guide's baseline placement)",
            "per_query_p50_ms": round(_pct(ts, 50), 2),
            "per_query_p95_ms": round(_pct(ts, 95), 2),
        }

    return {
        "encoder": enc.name,
        "rows": [run_compile_at_query(), run("lexical"), run("dense"),
                 run("hybrid_no_path"), run("hybrid")],
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--siis", default="data/siis_responses.json")
    ap.add_argument("--catalog", default="data/deeplinks.json")
    ap.add_argument("--atlas", default="atlas/current")
    ap.add_argument("--out", default="reports/metrics.json")
    ap.add_argument("--encoder", default="lexical", choices=["lexical", "semantic"])
    args = ap.parse_args()

    enc = HashedNgramEncoder() if args.encoder == "lexical" else make_encoder()
    cat = DeeplinkCatalog.load(args.catalog, enc)
    t0 = time.perf_counter()
    atlas, rejected = build_atlas(args.siis, cat, enc, version="current")
    build_ms = (time.perf_counter() - t0) * 1000.0
    Path(args.atlas).mkdir(parents=True, exist_ok=True)
    atlas.save(Path(args.atlas).parent)

    eng = Engine(str(Path(args.atlas).parent / "current"), args.catalog, encoder=enc)
    seeds = [str(r.get("original_query") or "") for r in
             json.loads(Path(args.siis).read_text(encoding="utf-8"))["responses"]]
    seeds = [s for s in seeds if s]

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "atlas": {
            "version": atlas.version,
            "plans": len(atlas.records),
            "stats": atlas.stats,
            "build_ms": round(build_ms, 1),
            "encoder": enc.name,
            "rejected": rejected,
        },
        "gate_stats": {
            "plans_passing_gates": sum(1 for r in atlas.records if r["gate"]["ok"]),
            "plans_failing_gates": sum(1 for r in atlas.records if not r["gate"]["ok"]),
        },
        "compliance": compliance(eng, seeds),
        "latency": latency(eng, seeds),
        "cache": cache_efficacy(eng, seeds),
        "ablation": ablation(args.siis, args.catalog, args.encoder),
        "service_counters": eng.metrics(),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
