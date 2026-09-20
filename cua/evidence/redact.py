"""What may be written down, decided by declaration rather than by guesswork.

The tempting way to build this is a list of regular expressions for things
that look sensitive -- sixteen digits in a row, anything near the word
"password". That approach fails in both directions at once: it misses the
member name that is sensitive because of what it *is* rather than what it
looks like, and it mangles the balance that is not. Worse, it fails silently,
and a redaction layer that fails silently is indistinguishable from one that
works.

So classification is declared, never inferred. The artifact already says that
``savings_balance`` is ``pii``, and it says so in a document a reviewer signed
off on. The recorder reads that and does as it is told. A value nobody
classified is recorded as-is, and the fix for that is to declare it -- not to
teach this module to guess.

There is a second, narrower mechanism: **known literal values**. A password
read from the environment, or the value of an input declared ``pii``, is
scrubbed from free text wherever it surfaces -- inside an error message, a
URL, a page dump. That is not pattern-matching for things that look secret; it
is matching values we have been *told* are secret, which is a different claim
entirely. It exists because declared classification can only protect a value
that arrives with its name attached, and the interesting leaks are the ones
where it does not.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

from cua.types import Sensitivity

#: Sentinel meaning "this field is not written at all", as distinct from a
#: field written as null -- which would be a lie about what happened.
OMITTED = object()

SECRET_MASK = "[secret]"

#: Values shorter than this are left out of the literal scrub. A
#: three-character secret cannot be protected by search-and-replace anyway,
#: and attempting it would shred every record in the run to no purpose. The
#: declared classification still covers it wherever it arrives under its name.
MIN_LITERAL_LENGTH = 4

SALT_ENV = "CUA_REDACTION_SALT"
DEFAULT_SALT = "cua-evidence"

#: Environment variables whose values must never appear in evidence. Only the
#: values are read; the names are never written alongside them.
SECRET_ENV_VARS: tuple[str, ...] = ("CUA_APP_PASS", "ANTHROPIC_API_KEY")


def default_salt() -> str:
    return os.environ.get(SALT_ENV) or DEFAULT_SALT


def env_secrets(names: Iterable[str] = SECRET_ENV_VARS) -> tuple[str, ...]:
    """Secret values present in this process's environment."""
    return tuple(value for name in names if (value := os.environ.get(name)))


