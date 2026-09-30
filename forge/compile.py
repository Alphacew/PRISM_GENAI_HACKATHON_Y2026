"""FORGE — the plan compiler.

Build-time pipeline. SIIS reference text goes in; frozen, gate-passing `Goal`
artifacts come out. This is the module that makes the serving path LLM-free:
structure extraction, constraint enforcement and deeplink resolution all happen
here, once, and the result is persisted.

Design rule: nothing reaches the atlas without passing `contract.audit_envelope`.
A failure at build time is a bug report; a failure at serve time is an incident.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .catalog import (
    NAV_RE,
    TAP_RE,
    DeeplinkCatalog,
    _clean_screen,
    path_signature,
)
from .nlp import canonical_atoms, content_tokens, domain_of, make_variations

# --------------------------------------------------------------------------- #
# Lexicons
# --------------------------------------------------------------------------- #

UI_VERBS = {
    "navigate", "open", "tap", "touch", "select", "choose", "press", "click",
    "toggle", "turn", "enable", "disable", "switch", "go", "swipe", "scroll",
    "check", "verify", "ensure", "confirm", "clear", "reset", "restart",
    "reboot", "remove", "uninstall", "update", "install", "connect", "disconnect",
    "sign", "log", "enter", "set", "change", "adjust", "drag", "hold", "slide",
    "wait", "allow", "grant", "deny", "unpair", "pair", "format", "back",
}

CRITICAL_RX = re.compile(
    r"\b(factory\s*reset|hard\s*reset|wipe|erase\s+all|format\s+(the\s+)?device|"
    r"safe\s*mode|firmware|software\s*update|reboot|restart\s+(the\s+)?(phone|device)|"
    r"reset\s+(all\s+)?settings|clear\s+all\s+data)\b",
    re.IGNORECASE,
)
MANUAL_RX = re.compile(
    r"\b(clean|wipe\s+with|cloth|brush|dry|port|pin|sim\s+tray|injector|"
    r"replace|hardware|service\s+cent(er|re)|technician|visit\s+an?\s+authorized|"
    r"contact\s+(customer\s+)?(support|care)|bring\s+the\s+device|physical)\b",
    re.IGNORECASE,
)
SAFE_RX = re.compile(
    r"\b(settings|toggle|switch|enable|disable|permission|permissions|"
    r"notification|notifications|brightness|volume|language|font|display|"
    r"network\s+mode|airplane)\b",
    re.IGNORECASE,
)

STEP_SPLIT_RX = re.compile(r",?\s+(?:and\s+then|then|and)\s+|,\s+(?=[A-Z][a-z])", re.IGNORECASE)
SECTION_RX = re.compile(r"^#{2,3}(?!#)\s*(?:Step\s*\d+\s*[:.\-]?\s*)?(?P<h>.+?)\s*$", re.M)
MODE_RX = re.compile(r"^\s*(?:To|For|In order to)\s+(?P<h>.+?)\s*:\s*$", re.IGNORECASE)
PREAMBLE_RX = re.compile(r"^#(?!#)\s*(?:Troubleshooting\s+)?(?P<h>.+?)\s*$", re.M)

RANK = {"auto": 0, "manual": 1, "critical": 2}


# --------------------------------------------------------------------------- #
# Phrase hygiene
# --------------------------------------------------------------------------- #

def _sentence(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    s = re.sub(r"^(?:Alternatively|Also|Then|Now|Next|Finally|After that|First|Second|Third)[,\s]+",
               "", s, flags=re.IGNORECASE)
    s = s.strip()
    if not s:
        return ""
    if not s.endswith((".", "!", "?", ":")):
        s += "."
    return s[0].upper() + s[1:]


def _imperative(line: str) -> str:
    """Rewrite a descriptive instruction into one imperative UI step."""
    s = line.strip().rstrip(".")
    s = re.sub(r"^(?:you (?:can|should|will|may|need to)\s+)", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^(?:try|please)\s+(?:to\s+)?", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^(?:go ahead and|simply|just)\s+", "", s, flags=re.IGNORECASE)
    # "go to Settings, tap Connections, and then tap Wi-Fi" -> first clause only;
    # callers split on STEP_SPLIT_RX before reaching here.
    if not s:
        return ""
    first = s.split()[0].lower().rstrip(",")
    if first not in UI_VERBS and first not in {"navigate", "ensure", "verify"}:
        # not an instruction we can replay - drop it rather than paraphrase it
        return ""
    return _sentence(s)


def _group_label(leaf: str) -> str:
    tok = [t for t in re.split(r"\s+", (leaf or "").strip()) if t][:4]
    out = " ".join(tok) or "Screen"
    out = _titlecase(out)
    return out if out else "Screen"


def _partition_by_screen(steps: Sequence[str]) -> List[List[str]]:
    """Walk the breadcrumb over an action's steps and cut on branch changes."""
    path: List[str] = []
    groups: List[List[str]] = [[]]
    for st in steps:
        m_nav = NAV_RE.match(st)
        m_tap = TAP_RE.match(st)
        target = _clean_screen((m_nav or m_tap).group(1)) if (m_nav or m_tap) else ""
        target = re.split(r"\s+(?:and then|then)\s+", target)[0].strip() if target else ""
        if m_nav and target:
            low = target.lower()
            if path and low == path[0].lower():
                path = [target]                      # restart, same branch: no cut
            elif path and low not in [p.lower() for p in path]:
                if len(groups[-1]) >= 3:
                    groups.append([])                # genuinely new branch
                path = [target]
            else:
                path.append(target)
        elif m_tap and target:
            low = target.lower()
            hit = next((i for i, p in enumerate(path) if p.lower() == low), None)
            if hit is not None:
                path = path[: hit + 1]
            else:
                path.append(target)
        groups[-1].append(st)
    return [g for g in groups if g]


