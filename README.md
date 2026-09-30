# FORGE — Smart Guided Troubleshooting Engine

**Samsung PRISM Generative AI Hackathon, 3rd Edition · Theme 02**
Team `SRM_Carrot` · SRMIST

Turns a vague device complaint into an ordered, schema-valid, deeplinked
troubleshooting plan — in under a millisecond, with **no model in the serving
path**.

---

## 🏆 Submission Deliverables & Links

| Deliverable | Location / URL | Description |
|---|---|---|
| 🎥 **Demo Video** | [**Google Drive Video Link**](https://drive.google.com/drive/folders/1QrjJKvw05YEvInxOWxkgEMnecfP9hlbw?usp=sharing) | 5-Minute unedited screen recording walkthrough |
| 📊 **Presentation Deck** | [`submission/SRMIST_SRM_Carrot_Submission.pptx`](submission/SRMIST_SRM_Carrot_Submission.pptx) | Complete 16-slide presentation with measured figures |
| 📝 **AI Usage Disclosure** | [`submission/LangAI3.0_AI_Disclosure.docx`](submission/LangAI3.0_AI_Disclosure.docx) | Fully populated official AI disclosure form |
| 📦 **Dependencies** | [`requirements.txt`](requirements.txt) | Python dependencies |
| 💻 **GitHub Repository** | [**github.com/Alphacew/PRISM_GENAI_HACKATHON_Y2026**](https://github.com/Alphacew/PRISM_GENAI_HACKATHON_Y2026) | Working prototype code & full reproduction |
| 🧪 **Evaluation Report** | [`docs/metrics.md`](docs/metrics.md) | Full Appendix C benchmark report |

---

## The one-sentence version

> Every expensive stage in the reference pipeline — structure extraction,
> deeplink resolution, constraint enforcement — is hoisted to **build time**; a
> request becomes a lookup.

That single inversion is what produces the latency, the cost and the determinism
numbers below. It is not a tuning result.

| Measured on the shipped corpus | Value | Guide's budget |
|---|---|---|
| Schema-valid output lines | **100%** | ≥ 99% |
| Rule compliance (goal / title / description / ordering) | **100%** | ≥ 95% |
| Absolute URL leaks | **0** | 0 |
| Deeplink catalog validity (exact URI match) | **100%** | 100% |
| Deeplink resolution rate (actions landing on an indexed screen) | **63.9%** | — |
| Auto actions carrying a valid actionable deeplink | **100%** | ≥ 98% |
| Screen-resolution accuracy (exact screen, not parent menu) | **84.62%** | ≥ 90% |
| P95 — fast path, atlas hit | **0.98 ms** | ≤ 300 ms |
| P95 — cold path, compile-on-miss | **7 ms** | ≤ 8000 ms |
| Inference cost per query | **$0.00** | tracked |
| Semantic cache recall — novel wording (exact-tier matches excluded) | **34.2%** | ≥ 80% *(gap — see Limitations)* |

All figures come from `python -m forge.eval`, whose output is committed at
`reports/metrics_final.json`.

---

## Quick start

```bash
# 1. install
uv venv .venv --python 3.12 && source .venv/bin/activate
uv pip install -r requirements.txt

# 2. compile the atlas from the SIIS corpus (~0.2 s)
python -m forge.build --siis data/siis_responses.json \
                      --catalog data/deeplinks.json --out atlas

# 3. run the service
FORGE_ATLAS=atlas/current FORGE_CATALOG=data/deeplinks.json \
  python -m uvicorn forge.api:app --host 0.0.0.0 --port 8000

# 4. or just reproduce every number in this README
python -m forge.eval --siis data/siis_responses.json \
                     --catalog data/deeplinks.json \
                     --atlas atlas/current --out reports/metrics_final.json
```

Docker:

```bash
docker build -t forge .
docker run -p 8000:8000 forge
curl localhost:8000/health
```

---

## API

### `POST /v1/troubleshoot`

```json
{
  "query": "phone swipe gestures wrong direction after app install",
  "siis_response": "<optional raw knowledge-base text>"
}
```

Omitting `siis_response` performs a semantic lookup against the pre-warmed atlas.
Supplying it, for an intent not already compiled, triggers the cold path
(compile-on-miss) for that request only.

Response — the organisers' schema, plus an additive `meta` block:

```json
{
  "query": "...",
  "query_variations": ["...", "..."],
  "response": { "contexts": [ { "goal": "...", "title": "...", "score": 0.93,
                                "actions": [ { "actionName": "...",
                                               "description": "It will ...",
                                               "category": "auto",
                                               "stepGroups": [ { "steps": ["..."],
                                                                 "actionableDeeplink": {...},
                                                                 "validationDeeplink": {...} } ] } ] } ] },
  "meta": { "latency_ms": 0, "cache_hit": true, "model": "none(rules)", "cost_usd": 0.0,
            "plan_ids": ["FG-..."], "atlas_version": "current",
            "stage": "serve", "cache_tier": "exact", "margin": 1.0,
            "tokens_in": 0, "tokens_out": 0 }
}
```

`meta` is additive — the schema fields the guide specifies are unchanged, so
every response still validates against `data/schema.py` verbatim.

| field | meaning |
|---|---|
| `cache_tier` | `exact` \| `semantic` \| `miss` — which tier answered |
| `stage` | `serve` \| `compile_on_miss` — whether a model-free compile ran |
| `margin` | similarity gap between the best and second-best intent match |
| `plan_ids` | which atlas plans produced the response (provenance) |
| `cost_usd`, `tokens_*` | always `0` / `0` — nothing is generated at serve time |

### `GET /health`
`200 {"status":"ok","atlas_loaded":true,"boot_ms":...}` once the atlas, index and
encoder are warm; `503` otherwise.

### `GET /v1/metrics`
Live counters: requests, cache-hit rate, exact/semantic/miss split, latency
percentiles, multi-context and no-match counts.

---

## How it works

```
COMPILE LANE  (once per corpus revision — `python -m forge.build`)

  SIIS markdown ──► structure parser ──► constraint compiler ──► target+polarity
                     forge/compile.py     forge/compile.py       resolver
                     · heading/mode        · goal syntax          forge/catalog.py
                       segmentation        · 2–3 word title       · family + target
                     · breadcrumb          · 5–7 word "It will"     mined from prose
                       walker              · Title Case           · enable/disable
                     · one step =          · deterministic          polarity
                       one interaction       repair               · path-depth demotion
                                                  │
                                                  ▼
                                         contract.audit_envelope()  ◄── hard gate
                                                  │
                                                  ▼
                                         PLAN ATLAS (versioned, content-hashed)

SERVE LANE  (per request — no model calls)

  POST /v1/troubleshoot ──► split multi-intent ──► intent signature
                                                    │
                              ┌─────────────────────┴─────────────────────┐
                              ▼                                           ▼
                     Tier 1: exact hash                     Tier 2: margin-gated kNN
                     (~0.2 ms)                              over alias paraphrases
                              └─────────────────────┬─────────────────────┘
                                                    ▼
                                       assemble Goal(s) → validate → JSON
```

### Why the deeplink resolution works

The catalog's URIs are opaque (`voiceassist://masked/act/9f2c…`), so the guide
forbids matching on them — and they would be meaningless anyway. But the
catalog's 578 descriptions follow a regular grammar, which we parse into a
family and a target:

| family | count | form |
|---|---|---|
| `open` | 253 | `Opens the <T> settings page in device Settings…` |
| `enable` / `disable` | 276 | `(Enables\|Disables) <T> via device Settings…` |
| `update` | 36 | `Updates the <T> to a specified value…` |
| `monitor` | 10 | `Diagnoses <T>…` |

Four signals score each candidate: BM25 + dense relevance, **target agreement**
(the entry's parsed target must share vocabulary with the trajectory),
**polarity** (a step that toggles ON must never resolve to the catalog's OFF
entry — the catalog ships both, and text similarity alone happily picks the
wrong one), and **path depth** (entries naming only a parent menu are demoted).

A **precision gate** then decides whether to emit the match at all. If the
winning entry names no target, or shares no vocabulary with the trajectory, we
decline and emit `voiceassist://dummy_positive` with authored copy, exactly as
the catalog's own placeholder entry instructs. That is why coverage is 63.9% and
accuracy is 84.62% rather than both being optimistic.

### The constraint compiler

Five of the guide's hygiene metrics are about string shape (goal syntax, title
length, `It will` + 5–7 words, Title Case, one interaction per step). String
shape is a compiler's job, not a prompt's. Each rule is a function with a
deterministic repair, and `contract.audit_envelope()` re-checks every finished
response. A plan that fails any gate is **rejected at build time** — it never
reaches the atlas. Failures are repaired, never retried, so two builds of the
same corpus are byte-identical.

### Multi-intent fan-out

`contexts` is a list for a reason. `split_intents()` decomposes a complaint only
when the clauses carry **disjoint problem atoms**, so a single symptom is never
fragmented; a genuinely multi-problem complaint returns several ranked Goals in
one response. Measured: 27 of 812 harness requests were multi-intent and all of
them returned multiple Goals.

---

## Repository layout

```
forge/
  contract.py    the organisers' schema (verbatim) + the seven mechanical gates
  nlp.py         normalisation, intent signatures, paraphrase generation, BM25,
                 pluggable encoders (hashed n-gram; Model2Vec behind same interface)
  catalog.py     catalog grammar parser, target+polarity resolver, precision gate
  compile.py     SIIS markdown → structure → constraint repair → validated Goal
  serve.py       atlas build/load, two-tier intent cache, serving engine
  api.py         FastAPI surface: /v1/troubleshoot, /health, /v1/metrics
  build.py       compile the atlas from a corpus
  eval.py        the measurement harness (compliance, latency, cache, ablation)
data/            the organisers' shipped artefacts, unmodified
tools/           figure + deck generators (the deck is reproducible, not hand-made)
reports/         measured metrics JSON + run logs
tests/           pytest suite, including a conformance fuzz over generated queries
```

---

## Honest limitations

1. **Paraphrase cache recall is 34.2% on genuinely novel wording, against the
   guide's 80% target.** The first step in reporting this honestly is refusing to
   let the exact tier inflate it: 293 of 540 test paraphrases normalise onto the
   seed's intent signature and are served from a hash lookup, so they measure the
   normaliser rather than the semantic cache. On the 158 that are genuinely new
   wording, recall is 34.2% and the wrong-serve rate is 13.3%. `tools/tune_gate.py`
   measures the trade rather than asserting it: buying recall back to 73.2% costs
   53.0% wrong-serve. The gate therefore ships precision-first, because sending a
   user to the wrong Settings screen is worse than a ranked best-effort answer.
   The fix is a better encoder, not a looser gate —
   `StaticEmbeddingEncoder` ships in `nlp.py` behind the same interface and
   reaches 79.8% novel recall (at 56.3% wrong-serve until re-tuned); both runs are
   in the repo as `reports/metrics_lexical.json` and `reports/metrics_semantic.json`.
2. **Screen-resolution accuracy is 84.62%, against the guide's 90% target.** Finer
   step splitting raised deeplink coverage from 42.4% to 63.9% and, in doing so,
   put more inferred targets in front of the accuracy check — some of which are
   wrong. This is a real regression, not a measurement artefact: the extra
   coverage came from targets the precision gate was previously declining. The
   fix is the same feedback loop as (3).
3. **Deeplink coverage is 63.9% of actions.** The rest decline to the documented
   placeholder; those are screens the shipped catalog does not index. We treat
   declining as correct behaviour and log the target name, which makes the decline
   log a ranked backlog of catalog entries worth adding.
4. **Corpus scale.** The atlas compiles the 20 supplied SIIS records in ~0.36 s.
   Nothing in the design assumes 20 — adding a corpus is a rebuild, not a
   redeploy — but we have not measured at 10k.
5. **Ablation result, reported as measured.** On this corpus the dense encoder
   alone accepts more matches (77.8%) than the shipped hybrid (63.9%) at similar
   mean target agreement (0.788 vs 0.789). The path and polarity terms buy
   precision on deep trajectories and correct polarity on enable/disable pairs
   (100% agreement); on a 20-document corpus their coverage cost is not yet
   repaid. We would rather print this row than tune the gate until the numbers
   flatter us.

---

## Reproducibility

Every number in this README is produced by `python -m forge.eval`. The harness
prints compliance, latency percentiles, cache efficacy and a five-row
architecture ablation to `reports/metrics_final.json`. Seeds are fixed, the
encoder is deterministic, and the build is single-threaded.

```bash
python -m pytest tests/ -q          # conformance fuzz + unit tests
python -m forge.eval ...            # regenerate every reported metric
```

Environment: Python 3.12, CPU only (measured on a 13th-gen Intel i5, no GPU).
Runtime dependencies are listed in `requirements.txt`.

## AI usage

AI coding assistants were used during development. The full declaration, per
feature, is in `docs/AI_DISCLOSURE.md`. The shipped system makes no model calls
at serve time: `meta.model` is `none(rules)` and `cost_usd` is `0.0` on every
response, which is verifiable from the response body itself.
