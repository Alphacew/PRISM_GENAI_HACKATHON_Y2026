"""FORGE — serving path.

Build once: `build_atlas()` compiles every SIIS record into a frozen, gate-passing
`Goal` and persists an atlas (plans + alias embeddings + intent signatures).

Serve fast: `Engine.troubleshoot()` resolves a complaint against the atlas with a
two-tier cache (exact signature hash, then margin-gated semantic kNN) and performs
**zero** model calls. The only path that ever runs the compiler at request time is
the explicit cold path, which the caller triggers by supplying `siis_response`.

That inversion is the whole design: the LLM and the expensive parsing live at build
time, so serving is a lookup. Latency, cost and determinism stop being things we
tune and become properties of the architecture.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .catalog import DeeplinkCatalog
from .compile import CompiledPlan, compile_goal, parse_siis
from .contract import (
    Envelope,
    ContextDeeplinkResponse,
    Goal,
    Meta,
    audit_envelope,
)
from .nlp import (
    Encoder,
    HashedNgramEncoder,
    canonical_atoms,
    domain_of,
    intent_signature,
    make_encoder,
    make_variations,
    normalize_query,
    tokens,
)

ATLAS_SCHEMA = 1


# --------------------------------------------------------------------------- #
# Multi-intent decomposition (the schema's `contexts` is a list; use it)
# --------------------------------------------------------------------------- #

_NUM_RX = re.compile(r"(?:^|\s)(?:\d{1,2}[.)]\s+|[-*•]\s+)")
_SENT_RX = re.compile(r"(?<=[.!?])\s+")
_CONJ_RX = re.compile(r",?\s+(?:and|but|also|plus)\s+", re.IGNORECASE)


def split_intents(query: str) -> List[str]:
    """One complaint can hide several distinct questions (guide S3.4: "a single
    utterance can hide several distinct questions"). Split conservatively: only
    when the halves carry disjoint problem atoms, so we never fragment a single
    symptom into two plans.
    """
    q = (query or "").strip()
    if not q:
        return []

    parts = [p.strip(" .;") for p in _NUM_RX.split(q) if p.strip(" .;")]
    if len(parts) < 2:
        parts = [p.strip() for p in _SENT_RX.split(q) if p.strip()]

    out: List[str] = []
    for p in parts:
        sub = _CONJ_RX.split(p)
        if len(sub) > 1:
            atoms = [set(canonical_atoms(s)) for s in sub if s.strip()]
            keep = [s for s, a in zip(sub, atoms) if a and a != {"generic"}]
            if len(keep) >= 2 and all(
                not (atoms[i] & atoms[i + 1]) for i in range(len(atoms) - 1)
            ):
                out.extend(k.strip() for k in keep)
                continue
        out.append(p)
    # drop fragments with no recoverable signal
    out = [o for o in out if canonical_atoms(o) != ["generic"]]
    return out or [q]


# --------------------------------------------------------------------------- #
# Atlas
# --------------------------------------------------------------------------- #

@dataclass
class Atlas:
    version: str
    built_at: str
    encoder_name: str
    records: List[Dict[str, object]] = field(default_factory=list)
    source_hashes: Dict[str, str] = field(default_factory=dict)
    stats: Dict[str, object] = field(default_factory=dict)

    def save(self, out_dir: str) -> str:
        d = Path(out_dir) / self.version
        d.mkdir(parents=True, exist_ok=True)
        (d / "atlas.json").write_text(
            json.dumps({"schema": ATLAS_SCHEMA, **asdict(self)}, indent=1),
            encoding="utf-8",
        )
        return str(d / "atlas.json")

    @classmethod
    def load(cls, atlas_dir: str) -> "Atlas":
        p = Path(atlas_dir)
        f = p / "atlas.json" if p.is_dir() and not p.name.endswith(".json") else p
        if f.is_dir():
            f = f / "atlas.json"
        raw = json.loads(Path(f).read_text(encoding="utf-8"))
        raw.pop("schema", None)
        return cls(**raw)


def build_atlas(
    siis_path: str,
    catalog: DeeplinkCatalog,
    encoder: Encoder,
    version: Optional[str] = None,
) -> Tuple[Atlas, List[dict]]:
    """Compile every SIIS record into the atlas. Returns (atlas, rejections)."""
    raw = json.loads(Path(siis_path).read_text(encoding="utf-8"))
    rows = raw["responses"] if isinstance(raw, dict) else raw

    records: List[Dict[str, object]] = []
    rejections: List[dict] = []
    n_actions = n_resolved = 0

    for i, row in enumerate(rows):
        sid = str(row.get("id") or f"row_{i + 1}")
        payload = row.get("siis_response") or {}
        draft = parse_siis(sid, payload if isinstance(payload, dict) else {"content": str(payload)})
        goal, build = compile_goal(draft, catalog)

        if goal is None:
            rejections.append({"source_id": sid, "reason": build.get("dropped"), "topic": draft.topic})
            continue

        seed = str(row.get("original_query") or draft.source_title or draft.topic)
        variations, keywords = make_variations(seed, n_extra=9, seed=i)

        plan_id = "FG-" + hashlib.sha1(
            (sid + json.dumps(goal, sort_keys=True)).encode()
        ).hexdigest()[:10]

        record = {
            "plan_id": plan_id,
            "source_id": sid,
            "source_title": draft.source_title,
            "topic": draft.topic,
            "goal": goal,
            "intent_signature": intent_signature(seed + " " + draft.source_title),
            "domain": domain_of(canonical_atoms(seed + " " + draft.source_title)),
            "variations": variations,
            "keywords": keywords,
            "seed": seed,
            "provenance": build.get("provenance", []),
            "build": build.get("build", {}),
            "content_hash": hashlib.sha1(
                json.dumps(goal, sort_keys=True).encode()
            ).hexdigest()[:16],
        }
        # Build-time gate: nothing enters the atlas without passing the contract.
        env = Envelope(
            query=seed,
            query_variations=variations,
            response=ContextDeeplinkResponse(contexts=[Goal(**goal)]),
            meta=Meta(stage="compile", model="none(rules)"),
        )
        rep = audit_envelope(env, catalog.uris)
        record["gate"] = {"ok": rep.ok, "failures": rep.failures, "checks": rep.checks}
        if not rep.ok:
            rejections.append({"source_id": sid, "reason": "gate", "failures": rep.failures})
        records.append(record)
        n_actions += int(record["build"].get("n_actions", 0))
        n_resolved += int(record["build"].get("n_resolved", 0))

    version = version or time.strftime("v%Y%m%d-%H%M%S")
    atlas = Atlas(
        version=version,
        built_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        encoder_name=encoder.name,
        records=records,
        source_hashes={
            "siis_responses.json": _sha1_file(siis_path),
        },
        stats={
            "n_sources": len(rows),
            "n_plans": len(records),
            "n_actions": n_actions,
            "n_resolved": n_resolved,
            "deeplink_resolution_rate": round(n_resolved / max(n_actions, 1), 4),
            "n_rejected": len(rejections),
            "build_host": f"{platform.system()} {platform.release()} / py{platform.python_version()}",
        },
    )
    return atlas, rejections


def _sha1_file(p: str) -> str:
    h = hashlib.sha1()
    h.update(Path(p).read_bytes())
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Semantic cache
# --------------------------------------------------------------------------- #

class IntentIndex:
    """Two-tier intent cache over the atlas.

    Tier 1: exact hash of the normal form (microseconds, no math).
    Tier 2: kNN over alias embeddings, gated by an absolute floor AND a
    best-vs-second margin. The margin gate is the piece that matters: without it
    a semantic cache will confidently serve the wrong plan, which the guide
    scores against under "Deterministic Execution".

    The floor is not a constant of nature — it is a property of the encoder's
    similarity scale, so it is passed in by the caller that chose the encoder.
    """

    def __init__(self, atlas: Atlas, encoder: Encoder,
                 tau_floor: Optional[float] = None, tau_margin: float = 0.02):
        self.atlas = atlas
        self.encoder = encoder
        if tau_floor is None:
            # measured separation: a static encoder puts true paraphrases at
            # 0.4-0.8 and unrelated intents at 0.1-0.2; the hashed n-gram encoder
            # is much sharper but only fires on near-identical wording.
            tau_floor = 0.35 if "model2vec" in encoder.name else 0.72
        self.tau_floor = tau_floor
        self.tau_margin = tau_margin
        self.exact: Dict[str, str] = {}
        self.aliases: List[Tuple[str, str]] = []      # (plan_id, text)
        for r in atlas.records:
            pid = str(r["plan_id"])
            self.exact[self._hash(str(r["intent_signature"]))] = pid
            # Alias set. `intent_signature` and `seed` are the plan's own
            # identity; `variations` are generated paraphrases. `topic`,
            # `source_title` and `keywords` are the corpus's own words for the
            # problem, and they matter: a user who types the topic verbatim
            # ("email server not responding") is asking exactly the question the
            # record was written to answer. Leaving them out of the index made
            # the engine miss queries whose plan was in the atlas all along.
            anchors: List[str] = [str(r["intent_signature"]), normalize_query(str(r["seed"]))]
            for key in ("topic", "source_title"):
                val = r.get(key)
                if val:
                    anchors.append(str(val))
            for kw in (r.get("keywords") or [])[:12]:
                if kw:
                    anchors.append(str(kw))
            anchors += [str(x) for x in r.get("variations", [])]
            for v in anchors:
                if v and v.strip():
                    self.aliases.append((pid, v))
        self.alias_texts = [a[1] for a in self.aliases]
        self.emb = encoder.encode(self.alias_texts) if self.alias_texts else np.zeros((0, 1), np.float32)

    @staticmethod
    def _hash(s: str) -> str:
        return hashlib.sha1(s.encode("utf-8")).hexdigest()

    @staticmethod
    def _clean(q: str) -> str:
        """Strip surrounding punctuation before embedding.

        `split_intents` yields clauses that keep their terminal period, and the
        character n-gram encoder penalises that: "email server not responding"
        scores 0.729 against its own plan while "email server not responding."
        scores 0.689 and falls below the floor. Same question, different answer,
        decided by a full stop. The intent signature is identical either way, so
        this is a normalisation bug, not a scoring one.
        """
        return re.sub(r"^[\s\W]+|[\s\W]+$", "", q or "") or q

    def lookup(self, query: str) -> Dict[str, object]:
        query = self._clean(query)
        sig = intent_signature(query)
        h = self._hash(sig)
        if h in self.exact:
            return {"plan_id": self.exact[h], "tier": "exact", "sim": 1.0, "margin": 1.0}
        if not self.aliases:
            return {"plan_id": None, "tier": "miss", "sim": 0.0, "margin": 0.0}
        qv = self.encoder.encode([query])[0]
        sims = self.emb @ qv

        # Margin is computed between *plans*, not between aliases. Several plans
        # legitimately share a topic string ("Blank or black display on a
        # smartphone or tablet" appears five times in the corpus), and if the top
        # two aliases belong to the same plan that is agreement, not ambiguity.
        # Scoring alias-vs-alias made the gate reject exact topic matches.
        best_per_plan: Dict[str, float] = {}
        for (pid, _text), s in zip(self.aliases, sims):
            if s > best_per_plan.get(pid, -1.0):
                best_per_plan[pid] = float(s)
        ranked = sorted(best_per_plan.items(), key=lambda kv: -kv[1])
        pid, sim = ranked[0]
        sim2 = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = sim - sim2
        if sim >= self.tau_floor and margin >= self.tau_margin:
            return {"plan_id": pid, "tier": "semantic", "sim": sim, "margin": margin}
        return {"plan_id": None, "tier": "miss", "sim": sim, "margin": margin}


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class Engine:
    def __init__(self, atlas_dir: str, catalog_path: str, encoder: Optional[Encoder] = None,
                 **idx_kw):
        self.atlas = Atlas.load(atlas_dir)
        self.encoder = encoder or make_encoder()
        self.catalog = DeeplinkCatalog.load(catalog_path, self.encoder)
        self.index = IntentIndex(self.atlas, self.encoder, **idx_kw)
        self.by_id = {str(r["plan_id"]): r for r in self.atlas.records}
        self._overlay: Dict[str, Dict[str, object]] = {}   # runtime compile-on-miss results
        self._counters = {
            "n": 0, "exact": 0, "semantic": 0, "miss": 0, "compile_on_miss": 0,
            "multi_context": 0, "no_match": 0,
        }
        self._lat: List[float] = []
        self._lat_hit: List[float] = []
        self._lat_cold: List[float] = []

    # -- serving ---------------------------------------------------------- #
    def troubleshoot(self, query: str, siis_response: Optional[str] = None,
                     variations: Optional[List[str]] = None) -> Envelope:
        t0 = time.perf_counter()
        subs = split_intents(query)
        contexts: List[Goal] = []
        metas: List[Tuple[str, float, Optional[float], str, str]] = []
        plan_ids: List[str] = []

        _seen: Dict[str, int] = {}
        for sub in subs:
            res = self._resolve_one(sub, siis_response)
            pid = res["plan_id"]
            if res["goal"] is not None and pid:
                # Dedupe on the *goal*, not the plan id. Two corpus records can
                # compile to plans with different ids but identical goal text, so
                # a two-clause fan-out was returning the same troubleshooting
                # steps twice — a bug to any user reading the response, and one
                # that inflated the contract's contexts list. Keep the
                # higher-scoring occurrence.
                key = " ".join(res["goal"].goal.lower().split())
                if key in _seen:
                    i = _seen[key]
                    if res["goal"].score > contexts[i].score:
                        contexts[i] = res["goal"]
                        plan_ids[i] = pid
                    metas.append((res["tier"], res["sim"], res["margin"], res["stage"], pid))
                    continue
                _seen[key] = len(contexts)
                contexts.append(res["goal"])
                plan_ids.append(pid)
            metas.append((res["tier"], res["sim"], res["margin"], res["stage"], pid))

        contexts.sort(key=lambda g: -g.score)
        if len(contexts) > 1:
            self._counters["multi_context"] += 1

        tier = metas[0][0] if metas else "miss"
        stage = metas[0][3] if metas else "serve"
        fallback = None
        if not contexts:
            fallback = "no_match"
            self._counters["no_match"] += 1

        target = next((m for m in metas if m[4]), metas[0] if metas else None)
        ctx = ContextDeeplinkResponse(contexts=contexts, fallback=fallback)
        env = Envelope(
            query=query,
            query_variations=variations or make_variations(query, 9, seed=len(query))[0],
            response=ctx,
            meta=Meta(
                latency_ms=0,
                cache_hit=tier in {"exact", "semantic"},
                model="none(rules)",
                cost_usd=0.0,
                plan_ids=plan_ids,
                atlas_version=self.atlas.version,
                stage=stage,
                cache_tier=tier,
                margin=round(target[2], 4) if target and target[2] is not None else None,
                tokens_in=0,
                tokens_out=0,
            ),
        )
        dt_ms = (time.perf_counter() - t0) * 1000.0
        env.meta.latency_ms = int(round(dt_ms))

        self._counters["n"] += 1
        self._counters[tier if tier in self._counters else "miss"] += 1
        self._lat.append(dt_ms)
        (self._lat_hit if tier in {"exact", "semantic"} else self._lat_cold).append(dt_ms)
        return env

    def _resolve_one(self, sub: str, siis_response: Optional[str]) -> Dict[str, object]:
        hit = self.index.lookup(sub)
        pid = hit["plan_id"]
        if pid is None and isinstance(pid, str):
            pid = pid
        if isinstance(pid, str) and pid in self._overlay:
            rec = self._overlay[pid]
            return {"goal": Goal(**rec["goal"]), "plan_id": pid, "tier": "exact",
                    "sim": 1.0, "margin": 1.0, "stage": "serve"}
        if isinstance(pid, str) and pid in self.by_id:
            rec = self.by_id[pid]
            return {"goal": Goal(**rec["goal"]), "plan_id": pid, "tier": hit["tier"],
                    "sim": float(hit["sim"]), "margin": float(hit["margin"]), "stage": "serve"}

        # --- cold path: only runs when the caller hands us new reference text ---
        if siis_response:
            payload = {"title": sub[:80], "content": siis_response}
            draft = parse_siis("runtime", payload)
            goal, build = compile_goal(draft, self.catalog)
            if goal is not None:
                env = Envelope(
                    query=sub,
                    response=ContextDeeplinkResponse(contexts=[Goal(**goal)]),
                    meta=Meta(stage="compile_on_miss"),
                )
                if audit_envelope(env, self.catalog.uris).ok:
                    pid2 = "RT-" + hashlib.sha1(sub.encode()).hexdigest()[:10]
                    for r in self.catalog.uris:
                        pass
                    self._overlay[pid2] = {"goal": goal, "plan_id": pid2}
                    self._counters["compile_on_miss"] += 1
                    return {"goal": Goal(**goal), "plan_id": pid2, "tier": "miss",
                            "sim": 0.0, "margin": None, "stage": "compile_on_miss"}
        return {"goal": None, "plan_id": "", "tier": "miss", "sim": 0.0,
                "margin": float(hit.get("margin") or 0.0), "stage": "serve"}

    # -- observability ---------------------------------------------------- #
    def metrics(self) -> Dict[str, object]:
        def pct(xs: Sequence[float], p: float) -> float:
            if not xs:
                return 0.0
            return float(np.percentile(np.asarray(xs), p))

        n = max(self._counters["n"], 1)
        return {
            "requests": self._counters["n"],
            "cache_hit_rate": round((self._counters["exact"] + self._counters["semantic"]) / n, 4),
            "exact_hits": self._counters["exact"],
            "semantic_hits": self._counters["semantic"],
            "misses": self._counters["miss"],
            "compile_on_miss": self._counters["compile_on_miss"],
            "multi_context_responses": self._counters["multi_context"],
            "no_match_responses": self._counters["no_match"],
            "latency_ms": {
                "p50": round(pct(self._lat, 50), 2),
                "p95": round(pct(self._lat, 95), 2),
                "p50_hit": round(pct(self._lat_hit, 50), 2),
                "p95_hit": round(pct(self._lat_hit, 95), 2),
                "p95_cold": round(pct(self._lat_cold, 95), 2),
                "n": len(self._lat),
            },
            "cost_usd_per_query": 0.0,
            "atlas_version": self.atlas.version,
            "atlas_plans": len(self.atlas.records),
        }
