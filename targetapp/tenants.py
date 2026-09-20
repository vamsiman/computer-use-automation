"""Per-institution configuration for the stand-in console.

Hundreds of institutions run the same vendor product, branded and configured
differently. This dict is the stand-in for that: one codebase, one deployment
per tenant, differing in branding, field wording and grid layout.

Only ``base`` is implemented here. The ``riverbend`` variant is added in the
tenant-reuse issue, where the point is to replay one artifact against both.
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
}

DEFAULT_TENANT = "base"


def current_key() -> str:
    key = os.environ.get("TENANT", DEFAULT_TENANT).strip().lower()
    return key if key in TENANTS else DEFAULT_TENANT


def current() -> dict[str, Any]:
    return TENANTS[current_key()]
