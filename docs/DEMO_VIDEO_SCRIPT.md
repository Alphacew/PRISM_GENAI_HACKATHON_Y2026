# Demo video — 5 minutes, single take, no edits

**Requirement (guide):** max 5 minutes, YouTube or Drive link. The organiser's own
submission video should show the prototype working, not a slideshow.

**Rules of thumb:** one unedited take beats polish. Show the terminal and the JSON.
Never show a slide where a live response would do.

---

## Pre-roll checklist (do this before you hit record)

```bash
# 1. build the atlas, start the service, confirm health
cd <repo>
source .venv/bin/activate
python -m forge.build --siis data/siis_responses.json --catalog data/deeplinks.json --out atlas
FORGE_ATLAS=atlas/current FORGE_CATALOG=data/deeplinks.json \
  python -m uvicorn forge.api:app --port 8000 &
sleep 3 && curl -s localhost:8000/health

# 2. pre-warm the atlas tier so the warm-hit shots are genuinely warm
python - <<'PY'
import json, urllib.request
rows = json.load(open("data/siis_responses.json"))["responses"]
for r in rows:
    body = json.dumps({"query": r["original_query"]}).encode()
    urllib.request.urlopen(urllib.request.Request(
        "http://127.0.0.1:8000/v1/troubleshoot", body,
        {"Content-Type": "application/json"}))
print("warmed", len(rows))
PY
```

- [ ] Terminal font ≥ 16pt, dark theme, no notification popups
- [ ] `docker build -t forge .` already run (show the build, don't wait for it)
- [ ] Screen recording at 1080p; mic check
- [ ] Close Slack/Discord/browser tabs

---

## Shot list

| # | Time | On screen | Say (paraphrase — do not read) |
|---|---|---|---|
| 0 | 0:00–0:20 | Title card: theme, team, one sentence | "This is FORGE. It turns a vague complaint like *screen flickers and the battery dies fast* into a deeplinked troubleshooting plan. The interesting part is where the work happens." |
| 1 | 0:20–0:50 | `fig1` — conventional vs FORGE placement | "The reference roadmap runs structure extraction, deeplink mapping and validation inside every request. We hoisted all three to build time. Serving becomes a lookup." |
| 2 | 0:50–1:20 | Terminal: `python -m forge.build ...` — let the JSON print | "Here is the build. It compiles the whole knowledge base: fourteen plans, thirty-three actions, every one audited against the contract before it is admitted. A plan that fails a gate never reaches the atlas." |
| 3 | 1:20–1:50 | `curl` cold request with `siis_response` | "A genuinely novel complaint with new reference text. P95 under twenty milliseconds, and `cache_tier` says `miss`, `stage` says `compile_on_miss`." |
| 4 | 1:50–2:20 | `curl` the same query again + `jq .meta` | "Same query again. `cache_tier` is `exact`, `latency_ms` is under one, and `cost_usd` is zero — it was zero the first time too." |
| 5 | 2:20–2:50 | `curl` a **paraphrase** the build never saw | "This wording was never compiled. It still lands on the same plan — that is the intent signature doing the work, not string matching." |
| 6 | 2:50–3:20 | `fig4` — target + polarity resolution | "Each step maps to the *exact* Settings screen, not a parent menu. And when the step toggles something on, we read the polarity off the catalog entry so we never resolve to the off entry and flip the user's setting the wrong way." |
| 7 | 3:20–3:50 | Multi-intent `curl` → three Goals in one response | "One utterance, three problems. `contexts` is a list, so it returns three ranked Goals. Almost every submission will return one." |
| 8 | 3:50–4:10 | `curl` a nonsense query → `contexts: []`, `fallback: no_match` | "When the corpus has no answer, we say so explicitly instead of inventing one." |
| 9 | 4:10–4:35 | `curl localhost:8000/v1/metrics` | "The service reports its own health: hit rate, tier split, latency percentiles, cost." |
| 10 | 4:35–4:55 | `fig6` — ablation chart | "And the result we did not want to show: on a twenty-document corpus the dense encoder alone accepts more matches than our hybrid. We report it. Here is why the target and polarity terms still earn their place." |
| 11 | 4:55–5:00 | Title card: repo URL + contact | "Repo's in the submission, everything reproducible with one command." |

---

## Things that will get filmed badly (avoid)

- **Do not scroll source code.** Nobody scores your tabs. Show behaviour.
- **Do not claim past-tense results** about anything unbuilt. Everything here runs;
  anything that does not run yet goes in the limitations slide, not the video.
- **Do not hide the ablation slide's bad news.** It is on screen for twenty seconds
  and it is the strongest credibility signal you have.
- **Do not re-record to remove a stutter.** Unedited single takes are explicitly
  preferred by this organiser.
- **Do not let the demo depend on network access.** The service is local; say so.
