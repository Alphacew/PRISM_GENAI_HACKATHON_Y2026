"""FORGE — contract layer.

The Pydantic contract is byte-for-byte the one Samsung shipped (Appendix A of the
Theme 02 guide) so that every response our API emits validates against the
organisers' own schema.py. Nothing here loosens it; validators only add the
mechanical rule gates the guide scores (Section 4.1 / 4.2).

Layering: this module knows nothing about retrieval, SIIS text or the atlas.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# 1. The organisers' schema, unchanged (guide Appendix A)
# --------------------------------------------------------------------------- #


class BaseDeeplink(BaseModel):
    deeplink: str


class Deeplink(BaseDeeplink):
    description: str
    message: Optional[str] = ""
    classes: Optional[Dict[str, str]] = None
    originalType: Optional[str] = None


class Condition(str, Enum):
    greater = "greater"
    equal = "equal"
    less = "less"


class ResultTypes(str, Enum):
    boolean = "boolean"
    intNum = "integer"
    string = "str"
    floatNum = "float"


class actionCategory(str, Enum):
    auto = "auto"
    manual = "manual"
    critical = "critical"


class ValidationDeepLink(BaseDeeplink):
    key: str
    resultType: Optional[ResultTypes] = None
    condition: Optional[Condition] = None
    value: Optional[str] = None


class StepGroup(BaseModel):
    steps: List[str]
    validationDeeplink: Optional[ValidationDeepLink] = None
    actionableDeeplink: Optional[Deeplink] = None


class Action(BaseModel):
    actionName: str
    description: str
    stepGroups: List[StepGroup]
    category: Optional[actionCategory] = actionCategory.manual


class Goal(BaseModel):
    goal: str
    title: str
    actions: List[Action]
    score: float


class ContextDeeplinkResponse(BaseModel):
    """RAG response containing a list of Goal objects.

    `fallback` is additive on the wire: the guide specifies fallback metadata for
    the no-match case (S4.2.3) but does not name a field, so it rides here as an
    optional extra. A consumer validating with the organisers' own model simply
    ignores it.
    """

    contexts: List[Goal] = []
    fallback: Optional[str] = None


# --------------------------------------------------------------------------- #
# 2. The wire envelope (guide Appendix B / Section 3.2 example)
# --------------------------------------------------------------------------- #


class Meta(BaseModel):
    latency_ms: int = 0
    cache_hit: bool = False
    model: str = "none(rules)"          # "none(rules)" == no LLM in the serving path
    cost_usd: float = 0.0
    # --- additive, non-breaking: makes the scored metrics self-evidencing ---
    plan_ids: List[str] = Field(default_factory=list)
    atlas_version: str = ""
    stage: str = "serve"                 # serve | compile_on_miss
    cache_tier: str = "none"             # exact | semantic | miss
    margin: Optional[float] = None       # sim(best) - sim(second) on the semantic tier
    tokens_in: int = 0
    tokens_out: int = 0


class Envelope(BaseModel):
    query: str
    query_variations: List[str] = Field(default_factory=list)
    response: ContextDeeplinkResponse
    meta: Meta


# --------------------------------------------------------------------------- #
# 3. The mechanical gates the guide scores (Section 4.1 / 4.2)
# --------------------------------------------------------------------------- #

_URL_DENYLIST = re.compile(
    r"(https?://|ftp://|www\.|\.com\b|\.in\b|\.org\b|\.net\b|\[[^\]]+\]\([^)]+\))",
    re.IGNORECASE,
)
_DOMAIN_WHITELIST = re.compile(r"^(voiceassist|bixby)://", re.IGNORECASE)

GOAL_RE = re.compile(
    r"^Follow these steps to perform this (?P<topic>[A-Z][A-Za-z0-9\- ]*?) "
    r"(?:Troubleshooting|Configuration)$"
)
DESC_RE = re.compile(r"^It will \S.*$")
TITLE_RE = re.compile(r"^[A-Z][a-z0-9]+(?:[- ][a-z0-9]+){1,2}$")

# Words that stay lowercase inside Title Case when they are not the first word.
_TC_MINOR = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "nor",
             "of", "on", "or", "the", "to", "up", "via", "with", "your"}


def is_title_case(text: str) -> bool:
    """True for Title Case where minor words may stay lowercase.

    `str.title()` is the wrong test: it produces "Check Email Access On A Pc",
    which is not how anyone writes an action name. Rolex-cased acronyms (PC, UI,
    Wi-Fi, QR) are preserved.
    """
    words = [w for w in (text or "").split() if w]
    if not words:
        return False
    for i, w in enumerate(words):
        core = w.strip("(),/-")
        if not core:
            continue
        if core.isupper() or core[0].isdigit():      # acronym / "24-hour"
            continue
        if i > 0 and core.lower() in _TC_MINOR:
            continue          # minor words may be either cased or lowercased
        elif not core[0].isupper():
            return False
    return True



def word_count(text: str) -> int:
    """Whitespace-delimited token count, which is how the visual editor counts."""
    return len([t for t in text.split() if t])


def leaks_url(text: str) -> bool:
    """True if *text* contains a web URL, a markdown link, or a bare TLD."""
    if not text:
        return False
    # The catalog URIs are the only sanctioned scheme; strip them before testing
    # so an embedded actionable link never reads as a leak.
    scrubbed = re.sub(r"(voiceassist|bixby)://[^\s\"']+", " DEEPLINK ", text)
    return bool(_URL_DENYLIST.search(scrubbed))


def is_catalog_uri(uri: str) -> bool:
    return bool(_DOMAIN_WHITELIST.match(uri or ""))


class GateReport(BaseModel):
    """Result of running the compiler gates over one Envelope."""

    ok: bool = True
    failures: List[str] = Field(default_factory=list)
    checks: Dict[str, int] = Field(default_factory=dict)

    def fail(self, code: str) -> None:
        self.ok = False
        self.failures.append(code)


def audit_envelope(env: Envelope, catalog_uris: Optional[set] = None) -> GateReport:
    """The conformance harness.

    Runs the guide's own rule list against a finished response. Used by the test
    suite (fuzz), by the eval harness, and at compile time before an artifact is
    admitted to the atlas. One function, one source of truth for the rules.
    """
    rep = GateReport()
    env_txt = env.model_dump_json()

    # --- 4.2.1 zero URL leaks, enforced on the serialized object -------------
    if leaks_url(env_txt):
        rep.fail("URL_LEAK")

    # --- 4.2.2 catalog integrity -------------------------------------------
    catalog_uris = catalog_uris or set()
    n_auto_with_dl = 0
    n_auto = 0
    for goal in env.response.contexts:
        # 4.1 goal syntax
        if not GOAL_RE.match(goal.goal):
            rep.fail("GOAL_SYNTAX")
        rep.checks["goal"] = rep.checks.get("goal", 0) + 1

        # 4.1 title 2-3 words, sentence case
        tw = word_count(goal.title)
        if tw < 2 or tw > 3 or not TITLE_RE.match(goal.title):
            rep.fail("TITLE_SHAPE")

        # 4.1 score in [0,1]
        if not (0.0 <= goal.score <= 1.0):
            rep.fail("SCORE_RANGE")

        # 4.2.5 / 4.1 plan hierarchy: auto -> manual -> critical, monotonic
        rank = {"auto": 0, "manual": 1, "critical": 2}
        seq = [rank[a.category.value] for a in goal.actions]
        if seq != sorted(seq):
            rep.fail("ORDER_VIOLATION")
        if seq and seq[-1] == 2 and 2 in seq[:-1]:
            pass  # critical present but not last would already be caught above

        for a in goal.actions:
            rep.checks["action"] = rep.checks.get("action", 0) + 1

            # 4.1 actionName Title Case
            if not is_title_case(a.actionName):
                rep.fail("ACTIONNAME_CASE")

            # 4.1 description: exactly 5-7 words, starts "It will"
            if not DESC_RE.match(a.description):
                rep.fail("DESC_PREFIX")
            dw = word_count(a.description)
            if dw < 5 or dw > 7:
                rep.fail("DESC_LENGTH")

            # 4.2.2 manual actions carry no actionable deeplink
            if not a.stepGroups:
                rep.fail("EMPTY_STEPGROUP")

            for sg in a.stepGroups:
                if not sg.steps:
                    rep.fail("EMPTY_STEPS")
                for s in sg.steps:
                    if leaks_url(s):
                        rep.fail("STEP_URL_LEAK")
                    # 4.1: one physical interaction per step
                    if s.count(".") > 1 and not s.endswith("."):
                        rep.fail("STEP_MULTI_INTERACTION")
                dl = sg.actionableDeeplink
                if a.category.value == "auto":
                    n_auto += 1
                    if dl is not None and is_catalog_uri(dl.deeplink):
                        n_auto_with_dl += 1
                        if catalog_uris and dl.deeplink not in catalog_uris:
                            rep.fail("DEEPLINK_NOT_IN_CATALOG")
                        if dl.deeplink.endswith("dummy_positive"):
                            # generic placeholder must carry authored copy
                            if word_count(dl.description) < 5 or word_count(dl.description) > 7:
                                rep.fail("DUMMY_DESC_LENGTH")
                    elif dl is None:
                        # an auto action without any deeplink is a resolution miss
                        rep.fail("AUTO_WITHOUT_DEEPLINK")
                if a.category.value == "manual" and dl is not None:
                    rep.fail("MANUAL_WITH_DEEPLINK")
                if sg.validationDeeplink is not None:
                    if not is_catalog_uri(sg.validationDeeplink.deeplink):
                        rep.fail("VALIDATION_URI_BAD")

    rep.checks["auto_actions"] = n_auto
    rep.checks["auto_with_deeplink"] = n_auto_with_dl
    return rep
