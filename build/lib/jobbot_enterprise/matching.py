# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Benjamin Stanley Frohman
"""Local, explainable matching (BM25 + mission + preferences). Same approach as the open-source
Overemployed-Job Bot matcher, re-implemented here; no external AI service, no data leaves the host.
A customer may plug in their own embedding model via `Embedder` (e.g. on their own GPUs)."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, Sequence

STOP = set("a an and are as at be by for from has have in is it its of on or our the their this to we with you your "
           "will who what work working role team join help".split())
CAUSES = {
    "climate": {"climate", "carbon", "decarbonization", "renewable", "solar", "energy", "sustainability"},
    "health": {"health", "healthcare", "medical", "patient", "clinical", "biotech"},
    "education": {"education", "learning", "students", "school", "teaching", "edtech"},
    "ai safety": {"alignment", "safety", "interpretability", "safe", "evals"},
    "science": {"research", "science", "scientific", "discovery", "lab"},
    "open source": {"open", "source", "oss", "community"},
    "access": {"accessibility", "inclusion", "equity", "underserved", "access"},
}


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9][a-z0-9+#.]*", (text or "").lower()) if t not in STOP and len(t) > 1]


class BM25:
    def __init__(self, docs: Sequence[Sequence[str]], k1: float = 1.4, b: float = 0.75):
        self.docs = [Counter(d) for d in docs]
        self.lens = [len(d) for d in docs]
        self.avg = (sum(self.lens) / len(self.lens)) if self.lens else 0.0
        df = Counter(t for d in self.docs for t in d)
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.k1, self.b = k1, b

    def score(self, query: Sequence[str], i: int) -> float:
        d, ln, s = self.docs[i], self.lens[i], 0.0
        for t in set(query):
            f = d.get(t, 0)
            if f:
                s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * ln / (self.avg or 1)))
        return s


class Embedder(Protocol):
    def sims(self, query: str, docs: list[str]) -> list[float]: ...


@dataclass
class Doc:
    id: str
    text: str
    location: str = ""
    remote: bool = False
    comp: Optional[int] = None


@dataclass
class Result:
    id: str
    score: float
    breakdown: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def _norm(xs: list[float]) -> list[float]:
    m = max(xs) if xs else 0.0
    return [x / m if m > 0 else 0.0 for x in xs]


def rank(query: dict, docs: Sequence[Doc], *, boost: Callable[[str], float] | None = None,
         embedder: Embedder | None = None, limit: int = 50) -> list[Result]:
    """query keys: skills, experience, causes, values, desired_impact, location, remote_ok, comp_floor."""
    if not docs:
        return []
    toks = [tokenize(d.text) for d in docs]
    bm = BM25(toks)
    skill_q = tokenize(" ".join([query.get("skills", ""), query.get("experience", "")]))
    cause_names = [c.strip().lower() for c in re.split(r"[,;\n]", query.get("causes", "")) if c.strip()]
    mission_terms = set(tokenize(" ".join([query.get("values", ""), query.get("desired_impact", "")])))
    for c in cause_names:
        mission_terms |= CAUSES.get(c, set(tokenize(c)))
    skill = _norm([bm.score(skill_q, i) for i in range(len(docs))])
    mission = _norm([bm.score(list(mission_terms), i) for i in range(len(docs))])
    if embedder is not None:
        sem = embedder.sims(" ".join(query.get(k, "") for k in ("skills", "experience", "desired_impact")),
                            [d.text for d in docs])
        skill = [0.6 * a + 0.4 * max(0.0, b) for a, b in zip(skill, sem)]
    out = []
    for i, d in enumerate(docs):
        tset, reasons = set(toks[i]), []
        hits = [t for t in dict.fromkeys(skill_q) if t in tset][:5]
        if hits:
            reasons.append("Skills overlap: " + ", ".join(hits) + ".")
        causes_hit = [c for c in cause_names if CAUSES.get(c, set(tokenize(c))) & tset]
        if causes_hit:
            reasons.append("Mission fit: " + ", ".join(causes_hit) + ".")
        pref = 0.5
        loc = (query.get("location") or "").lower()
        if d.remote and query.get("remote_ok", True):
            pref, _ = 1.0, reasons.append("Remote works for both sides.")
        elif loc and loc in (d.location or "").lower():
            pref, _ = 1.0, reasons.append(f"Location match: {d.location}.")
        floor = query.get("comp_floor") or 0
        if floor and d.comp is not None:
            if d.comp >= floor:
                reasons.append(f"Compensation {d.comp:,} meets the {floor:,} floor.")
            else:
                pref *= 0.4
                reasons.append(f"Compensation {d.comp:,} is below the {floor:,} floor.")
        fb = boost(d.id) if boost else 0.0
        if fb > 0:
            reasons.append("Similar to people your team advanced before.")
        score = 0.5 * skill[i] + 0.3 * mission[i] + 0.2 * pref + fb
        out.append(Result(d.id, round(100 * max(0.0, min(score, 1.15)) / 1.15, 1),
                          {"skills": round(skill[i], 3), "mission": round(mission[i], 3), "preferences": round(pref, 3),
                           "feedback": round(fb, 3)}, reasons or ["Weak overlap; shown for completeness."]))
    out.sort(key=lambda r: (-r.score, r.id))
    return out[:limit]