def _is_ui_step(line: str) -> bool:
    t = content_tokens(line)
    if not t:
        return False
    return t[0] in UI_VERBS or bool(re.match(
        r"^\s*(navigate|tap|touch|select|choose|press|toggle|turn|go|open|enable|disable)",
        line, re.IGNORECASE))


# "Press and hold" is one interaction, not two. Protecting these before the
# clause split is what stops the step list filling up with fragments like "Press."
PROTECTED = {
    "press and hold": "\x01", "touch and hold": "\x02", "tap and hold": "\x03",
    "turn on": "\x04", "turn off": "\x05", "sign in": "\x06", "log in": "\x07",
    "back up": "\x08", "set up": "\x09", "swipe up": "\x0a", "swipe down": "\x0b",
    "swipe left": "\x0c", "swipe right": "\x0d", "plug in": "\x0e",
    "power on": "\x0f", "power off": "\x10", "wipe cache": "\x11",
    "clear cache": "\x12", "clear data": "\x13", "safe mode": "\x14",
    "quick settings": "\x15", "airplane mode": "\x16", "do not disturb": "\x17",
}


def _protect(s: str) -> str:
    # "between X and Y" is a single choice, not two clauses. Collapse its
    # conjunction before the clause splitter ever sees it.
    s = re.sub(r"\bbetween\s+([^,.;:]{1,40}?)\s+and\s+",
               lambda m: f"between {m.group(1)} \x18 ", s, flags=re.IGNORECASE)
    for k, v in PROTECTED.items():
        s = re.sub(k, v, s, flags=re.IGNORECASE)
    return s


def _restore(s: str) -> str:
    s = s.replace("\x18", "and")
    for k, v in PROTECTED.items():
        s = s.replace(v, k)
    return s


_SENT_SPLIT_RX = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


