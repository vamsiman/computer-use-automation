"""What the agent is permitted to do, as configuration rather than code.

Two independent gates, deliberately kept apart:

*Where* we may act -- origins and path patterns. An agent that wanders off the
application it was pointed at is the failure mode this exists for, and it does
not become acceptable because the action itself looked harmless.

*What* we may do -- action types, and how much risk may run unattended. A flow
can be on a perfectly allowed page and still be barred from submitting an
irreversible form.

Either gate alone leaves an obvious hole. Together they mean a permitted
action on a permitted page is still refused if nobody has agreed to the
consequences.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml

from cua.types import ActionType, RiskLevel

DEFAULT_POLICY_PATH = Path("policy.yaml")

#: Ordered least to most dangerous, so thresholds can be compared.
RISK_ORDER: tuple[RiskLevel, ...] = (
    RiskLevel.SAFE,
    RiskLevel.CAUTION,
    RiskLevel.IRREVERSIBLE,
)


def risk_rank(level: RiskLevel) -> int:
    return RISK_ORDER.index(level)


def origin_of(url: str) -> str:
    """scheme://host[:port], lowercased. The unit an allowlist reasons about."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def path_of(url: str) -> str:
    return urlparse(url).path or "/"


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Path globbing where ``**`` crosses separators and ``*`` does not.

    ``fnmatch`` is not usable here: its ``*`` matches ``/`` too, so a rule
    meant to permit ``/members/*`` would silently also permit
    ``/members/../admin/delete``. The distinction between the two wildcards is
    the whole point of writing this out.
    """
    out = ["^"]
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        i += 1
    out.append("$")
    return re.compile("".join(out))


@dataclass(frozen=True)
class Allowlist:
    origins: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    actions: frozenset[ActionType] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "_path_matchers", tuple(glob_to_regex(p) for p in self.paths)
        )

    def permits_origin(self, url: str) -> bool:
        origin = origin_of(url)
        return bool(origin) and origin in self.origins

    def permits_path(self, url: str) -> bool:
        path = path_of(url)
        return any(m.match(path) for m in self._path_matchers)  # type: ignore[attr-defined]

    def permits_action(self, action_type: ActionType) -> bool:
        return action_type in self.actions


@dataclass(frozen=True)
class RiskPolicy:
    """How much may happen without a person agreeing to it."""

    #: Highest risk permitted to run unattended.
    unattended_max: RiskLevel = RiskLevel.CAUTION
    #: Risk levels refused outright, whatever anyone approves.
    blocked: frozenset[RiskLevel] = frozenset()
    #: Control names that mark a click as irreversible when the artifact has
    #: not said. Used during discovery, where nothing has classified the step
    #: yet, so the guess has to lean toward caution.
    irreversible_hints: tuple[str, ...] = (
        "confirm",
        "submit",
        "commit",
        "delete",
        "remove",
        "transfer",
        "post",
        "approve",
        "authorize",
    )

    def needs_approval(self, level: RiskLevel) -> bool:
        return risk_rank(level) > risk_rank(self.unattended_max)

    def is_blocked(self, level: RiskLevel) -> bool:
        return level in self.blocked


@dataclass(frozen=True)
class Policy:
    allowlist: Allowlist = field(default_factory=Allowlist)
    risk: RiskPolicy = field(default_factory=RiskPolicy)

    @classmethod
    def from_dict(cls, raw: dict) -> "Policy":
        allow = raw.get("allowlist", {}) or {}
        risk = raw.get("risk", {}) or {}
        return cls(
            allowlist=Allowlist(
                origins=tuple(
                    origin_of(o) or o.lower() for o in allow.get("origins", [])
                ),
                paths=tuple(allow.get("paths", [])),
                actions=frozenset(
                    ActionType(a) for a in allow.get("actions", [])
                ),
            ),
            risk=RiskPolicy(
                unattended_max=RiskLevel(
                    risk.get("unattended_max", RiskLevel.CAUTION)
                ),
                blocked=frozenset(
                    RiskLevel(r) for r in risk.get("blocked", [])
                ),
                irreversible_hints=tuple(
                    risk.get("irreversible_hints", RiskPolicy().irreversible_hints)
                ),
            ),
        )

    @classmethod
    def load(cls, path: str | Path = DEFAULT_POLICY_PATH) -> "Policy":
        return cls.from_dict(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
