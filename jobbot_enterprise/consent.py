# Copyright (c) 2026 Benjamin Stanley Frohman. All rights reserved.
# Proprietary and confidential. No use, copying or distribution without written permission.
"""Candidate consent: the candidate decides which profile fields a company may see or score."""
from __future__ import annotations

import json

FIELDS = ("name", "email", "phone", "location", "links", "skills", "experience", "values", "causes",
          "desired_impact", "comp_expectation", "resume_text")
DEFAULT_GRANT = ("name", "email", "skills", "experience")
MATCHABLE = ("skills", "experience", "values", "causes", "desired_impact", "location", "resume_text")


def clean_grant(fields) -> list[str]:
    bad = sorted(set(fields) - set(FIELDS))
    if bad:
        raise ValueError(f"unknown consent fields: {', '.join(bad)}")
    return sorted(set(fields))


def visible(profile: dict, grant: list[str]) -> dict:
    """Only the fields the candidate granted; everything else is withheld, not blanked."""
    return {k: profile[k] for k in grant if k in profile and profile[k] not in (None, "")}


def load(row_profile: str, row_fields: str | None) -> tuple[dict, list[str]]:
    return json.loads(row_profile), json.loads(row_fields) if row_fields else []