def split_clauses(line: str) -> List[str]:
    """Split a run-on instruction line into one-interaction clauses.

    Two passes, and both matter for the "one physical interaction per step" rule:
    first break sentences ("...Settings. Tap Apps." is two steps), then break
    compound clauses while keeping protected collocations intact.
    """
    sentences: List[str] = []
    for sent in _SENT_SPLIT_RX.split(line.strip()):
        if sent.strip():
            sentences.append(sent.strip())

    out: List[str] = []
    for sent in sentences:
        parts = STEP_SPLIT_RX.split(_protect(sent))
        for p in parts:
            p = _restore(p).strip().rstrip(".").strip()
            if not p:
                continue
            if not out and not _is_ui_step(p):
                continue
            # A continuation fragment belongs to the previous step only when it
            # is genuinely a fragment. Long stretches of prose that merely lack a
            # recognised verb are separate instructions — merging those produced
            # 90-word "steps" that no user could follow, which is worse than an
            # extra step.
            if out and not _is_ui_step(p) and len(p.split()) <= 4 and not re.match(
                    r"^\s*(tap|touch|select|choose|press|toggle|turn|go|open|enable|disable|enter|set)",
                    p, re.IGNORECASE):
                out[-1] = out[-1] + ", " + p
                continue
            out.append(p)
    return [x for x in (_imperative(p) for p in out) if x]


# --------------------------------------------------------------------------- #
# Description synthesis: exactly 5-7 words, must start "It will"
# --------------------------------------------------------------------------- #

_VERB_MAP = {
    "check": "check", "verify": "verify", "ensure": "verify", "clear": "clear",
    "reset": "reset", "restart": "restart", "reboot": "restart", "remove": "remove",
    "connect": "connect", "disconnect": "disconnect", "update": "update",
    "enable": "enable", "disable": "disable", "toggle": "switch", "turn": "switch",
    "change": "change", "adjust": "adjust", "select": "choose", "choose": "choose",
    "open": "access", "navigate": "access", "back": "back up", "sign": "sign in",
    "set": "set", "review": "review", "allow": "enable", "grant": "grant",
    "install": "install", "uninstall": "remove", "pair": "pair", "unpair": "unpair",
    "format": "erase", "wipe": "erase", "swipe": "open", "test": "test",
}


def describe(action_name: str, steps: Sequence[str] = ()) -> str:
    """Synthesise an exactly-5-to-7-word "It will ..." benefit line.

    Deterministic generate-and-repair. The action name usually carries a screen
    qualifier ("... - Clear the App's Cache"), so we keep the head after the last
    " - " and the last two content tokens (the noun phrase), then land the count
    in range. No LLM, no retry, no variance between runs.
    """
    name = action_name.split(" - ")[-1].strip()
    words = [w for w in name.split() if w]
    if not words:
        return "It will fix the device settings"
    verb = _VERB_MAP.get(words[0].lower(), "fix")
    toks = content_tokens(" ".join(words[1:]))
    if len(toks) > 2:
        toks = toks[-2:]                     # keep the noun head, drop modifiers
    obj = " ".join(toks) or "settings"

    for cand in (
        f"It will {verb} {obj}",
        f"It will let you {verb} {obj}",
        f"It will {verb} {obj} for you",
        f"It will let you {verb} the {obj}",
    ):
        if 5 <= len(cand.split()) <= 7:
            return cand

    filler = ["for you", "on your phone", "on the device", "in the app",
              "for this issue", "and apply changes"]
    base = f"It will {verb} {obj}"
    for f in filler:
        cand = f"{base} {f}"
        if 5 <= len(cand.split()) <= 7:
            return cand
    parts = base.split()
    if len(parts) > 7:
        base = " ".join(parts[:7])
    while len(base.split()) < 5:
        base += " now"
    return base


