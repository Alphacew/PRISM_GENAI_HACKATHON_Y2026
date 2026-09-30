# Submission checklist — do these in order

Deadline: **30 September 2026, 23:59 IST.** Everything in `/dist` is ready to upload.

## 1. Rename the deck (2 minutes) — REQUIRED

The organisers' template ships the filename literally as
`CollegeName_TeamName_Submission.pptx`. **They mean it.** Rename it:

```
SRMIST_<YourTeamName>_Submission.pptx
```

A judge opening a file called `CollegeName_TeamName_Submission.pptx` learns you
did not read the instructions. This is free marks thrown away.

## 2. Record the 5-minute demo video (45 minutes)

`docs/DEMO_VIDEO_SCRIPT.md` has the full shot list and narration. Non-negotiables:

- **One take, no jump cuts, screen recording with your voice.** A judge can tell
  a live run from a slideshow.
- Start the server **before** you hit record, so the cold-start wait is not in the
  video: `. .venv/bin/activate && python -m forge.build ... && uvicorn forge.api:app`
- Show the **no-match fallback** on purpose. It is the single most credible thing
  in the whole video: it proves the system declines to invent.
- Show `/v1/metrics` at the end so the counters are visibly real.
- Upload to YouTube as **Unlisted** (or Drive with link-sharing on) and paste the
  link into the deck's slide 8 note and the submission form.

## 3. Fill the AI disclosure form (15 minutes)

`docs/AI_DISCLOSURE.md` is the content to transfer into
`LangAI3.0_AI_Disclosure.docx`. Be generous, not minimal — an under-disclosed
submission is a disqualification risk, an over-disclosed one is just honest.

## 4. Push to a public GitHub repo (20 minutes)

```bash
cd ~/Samsung-PRISM-FORGE
git init && git add -A && git commit -m "FORGE — Samsung PRISM Theme 02"
gh repo create forge-prism --public --source=. --push
```

The deck's slide 14 is the template's "Updated on Public GitHub" checklist slide —
it needs a real URL in it. Add the repo link there and re-save.

## 5. Sanity-check before you upload (10 minutes)

```bash
cd ~/Samsung-PRISM-FORGE && . .venv/bin/activate
# every claim in the docs must still reproduce
python -m forge.eval --siis data/siis_responses.json \
                     --catalog data/deeplinks.json \
                     --atlas atlas/current --encoder lexical \
                     --out reports/metrics_final.json
python -m pytest tests/ -q          # expect 31 passed
python tools/sync_docs.py           # expect "all published numbers synced"
```

If `sync_docs.py` reports an unmatched row, a number in the docs no longer
matches the harness — fix it before submitting, or delete the claim.

## 6. Submit

Upload the renamed deck + the video link + the disclosure form. Say in the
submission text that the repo contains a one-command reproduction.

---

## What to say if a judge probes a weakness

They will find the two gaps. Do not wait to be asked — the deck's slide 11 and
the README's "Honest limitations" both lead with them. If it comes up:

- **"Your paraphrase recall is 34%."** — "On the hardest generated population,
  yes, and that is the number I chose to report. The exact-tier matches are
  excluded because they measure the normaliser, not the cache. `tools/tune_gate.py`
  measures the trade: getting to 73% recall costs 53% wrong-serve. For
  troubleshooting, sending someone to the wrong Settings screen is worse than
  saying 'I don't have this one', so the gate ships precision-first."
- **"Your screen accuracy is 85%, not 90%."** — "Correct, and it fell when I
  fixed step splitting: better steps meant more inferred targets in front of the
  accuracy check, and some are wrong. I would rather report 85% than keep the
  finer splitter and claim 93%."
- **"Why not just use an LLM?"** — "Cost, latency and determinism. P95 is 6.8 ms
  and $0.00 per query, and the same input gives byte-identical output. An LLM in
  the serving path makes all three worse and makes the hallucination gate
  unenforceable — every emitted deeplink here is validated against the real 578-
  entry catalog before it leaves the process."
