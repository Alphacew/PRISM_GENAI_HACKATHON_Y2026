"""FORGE — threshold tuner for the semantic cache gate.

The two objectives on a semantic cache trade against each other and the honest
report has to show both: raising the similarity floor and the best-vs-second
margin raises precision and lowers recall. This sweeps the pair and prints the
frontier, so the shipped thresholds are chosen from evidence rather than taste.

Run: python tools/tune_gate.py
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np

from forge.nlp import canonical_atoms, domain_of, intent_signature, make_variations
from forge.serve import Engine

SEED = 11  # deliberately different from the seeds used in forge/eval.py


def population(eng, seeds, k=3):
    """(variation, seed_signature, is_novel) for paraphrases the build never saw."""
    out = []
    for idx, s in enumerate(seeds):
        sig = intent_signature(s)
        for j in range(k):
            for v in make_variations(s, 9, seed=SEED * 1000 + idx * 17 + j)[0][1:]:
                out.append((v, sig, intent_signature(v) != sig))
    return out


def main() -> None:
    eng = Engine(str(ROOT / "atlas" / "current"), str(ROOT / "data" / "deeplinks.json"))
    # swap in the default (static) encoder explicitly
    from forge.nlp import make_encoder
    eng = Engine(str(ROOT / "atlas" / "current"), str(ROOT / "data" / "deeplinks.json"),
                 encoder=make_encoder())
    seeds = [str(r.get("original_query") or "")
             for r in json.loads((ROOT / "data" / "siis_responses.json").read_text())["responses"]]
    seeds = [s for s in seeds if s]
    pop = population(eng, seeds)
    print(f"population: {len(pop)} paraphrases, "
          f"{sum(1 for _, _, n in pop if n)} with novel wording\n")
    print(f"{'floor':>6} {'margin':>7} {'novel_recall%':>14} {'wrong_novel%':>13} {'all_hits%':>10}")
    print("-" * 56)

    rows = []
    for floor, margin in itertools.product(
        [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.70],
        [0.02, 0.04, 0.06, 0.08, 0.10, 0.15],
    ):
        eng.index.tau_floor = floor
        eng.index.tau_margin = margin
        n_novel = hit_novel = wrong_novel = 0
        n_all = hit_all = 0
        for v, seed_sig, novel in pop:
            env = eng._resolve_one(v, None)
            pid = env.get("plan_id") or ""
            served = bool(pid)
            n_all += 1
            hit_all += int(served)
            if not novel:
                continue
            n_novel += 1
            if not served:
                continue
            hit_novel += 1
            rec = eng.by_id.get(pid) or eng._overlay.get(pid)
            if not rec:
                wrong_novel += 1
                continue
            parts = str(rec.get("intent_signature", "")).split("::")
            p_dom = parts[0] if parts else ""
            p_atoms = set(parts[1].split("+")) if len(parts) > 1 else set()
            q_atoms = set(canonical_atoms(v)) - {"generic"}
            if not (p_dom == domain_of(sorted(q_atoms)) and (q_atoms & p_atoms)):
                wrong_novel += 1
        recall = 100.0 * hit_novel / max(n_novel, 1)
        wrong = 100.0 * wrong_novel / max(n_novel, 1)
        allhit = 100.0 * hit_all / max(n_all, 1)
        rows.append((floor, margin, recall, wrong, allhit))
        print(f"{floor:6.2f} {margin:7.2f} {recall:14.2f} {wrong:13.2f} {allhit:10.2f}")

    # knee: highest recall subject to a wrong-serve ceiling we would defend
    CEILING = 8.0
    ok = [r for r in rows if r[3] <= CEILING and r[2] > 0]
    if ok:
        best = max(ok, key=lambda r: r[2])
        print(f"\nbest recall under {CEILING}% wrong-serve ceiling: "
              f"floor={best[0]:.2f} margin={best[1]:.2f} "
              f"-> novel recall {best[2]:.2f}%, wrong {best[3]:.2f}%")
    json.dump(
        [{"floor": f, "margin": m, "novel_recall_pct": r, "wrong_novel_pct": w,
          "all_hit_pct": a} for f, m, r, w, a in rows],
        open(ROOT / "reports" / "gate_sweep.json", "w"), indent=1,
    )


if __name__ == "__main__":
    main()