def title_from_topic(topic: str) -> str:
    """2-3 word sentence-case title for the plan."""
    toks = content_tokens(topic)
    generic = {"smartphone", "tablet", "device", "troubleshooting", "issue",
               "issues", "problem", "guide", "fix", "error", "not", "working",
               "how", "step", "steps", "help", "your", "you", "on"}
    core = [t for t in toks if t not in generic][:3]
    if len(core) < 2:
        core = (core + toks)[:2]
    if not core:
        core = ["device", "settings"]
    out = " ".join(core[:3])
    return out[0].upper() + out[1:] if out else "Device settings"


def topic_from_topic(topic: str) -> str:
    """Title-Case topic used inside the goal sentence."""
    t = title_from_topic(topic)
    return " ".join(w.capitalize() if not w.isupper() else w for w in t.split())


def classify(action_name: str, steps: Sequence[str], resolved: bool) -> str:
    """auto < manual < critical, decided by lexicons over the action's own text."""
    blob = " ".join([action_name, *steps])
    if CRITICAL_RX.search(blob):
        return "critical"
    if MANUAL_RX.search(blob) or not resolved:
        return "manual"
    if SAFE_RX.search(blob) or resolved:
        return "auto"
    return "manual"


# --------------------------------------------------------------------------- #
# Structure extraction
# --------------------------------------------------------------------------- #

@dataclass
class ActionDraft:
    action_name: str
    steps: List[str]
    provenance: List[Tuple[int, int, str]] = field(default_factory=list)


@dataclass
class GoalDraft:
    source_id: str
    source_title: str
    topic: str
    actions: List[ActionDraft] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def parse_siis(source_id: str, payload: Dict[str, object]) -> GoalDraft:
    """SIIS record -> GoalDraft.

    The reference text is semi-structured markdown (`## Step N: <heading>` and
    imperative lines beneath). Parsing that structure directly is why we can
    guarantee "no hallucinated steps": every emitted step is a span of the source.
    """
    title = str(payload.get("title") or "").strip()
    content = str(payload.get("content") or "")

    # Content often names the topic better than the record title does.
    pre = PREAMBLE_RX.search(content)
    source_topic = title
    if pre and len(pre.group("h")) > 4:
        h = pre.group("h")
        h = re.sub(r"^(?:Troubleshooting|Resolve|Fix)\s+", "", h, flags=re.I)
        h = re.sub(r"^#+\s*", "", h).strip()
        source_topic = h or title

    draft = GoalDraft(source_id=source_id, source_title=title, topic=source_topic)

    # Split on "## Step N: Heading" and on bare "## Heading"
    matches = list(SECTION_RX.finditer(content))
    sections: List[Tuple[str, str]] = []
    if matches:
        for i, m in enumerate(matches):
            h = re.sub(r"^#+\s*", "", m.group("h")).strip()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            sections.append((h, content[m.end():end]))
    else:
        sections.append((source_topic, content))

    for heading, body in sections:
        # Sub-modes inside a section ("To clear the app's cache:") start new actions.
        chunks: List[Tuple[str, str]] = []
        cur_name, cur_lines = heading, []
        for raw in body.splitlines():
            line = raw.strip()
            mm = MODE_RX.match(line)
            if mm:
                if cur_lines:
                    chunks.append((cur_name, "\n".join(cur_lines)))
                cur_name, cur_lines = f"{heading} - {mm.group('h')}", []
                continue
            cur_lines.append(raw)
        if cur_lines:
            chunks.append((cur_name, "\n".join(cur_lines)))

        for name, text in chunks:
            steps: List[str] = []
            prov: List[Tuple[int, int, str]] = []
            for raw in text.splitlines():
                line = raw.strip().lstrip("-*•").strip()
                if not line or line.startswith("#") or line.startswith("|"):
                    continue
                if not _is_ui_step(line):
                    # keep the clause if it embeds a replayable instruction
                    if not re.match(r"^\s*(?:Alternatively|Also|Then|Next)[,\s]", line, re.I):
                        continue
                for st in split_clauses(line):
                    if st and st not in steps:
                        pos = content.find(line)
                        prov.append((max(pos, 0), max(pos, 0) + len(line), line))
                        steps.append(st)
            if steps:
                # One action = one destination screen. Partition by walking the
                # UI breadcrumb incrementally: a tap descends, a fresh
                # "Navigate to X" restarts the branch, and only a branch change
                # after real progress starts a new action.
                groups = _partition_by_screen(steps)
                for gi, g in enumerate(groups):
                    if len(groups) == 1:
                        nm = name
                    else:
                        lbl = path_signature(g)[1] or f"Step {gi + 1}"
                        nm = f"{name} - {_group_label(lbl)}"
                    draft.actions.append(
                        ActionDraft(action_name=nm.strip(), steps=g, provenance=prov[: len(g)] or prov)
                    )
    if not draft.actions:
        draft.warnings.append("no_replayable_steps")
    return draft


