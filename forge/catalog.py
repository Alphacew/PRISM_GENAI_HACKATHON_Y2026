"""FORGE — deeplink catalog and target resolution.

The catalog's URIs are opaque masked tokens, so the guide forbids matching on
them (S7.4). Everything here mines structure out of the *metadata* instead.

The shipped 578-entry catalog turns out to have a regular grammar, which is the
opening this module exploits:

    253  "Opens the <T> settings page in device Settings on the device."    open
    276  "(Enables|Disables) <T> via device Settings on the device."     enable/disable
     36  "Updates the <T> to a specified value via device Settings ..."    update
     10  "Diagnoses|Runs|Retrieves <T> ..."                               monitor
      1  the documented generic placeholder

<T> is the *target*: a settings screen or a toggleable setting. Three
consequences, each scored elsewhere in the guide:

1. Target extraction beats raw text similarity, because "Opens the Wi-Fi scanning
   settings page" and the step "Toggle on Wi-Fi scanning" share a target even
   when they share few words.
2. The enable/disable pairs carry explicit polarity in `originalType`
   (onURL/offURL). A step that toggles *on* must never resolve to the `off`
   entry; getting this right is also what makes the paired `validationDeeplink`
   value correct instead of guessed.
3. The `monitor` entries name whole symptoms ("Diagnoses excessive battery
   drain"), so they cover complaint domains that no single settings page does.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .contract import Condition, Deeplink, ResultTypes, ValidationDeepLink
from .nlp import BM25, Encoder, content_tokens

DUMMY_ID = "DL-DUMMY"
DUMMY_URI = "voiceassist://dummy_positive"

# --------------------------------------------------------------------------- #
# Step -> breadcrumb
# --------------------------------------------------------------------------- #

NAV_RE = re.compile(
    r"^\s*(?:navigate to and open|navigate to|go to|open|head to|swipe down .*? open)\s+(.+?)\.?\s*$",
    re.IGNORECASE,
)
TAP_RE = re.compile(
    r"^\s*(?:tap on|tap|touch and hold|touch|select|choose|press|click on|click)\s+(.+?)\.?\s*$",
    re.IGNORECASE,
)
TOGGLE_RE = re.compile(
    r"^\s*(?:toggle on|toggle off|turn on|turn off|enable|disable|switch on|switch off)\s+(.+?)\.?\s*$",
    re.IGNORECASE,
)

_TAIL_BOILER = re.compile(
    r"\s+(?:via|in|under)\s+(?:the\s+)?device\s+settings.*$", re.IGNORECASE
)
_ON_DEVICE = re.compile(r"\s+on\s+(?:the|your)\s+device.*$", re.IGNORECASE)
_VERB = re.compile(
    r"^(opens?|disables?|enables?|updates?|retrieves?|checks?|diagnoses?|configures?|runs?|"
    r"switches?|turns?)\s+(?:the\s+)?",
    re.IGNORECASE,
)
_FAMILY_OF_VERB = {
    "open": "open", "opens": "open",
    "enable": "enable", "enables": "enable",
    "disable": "disable", "disables": "disable",
    "update": "update", "updates": "update",
    "retrieve": "monitor", "retrieves": "monitor",
    "check": "monitor", "checks": "monitor",
    "diagnose": "monitor", "diagnoses": "monitor",
    "run": "monitor", "runs": "monitor",
    "configure": "open", "configures": "open",
    "switch": "update", "switches": "update",
    "turn": "update", "turns": "update",
}

_POLARITY_ON = re.compile(r"\b(toggle on|turn on|enable|switch on|activate|allow|grant)\b", re.I)
_POLARITY_OFF = re.compile(r"\b(toggle off|turn off|disable|switch off|deactivate|deny)\b", re.I)

GENERIC_SCOPE_RE = re.compile(
    r"^(?:device\s+)?settings$|^the settings$|^settings app$", re.I
)


def _clean_screen(name: str) -> str:
    s = (name or "").strip().rstrip(".")
    s = re.sub(r"^(the|your|device)\s+", "", s, flags=re.I)
    s = re.sub(r"\s+(on the device|on your device|screen|page)$", "", s, flags=re.I)
    return s.strip()


def target_signature(description: str) -> Tuple[str, str]:
    """(family, target) for a catalog entry.

    `family` is one of open | enable | disable | update | monitor. `target` is the
    settings screen or toggleable setting the entry operates on. An empty target
    means the entry identifies no specific screen.
    """
    d = _clean_screen(description or "")
    m = _VERB.match(d)
    family = _FAMILY_OF_VERB.get(m.group(1).lower(), "open") if m else "open"
    if m:
        d = d[m.end():]
    d = _TAIL_BOILER.sub("", d)
    d = _ON_DEVICE.sub("", d)
    d = re.sub(r"\s+settings page$", "", d, flags=re.I)
    d = re.sub(r"\s+settings$", "", d, flags=re.I)
    d = re.sub(r"\s+to a specified value$", "", d, flags=re.I)
    if family == "monitor":
        d = re.split(
            r"\s+to\s+(?:monitor|identify|clean|improve|suggest|ensure)\b", d, flags=re.I
        )[0]
    d = re.sub(r"^the\s+", "", d, flags=re.I).strip(" .")
    if GENERIC_SCOPE_RE.match(d) or d.lower() in {"", "device"}:
        return family, ""
    return family, d


def path_signature(steps: Sequence[str]) -> Tuple[List[str], str]:
    """Walk UI steps -> (breadcrumb, leaf). The breadcrumb is the screen path."""
    path: List[str] = []
    for s in steps:
        for rx, is_reset in ((NAV_RE, True), (TOGGLE_RE, False), (TAP_RE, False)):
            m = rx.match(s)
            if not m:
                continue
            target = _clean_screen(m.group(1))
            target = re.split(r"\s+(?:and then|then)\s+", target)[0]
            target = re.sub(r"\s+(?:option|options|menu)$", "", target, flags=re.I)
            if not target or len(target) > 48:
                continue
            low = target.lower()
            if is_reset:
                if path and path[0].lower() != low:
                    path = [target]
                else:
                    path.append(target)
            else:
                if path and low in {p.lower() for p in path}:
                    continue
                path.append(target)
            break
    leaf = path[-1] if path else ""
    return path, leaf


def step_polarity(steps: Sequence[str]) -> str:
    """enable | disable | none, read off the action's own wording."""
    blob = " ".join(steps)
    if _POLARITY_ON.search(blob):
        return "enable"
    if _POLARITY_OFF.search(blob):
        return "disable"
    return "none"


