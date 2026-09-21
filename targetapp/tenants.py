"""Per-institution configuration for the stand-in console.

Hundreds of institutions run the same vendor product, branded and configured
differently. This dict is the stand-in for that: one codebase, one deployment
per tenant, differing in branding, field wording and grid layout.

Two tenants, differing only in the three things that actually break
automation: what a field is *called*, what order a grid's columns are in, and
what the branding says. None of it changes the task, which is the whole point
-- one capability has to reach a savings balance in both.
"""

from __future__ import annotations

import os
from typing import Any

TENANTS: dict[str, dict[str, Any]] = {
    "base": {
        "key": "base",
        "institution": "Meridian Trust Federal Credit Union",
        "short_name": "Meridian Trust FCU",
        "product": "MemberConsole",
        "product_version": "4.3.1",
        "accent": "#1f4e79",
        "accent_dark": "#143654",
        # Wording the automation has to target. Changing these between tenants
        # is exactly the drift the locator fallback chain has to absorb.
        "member_label": "Member ID:",
        "member_field_note": "Enter the 5-digit member number.",
        "accounts_columns": ["Type", "Account No.", "Balance", "Opened"],
    },
    "riverbend": {
        "key": "riverbend",
        "institution": "Riverbend Community Credit Union",
        "short_name": "Riverbend CCU",
        "product": "MemberConsole",
        "product_version": "4.3.1",
        "accent": "#3d6b35",
        "accent_dark": "#294a23",
        # "Member Number", not "Member ID". A one-word difference that breaks
        # any locator anchored on the label text -- which, because this field
        # has no accessible name of its own, is the only thing there is to
        # anchor on. The single most ordinary form of drift there is.
        "member_label": "Member Number:",
        "member_field_note": "Enter the 5-digit member number.",
        # Balance moved from third column to second. A structural locator
        # (row 2, cell 3) still resolves here and reads the *account number*
        # -- confidently, and wrongly. A locator keyed on the column name
        # follows the move.
        "accounts_columns": ["Type", "Balance", "Account No.", "Opened"],
    },
}

DEFAULT_TENANT = "base"


def current_key() -> str:
    key = os.environ.get("TENANT", DEFAULT_TENANT).strip().lower()
    return key if key in TENANTS else DEFAULT_TENANT


def current() -> dict[str, Any]:
    return TENANTS[current_key()]