# --------------------------------------------------------------------------- #
# Draft -> contract-legal Goal
# --------------------------------------------------------------------------- #

@dataclass
class CompiledPlan:
    plan_id: str
    goal: Dict[str, object]
    intent_signature: str
    domain: str
    variations: List[str]
    keywords: str
    provenance: List[Dict[str, object]]
    build: Dict[str, object]

    def to_json(self) -> Dict[str, object]:
        return asdict(self)


def describe_screen(leaf: str) -> str:
    """Authored copy for the `dummy_positive` fallback.

    The catalog's own placeholder entry instructs us to write this ourselves:
    5-7 words naming the concrete screen taken from the steps.
    """
    leaf = (leaf or "").strip().lower()
    leaf = re.sub(r"^(?:the|your)\s+", "", leaf)
    if not leaf:
        return "It will open the relevant settings"
    for cand in (
        f"It will open the {leaf} settings",
        f"It will open {leaf} in device settings",
        f"It will open the {leaf} screen for you",
    ):
        if 5 <= len(cand.split()) <= 7:
            return cand
    toks = content_tokens(leaf) or ["settings"]
    while toks:
        c = f"It will open the {' '.join(toks)} settings"
        if 5 <= len(c.split()) <= 7:
            return c
        toks = toks[:-1]
    return "It will open the device settings"


