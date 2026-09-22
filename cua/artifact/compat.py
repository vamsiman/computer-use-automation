"""Is this capability allowed to run against this version of the application?

``AppRef.version_range`` has always said what an artifact was built for, and
until now nothing read it. Its own docstring names the reason it exists: *a
capability recorded on 4.2 quietly failing on 5.0 is worse than one that
refuses to run*. This module is that sentence, enforced.

The check is deliberately **conservative in one direction only**. An unknown
version does not block a run: plenty of applications never say what they are,
and a system that refused to work whenever it could not identify the software
would be useless in exactly the legacy estate it was built for. A *known*
version outside a *declared* range does block, because at that point both
facts are in hand and proceeding is a choice to ignore one of them.
"""

from __future__ import annotations

import re

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

#: Means "anything", and is the default, so an artifact that says nothing
#: about versions constrains nothing.
ANY = "*"

#: Ranges are written the way a person writes them -- ">=4.2 <5.0" -- rather
#: than in the comma-separated form the parser wants.
_SEPARATORS = re.compile(r"[\s,]+")


def normalise(version_range: str) -> str:
    """Turn a human-written range into a specifier set."""
    parts = [part for part in _SEPARATORS.split(version_range.strip()) if part]
    return ",".join(parts)


def parse_version(raw: str) -> Version | None:
    """A version from whatever the application happened to say.

    Applications announce themselves as "MemberConsole 4.3.1", or "v4.3.1", or
    "Release 4.3.1 (build 9912)". Pulling the first dotted number out of the
    string is cruder than a real parser and considerably more likely to work
    on the sort of software this is aimed at.
    """
    if not raw:
        return None
    match = re.search(r"\d+(?:\.\d+)*", raw)
    if not match:
        return None
    try:
        return Version(match.group(0))
    except InvalidVersion:
        return None


def is_compatible(version_range: str | None, app_version: str | None) -> bool:
    """Would this capability run against this application version?

    True whenever the question cannot be answered, which is most of the time
    and is the point. See the module docstring.
    """
    if not version_range or version_range == ANY:
        return True
    version = parse_version(app_version or "")
    if version is None:
        return True
    try:
        return version in SpecifierSet(normalise(version_range))
    except InvalidSpecifier:
        # A range nobody can parse is a documentation string, not a gate. The
        # validator is where a malformed range should be caught; refusing to
        # run over one here would turn a typo into an outage.
        return True


def explain(version_range: str | None, app_version: str | None) -> str:
    """Why the run was refused, in the terms an operator would use."""
    return (
        f"this capability was recorded for {version_range}, and the "
        f"application reports {app_version!r}"
    )
