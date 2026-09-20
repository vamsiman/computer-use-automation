"""Where artifacts live.

The filesystem, as YAML, in git. Not a database, and the reason is the brief's
own word: artifacts must be *reviewable*. A pull request diff is the most
reviewable form a change to an automated bank procedure can take -- you can see
that a locator moved, that a step's risk went from safe to irreversible, that
somebody widened an input pattern -- and review is the control that matters
most for a system that acts unattended.

The cost is real and worth naming: no queries, no indexes, no concurrent
writers. At the scale in the brief -- hundreds of tenants, twenty apps each --
that is thousands of small text files, which git handles perfectly well. If it
ever needed to be a database, the store interface is the only thing that would
change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import yaml

from cua.artifact.models import Artifact
from cua.artifact.validate import validate_or_raise

DEFAULT_ROOT = Path("capabilities")


class CapabilityNotFound(LookupError):
    pass


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def to_yaml(artifact: Artifact) -> str:
    """Serialise, preserving declaration order and keeping it diff-friendly."""
    payload = artifact.model_dump(mode="json", exclude_none=True)
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=88)


def from_yaml(text: str) -> Artifact:
    return Artifact.model_validate(yaml.safe_load(text))


class CapabilityStore:
    """``capabilities/<id>/<version>.yaml``, plus ``tenants/<tenant>.yaml``."""

    def __init__(self, root: str | Path = DEFAULT_ROOT) -> None:
        self.root = Path(root)

    # --- paths ---

    def dir_for(self, capability_id: str) -> Path:
        return self.root / capability_id

    def path_for(self, capability_id: str, version: str) -> Path:
        return self.dir_for(capability_id) / f"{version}.yaml"

    def tenant_path_for(self, capability_id: str, tenant: str) -> Path:
        return self.dir_for(capability_id) / "tenants" / f"{tenant}.yaml"

    # --- writing ---

    def save(self, artifact: Artifact, *, validate: bool = True) -> Path:
        if validate:
            validate_or_raise(artifact)

        meta = artifact.capability
        if meta.extends:
            path = self.tenant_path_for(meta.id, meta.tenant or "unknown")
        else:
            path = self.path_for(meta.id, meta.version)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(to_yaml(artifact), encoding="utf-8")
        return path

    # --- reading ---

    def versions(self, capability_id: str) -> list[str]:
        directory = self.dir_for(capability_id)
        if not directory.is_dir():
            return []
        found = [p.stem for p in directory.glob("*.yaml")]
        return sorted(found, key=_version_key)

    def load(
        self,
        capability_id: str,
        version: str | None = None,
        *,
        validate: bool = True,
    ) -> Artifact:
        """Load one artifact. ``version=None`` means the highest version.

        Resolving "latest" by semver rather than by file mtime is deliberate:
        a checkout reorders timestamps, and an automation layer that picks a
        different capability version depending on when the repo was cloned is
        not one anybody should trust.
        """
        if version is None:
            available = self.versions(capability_id)
            if not available:
                raise CapabilityNotFound(f"no versions of {capability_id!r} in {self.root}")
            version = available[-1]

        path = self.path_for(capability_id, version)
        if not path.is_file():
            raise CapabilityNotFound(f"{capability_id}@{version} not found at {path}")

        artifact = from_yaml(path.read_text(encoding="utf-8"))
        if validate:
            validate_or_raise(artifact)
        return artifact

    def load_tenant_override(self, capability_id: str, tenant: str) -> Artifact | None:
        path = self.tenant_path_for(capability_id, tenant)
        if not path.is_file():
            return None
        return from_yaml(path.read_text(encoding="utf-8"))

    # --- listing ---

    def capability_ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            p.name for p in self.root.iterdir() if p.is_dir() and any(p.glob("*.yaml"))
        )

    def catalogue(self) -> Iterator[Artifact]:
        """Latest version of every capability -- what an agent would browse.

        Loaded without coherence validation, and one unreadable artifact is
        skipped rather than fatal: a browsable catalogue should not go dark
        because a single document is mid-edit. Anything invoked is loaded
        again through :meth:`load`, which does validate.
        """
        for capability_id in self.capability_ids():
            try:
                yield self.load(capability_id, validate=False)
            except Exception:
                continue
