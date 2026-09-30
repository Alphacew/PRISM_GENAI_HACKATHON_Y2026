#!/usr/bin/env python3
"""FORGE — Demo Video Runner.

Supports two modes:
  1. Clean Mode (Default for video recording):
     Only prints realistic terminal commands ($ curl ...) and formatted JSON.
     Zero script text or prompt hints on screen. Completely looks like a live terminal.
     
  2. Prompter Mode (--prompter):
     Shows the full shot cues and on-screen narration text in the terminal.

Usage:
  python tools/demo_runner.py            # Clean terminal mode (for recording)
  python tools/demo_runner.py --prompter # Shows speech cues in terminal
  python tools/demo_runner.py --test     # Automated pre-flight check
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
        "dim": "\033[2m",
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


def wait_step(prompt: str, auto: bool = False, clean: bool = True):
    if auto:
        time.sleep(1.0)
        return
    try:
        if clean:
            input(f"\n{c('Press [Enter] for next command...', 'dim')} ")
        else:
            input(f"\n{c('>>> [PRESS ENTER TO ADVANCE] >>>', 'green')} {prompt} ")
    except (KeyboardInterrupt, EOFError):
        print("\nExiting demo.")
        sys.exit(0)


def run_demo(auto: bool = False, prompter: bool = False):
    clean = not prompter

    # Pre-roll server check
    if not check_server():
        print(c("\n[!] Starting FORGE server on port 8079...", "yellow"))
        env = os.environ.copy()
        env["FORGE_ATLAS"] = str(ROOT / "atlas" / "current")
        env["FORGE_CATALOG"] = str(ROOT / "data" / "deeplinks.json")
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "forge.api:app", "--port", "8079", "--host", "127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        for _ in range(15):
            if check_server():
                break
            time.sleep(0.5)

    # Pre-warm
    try:
        siis_data = json.loads((ROOT / "data" / "siis_responses.json").read_text(encoding="utf-8"))
        for row in siis_data["responses"]:
            post("/v1/troubleshoot", {"query": row["original_query"]})
    except Exception:
        pass

    if not clean:
        print("=" * 78)
        print(c("FORGE — Interactive 5-Minute Demo Video Runner", "cyan"))
        print("=" * 78)

    # -----------------------------------------------------------------------
    # Step 1: Ready to start
    # -----------------------------------------------------------------------
    if clean:
        os.system("clear")
        print(c("FORGE Terminal ready. Start screen recording now.", "green"))
    else:
        print(c("SHOT 0 · Title card in browser", "magenta"))
        print(c("SAY:", "cyan"), '"This is FORGE, by team SRM_Carrot from SRMIST..."')
    wait_step("Ready for Build Step", auto, clean)

    # -----------------------------------------------------------------------
    # Step 2: Build Lane
    # -----------------------------------------------------------------------
    if clean:
        os.system("clear")
    else:
        print("\n" + "=" * 78)
        print(c("SHOT 2 · Atlas Compilation Build Lane", "magenta"))
        print(c("SAY:", "cyan"), '"Here is the build. It compiles 14 plans in 300 ms..."')

    print(f"{c('user@samsung-dev:~/forge$', 'cyan')} python -m forge.build --siis data/siis_responses.json --catalog data/deeplinks.json --out atlas")
    cmd = [sys.executable, "-m", "forge.build", "--siis", "data/siis_responses.json", "--catalog", "data/deeplinks.json", "--out", "atlas"]
    subprocess.run(cmd, cwd=str(ROOT))
    wait_step("Ready for Cold Request", auto, clean)

    # -----------------------------------------------------------------------
    # Step 3: Cold Request (Compile-on-Miss)
    # -----------------------------------------------------------------------
    if clean:
        os.system("clear")
    else:
        print("\n" + "=" * 78)
        print(c("SHOT 3 · Cold Request (Compile-on-Miss)", "magenta"))
        print(c("SAY:", "cyan"), '"A genuinely novel complaint with new reference text..."')

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
    print(f"{c('user@samsung-dev:~/forge$', 'cyan')} curl -s -X POST {BASE_URL}/v1/troubleshoot -H 'Content-Type: application/json' -d @cold_query.json | jq .")
    res3 = post("/v1/troubleshoot", cold_payload)
    print(json.dumps(res3, indent=2))
    wait_step("Ready for Warm Request", auto, clean)

    # -----------------------------------------------------------------------
    # Step 4: Warm Request (Exact Hash)
    # -----------------------------------------------------------------------
    if clean:
        os.system("clear")
    else:
        print("\n" + "=" * 78)
        print(c("SHOT 4 · Warm Cache Hit (< 1ms)", "magenta"))
        print(c("SAY:", "cyan"), '"Same query again. cache_tier is exact, latency is under 1 millisecond..."')

    repeat_payload = {"query": "haptic vibration motor rattles when typing"}
    print(f"{c('user@samsung-dev:~/forge$', 'cyan')} curl -s -X POST {BASE_URL}/v1/troubleshoot -d '{json.dumps(repeat_payload)}' | jq .")
    res4 = post("/v1/troubleshoot", repeat_payload)
    print(json.dumps(res4, indent=2))
    wait_step("Ready for Paraphrase", auto, clean)

    # -----------------------------------------------------------------------
    # Step 5: Unseen Paraphrase Query
    # -----------------------------------------------------------------------
    if clean:
        os.system("clear")
    else:
        print("\n" + "=" * 78)
        print(c("SHOT 5 · Unseen Paraphrase Query", "magenta"))
        print(c("SAY:", "cyan"), '"This colloquial wording was never compiled..."')

    para_payload = {"query": "phone swipe gestures wrong direction after app install"}
    print(f"{c('user@samsung-dev:~/forge$', 'cyan')} curl -s -X POST {BASE_URL}/v1/troubleshoot -d '{json.dumps(para_payload)}' | jq .")
    res5 = post("/v1/troubleshoot", para_payload)
    print(json.dumps(res5, indent=2))
    wait_step("Ready for Multi-Intent", auto, clean)

    # -----------------------------------------------------------------------
    # Step 6: Multi-Intent Fan-Out
    # -----------------------------------------------------------------------
    if clean:
        os.system("clear")
    else:
        print("\n" + "=" * 78)
        print(c("SHOT 7 · Multi-Intent Fan-Out", "magenta"))
        print(c("SAY:", "cyan"), '"One utterance, multiple problems..."')

    multi_payload = {"query": "screen flickers and the battery drains fast and email server not responding"}
    print(f"{c('user@samsung-dev:~/forge$', 'cyan')} curl -s -X POST {BASE_URL}/v1/troubleshoot -d '{json.dumps(multi_payload)}' | jq '.response.contexts[].title'")
    res7 = post("/v1/troubleshoot", multi_payload)
    titles = [ctx["title"] for ctx in res7.get("response", {}).get("contexts", [])]
    print(json.dumps(titles, indent=2))
    print(f"\n{c('// Full contexts count:', 'dim')} {len(res7.get('response', {}).get('contexts', []))}")
    wait_step("Ready for Nonsense Fallback", auto, clean)

    # -----------------------------------------------------------------------
    # Step 7: Graceful No-Match Fallback
    # -----------------------------------------------------------------------
    if clean:
        os.system("clear")
    else:
        print("\n" + "=" * 78)
        print(c("SHOT 8 · Graceful No-Match Fallback", "magenta"))
        print(c("SAY:", "cyan"), '"When the corpus has no answer, we say so explicitly..."')

    none_payload = {"query": "how to bake sourdough bread on my microwave oven"}
    print(f"{c('user@samsung-dev:~/forge$', 'cyan')} curl -s -X POST {BASE_URL}/v1/troubleshoot -d '{json.dumps(none_payload)}' | jq .")
    res8 = post("/v1/troubleshoot", none_payload)
    print(json.dumps(res8, indent=2))
    wait_step("Ready for Metrics", auto, clean)

    # -----------------------------------------------------------------------
    # Step 8: Production Metrics
    # -----------------------------------------------------------------------
    if clean:
        os.system("clear")
    else:
        print("\n" + "=" * 78)
        print(c("SHOT 9 · Production Metrics", "magenta"))
        print(c("SAY:", "cyan"), '"The service reports its own health..."')

    print(f"{c('user@samsung-dev:~/forge$', 'cyan')} curl -s {BASE_URL}/v1/metrics | jq .")
    res9 = get("/v1/metrics")
    print(json.dumps(res9, indent=2))
    wait_step("Demo complete! Switch back to slides.", auto, clean)

    if clean:
        os.system("clear")
        print(c("Finished terminal demo. Switch to presentation slide for wrap up!", "green"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Run automated test")
    parser.add_argument("--prompter", action="store_true", help="Show narration cues in terminal")
    args = parser.parse_args()
    run_demo(auto=args.test, prompter=args.prompter)
