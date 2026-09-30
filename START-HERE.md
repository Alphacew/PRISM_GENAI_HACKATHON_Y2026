# FORGE — Samsung PRISM Generative AI Hackathon (3rd Edition)

**Theme 02 — Smart Guided Troubleshooting Engine** · Team `SRM_Carrot` · SRMIST

Deadline: **30 September 2026, 23:59 IST**

---

## What this folder is

Everything produced for this submission, in one place — the deck, the working
engine, the measurements, the original organisers' briefs, and the scripts that
regenerate every published number.

The project was previously living under `~/.hermes/cache/scratch/`, which is a
scratch directory that gets pruned after 24h idle. **This folder is the durable
copy.** Nothing here depends on that scratch path any more.

---

## Start here — four tasks before the deadline

Full detail and the exact commands are in `SUBMISSION_CHECKLIST.md`. Summary:

| # | Task | Time | Why it matters |
|---|---|---|---|
| 1 | **Rename the deck** to `SRMIST_<YourTeamName>_Submission.pptx` | 2 min | The template ships the literal filename `CollegeName_TeamName_Submission.pptx` and means it |
| 2 | **Record the 5-minute demo video** | 45 min | The only artifact that cannot be regenerated. Shot list in `docs/DEMO_VIDEO_SCRIPT.md` |
| 3 | **Fill the AI disclosure form** | 15 min | Content drafted in `docs/AI_DISCLOSURE.md`; under-disclosure is a disqualification risk |
| 4 | **Push to a public GitHub repo** | 20 min | Slide 14 is the template's "Updated on Public GitHub" slide and needs a real URL |

**Do the video first.** The deck and docs can be rebuilt in seconds; the video cannot.

---

## Layout

```
submission/    The deck — upload this. Two copies: as-generated, and a
               pre-named one to rename to your team name.
docs/          metrics.md (the full evaluation report), DEMO_VIDEO_SCRIPT.md
               (shot list for the 5-min video), AI_DISCLOSURE.md (content for
               the organisers' disclosure form).
figures/       The 8 original figures (also embedded in the deck).
forge/         The engine — contract, NLP, catalog resolver, compiler,
               serving engine, FastAPI, eval harness.
tools/         Build scripts: figures, deck, threshold tuner, doc sync.
tests/         31 tests, each pinning a claim the submission makes.
data/          The organisers' 20 SIIS records + the 578-entry deeplink catalog.
atlas/         The compiled atlas — the build output the service loads.
               Regenerable: `python -m forge.build` rebuilds it in ~0.3 s.
reports/       Harness output: metrics_final.json (every published number),
               gate_sweep.json (the recall/precision frontier), and run logs.
inputs/        The original organisers' artifacts: brief, all five theme
               guides, the submission template, the AI disclosure form, the
               OCR'd Theme 2 pages.
deck/          Deck build intermediates: the 8 figures and the live demo
               transcript captured from a real engine call at build time.
README.md      Full technical write-up.
SUBMISSION_CHECKLIST.md   The four tasks, with exact commands.
dist-forge-prism.zip      The repo contents, zipped for GitHub upload.
.venv/         Pre-created and ready — no setup needed.
```

---

## The idea in one paragraph

Every other team will put an LLM in the request path. FORGE moves the
*understanding* stage to build time: the knowledge base is compiled once into a
validated atlas of deeplinked plans, and serving is a lookup plus a constraint
check. Measured on the supplied corpus: **P95 6.8 ms** on an atlas hit and
**20.7 ms** cold against a 300 ms / 8000 ms budget, **$0.00 per query**, no GPU,
no network, no API key — with **100% schema conformance, zero URL leaks and 100%
auto-action deeplink coverage**, because every emitted deeplink is validated
against the real 578-entry catalog before it leaves the process.

## Two gaps, stated up front

A judge will find these, so the deck leads with them (slide 11) and
`docs/metrics.md` §5 documents the measurement behind each:

1. **Paraphrase recall is 34.2%** against the guide's 80% target, on genuinely
   novel wording. The exact-match tier is excluded because it measures the
   normaliser, not the cache. `tools/tune_gate.py` measures the trade: reaching
   73% recall costs 53% wrong-serve, so the gate ships precision-first.
2. **Screen-resolution accuracy is 84.6%** against the 90% target. Fixing
   run-on steps raised deeplink coverage 42.4% → 63.9% and put more inferred
   targets in front of the accuracy check. A real regression, reported as one.

---

## Reproducing every number

```bash
cd ~/Samsung-PRISM-FORGE
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python -m forge.eval --siis data/siis_responses.json \
                     --catalog data/deeplinks.json \
                     --atlas atlas/current --encoder lexical \
                     --out reports/metrics_final.json
python -m pytest tests/ -q     # expect: 30 passed
python tools/sync_docs.py      # expect: all published numbers synced
```

`sync_docs.py` rewrites every published figure in the Markdown docs from
`reports/metrics_final.json` and **exits non-zero without writing anything** if a
row no longer matches — so a stale number fails loudly instead of shipping. The
deck reads the same JSON directly and cannot drift at all; `tools/make_deck.py`
also calls the live engine at build time to produce the demo transcript on slide 8.

To serve it:

```bash
FORGE_ATLAS=atlas/current FORGE_CATALOG=data/deeplinks.json \
  python -m uvicorn forge.api:app --host 127.0.0.1 --port 8000
curl localhost:8000/health
```

Full technical write-up: `README.md` and `docs/metrics.md`.
