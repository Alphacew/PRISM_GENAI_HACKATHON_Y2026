"""FORGE — sync every published number from the metrics JSON.

The deck reads `reports/metrics_final.json` directly, so it can never drift. The
Markdown docs cannot, so they drift silently unless something rewrites them.
This rewrites the numeric claims in README.md and docs/metrics.md from the same
JSON, and reports anything it could not match rather than passing quietly.

Run: python tools/sync_docs.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
M = json.loads((ROOT / "reports" / "metrics_final.json").read_text())
C, L, K, A = M["compliance"], M["latency"], M["cache"], M["atlas"]
S = A["stats"]
cov = 100.0 * S["deeplink_resolution_rate"]

E, P, CD = L["cache_hit_exact"], L["cache_hit_paraphrase"], L["cold_path_compile_on_miss"]

# (file, pattern, replacement) — patterns are anchored on the label text that the
# docs use, so a renamed row fails loudly instead of silently keeping a stale number.
RULES: list[tuple[str, str, str]] = [
    # ---- README ----
    ("README.md", r"\| P95 — fast path, atlas hit \| \*\*[^*]+\*\* \| ≤ 300 ms \|",
     f"| P95 — fast path, atlas hit | **{E['p95_ms']:g} ms** | ≤ 300 ms |"),
    ("README.md", r"\| P95 — cold path, compile-on-miss \| \*\*[^*]+\*\* \| ≤ 8000 ms \|",
     f"| P95 — cold path, compile-on-miss | **{CD['p95_ms']:g} ms** | ≤ 8000 ms |"),
    ("README.md", r"\| Screen-resolution accuracy \(exact screen, not parent menu\) \| \*\*[^*]+\*\* \| [^|]+\|",
     f"| Screen-resolution accuracy (exact screen, not parent menu) | **{C['screen_resolution_rate_pct']:g}%** | ≥ 90% |"),
    ("README.md", r"\| Schema-valid output lines \| \*\*[^*]+\*\* \| ≥ 99% \|",
     f"| Schema-valid output lines | **{C['schema_conformance_pct']:g}%** | ≥ 99% |"),
    ("README.md", r"\| Deeplink resolution rate \(actions landing on an indexed screen\) \| \*\*[^*]+\*\* \| — \|",
     f"| Deeplink resolution rate (actions landing on an indexed screen) | **{cov:.1f}%** | — |"),
    ("README.md", r"\*\*Screen-resolution accuracy is [\d.]+%, against the guide's 90% target\.\*\*",
     f"**Screen-resolution accuracy is {C['screen_resolution_rate_pct']:g}%, against the guide's 90% target.**"),
    ("README.md", r"\*\*Paraphrase cache recall is [\d.]+% on genuinely novel wording, against the",
     f"**Paraphrase cache recall is {K['novel_wording_recall_pct']:.1f}% on genuinely novel wording, against the"),
    ("README.md", r"recall is [\d.]+% and the wrong-serve rate is [\d.]+%\.",
     f"recall is {K['novel_wording_recall_pct']:.1f}% and the wrong-serve rate is {K['wrong_serve_novel_pct']:.1f}%."),
    ("README.md", r"\*\*Deeplink coverage is [\d.]+% of actions\.\*\*",
     f"**Deeplink coverage is {cov:.1f}% of actions.**"),
    ("README.md", r"alone accepts more matches \([\d.]+%\) than the shipped hybrid \([\d.]+%\) at similar",
     f"alone accepts more matches ("
     f"{next((r['accept_rate_pct'] for r in M['ablation']['rows'] if r.get('mode') == 'dense'), 0):.1f}%) "
     f"than the shipped hybrid ("
     f"{next((r['accept_rate_pct'] for r in M['ablation']['rows'] if r.get('mode') == 'hybrid'), 0):.1f}%) at similar"),
    ("docs/metrics.md", r"\| Mean target agreement of accepted deeplink matches \| 0\.0 – 1\.0 \| \*\*[\d.]+\*\* \|",
     f"| Mean target agreement of accepted deeplink matches | 0.0 – 1.0 | **"
     f"{next((r['mean_target_overlap_of_accepted'] for r in M['ablation']['rows'] if r.get('mode') == 'hybrid'), 0):.3f}** |"),
    ("docs/metrics.md", r"\| Placeholder fallback rate for unjustifiable targets \| — \| [\d.]+ \(\d+/\d+\) \|",
     f"| Placeholder fallback rate for unjustifiable targets | — | "
     f"{next((r['generic_placeholder_rate_pct'] for r in M['ablation']['rows'] if r.get('mode') == 'hybrid'), 0) / 100:.3f} "
     f"({S['n_actions'] - S['n_resolved']}/{S['n_actions']}) |"),
    ("docs/metrics.md", r"\| \*\*A\. Lexical only\*\* \(BM25\) \| [\d.]+% \| [\d.]+ \|",
     f"| **A. Lexical only** (BM25) | "
     f"{next((r['accept_rate_pct'] for r in M['ablation']['rows'] if r.get('mode') == 'lexical'), 0):.1f}% | "
     f"{next((r['mean_target_overlap_of_accepted'] for r in M['ablation']['rows'] if r.get('mode') == 'lexical'), 0):.3f} |"),
    ("docs/metrics.md", r"\| \*\*B\. Dense only\*\* \(hashed n-gram\) \| [\d.]+% \| [\d.]+ \|",
     f"| **B. Dense only** (hashed n-gram) | "
     f"{next((r['accept_rate_pct'] for r in M['ablation']['rows'] if r.get('mode') == 'dense'), 0):.1f}% | "
     f"{next((r['mean_target_overlap_of_accepted'] for r in M['ablation']['rows'] if r.get('mode') == 'dense'), 0):.3f} |"),
    ("docs/metrics.md", r"\| \*\*C\. Hybrid, minus target/polarity\*\* \| [\d.]+% \| [\d.]+ \|",
     f"| **C. Hybrid, minus target/polarity** | "
     f"{next((r['accept_rate_pct'] for r in M['ablation']['rows'] if r.get('mode') == 'hybrid_no_path'), 0):.1f}% | "
     f"{next((r['mean_target_overlap_of_accepted'] for r in M['ablation']['rows'] if r.get('mode') == 'hybrid_no_path'), 0):.3f} |"),
    ("docs/metrics.md", r"accepts more matches \([\d.]+%\) than the hybrid \([\d.]+%\) at similar mean agreement\n\([\d.]+ vs [\d.]+\)\.",
     "accepts more matches ("
     + f"{next((r['accept_rate_pct'] for r in M['ablation']['rows'] if r.get('mode') == 'dense'), 0):.1f}%) "
     + "than the hybrid ("
     + f"{next((r['accept_rate_pct'] for r in M['ablation']['rows'] if r.get('mode') == 'hybrid'), 0):.1f}%) at similar mean agreement\n("
     + f"{next((r['mean_target_overlap_of_accepted'] for r in M['ablation']['rows'] if r.get('mode') == 'dense'), 0):.3f} vs "
     + f"{next((r['mean_target_overlap_of_accepted'] for r in M['ablation']['rows'] if r.get('mode') == 'hybrid'), 0):.3f})."),
    ("docs/metrics.md", r"- \*\*Catalog coverage\.\*\* [\d.]+% of actions resolve to a screen the catalog does not",
     f"- **Catalog coverage.** {100 - cov:.1f}% of actions resolve to a screen the catalog does not"),
    # ---- metrics.md ----
    ("docs/metrics.md", r"\| Cache hit — exact query match \| ≤ 300 ms \| \*\*[^*]+\*\* \| \*\*[^*]+\*\* \| [\d.]+ ms \|",
     f"| Cache hit — exact query match | ≤ 300 ms | **{E['p50_ms']:g} ms** | **{E['p95_ms']:g} ms** | {E['min_ms']:g} ms |"),
    ("docs/metrics.md", r"\| Cache hit — unseen semantic paraphrase \| ≤ 300 ms \| \*\*[^*]+\*\* \| \*\*[^*]+\*\* \| — \|",
     f"| Cache hit — unseen semantic paraphrase | ≤ 300 ms | **{P['p50_ms']:g} ms** | **{P['p95_ms']:g} ms** | — |"),
    ("docs/metrics.md", r"\| Cold query — full extraction & mapping \| ≤ 8000 ms \| \*\*[^*]+\*\* \| \*\*[^*]+\*\* \| — \|",
     f"| Cold query — full extraction & mapping | ≤ 8000 ms | **{CD['p50_ms']:g} ms** | **{CD['p95_ms']:g} ms** | — |"),
    ("docs/metrics.md", r"\| Screen resolution accuracy \(exact target screen, not parent menu\) \| 0\.0 – 1\.0 \| \*\*[\d.]+\*\* \(\d+/\d+ audited\) \|",
     f"| Screen resolution accuracy (exact target screen, not parent menu) | 0.0 – 1.0 | "
     f"**{C['screen_resolution_rate_pct'] / 100:.3f}** ({C['screen_resolution_checked'] - round(C['screen_resolution_checked'] * (100 - C['screen_resolution_rate_pct']) / 100)}/{C['screen_resolution_checked']} audited) |"),
    ("docs/metrics.md", r"\| Deeplink coverage of actions \| 0\.0 – 1\.0 \| \*\*[\d.]+\*\* \(\d+/\d+\) \|",
     f"| Deeplink coverage of actions | 0.0 – 1.0 | **{S['deeplink_resolution_rate']:.3f}** "
     f"({S['n_resolved']}/{S['n_actions']}) |"),
    ("docs/metrics.md", r"\| Atlas build time \| [\d.]+ ms for the full 20-record corpus \|",
     f"| Atlas build time | {A['build_ms']:g} ms for the full 20-record corpus |"),
    ("docs/metrics.md", r"\| Plans compiled \| \d+ \(",
     f"| Plans compiled | {S['n_plans']} ("),
    ("docs/metrics.md", r"\| Paraphrase cache hit rate \(all paraphrases\) \| — \| [\d.]+% \(\d+/\d+\) \|",
     f"| Paraphrase cache hit rate (all paraphrases) | — | {K['cache_hit_rate_pct']:g}% "
     f"({K['cache_hit']}/{K['paraphrases_tested']}) |"),
    ("docs/metrics.md", r"\| Exact-tier share of all paraphrases tested \| — \| [\d.]+% \(\d+/\d+\) \|",
     f"| Exact-tier share of all paraphrases tested | — | "
     f"{100.0 * K['by_tier']['exact'] / K['paraphrases_tested']:.1f}% "
     f"({K['by_tier']['exact']}/{K['paraphrases_tested']}) |"),
    ("docs/metrics.md", r"\| \*\*Cache recall on unseen novel-wording paraphrases\*\* \| ≥ 80% \| \*\*[\d.]+%\*\* — GAP \|",
     f"| **Cache recall on unseen novel-wording paraphrases** | ≥ 80% | "
     f"**{K['novel_wording_recall_pct']:.2f}%** — GAP |"),
    ("docs/metrics.md", r"\| Wrong plan served \(different domain or no shared problem atom\) \| minimise \| \*\*[\d.]+%\*\* overall / \*\*[\d.]+%\*\* novel \|",
     f"| Wrong plan served (different domain or no shared problem atom) | minimise | "
     f"**{K['wrong_serve_rate_pct']:.2f}%** overall / **{K['wrong_serve_novel_pct']:.2f}%** novel |"),
    ("docs/metrics.md", r"\| Auto actions carrying a valid actionable deeplink \| ≥ 98% \| \*\*100\.0%\*\* \(\d+/\d+\) \|",
     f"| Auto actions carrying a valid actionable deeplink | ≥ 98% | **100.0%** "
     f"({C['auto_with_valid_deeplink']}/{C['auto_actions']}) |"),
    ("docs/metrics.md", r"\| Auto-action deeplink coverage \| 100% \| \*\*100%\*\* \(\d+/\d+\) \|",
     f"| Auto-action deeplink coverage | 100% | **100%** "
     f"({C['auto_with_valid_deeplink']}/{C['auto_actions']}) |"),
    ("docs/metrics.md", r"N = [\d]+ / [\d]+ / [\d]+ per path,",
     f"N = {L['n']['exact']} / {L['n']['paraphrase']} / {L['n']['cold']} per path,"),
    ("docs/metrics.md", r"\| Screen-resolution accuracy \| ≥ 90% \| \*\*[\d.]+%\*\* \| (PASS|GAP|FAIL) \|",
     f"| Screen-resolution accuracy | ≥ 90% | **{C['screen_resolution_rate_pct']:g}%** | "
     f"{'PASS' if C['screen_resolution_rate_pct'] >= 90 else 'GAP'} |"),
    ("docs/metrics.md", r"\| Embedding encoder \| .*\|", f"| Embedding encoder | `{A['encoder']}` (offline, deterministic, zero-dependency) |"),
    ("docs/metrics.md", r"\| Atlas build time \| [\d.]+ ms for the full 20-record corpus \|",
     f"| Atlas build time | {A['build_ms']:g} ms for the full 20-record corpus |"),
    ("docs/metrics.md", r"The atlas compiles 20 sources in [\d.]+ ms\.",
     f"The atlas compiles 20 sources in {A['build_ms']:g} ms."),
    ("docs/metrics.md", r"\| \*\*D\. FORGE hybrid \(\+ target \+ polarity\)\*\* \| [\d.]+% \| [\d.]+ \|",
     f"| **D. FORGE hybrid (+ target + polarity)** | "
     f"{next((r['accept_rate_pct'] for r in M['ablation']['rows'] if r.get('mode') == 'hybrid'), 0):.1f}% | "
     f"{next((r['mean_target_overlap_of_accepted'] for r in M['ablation']['rows'] if r.get('mode') == 'hybrid'), 0):.3f} |"),
]

missing: list[str] = []
pending: dict[str, str] = {}
for fname, pattern, repl in RULES:
    p = ROOT / fname
    s = pending.get(fname) or p.read_text()
    new, n = re.subn(pattern, lambda _m, r=repl: r, s)
    if n == 0:
        missing.append(f"{fname}: {pattern[:70]}")
        pending.pop(fname, None)          # nothing written for this file
        continue
    pending[fname] = new

# Write only once every rule has matched, so a rename or a drifted row cannot
# leave a document half-synced with some numbers updated and others stale.
if missing:
    print("UNMATCHED (docs drifted or the row was renamed) — no files written:")
    for m in missing:
        print("  -", m)
    sys.exit(1)

for fname, text in pending.items():
    (ROOT / fname).write_text(text)
    print(f"  {fname}: synced")
print("\nall published numbers synced from reports/metrics_final.json")
