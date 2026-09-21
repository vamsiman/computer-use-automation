"""One capability, many tenants, without forking it.

The brief's heterogeneity question in its concrete form: the same vendor
product, deployed twice, configured differently. Riverbend calls the field
"Member Number" instead of "Member ID" and puts its accounts columns in
another order. Nothing about the *task* differs -- look up a member, read
their savings balance -- and answering this by keeping two copies of the
capability answers the wrong question, because the second copy starts drifting
from the first the day it is written.

So a tenant artifact is a **sparse document**: it names the base it extends
and lists only what differs.

```yaml
extends: member.read_savings_balance@1.0.0
tenant: cu-riverbend
steps:
  - id: s2
    target:
      primary: {strategy: label_proximity, label: "Member Number:"}
```

Three things about the merge are deliberate.

**By step id, never by position.** A positional merge silently rewrites the
wrong step the moment anybody inserts one, and it does so in a document that
still looks correct in review.

**The result is validated, the override is not.** A sparse step has no intent
and no action, so it cannot be an ``Artifact`` -- and requiring it to be one
would mean copying both fields out of the base into every override, which is
the duplication this file exists to prevent. What has to be coherent is the
merged document, and that is what gets checked.

**A tenant may say where a control is; it may not say what to do.** No new
steps, no changed actions, and above all no changed inputs or outputs. Every
tenant of a capability answers to the same signature, or the catalogue is
lying about at least one of them.
"""

from __future__ import annotations

from typing import Any, Mapping

from cua.artifact.models import Artifact


class OverrideError(ValueError):
    """A tenant document that does not describe a legal variation."""


#: What a tenant may vary, per step. Everything here is about *finding* a
#: control or *knowing you arrived*, never about what is done to it.
STEP_FIELDS = frozenset({"target", "checkpoint", "args", "intent"})

#: And at the top level, beyond the capability's own identity.
ARTIFACT_FIELDS = frozenset({"preconditions", "outcomes", "recoveries", "success"})

#: Things a tenant may restate about itself without varying the capability.
META_FIELDS = frozenset(
    {"id", "version", "title", "description", "app", "tenant", "extends", "status"}
)

#: Changing either of these would mean two tenants of one capability are not
#: interchangeable to a caller, which defeats the purpose of sharing an id.
CONTRACT_FIELDS = frozenset({"inputs", "outputs"})


def merge(base: Artifact, override: Mapping[str, Any]) -> Artifact:
    """Apply a sparse tenant document to a base capability.

    Neither input is modified. The result carries the tenant's identity and
    the base's contract: the same signature, reached by a different route.
    """
    document: dict[str, Any] = base.model_dump(mode="json")
    meta = dict(override.get("capability") or {})

    tenant = meta.get("tenant")
    if not tenant:
        raise OverrideError("a tenant override must name its tenant")

    refused = sorted(set(override) & CONTRACT_FIELDS)
    if refused:
        raise OverrideError(
            f"a tenant override may not change {refused}: every tenant of a "
            "capability answers to the same signature"
        )

    document["capability"].update(
        {key: value for key, value in meta.items() if key in META_FIELDS}
    )
    document["capability"]["tenant"] = tenant
    document["capability"].setdefault("extends", base.ref)
    if not document["capability"].get("extends"):
        document["capability"]["extends"] = base.ref

    _apply_steps(document, override.get("steps") or [])

    for field in ARTIFACT_FIELDS & set(override):
        document[field] = override[field]

    # The merged document is the one that has to be coherent, so this is
    # where validation belongs.
    return Artifact.model_validate(document)


def _apply_steps(document: dict[str, Any], steps: list[Mapping[str, Any]]) -> None:
    by_id = {step["id"]: step for step in document["steps"]}

    for patch in steps:
        step_id = patch.get("id")
        if not step_id:
            raise OverrideError("every overridden step must name the id it replaces")

        target = by_id.get(step_id)
        if target is None:
            raise OverrideError(
                f"step {step_id!r} is not in the base capability. A tenant may "
                "describe its screens differently; it may not add steps -- that "
                "is a different capability, and the caller's signature would "
                "stop describing what runs."
            )

        forbidden = sorted(set(patch) - STEP_FIELDS - {"id"})
        if forbidden:
            raise OverrideError(
                f"step {step_id!r} tries to change {forbidden}; a tenant "
                f"override may only vary {sorted(STEP_FIELDS)}"
            )

        for field in STEP_FIELDS & set(patch):
            target[field] = patch[field]


def for_tenant(library: Any, base: Artifact, tenant: str) -> Artifact:
    """The tenant's version, or the base unchanged when nothing differs.

    A tenant with no override file is **not an error**, and that case is the
    one worth measuring rather than asserting: the base capability, run
    against a differently-configured deployment, succeeding on a lower locator
    tier. That is graceful degradation demonstrated. Adding the override
    returns the step to its primary, which is the repair -- and the tier log
    is the evidence for both halves.
    """
    override = library.load_tenant_override(base.capability.id, tenant)
    if not override:
        result = base.model_copy(deep=True)
        result.capability.tenant = tenant
        return result
    return merge(base, override)