def compile_goal(
    draft: GoalDraft, catalog: DeeplinkCatalog, mode: str = "hybrid"
) -> Tuple[Optional[dict], dict]:
    """GoalDraft -> (Goal dict or None, build record)."""
    actions: List[dict] = []
    prov: List[dict] = []
    n_resolved = 0
    n_generic = 0
    overlaps: List[float] = []
    n_polarity_ok = 0
    n_polarity_considered = 0

    for ad in draft.actions:
        res = catalog.resolve(ad.steps, ad.action_name, mode=mode)
        if res.get("accepted"):
            overlaps.append(float(res.get("overlap") or 0.0))
            pol = str(res.get("polarity") or "none")
            fam = getattr(res.get("entry"), "family", "")
            if pol in {"enable", "disable"} and fam in {"enable", "disable"}:
                n_polarity_considered += 1
                n_polarity_ok += int(fam == pol)
        leaf = str(res["leaf"])
        path = list(res.get("path") or [])          # type: ignore[arg-type]
        mentions_settings = any("settings" in s.lower() for s in ad.steps)
        opens_settings = (bool(path) and str(path[0]).lower().startswith("settings")) \
            or mentions_settings

        if res["entry"] is not None:
            entry = res["entry"]
            dl = catalog.to_actionable(entry)          # type: ignore[arg-type]
            n_resolved += 1
            resolved = True
            vuri = catalog.to_validation(entry, value=_toggle_value(ad.steps))  # type: ignore[arg-type]
            category = classify(ad.action_name, ad.steps, True)
        elif opens_settings:
            # The trajectory opens a Settings screen the catalog does not index.
            # The catalog's own placeholder tells us what to do here: emit
            # `voiceassist://dummy_positive` and author the copy ourselves.
            n_generic += 1
            resolved = False
            dl = catalog.to_generic(leaf, describe_screen(leaf))
            vuri = None
            category = classify(ad.action_name, ad.steps, True)
        else:
            n_generic += 1
            resolved = False
            dl = None
            vuri = None
            category = "manual"
        sg: Dict[str, object] = {
            "steps": [_sentence(s) for s in ad.steps],
            "validationDeeplink": vuri.model_dump() if vuri else None,
            "actionableDeeplink": dl.model_dump() if dl else None,
        }
        # manual actions cannot carry an actionable deeplink (guide S4.1)
        if category == "manual":
            sg["actionableDeeplink"] = None
            sg["validationDeeplink"] = None

        actions.append({
            "actionName": _titlecase(ad.action_name),
            "description": describe(ad.action_name, ad.steps),
            "stepGroups": [sg],
            "category": category,
        })
        prov.append({
            "action": _titlecase(ad.action_name),
            "resolution": res["basis"],
            "confidence": round(float(res["score"]), 4),
            "spans": [{"start": s, "end": e, "text": t} for s, e, t in ad.provenance],
        })

    if not actions:
        return None, {"dropped": "no_actions", "n_actions": 0}

    actions.sort(key=lambda a: RANK[str(a["category"])])   # stable: safe first

    topic = topic_from_topic(draft.topic)
    goal = {
        "goal": f"Follow these steps to perform this {topic} Troubleshooting",
        "title": title_from_topic(draft.topic),
        "actions": actions,
        "score": round(
            min(0.99, 0.55 + 0.04 * n_resolved - 0.05 * n_generic), 4
        ),
    }
    build = {
        "n_actions": len(actions),
        "n_resolved": n_resolved,
        "n_generic": n_generic,
        "resolution_rate": round(n_resolved / len(actions), 4),
        "mean_target_overlap": round(sum(overlaps) / len(overlaps), 4) if overlaps else 0.0,
        "polarity_agreement": round(n_polarity_ok / n_polarity_considered, 4)
        if n_polarity_considered else None,
        "n_polarity_considered": n_polarity_considered,
    }
    return goal, {"build": build, "provenance": prov}


def _toggle_value(steps: Sequence[str]) -> str:
    blob = " ".join(steps).lower()
    if re.search(r"\b(toggle off|turn off|disable|switch off)", blob):
        return "False"
    return "True"


# Acronyms and product spellings that must survive Title Case intact.
_ACRONYM = {
    "wifi": "Wi-Fi", "wi-fi": "Wi-Fi", "wi": "Wi", "pc": "PC", "ui": "UI",
    "qr": "QR", "otp": "OTP", "usb": "USB", "gps": "GPS", "sim": "SIM",
    "esim": "eSIM", "vpn": "VPN", "nfc": "NFC", "os": "OS", "ai": "AI",
    "id": "ID", "url": "URL", "api": "API", "sd": "SD", "lte": "LTE",
    "5g": "5G", "4g": "4G", "bluetooth": "Bluetooth", "ip": "IP", "sms": "SMS",
    "mms": "MMS", "pin": "PIN", "imei": "IMEI", "mac": "MAC", "ram": "RAM",
    "rom": "ROM", "hdr": "HDR", "oled": "OLED", "lcd": "LCD", "amoled": "AMOLED",
}


def _titlecase(name: str) -> str:
    out: List[str] = []
    for i, w in enumerate(name.split()):
        bare = w.strip("(),/-").lower()
        if bare in _ACRONYM:
            out.append(_ACRONYM[bare])
            continue
        if w.isupper() or (w and w[0].isdigit()):
            out.append(w)
        elif i > 0 and w.lower() in {"a", "an", "and", "at", "by", "for", "in",
                                     "of", "on", "or", "the", "to", "via", "with"}:
            out.append(w.lower())
        else:
            # capitalise the head, preserve any existing inner capitals
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)