@dataclass(frozen=True)
class Redactor:
    """Decides what a record may contain, given what has been declared.

    Built once per run from the capability being replayed and handed to the
    recorder. Every write passes through it; there is deliberately no second
    path to disk.
    """

    #: Field name -> declared classification, read off the capability contract.
    rules: Mapping[str, Sensitivity]
    #: Literal values to scrub from free text, longest first so a value that
    #: contains another is replaced whole.
    literals: tuple[tuple[str, str], ...] = ()
    salt: str = ""
    #: Treat a value the run has *read* as sensitive even though nothing
    #: declared it. For discovery, where there is no contract yet to read a
    #: classification from -- discovery is the thing that produces one.
    #:
    #: Affects only the literal scrub, never the name-keyed classification. A
    #: blanket default of "every field is PII" would tokenise `kind` and
    #: `step_id` too and leave a log nobody can read, which is not a safety
    #: improvement, it is a deleted log.
    assume_sensitive: bool = False

    def __post_init__(self) -> None:
        if not self.salt:
            object.__setattr__(self, "salt", default_salt())

    # --- construction ----------------------------------------------------

    @classmethod
    def for_artifact(cls, artifact, secrets: Iterable[str] | None = None) -> "Redactor":
        """Read the classification map straight off the capability contract."""
        rules = {
            name: spec.sensitivity
            for name, spec in (
                list(artifact.inputs.items()) + list(artifact.outputs.items())
            )
        }
        values = env_secrets() if secrets is None else secrets
        return cls(rules=rules).with_secrets(values)

    @classmethod
    def for_discovery(cls) -> "Redactor":
        """For a run with no artifact, because it is making one.

        Discovery is a high-exposure operation by its nature: it reads whole
        screens of a live system and the model discusses what it found in
        prose. This covers what the run *extracts*, from the moment it is
        extracted. It cannot cover a value quoted in the model's own summary
        of a page it read earlier, and pretending otherwise would be worse
        than saying so -- which is why discovery evidence is governed by
        retention as much as by masking.
        """
        return cls(rules={}, assume_sensitive=True).with_secrets(env_secrets())

    @classmethod
    def empty(cls) -> "Redactor":
        """For a run with no artifact yet -- discovery, or a bare session.

        Still scrubs environment secrets, because the browser is being driven
        with real credentials either way.
        """
        return cls(rules={}).with_secrets(env_secrets())

    def with_secrets(self, values: Iterable[str]) -> "Redactor":
        return self._add_literals((value, SECRET_MASK) for value in values)

    def bind(self, values: Mapping[str, Any]) -> "Redactor":
        """Learn this run's actual input values, so they can be scrubbed.

        Declared classification protects a value arriving under its own name.
        This protects the same value when it turns up somewhere nameless:
        interpolated into a URL, quoted back inside an error message, or
        embedded in a page dump captured at the moment of failure.
        """
        additions = []
        for name, value in values.items():
            level = self.classify(name)
            if level is Sensitivity.SECRET:
                additions.append((str(value), SECRET_MASK))
            elif level is Sensitivity.PII or (
                self.assume_sensitive and level is Sensitivity.INTERNAL
            ):
                additions.append((str(value), self.token(name, value)))
        return self._add_literals(additions)

    def _add_literals(self, pairs: Iterable[tuple[str, str]]) -> "Redactor":
        merged = dict(self.literals)
        for value, mask in pairs:
            if value and len(value) >= MIN_LITERAL_LENGTH:
                merged[value] = mask
        ordered = tuple(
            sorted(merged.items(), key=lambda pair: len(pair[0]), reverse=True)
        )
        return replace(self, literals=ordered)

    # --- decisions -------------------------------------------------------

    def classify(self, name: str) -> Sensitivity:
        return self.rules.get(name, Sensitivity.INTERNAL)

    def token(self, name: str, value: Any) -> str:
        """A stable stand-in for a PII value.

        Masking everything to one constant would make records about two
        different members indistinguishable, which destroys the log's
        usefulness for the only thing evidence is for: reconstructing what
        happened. A salted digest keeps records correlatable without being
        readable.

        Its limit is worth stating plainly. This is not encryption. Against a
        small value space -- a five-digit member number, say -- anyone holding
        the salt can enumerate it in milliseconds. It guards against casual
        exposure of a log file, not against an adversary who has the log and
        the salt together.
        """
        seed = f"{self.salt}:{name}:{value}".encode("utf-8")
        return f"[pii:{hashlib.sha256(seed).hexdigest()[:8]}]"

    def field(self, name: str, value: Any) -> Any:
        """Redact one named value. May return ``OMITTED``."""
        level = self.classify(name)
        if level is Sensitivity.SECRET:
            return OMITTED
        if level is Sensitivity.PII and value is not None:
            return self.token(name, value)
        return self.structure(value)

    def text(self, value: str) -> str:
        """Scrub known literal values out of free text."""
        for literal, mask in self.literals:
            if literal in value:
                value = value.replace(literal, mask)
        return value

    def mapping(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Redact a name-keyed mapping, dropping anything classified secret.

        Dropped names are listed under ``_withheld``. Recording that a field
        existed and was withheld is honest in a way that silently omitting it
        is not: an auditor can then tell the difference between "no credential
        was involved here" and "one was, and we are not writing it down".
        """
        out: dict[str, Any] = {}
        withheld: list[str] = []
        for name, value in values.items():
            redacted = self.field(str(name), value)
            if redacted is OMITTED:
                withheld.append(str(name))
            else:
                out[str(name)] = redacted
        if withheld:
            out["_withheld"] = sorted(withheld)
        return out

    def structure(self, value: Any) -> Any:
        """Redact anything, recursively. The single entry point for a record."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return self.mapping(value)
        if isinstance(value, (list, tuple)):
            return [self.structure(item) for item in value]
        return value