@dataclass
class CatalogEntry:
    id: str
    deeplink: str
    description: str
    message: str = ""
    qna_description: str = ""
    original_type: Optional[str] = None
    control_type: Optional[int] = None
    validation: Optional[Dict[str, str]] = None
    family: str = "open"
    leaf: str = ""
    is_generic: bool = False
    is_placeholder: bool = False
    index_text: str = ""
    tokens: List[str] = field(default_factory=list)

    @property
    def validation_uri(self) -> Optional[str]:
        if isinstance(self.validation, dict):
            return self.validation.get("deeplink")
        return None

    @property
    def validation_key(self) -> str:
        if isinstance(self.validation, dict):
            return self.validation.get("key") or ""
        return ""


class DeeplinkCatalog:
    """Index over the shipped catalog. Load once, resolve many."""

    def __init__(self, entries: List[CatalogEntry], encoder: Encoder):
        self.entries = entries
        self.encoder = encoder
        self.uris = {e.deeplink for e in entries}
        idx_texts = [e.index_text for e in entries]
        self.bm25 = BM25([e.tokens for e in entries])
        self.emb = encoder.encode(idx_texts)
        self.dummy = next((e for e in entries if e.is_placeholder), None)
        self._placeholder_mask = np.array([e.is_placeholder for e in entries])
        self._leaf_tokens = [set(content_tokens(e.leaf)) for e in entries]

    # -- loading ---------------------------------------------------------- #
    @classmethod
    def load(cls, path: str, encoder: Encoder) -> "DeeplinkCatalog":
        raw = json.load(open(path, "r", encoding="utf-8"))
        items = raw["deeplinks"] if isinstance(raw, dict) else raw
        out: List[CatalogEntry] = []
        for it in items:
            desc = it.get("description") or ""
            family, leaf = target_signature(desc)
            placeholder = (it.get("originalType") or "") == "placeholder"
            e = CatalogEntry(
                id=it.get("id") or "",
                deeplink=it.get("deeplink") or "",
                description=desc,
                message=it.get("message") or "",
                qna_description=it.get("qna_description") or "",
                original_type=it.get("originalType"),
                control_type=it.get("control_type"),
                validation=it.get("validation"),
                family=family,
                leaf=leaf,
                is_generic=(leaf == ""),
                is_placeholder=placeholder,
            )
            # the extracted target is the strongest matching signal, so it leads
            # the index text (doubled to weight it against the long prose fields)
            e.index_text = " ".join(
                x for x in (e.leaf, e.leaf, desc, e.message, e.qna_description) if x
            )
            e.tokens = content_tokens(e.index_text)
            out.append(e)
        return cls(out, encoder)

    # -- the resolver ----------------------------------------------------- #
    def resolve(
        self,
        steps: Sequence[str],
        action_name: str = "",
        top_k: int = 5,
        floor: float = 0.30,
        mode: str = "hybrid",
    ) -> Dict[str, object]:
        """Resolve an action's UI trajectory to a catalog entry.

        `mode` exists to make the ablation measurable rather than asserted:
        lexical (BM25 only), dense (embeddings only), hybrid, and
        hybrid_no_path (hybrid minus the target and polarity terms).
        """
        path, leaf = path_signature(steps)
        polarity = step_polarity(steps)
        query = " ".join([action_name, leaf, " ".join(steps)])
        q_tokens = content_tokens(query)

        lex = self.bm25.scores(q_tokens)
        lex = lex / (lex.max() + 1e-9) if lex.size and lex.max() > 0 else lex
        qv = self.encoder.encode([query])[0]
        dense = (self.emb @ qv + 1.0) / 2.0          # cosine -> [0,1]

        rel = 0.55 * lex + 0.45 * dense
        if mode == "lexical":
            rel = lex
        elif mode == "dense":
            rel = dense
        rel = np.where(self._placeholder_mask, -1.0, rel)

        leaf_toks = set(content_tokens(leaf))
        path_toks = set(content_tokens(" ".join(path)))
        action_toks = set(content_tokens(action_name))
        factor = np.ones(len(self.entries), dtype=np.float32)
        for i, e in enumerate(self.entries):
            if e.is_placeholder:
                factor[i] = 1.0
                continue
            if mode == "hybrid_no_path":
                factor[i] = 1.0
                continue
            e_leaf = self._leaf_tokens[i]
            if e_leaf:
                overlap = len(e_leaf & leaf_toks) / len(e_leaf)
                path_overlap = len(e_leaf & path_toks) / len(e_leaf)
                name_overlap = len(e_leaf & action_toks) / len(e_leaf)
            else:
                overlap = path_overlap = name_overlap = 0.0

            # 1. is this the right kind of operation on the target?
            f = 1.0
            if polarity != "none" and e.family in {"enable", "disable"}:
                f = 1.25 if e.family == polarity else 0.55
            elif e.family in {"enable", "disable"}:
                f = 0.90
            elif e.family == "monitor":
                f = 1.10

            # 2. does it point at the screen the trajectory ends on?
            if e_leaf:
                if max(overlap, name_overlap) >= 0.6:
                    f *= 1.35
                elif path_overlap >= 0.5:
                    f *= 1.15
                elif len(path) >= 2 and max(overlap, path_overlap, name_overlap) < 0.2:
                    f *= 0.55
            elif e.is_generic and len(path) >= 2:
                f *= 0.40                     # parent-menu entry, but path is deep
            factor[i] = f

        score = rel * factor
        # The placeholder must never win the ranking; set this on the score, not on
        # rel and factor separately (a -1 in both made it the argmax).
        score = np.where(self._placeholder_mask, -1.0, score)
        order = np.argsort(-score, kind="stable")[:top_k]
        best = self.entries[int(order[0])] if len(order) else None
        best_score = float(score[int(order[0])]) if len(order) else 0.0
        margin = (
            float(score[int(order[0])] - score[int(order[1])]) if len(order) > 1 else 0.0
        )
        basis = (
            f"path={' > '.join(path) or 'n/a'}; leaf='{leaf}'; polarity={polarity}; "
            f"rel={float(rel[int(order[0])]):.3f}; factor={float(factor[int(order[0])]):.2f}; "
            f"picked={(best.id + '(' + best.family + ':' + best.leaf[:32] + ')') if best else 'none'}"
        )

        if best is None or best.is_placeholder or best_score < floor:
            return {"entry": None, "score": best_score, "margin": margin, "leaf": leaf,
                    "path": path, "polarity": polarity,
                    "basis": basis + f"; below floor {floor} -> generic placeholder",
                    "generic": True, "accepted": False, "overlap": 0.0}

        # Precision gate. Emitting a *specific* deeplink is a claim about which
        # screen the user will land on, so the resolver must be able to say why.
        # If the winning entry names no target, or its target shares no vocabulary
        # with the trajectory, we decline and use the documented placeholder
        # instead of guessing. This is what keeps "screen resolution accuracy"
        # high while coverage drops honestly.
        best_leaf = self._leaf_tokens[int(order[0])]
        if not best_leaf:
            target_ok = False
            why = "winner names no target"
        else:
            ov = max(
                len(best_leaf & leaf_toks) / len(best_leaf),
                len(best_leaf & path_toks) / len(best_leaf),
                len(best_leaf & action_toks) / len(best_leaf),
            )
            need = 0.34 if best.family == "monitor" else 0.5
            target_ok = ov >= need
            why = f"target overlap {ov:.2f} vs needed {need:.2f}"
        if not target_ok:
            return {"entry": None, "score": best_score, "margin": margin, "leaf": leaf,
                    "path": path, "polarity": polarity,
                    "basis": basis + f"; declined ({why}) -> generic placeholder",
                    "generic": True, "accepted": False, "overlap": 0.0}
        return {"entry": best, "score": best_score, "margin": margin, "leaf": leaf,
                "path": path, "polarity": polarity,
                "basis": basis + f"; accepted ({why})", "generic": False,
                "accepted": True, "overlap": float(ov)}

    # -- builders --------------------------------------------------------- #
    def to_actionable(self, entry: CatalogEntry) -> Deeplink:
        return Deeplink(
            deeplink=entry.deeplink,
            description=entry.description,
            message=entry.message,
            originalType=entry.original_type,
        )

    def to_generic(self, leaf: str, fallback_text: str) -> Deeplink:
        """`voiceassist://dummy_positive` with authored, contract-legal copy.

        The catalog's own placeholder entry instructs exactly this: write the
        description and message ourselves (5-7 words, naming the concrete screen).
        """
        return Deeplink(
            deeplink=DUMMY_URI,
            description=fallback_text,
            message=f"Open {leaf}" if leaf else "Open the relevant Settings screen",
            originalType="placeholder",
        )

    def to_validation(
        self, entry: CatalogEntry, value: Optional[str] = None
    ) -> Optional[ValidationDeepLink]:
        uri = entry.validation_uri
        if not uri:
            return None
        if value is None:
            # the entry's own polarity is ground truth for the assertion
            value = "False" if entry.family == "disable" else "True"
        return ValidationDeepLink(
            deeplink=uri,
            key=entry.validation_key or entry.message or "state",
            resultType=ResultTypes.boolean if value in {"True", "False"} else ResultTypes.string,
            condition=Condition.equal,
            value=value,
        )
