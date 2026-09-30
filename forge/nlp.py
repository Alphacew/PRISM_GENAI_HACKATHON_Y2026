"""FORGE — NLP layer.

Three jobs, all CPU-only and dependency-light:

1. `normalize` / `intent_signature` — turn a raw colloquial complaint into a
   canonical intent key. This is the semantic cache key (guide S3.3: "cache keys
   must be semantically grounded") and the thing that stops cache fragmentation.
2. `Encoders` — pluggable text->vector. `HashedNgramEncoder` is pure numpy and
   always available; `StaticEmbeddingEncoder` uses model2vec (a distilled static
   lookup-table encoder, no transformer forward pass) when installed. The
   retrieval stack is written against the interface so both are measurable.
3. `BM25` — Okapi BM25 in numpy, for the lexical half of hybrid retrieval.
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter
from typing import Dict, Iterable, List, Sequence, Tuple

import numpy as np

# --------------------------------------------------------------------------- #
# Tokenisation
# --------------------------------------------------------------------------- #

_WORD = re.compile(r"[a-z0-9]+")

STOP = {
    "a", "an", "the", "my", "i", "me", "is", "are", "was", "were", "be", "been",
    "and", "or", "but", "if", "then", "so", "of", "to", "in", "on", "at", "for",
    "with", "from", "by", "as", "it", "its", "this", "that", "these", "those",
    "am", "do", "does", "did", "doing", "have", "has", "had", "can", "cant",
    "cannot", "could", "would", "should", "will", "just", "very", "really",
    "please", "help", "device", "phone", "device's", "im", "ive", "dont",
}


def tokens(text: str) -> List[str]:
    return _WORD.findall((text or "").lower())


def content_tokens(text: str) -> List[str]:
    return [t for t in tokens(text) if t not in STOP and len(t) > 1]


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", (text or "").lower())


# --------------------------------------------------------------------------- #
# Domain vocabulary: brand/model noise + colloquial -> canonical synonymy
# --------------------------------------------------------------------------- #

# Words that identify the handset, not the problem. Removing them is what lets
# "My Nexa Fold X1 screen goes black" and "tablet screen blank" collide.
DEVICE_NOISE = re.compile(
    r"\b(samsung|galaxy|nexa|techcorp|xiaomi|redmi|realme|oneplus|pixel|android|"
    r"a1[0-9]|a[0-9]{2}|s2[0-9]|x1|fold|flip|ultra|plus|lite|pro|max|tab|tablet|"
    r"smartphone|handset|mobile|5g|lte)\b",
    re.IGNORECASE,
)

# Colloquial symptom -> canonical problem atom. Cheap, auditable, and it is the
# reason a paraphrase lands on the same intent key without an LLM call.
SYNONYMS: Dict[str, str] = {
    # --- display
    "flicker": "flicker", "flickers": "flicker", "flickering": "flicker",
    "flashes": "flicker", "flashing": "flicker", "flash": "flicker",
    "blinking": "flicker", "strobe": "flicker", "strobing": "flicker",
    "blank": "blank", "black": "blank", "dark": "blank", "white": "blank",
    "dead": "blank", "nothing": "blank", "unresponsive": "blank",
    "cracked": "crack", "crack": "crack", "shattered": "crack", "broken": "crack",
    "distorted": "distort", "distortion": "distort", "garbled": "distort",
    "ghosting": "distort", "artifacts": "distort", "lines": "distort",
    "faded": "distort", "washed": "distort", "tint": "distort",
    "touch": "touch", "taps": "touch", "tapping": "touch",
    "laggy": "touch_lag", "unresponsive_touch": "touch_lag", "delayed": "touch_lag",
    "toch": "touch", "responsivenss": "touch", "responsiveness": "touch",
    "green": "tint_green", "pink": "tint_pink", "purple": "tint_pink",
    # --- power / battery
    "drain": "battery_drain", "drains": "battery_drain", "draining": "battery_drain",
    "dies": "battery_drain", "die": "battery_drain", "dead_battery": "battery_drain",
    "discharge": "battery_drain", "backup": "battery_life",
    "overheat": "overheat", "heating": "overheat", "hot": "overheat",
    "warm": "overheat", "temperature": "overheat",
    "charging": "charge", "charge": "charge", "charger": "charge",
    "charging_slow": "charge_slow", "plugged": "charge",
    # --- performance
    "slow": "slow", "sluggish": "slow", "lag": "slow", "freeze": "freeze",
    "freezes": "freeze", "frozen": "freeze", "stuck": "freeze", "hang": "freeze",
    "hangs": "freeze", "crash": "crash", "crashes": "crash", "restart": "reboot",
    "reboots": "reboot", "restarting": "reboot", "rebooting": "reboot",
    "storage": "storage_full", "memory": "storage_full", "space": "storage_full",
    # --- camera
    "camera": "camera", "blurry": "camera_focus", "blur": "camera_focus",
    "blurred": "camera_focus", "focus": "camera_focus", "grainy": "camera_quality",
    "noisy": "camera_quality", "pixelated": "camera_quality",
    # --- connectivity / software
    "email": "email", "mail": "email", "wifi": "wifi", "wi-fi": "wifi",
    "bluetooth": "bluetooth", "network": "network", "signal": "network",
    "internet": "network", "data": "network", "update": "update", "updated": "update",
    "upgrade": "update", "app": "app", "apps": "app", "application": "app",
    "install": "install", "installed": "install", "installing": "install",
    "download": "install", "downloaded": "install",
    "swipe": "swipe", "swipes": "swipe", "swiping": "swipe", "gesture": "swipe",
    "gestures": "swipe", "navigation": "swipe", "direction": "swipe",
    "settings": "settings", "seting": "settings", "option": "settings",
}

# Which subsystem a complaint belongs to (used for scoring/ordering only).
DOMAIN_OF: Dict[str, str] = {
    "flicker": "display", "blank": "display", "crack": "display",
    "distort": "display", "touch": "display", "touch_lag": "display",
    "tint_green": "display", "tint_pink": "display", "swipe": "display",
    "battery_drain": "battery", "battery_life": "battery", "overheat": "battery",
    "charge": "battery", "charge_slow": "battery",
    "slow": "performance", "freeze": "performance", "crash": "performance",
    "reboot": "performance", "storage_full": "performance",
    "camera": "camera", "camera_focus": "camera", "camera_quality": "camera",
    "email": "connectivity", "wifi": "connectivity", "bluetooth": "connectivity",
    "network": "connectivity", "update": "software", "app": "software",
    "install": "software",
}


def canonical_atoms(text: str) -> List[str]:
    """Colloquial text -> ordered, deduped canonical problem atoms."""
    toks = content_tokens(DEVICE_NOISE.sub(" ", _fold(text)))
    atoms: List[str] = []
    for t in toks:
        a = SYNONYMS.get(t)
        if a and a not in atoms:
            atoms.append(a)
    if not atoms:
        base = [t for t in toks if len(t) > 3][:4]
        atoms = base or ["generic"]
    return atoms


def domain_of(atoms: Sequence[str]) -> str:
    for a in atoms:
        if a in DOMAIN_OF:
            return DOMAIN_OF[a]
    return "other"


def intent_signature(text: str) -> str:
    """Stable semantic cache key: sorted atoms + domain + a coarse trigger marker.

    Sorted so word order does not fragment the cache ("screen flickers and goes
    blank" == "goes blank and the screen flickers").
    """
    atoms = canonical_atoms(text)
    trig = "trigger:app" if {"app", "install"} & set(atoms) else (
        "trigger:update" if "update" in atoms else "trigger:none"
    )
    return f"{domain_of(atoms)}::{'+'.join(sorted(set(atoms)))}::{trig}"


def normalize_query(text: str) -> str:
    """Display form of the cache key — used for exact-hash tier + query_variations."""
    atoms = sorted(set(canonical_atoms(text)))
    return " ".join(atoms)


# --------------------------------------------------------------------------- #
# Paraphrase generation (guide S4.1: 8-10 distinct registers, deterministic)
# --------------------------------------------------------------------------- #

_FORMAL = [
    "My device {s} and I would like assistance resolving this.",
    "{S} on my device after the recent change; please advise.",
    "I am experiencing the following issue: {s}.",
]
_CASUAL = [
    "so my {d} keeps {s} and its driving me crazy",
    "hey my {d} {s} out of nowhere, any fix?",
    "my {d} just {s}, started this morning",
]
_KEYWORD = ["{k}", "{k} fix", "how to fix {k}"]
_FRUSTRATED = [
    "this is so annoying, my {d} {s} and i cant do anything about it",
    "seriously why does my {d} {s} every single time, ive tried everything",
]
_TYPO = ["my {d} {s} and i cant figur out wat to doo", "device {s} pls halp"]


def _typo(word: str, rng: np.random.Generator) -> str:
    if len(word) < 4:
        return word
    i = int(rng.integers(1, len(word) - 1))
    if rng.random() < 0.5:
        return word[:i] + word[i + 1:]                      # deletion
    if i + 1 < len(word):
        return word[:i] + word[i + 1] + word[i] + word[i + 2:]  # transposition
    return word + word[-1]


def make_variations(
    raw_query: str, n_extra: int = 9, seed: int = 0
) -> Tuple[List[str], str]:
    """Return (variations, keyword_form). Deterministic given the seed.

    Variation 0 is always the raw complaint, so the set is 8-10 items and the
    caller can index the raw string too.
    """
    atoms = canonical_atoms(raw_query)
    keyword = " ".join(atoms)
    rng = np.random.default_rng(seed)
    out: List[str] = [raw_query.strip()]
    d = "phone"
    s_phrase = keyword.replace("+", " ")
    pools = [_FORMAL, _CASUAL, _KEYWORD, _FRUSTRATED, _TYPO]
    i = 0
    while len(out) < n_extra + 1 and i < 40:
        pool = pools[i % len(pools)]
        tmpl = pool[int(rng.integers(0, len(pool)))]
        cand = tmpl.format(s=s_phrase, S=s_phrase.capitalize(), d=d, k=keyword)
        cand = " ".join(_typo(w, rng) for w in cand.split()) if pool is _TYPO else cand
        if cand not in out:
            out.append(cand)
        i += 1
    return out[:10], keyword


# --------------------------------------------------------------------------- #
# Encoders
# --------------------------------------------------------------------------- #


class Encoder:
    """Interface: encode(list[str]) -> (n, d) float32, L2-normalised."""

    name = "base"
    dim = 0

    def encode(self, texts: Sequence[str]) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError


class HashedNgramEncoder(Encoder):
    """Character 3-5 gram hashing with sublinear IDF. Pure numpy, zero deps.

    A lexical dense vector: robust to morphology and typos ("dispay" still
    collides with "display") which is exactly what colloquial complaints need.
    """

    name = "hashed-char-ngram-3-5g"

    def __init__(self, dim: int = 4096, idf: np.ndarray | None = None):
        self.dim = dim
        self.idf = idf

    @staticmethod
    def _grams(text: str) -> List[str]:
        t = f" {_fold(text)} "
        t = re.sub(r"\s+", " ", t)
        gs = []
        for n in (3, 4, 5):
            gs.extend(t[i:i + n] for i in range(max(0, len(t) - n + 1)))
        return gs

    def _raw(self, texts: Sequence[str]) -> np.ndarray:
        m = np.zeros((len(texts), self.dim), dtype=np.float32)
        for r, text in enumerate(texts):
            for g in self._grams(text):
                h = int.from_bytes(hashlib.blake2b(g.encode(), digest_size=8).digest(), "big")
                m[r, h % self.dim] += 1.0
        return m

    def fit(self, corpus: Sequence[str]) -> "HashedNgramEncoder":
        m = self._raw(corpus)
        df = (m > 0).sum(axis=0)
        n = max(1, len(corpus))
        self.idf = np.log((n + 1) / (df + 1)).astype(np.float32) + 1e-6
        return self

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        m = self._raw(texts)
        if self.idf is not None:
            m = m * self.idf
        m = np.log1p(m)
        n = np.linalg.norm(m, axis=1, keepdims=True)
        return (m / np.maximum(n, 1e-9)).astype(np.float32)


class StaticEmbeddingEncoder(Encoder):
    """model2vec distilled static embeddings: a lookup table, ~microseconds/query.

    No transformer forward pass at inference, which is what makes a semantic
    cache viable inside a 300 ms budget on CPU. Falls back silently if absent.
    """

    name = "model2vec-static"

    def __init__(self, model_id: str = "minishlab/potion-base-8M"):
        from model2vec import StaticModel  # noqa: PLC0415

        self._m = StaticModel.from_pretrained(model_id)
        self.dim = int(self._m.dim)
        self.name = f"model2vec:{model_id}"

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        v = np.asarray(self._m.encode(list(texts)), dtype=np.float32)
        n = np.linalg.norm(v, axis=1, keepdims=True)
        return v / np.maximum(n, 1e-9)


def make_encoder(prefer_semantic: bool = True) -> Encoder:
    if prefer_semantic:
        try:
            return StaticEmbeddingEncoder()
        except Exception:
            pass
    return HashedNgramEncoder()


# --------------------------------------------------------------------------- #
# BM25
# --------------------------------------------------------------------------- #


class BM25:
    """Okapi BM25 over a pre-tokenised corpus. numpy, no external deps."""

    def __init__(self, corpus: Sequence[Sequence[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.corpus = [list(d) for d in corpus]
        self.n = len(self.corpus)
        self.doc_len = np.array([len(d) for d in self.corpus], dtype=np.float32)
        self.avgdl = float(self.doc_len.mean()) if self.n else 1.0
        self.tf: List[Counter] = [Counter(d) for d in self.corpus]
        df: Counter = Counter()
        for c in self.tf:
            df.update(c.keys())
        self.df = df
        self.vocab = {t: i for i, t in enumerate(df.keys())}
        self.idf = np.zeros(len(self.vocab), dtype=np.float32)
        for t, i in self.vocab.items():
            self.idf[i] = math.log(1 + (self.n - df[t] + 0.5) / (df[t] + 0.5))

    def scores(self, query: Sequence[str]) -> np.ndarray:
        s = np.zeros(self.n, dtype=np.float32)
        for t in query:
            i = self.vocab.get(t)
            if i is None:
                continue
            idf = self.idf[i]
            for d in range(self.n):
                f = self.tf[d].get(t, 0)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self.doc_len[d] / self.avgdl)
                s[d] += idf * f * (self.k1 + 1) / denom
        return s


def softmax_norm(x: np.ndarray) -> np.ndarray:
    if x.size == 0:
        return x
    x = x - x.max()
    e = np.exp(x)
    return e / max(e.sum(), 1e-9)
