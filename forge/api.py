"""FORGE — HTTP surface.

POST /v1/troubleshoot   complaint (+ optional SIIS text) -> actionable plan
GET  /health            readiness: atlas, index and encoder all warm
GET  /v1/metrics        live latency / hit-rate / cost counters

The service is stateless except for the in-process compile-on-miss overlay, which
is deliberately session-scoped (guide Theme 05 shares this constraint family:
no cross-session caching). The atlas on disk is the only durable state.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .serve import Engine

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ATLAS = os.path.join(_HERE, "atlas", "current")
DEFAULT_CATALOG = os.path.join(_HERE, "data", "deeplinks.json")

app = FastAPI(title="FORGE — Smart Guided Troubleshooting Engine", version="1.0.0")

_state: dict = {"engine": None, "ready": False, "error": None, "boot_ms": None}


class TroubleshootRequest(BaseModel):
    query: str
    siis_response: Optional[str] = None


@app.on_event("startup")
def _startup() -> None:
    t0 = time.perf_counter()
    try:
        _state["engine"] = Engine(
            os.environ.get("FORGE_ATLAS", DEFAULT_ATLAS),
            os.environ.get("FORGE_CATALOG", DEFAULT_CATALOG),
        )
        _state["ready"] = True
    except Exception as exc:  # pragma: no cover - reported through /health
        _state["error"] = f"{type(exc).__name__}: {exc}"
        _state["ready"] = False
    _state["boot_ms"] = round((time.perf_counter() - t0) * 1000, 1)


@app.get("/health")
def health():
    body = {
        "status": "ok" if _state["ready"] else "degraded",
        "atlas_loaded": bool(_state["ready"]),
        "boot_ms": _state["boot_ms"],
    }
    if _state["error"]:
        body["error"] = _state["error"]
    code = 200 if _state["ready"] else 503
    return JSONResponse(body, status_code=code)


@app.get("/v1/metrics")
def metrics():
    eng = _state.get("engine")
    if eng is None:
        return JSONResponse({"error": "engine not ready"}, status_code=503)
    return eng.metrics()


@app.post("/v1/troubleshoot")
def troubleshoot(req: TroubleshootRequest):
    eng = _state.get("engine")
    if eng is None:
        return JSONResponse({"error": "engine not ready"}, status_code=503)
    if not req.query or not req.query.strip():
        return JSONResponse(
            {"query": req.query or "", "query_variations": [],
             "response": {"contexts": [], "fallback": "empty_query"},
             "meta": {"latency_ms": 0, "cache_hit": False, "model": "none(rules)",
                      "cost_usd": 0.0}},
            status_code=200,
        )
    env = eng.troubleshoot(req.query, req.siis_response)
    return JSONResponse(env.model_dump())

_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html")


@app.get("/", include_in_schema=False)
def ui():
    """Demo UI: a thin browser client over /v1/troubleshoot and /v1/metrics."""
    return FileResponse(_STATIC)


@app.get("/v1/examples")
def examples():
    """One sample query per compiled plan (drawn from its alias set), for the demo UI."""
    import json
    path = os.path.join(os.environ.get("FORGE_ATLAS", DEFAULT_ATLAS), "atlas.json")
    try:
        with open(path, encoding="utf-8") as fh:
            records = json.load(fh).get("records", [])
    except Exception:
        return {"examples": []}
    def natural(q: str) -> int:
        # prefer conversational phrasings; fall back to the shortest usable one
        return 0 if q.startswith(("hey my phone", "my phone just")) else 1

    out, seen = [], set()
    for rec in records:
        title = rec.get("goal", {}).get("title", "")
        cands = [q for q in rec.get("variations", []) if len(q) >= 12 and "_" not in q]
        if title in seen or not cands:
            continue
        out.append({"title": title, "query": sorted(cands, key=lambda q: (natural(q), len(q)))[0]})
        seen.add(title)
        if len(out) >= 6:
            break
    return {"examples": out}
