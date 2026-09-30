#!/usr/bin/env python3
"""FORGE — Interactive Demo Video Runner for 5-Minute Single-Take Recording.

Follows `docs/DEMO_VIDEO_SCRIPT.md` shot-for-shot.
Press [ENTER] to advance through each shot.
Displays on-screen narration cues and executes live HTTP requests against
the running FORGE server at http://127.0.0.1:8000.

Usage:
  python tools/demo_runner.py            # interactive mode for video recording
  python tools/demo_runner.py --test     # automated pre-flight verification
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def _find_base_url() -> str:
    for port in (8079, 8000):
        try:
            url = f"http://127.0.0.1:{port}/health"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                data = json.loads(resp.read().decode())
                if data.get("atlas_loaded") is True:
                    return f"http://127.0.0.1:{port}"
        except Exception:
            continue
    return "http://127.0.0.1:8079"

BASE_URL = _find_base_url()


def c(text: str, color: str = "bold") -> str:
    codes = {
        "bold": "\033[1m",
        "green": "\033[32m\033[1m",
        "cyan": "\033[36m\033[1m",
        "yellow": "\033[33m\033[1m",
        "blue": "\033[34m\033[1m",
        "magenta": "\033[35m\033[1m",
        "reset": "\033[0m",
    }
    return f"{codes.get(color, '')}{text}{codes['reset']}"


def post(endpoint: str, payload: dict) -> dict:
    url = f"{BASE_URL}{endpoint}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req) as resp:
        res = json.loads(resp.read().decode("utf-8"))
    res["_transport_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return res


def get(endpoint: str) -> dict:
    url = f"{BASE_URL}{endpoint}"
    t0 = time.perf_counter()
    with urllib.request.urlopen(url) as resp:
        res = json.loads(resp.read().decode("utf-8"))
    res["_transport_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return res


def check_server():
    try:
        h = get("/health")
        if h.get("status") == "ok":
            return True
    except Exception:
        pass
    return False


def wait_step(prompt: str, auto: bool = False):
    if auto:
        print(f"\n{c('[AUTO ADVANCE]', 'yellow')} {prompt}\n")
        time.sleep(1.0)
    else:
        try:
            input(f"\n{c('>>> [PRESS ENTER TO ADVANCE] >>>', 'green')} {prompt} ")
        except (KeyboardInterrupt, EOFError):
            print("\nExiting demo.")
            sys.exit(0)


def run_demo(auto: bool = False):
    print("=" * 78)
    print(c("FORGE — Interactive 5-Minute Demo Video Runner", "cyan"))
    print("Samsung PRISM Generative AI Hackathon (3rd Edition) · Theme 02")
    print("Team: SRM_Carrot (SRMIST)")
    print("=" * 78)

    # Pre-roll server check
    if not check_server():
        print(c("\n[!] FORGE service not detected at http://127.0.0.1:8000", "yellow"))
        print("Starting in-process background Uvicorn server...")
        env = os.environ.copy()
        env["FORGE_ATLAS"] = str(ROOT / "atlas" / "current")
        env["FORGE_CATALOG"] = str(ROOT / "data" / "deeplinks.json")
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "forge.api:app", "--port", "8000", "--host", "127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        for _ in range(15):
            if check_server():
                break
            time.sleep(0.5)
        if not check_server():
            print(c("ERROR: Failed to start FORGE server.", "yellow"))
            sys.exit(1)
        print(c("Server online & healthy!", "green"))

    # Pre-warm
    try:
        siis_data = json.loads((ROOT / "data" / "siis_responses.json").read_text(encoding="utf-8"))
        for row in siis_data["responses"]:
            post("/v1/troubleshoot", {"query": row["original_query"]})
        print(c(f"Pre-warmed {len(siis_data['responses'])} atlas queries.", "green"))
    except Exception as e:
        print(c(f"Pre-warm warning: {e}", "yellow"))

    # -----------------------------------------------------------------------
    # Shot 0
    # -----------------------------------------------------------------------
    print("\n" + "=" * 78)
    print(c("SHOT 0 · 0:00–0:20 · Title Card & Introduction", "magenta"))
    print(c("ON SCREEN:", "bold"), "Slide 1 (Title Card)")
    print(c("SAY:", "cyan"), '"This is FORGE. It turns a vague complaint like *screen flickers and the battery dies fast* into a deeplinked troubleshooting plan. The interesting part is where the work happens."')
    wait_step("Ready for Shot 1", auto)

    # -----------------------------------------------------------------------
    # Shot 1
    # -----------------------------------------------------------------------
    print("\n" + "=" * 78)
    print(c("SHOT 1 · 0:20–0:50 · Architecture & Build-Time Shift", "magenta"))
    print(c("ON SCREEN:", "bold"), "Slide 2 / Figure 1 (Conventional vs FORGE Placement)")
    print(c("SAY:", "cyan"), '"The reference roadmap runs structure extraction, deeplink mapping and validation inside every request. We hoisted all three to build time. Serving becomes a lookup."')
    wait_step("Ready for Shot 2", auto)

    # -----------------------------------------------------------------------
    # Shot 2
    # -----------------------------------------------------------------------
    print("\n" + "=" * 78)
    print(c("SHOT 2 · 0:50–1:20 · Atlas Compilation Build Lane", "magenta"))
    print(c("ON SCREEN:", "bold"), "Terminal: Running atlas build")
    print(c("SAY:", "cyan"), '"Here is the build. It compiles the whole knowledge base: 14 plans, 33 actions, every one audited against the contract before it is admitted. A plan that fails a gate never reaches the atlas."')
    cmd = [sys.executable, "-m", "forge.build", "--siis", "data/siis_responses.json", "--catalog", "data/deeplinks.json", "--out", "atlas"]
    print(f"\n$ {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(ROOT))
    wait_step("Ready for Shot 3 (Cold Request)", auto)

    # -----------------------------------------------------------------------
    # Shot 3
    # -----------------------------------------------------------------------
    print("\n" + "=" * 78)
    print(c("SHOT 3 · 1:20–1:50 · Cold Request (Compile-on-Miss)", "magenta"))
    print(c("ON SCREEN:", "bold"), "Terminal: curl POST /v1/troubleshoot with novel siis_response")
    print(c("SAY:", "cyan"), '"A genuinely novel complaint with new reference text. P95 under twenty milliseconds, and cache_tier says miss, stage says compile_on_miss."')
    cold_payload = {
        "query": "haptic vibration motor rattles when typing",
        "siis_response": (
            "# Troubleshooting Haptic Vibration Motor Rattling\n\n"
            "## Adjust Vibration Intensity\n"
            "Navigate to and open Settings.\n"
            "Tap on Sounds and vibration.\n"
            "Tap on Vibration intensity.\n"
            "Reduce vibration sliders to lower physical motor resonance.\n\n"
            "## Physical Inspection\n"
            "Visit an authorized TechCorp Service Center if mechanical rattling persists.\n"
        )
    }
    print(f"\n$ curl -X POST {BASE_URL}/v1/troubleshoot -d '{json.dumps(cold_payload)[:60]}...'")
    res3 = post("/v1/troubleshoot", cold_payload)
    print(c("RESPONSE META:", "green"), json.dumps(res3.get("meta"), indent=2))
    print(c("CONTEXT TITLE:", "green"), res3["response"]["contexts"][0]["title"] if res3["response"]["contexts"] else "None")
    print(c("ACTION 1:", "green"), res3["response"]["contexts"][0]["actions"][0]["actionName"] if res3["response"]["contexts"] else "None")
    wait_step("Ready for Shot 4 (Warm Cache Hit)", auto)

    # -----------------------------------------------------------------------
    # Shot 4
    # -----------------------------------------------------------------------
    print("\n" + "=" * 78)
    print(c("SHOT 4 · 1:50–2:20 · Warm Cache Hit (Sub-millisecond Exact Lookup)", "magenta"))
    print(c("ON SCREEN:", "bold"), "Terminal: curl POST /v1/troubleshoot for same query")
    print(c("SAY:", "cyan"), '"Same query again. cache_tier is exact, latency_ms is under one, and cost_usd is zero — it was zero the first time too."')
    repeat_payload = {"query": "haptic vibration motor rattles when typing"}
    print(f"\n$ curl -X POST {BASE_URL}/v1/troubleshoot -d '{json.dumps(repeat_payload)}'")
    res4 = post("/v1/troubleshoot", repeat_payload)
    print(c("RESPONSE META:", "green"), json.dumps(res4.get("meta"), indent=2))
    wait_step("Ready for Shot 5 (Unseen Paraphrase)", auto)

    # -----------------------------------------------------------------------
    # Shot 5
    # -----------------------------------------------------------------------
    print("\n" + "=" * 78)
    print(c("SHOT 5 · 2:20–2:50 · Unseen Paraphrase Query", "magenta"))
    print(c("ON SCREEN:", "bold"), "Terminal: curl POST /v1/troubleshoot with unseen colloquial wording")
    print(c("SAY:", "cyan"), '"This wording was never compiled. It still lands on the same plan — that is the intent signature doing the work, not string matching."')
    para_payload = {"query": "phone swipe gestures wrong direction after app install"}
    print(f"\n$ curl -X POST {BASE_URL}/v1/troubleshoot -d '{json.dumps(para_payload)}'")
    res5 = post("/v1/troubleshoot", para_payload)
    print(c("RESPONSE META:", "green"), json.dumps(res5.get("meta"), indent=2))
    if res5["response"]["contexts"]:
        ctx = res5["response"]["contexts"][0]
        print(c("RESOLVED GOAL:", "green"), ctx["goal"])
        print(c("FIRST STEP:", "green"), ctx["actions"][0]["stepGroups"][0]["steps"][0])
        print(c("ACTIONABLE DEEPLINK:", "green"), json.dumps(ctx["actions"][0]["stepGroups"][0]["actionableDeeplink"], indent=2))
    wait_step("Ready for Shot 6 (Figure 4)", auto)

    # -----------------------------------------------------------------------
    # Shot 6
    # -----------------------------------------------------------------------
    print("\n" + "=" * 78)
    print(c("SHOT 6 · 2:50–3:20 · Target + Polarity Resolution", "magenta"))
    print(c("ON SCREEN:", "bold"), "Slide / Figure 4 (Target + Polarity Resolution)")
    print(c("SAY:", "cyan"), '"Each step maps to the exact Settings screen, not a parent menu. And when the step toggles something on, we read the polarity off the catalog entry so we never resolve to the off entry and flip the user\'s setting the wrong way."')
    wait_step("Ready for Shot 7 (Multi-Intent Fan-out)", auto)

    # -----------------------------------------------------------------------
    # Shot 7
    # -----------------------------------------------------------------------
    print("\n" + "=" * 78)
    print(c("SHOT 7 · 3:20–3:50 · Multi-Intent Fan-Out", "magenta"))
    print(c("ON SCREEN:", "bold"), "Terminal: curl with compound multi-problem complaint")
    print(c("SAY:", "cyan"), '"One utterance, multiple problems. contexts is a list, so it returns multiple ranked Goals. Almost every submission will return one."')
    multi_payload = {"query": "screen flickers and the battery drains fast and email server not responding"}
    print(f"\n$ curl -X POST {BASE_URL}/v1/troubleshoot -d '{json.dumps(multi_payload)}'")
    res7 = post("/v1/troubleshoot", multi_payload)
    print(c("RESPONSE META:", "green"), json.dumps(res7.get("meta"), indent=2))
    print(c(f"RETURNED CONTEXTS COUNT: {len(res7['response']['contexts'])}", "green"))
    for idx, ctx in enumerate(res7["response"]["contexts"], 1):
        print(f"  {idx}. {c(ctx['title'], 'bold')} — {ctx['goal']}")
    wait_step("Ready for Shot 8 (Nonsense Fallback)", auto)

    # -----------------------------------------------------------------------
    # Shot 8
    # -----------------------------------------------------------------------
    print("\n" + "=" * 78)
    print(c("SHOT 8 · 3:50–4:10 · Graceful No-Match Fallback", "magenta"))
    print(c("ON SCREEN:", "bold"), "Terminal: curl with out-of-domain nonsense complaint")
    print(c("SAY:", "cyan"), '"When the corpus has no answer, we say so explicitly instead of inventing one."')
    none_payload = {"query": "how to bake sourdough bread on my microwave oven"}
    print(f"\n$ curl -X POST {BASE_URL}/v1/troubleshoot -d '{json.dumps(none_payload)}'")
    res8 = post("/v1/troubleshoot", none_payload)
    print(c("FULL RESPONSE BODY:", "green"), json.dumps(res8, indent=2))
    wait_step("Ready for Shot 9 (Live Metrics)", auto)

    # -----------------------------------------------------------------------
    # Shot 9
    # -----------------------------------------------------------------------
    print("\n" + "=" * 78)
    print(c("SHOT 9 · 4:10–4:35 · Production Metrics & Health Endpoint", "magenta"))
    print(c("ON SCREEN:", "bold"), "Terminal: curl GET /v1/metrics")
    print(c("SAY:", "cyan"), '"The service reports its own health: hit rate, tier split, latency percentiles, cost."')
    print(f"\n$ curl {BASE_URL}/v1/metrics")
    res9 = get("/v1/metrics")
    print(c("LIVE METRICS:", "green"), json.dumps(res9, indent=2))
    wait_step("Ready for Shot 10 (Ablation Analysis)", auto)

    # -----------------------------------------------------------------------
    # Shot 10
    # -----------------------------------------------------------------------
    print("\n" + "=" * 78)
    print(c("SHOT 10 · 4:35–4:55 · Architectural Ablation & Honest Frontiers", "magenta"))
    print(c("ON SCREEN:", "bold"), "Slide / Figure 6 (Ablation Analysis & Frontier Curve)")
    print(c("SAY:", "cyan"), '"And the result we did not want to show: on a twenty-document corpus the dense encoder alone accepts more matches than our hybrid. We report it. Here is why the target and polarity terms still earn their place."')
    wait_step("Ready for Shot 11 (Closing Card)", auto)

    # -----------------------------------------------------------------------
    # Shot 11
    # -----------------------------------------------------------------------
    print("\n" + "=" * 78)
    print(c("SHOT 11 · 4:55–5:00 · Closing Card & GitHub Repository", "magenta"))
    print(c("ON SCREEN:", "bold"), "Slide 16 (Closing Card & Checklist)")
    print(c("SAY:", "cyan"), '"Repo is in the submission, everything reproducible with one command."')
    print("\n" + "=" * 78)
    print(c("DEMO RUN COMPLETE! Perfect 5-minute video recording workflow ready.", "green"))
    print("=" * 78)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Run automated pre-flight test without pausing")
    args = parser.parse_args()
    run_demo(auto=args.test)
