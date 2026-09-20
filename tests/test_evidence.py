"""What the run is allowed to write down.

No browser. The recorder writes files and the redactor decides what goes in
them; neither needs a live application, which is the point of keeping evidence
a separate layer rather than something the replay engine does inline.

The load-bearing test in this file is the last one: it walks every byte of a
finished bundle looking for a password and a member name. If redaction is ever
routed around -- a new writer that formats its own JSON, a debug dump added in
a hurry -- that test is what notices.
"""

from __future__ import annotations

import json
import os

import pytest

from cua.artifact.store import CapabilityStore
from cua.evidence import (
    EvidenceConfig,
    Recorder,
    Redactor,
    SECRET_MASK,
    env_secrets,
    new_run_id,
)
from cua.evidence.redact import MIN_LITERAL_LENGTH, OMITTED
from cua.primitives import A11yNode, Snapshot
from cua.types import Sensitivity

PASSWORD = "demo-pass-2024"
MEMBER_NAME = "Rosalind Okonkwo"

RULES = {
    "member_id": Sensitivity.INTERNAL,
    "member_name": Sensitivity.PII,
    "savings_balance": Sensitivity.PII,
    "password": Sensitivity.SECRET,
    "branch": Sensitivity.PUBLIC,
}


@pytest.fixture
def redactor() -> Redactor:
    return Redactor(rules=RULES, salt="test-salt").with_secrets([PASSWORD])


@pytest.fixture
def config(tmp_path) -> EvidenceConfig:
    return EvidenceConfig(root=tmp_path / "runs")


class FakeSurface:
    """Enough of a surface to exercise evidence capture, and no more."""

    def __init__(self, *, shots: bool = True, source: bool = True) -> None:
        self.shots = shots
        self.written: list[str] = []
        self._source = source

    def observe(self) -> Snapshot:
        tree = A11yNode(
            role="document",
            children=[
                A11yNode(role="heading", name="Member Details"),
                A11yNode(role="text", name=MEMBER_NAME),
                A11yNode(role="textbox", name="Password", value="********"),
            ],
        )
        return Snapshot(location="http://localhost:5000/members/10001", tree=tree)

    def screenshot(self, path: str) -> str:
        if not self.shots:
            raise RuntimeError("no display")
        with open(path, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n")
        self.written.append(path)
        return path

    def page_source(self) -> str:
        if not self._source:
            raise RuntimeError("frame detached")
        return f"<html><input value='{PASSWORD}'><td>{MEMBER_NAME}</td></html>"


def read_log(recorder: Recorder) -> list[dict]:
    text = recorder.log_path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# --- classification is declared, not guessed ------------------------------


def test_an_undeclared_field_is_recorded_rather_than_guessed_at(redactor):
    """Deliberate. A module that guesses would sometimes guess wrong in the
    direction of letting something through, and it would do it silently. If a
    value needs protecting, the fix is to declare it in the artifact -- which
    is a reviewable change to a document, not a regex nobody re-reads."""
    assert redactor.field("teller_note", "4111 1111 1111 1111") == (
        "4111 1111 1111 1111"
    )


def test_public_and_internal_pass_through(redactor):
    assert redactor.field("branch", "Riverbend") == "Riverbend"
    assert redactor.field("member_id", "10001") == "10001"


def test_pii_is_masked(redactor):
    masked = redactor.field("member_name", MEMBER_NAME)
    assert MEMBER_NAME not in masked
    assert masked.startswith("[pii:")


def test_a_secret_is_not_written_at_all(redactor):
    assert redactor.field("password", PASSWORD) is OMITTED


def test_a_withheld_field_is_named_but_not_written(redactor):
    """An auditor should be able to tell "a credential was involved and we did
    not write it down" from "no credential was involved"."""
    record = redactor.mapping({"member_id": "10001", "password": PASSWORD})
    assert "password" not in record
    assert record["_withheld"] == ["password"]
    assert PASSWORD not in json.dumps(record)


# --- the PII token --------------------------------------------------------


def test_the_same_value_gets_the_same_token(redactor):
    """Correlatable without being readable. Masking everything to one constant
    would make two records about two different members indistinguishable,
    which destroys the log for the one job evidence has."""
    assert redactor.field("member_name", "Ada") == redactor.field("member_name", "Ada")
    assert redactor.field("member_name", "Ada") != redactor.field("member_name", "Bea")


def test_the_token_is_salted():
    a = Redactor(rules=RULES, salt="one").token("member_name", "Ada")
    b = Redactor(rules=RULES, salt="two").token("member_name", "Ada")
    assert a != b


def test_the_salt_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("CUA_REDACTION_SALT", "deployment-specific")
    assert Redactor(rules={}).salt == "deployment-specific"


# --- known literals, the backstop ----------------------------------------


def test_a_secret_is_scrubbed_from_free_text(redactor):
    """Declared classification only protects a value arriving under its own
    name. This is the other half: an error message that quotes the password
    back is exactly how credentials reach logs in practice."""
    message = f"login failed for teller1 / {PASSWORD} at 09:12"
    assert PASSWORD not in redactor.text(message)
    assert SECRET_MASK in redactor.text(message)


def test_binding_scrubs_a_pii_input_wherever_it_appears(redactor):
    bound = redactor.bind({"member_name": MEMBER_NAME, "member_id": "10001"})
    dumped = bound.text(f"<td>{MEMBER_NAME}</td> member 10001")
    assert MEMBER_NAME not in dumped
    # member_id is only internal, so it survives -- redaction follows the
    # declaration rather than a hunch about what looks identifying.
    assert "10001" in dumped


def test_a_very_short_secret_is_left_out_of_the_literal_scrub():
    """Search-and-replace on a two-character value would shred every record in
    the run and protect nothing. The declared rule still covers it wherever it
    arrives by name."""
    short = "ab"
    assert len(short) < MIN_LITERAL_LENGTH
    scrubbed = Redactor(rules={}).with_secrets([short])
    assert scrubbed.text("a table of abbreviations") == "a table of abbreviations"


def test_the_longest_literal_wins():
    r = Redactor(rules={}).with_secrets(["pass", "pass-2024-long"])
    assert "pass-2024-long" not in r.text("token pass-2024-long here")


def test_redaction_reaches_into_nested_structures(redactor):
    payload = {
        "steps": [
            {"extracted": {"member_name": MEMBER_NAME, "branch": "Riverbend"}},
            {"error": f"bad creds {PASSWORD}"},
        ]
    }
    safe = json.dumps(redactor.structure(payload))
    assert MEMBER_NAME not in safe
    assert PASSWORD not in safe
    assert "Riverbend" in safe


def test_env_secrets_reads_values_not_names(monkeypatch):
    monkeypatch.setenv("CUA_APP_PASS", "env-password-value")
    assert "env-password-value" in env_secrets()


def test_the_classification_map_is_read_off_the_artifact():
    """The artifact is the authority. Nothing here re-decides what is
    sensitive; the reviewed document already said."""
    artifact = CapabilityStore().load("member.read_savings_balance")
    built = Redactor.for_artifact(artifact, secrets=())
    assert built.classify("savings_balance") is Sensitivity.PII
    assert built.classify("member_name") is Sensitivity.PII
    assert built.classify("member_id") is Sensitivity.INTERNAL


# --- the recorder ---------------------------------------------------------


def test_the_bundle_has_the_expected_shape(config, redactor):
    rec = Recorder.start(config=config, redactor=redactor, inputs={"member_id": "10001"})
    surface = FakeSurface()
    with rec.step("s1", "Open the search screen", "navigate") as step:
        step.tier = 0
        step.screenshot = rec.screenshot(surface, label="001")
    rec.finish({"status": "Success"})

    assert (rec.dir / "run.jsonl").exists()
    assert (rec.dir / "meta.json").exists()
    assert (rec.dir / "result.json").exists()
    assert (rec.dir / "steps" / "001.png").exists()


def test_the_log_is_one_json_object_per_line(config, redactor):
    """Line-delimited rather than one document, so a run that dies mid-step
    still leaves a readable log -- and that is the run whose log matters."""
    rec = Recorder.start(config=config, redactor=redactor)
    rec.event("note", detail="one")
    rec.event("note", detail="two")
    records = read_log(rec)
    assert [r["seq"] for r in records] == [1, 2]
    assert all("at" in r and "kind" in r for r in records)


def test_the_resolved_tier_is_recorded_per_step(config, redactor):
    """Not diagnostic detail. A capability that slid from tier 0 to tier 3 is
    still passing its tests and is already breaking; this is the only place
    that shows up before it does."""
    rec = Recorder.start(config=config, redactor=redactor)
    with rec.step("s2", "Enter the member number", "type") as step:
        step.tier = 1
        step.degraded = False
    record = read_log(rec)[0]
    assert record["tier"] == 1
    assert record["step_id"] == "s2"
    assert record["intent"] == "Enter the member number"


def test_a_step_is_timed(config, redactor):
    rec = Recorder.start(config=config, redactor=redactor)
    with rec.step("s1", "Wait", "wait_for"):
        pass
    assert read_log(rec)[0]["duration_ms"] >= 0


def test_a_step_that_raises_is_still_recorded_and_the_error_propagates(
    config, redactor
):
    """The step that raised is the one most worth having in the log. Writing
    the record only on the happy path is how a crash becomes a silent gap."""
    rec = Recorder.start(config=config, redactor=redactor)
    with pytest.raises(ValueError):
        with rec.step("s3", "Submit the search", "click"):
            raise ValueError("element detached")

    record = read_log(rec)[0]
    assert record["ok"] is False
    assert "element detached" in record["error"]


def test_an_extracted_value_is_redacted_by_its_declared_name(config, redactor):
    """Extracted values are keyed by output name on purpose: that is what
    makes the declared classification apply to them without the recorder
    knowing anything about balances."""
    rec = Recorder.start(config=config, redactor=redactor)
    with rec.step("s5", "Read the savings balance", "extract") as step:
        step.extracted = {"savings_balance": "4821.55"}
    record = read_log(rec)[0]
    assert record["extracted"]["savings_balance"].startswith("[pii:")
    assert "4821.55" not in json.dumps(record)


def test_a_value_learned_while_running_is_scrubbed_from_later_records(
    config, redactor
):
    """The moment a step reads a member's name, that string starts appearing
    in places with no name attached -- a failure reason, a tree dumped when
    the next step goes wrong. Declared classification alone would catch the
    first appearance and miss every later one."""
    rec = Recorder.start(config=config, redactor=redactor)
    with rec.step("s4", "Read the member's name", "extract") as step:
        step.extracted = {"member_name": MEMBER_NAME}

    rec.event("note", detail=f"{MEMBER_NAME} has no savings row")
    rec.failure(FakeSurface(), step_id="s5", reason="no savings row")

    assert MEMBER_NAME not in rec.log_path.read_text(encoding="utf-8")
    tree = (rec.dir / "failure" / "a11y.txt").read_text(encoding="utf-8")
    assert MEMBER_NAME not in tree


def test_an_error_string_containing_a_secret_is_scrubbed(config, redactor):
    rec = Recorder.start(config=config, redactor=redactor)
    with pytest.raises(RuntimeError):
        with rec.step("s1", "Sign in", "type"):
            raise RuntimeError(f"rejected password {PASSWORD}")
    assert PASSWORD not in rec.log_path.read_text(encoding="utf-8")


def test_a_business_outcome_is_logged_as_an_outcome_not_a_failure(config, redactor):
    rec = Recorder.start(config=config, redactor=redactor)
    rec.outcome("MEMBER_NOT_FOUND", "No member exists with that number.")
    record = read_log(rec)[0]
    assert record["kind"] == "outcome"
    assert record["code"] == "MEMBER_NOT_FOUND"


def test_a_recovery_records_its_count_against_budget(config, redactor):
    """A recovery firing twice as often this month as last is the first sign
    the application changed under us, and that is only visible if the count is
    written down."""
    rec = Recorder.start(config=config, redactor=redactor)
    rec.recovery("SYSTEM_NOTICE", "dismiss", occurrence=1, budget=2)
    record = read_log(rec)[0]
    assert (record["occurrence"], record["budget"]) == (1, 2)


def test_human_actions_go_through_the_same_redactor(config, redactor):
    """A human typing into a field declared secret does not make it less
    secret."""
    rec = Recorder.start(config=config, redactor=redactor)
    rec.human_actions([{"role": "textbox", "name": "Password", "value": PASSWORD}])
    assert PASSWORD not in rec.log_path.read_text(encoding="utf-8")


# --- retention ------------------------------------------------------------


def test_screenshots_can_be_switched_off(tmp_path, redactor):
    """The honest weak point: redaction works on structured records, and a PNG
    is not one. A balance on screen is in the pixels whatever the artifact
    declared, so a deployment handling real data can turn image capture off
    and still get a complete structured log."""
    config = EvidenceConfig(root=tmp_path / "runs", screenshots=False)
    rec = Recorder.start(config=config, redactor=redactor)
    surface = FakeSurface()

    assert rec.screenshot(surface) is None
    rec.failure(surface, step_id="s4", reason="checkpoint never held")

    assert list(rec.dir.rglob("*.png")) == []
    assert surface.written == []
    # The structured half of the failure bundle is still there.
    assert (rec.dir / "failure" / "a11y.txt").exists()


def test_the_retention_flags_are_recorded_so_an_absence_is_explainable(
    tmp_path, redactor
):
    """A reader finding no screenshots should be able to tell retention was
    off, not conclude the run never took any."""
    config = EvidenceConfig(root=tmp_path / "runs", screenshots=False)
    rec = Recorder.start(config=config, redactor=redactor)
    meta = json.loads((rec.dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["retention"]["screenshots"] is False


def test_page_source_is_gated_separately(tmp_path, redactor):
    """Markup carries what the accessibility tree elides -- hidden fields,
    view state, full account numbers -- so it is the richest thing in the
    bundle and gets its own switch."""
    config = EvidenceConfig(root=tmp_path / "runs", page_source=False)
    rec = Recorder.start(config=config, redactor=redactor)
    rec.failure(FakeSurface(), step_id="s4", reason="boom")
    assert not (rec.dir / "failure" / "page.html").exists()
    assert (rec.dir / "failure" / "a11y.txt").exists()


def test_config_reads_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CUA_EVIDENCE_SCREENSHOTS", "0")
    monkeypatch.setenv("CUA_EVIDENCE_ROOT", str(tmp_path))
    config = EvidenceConfig.from_env()
    assert config.screenshots is False
    assert config.failure_bundle is True


# --- failure capture ------------------------------------------------------


def test_the_failure_bundle_captures_what_the_system_could_see(config, redactor):
    """The tree at the instant of failure is the single most useful thing to
    hand someone who was not watching: it is usually the difference between
    "the app changed" and "the locator was wrong"."""
    rec = Recorder.start(config=config, redactor=redactor)
    captured = rec.failure(FakeSurface(), step_id="s5", reason="row not found")

    assert captured["a11y"] == "a11y.txt"
    assert captured["screenshot"] == "screenshot.png"
    tree = (rec.dir / "failure" / "a11y.txt").read_text(encoding="utf-8")
    assert "Member Details" in tree


def test_the_failure_bundle_is_redacted_too(config, redactor):
    rec = Recorder.start(config=config, redactor=redactor)
    rec.failure(FakeSurface(), step_id="s5", reason="row not found")
    html = (rec.dir / "failure" / "page.html").read_text(encoding="utf-8")
    assert PASSWORD not in html


def test_evidence_capture_never_breaks_the_run(config, redactor):
    """A screenshot that cannot be taken is a note in the log, not a failed
    run. Evidence is there to explain what happened, and it has no business
    changing what happens."""
    rec = Recorder.start(config=config, redactor=redactor)
    broken = FakeSurface(shots=False, source=False)
    rec.failure(broken, step_id="s1", reason="whatever")

    kinds = [r["kind"] for r in read_log(rec)]
    assert "evidence_error" in kinds
    assert "failure" in kinds


# --- meta and result ------------------------------------------------------


def test_meta_carries_the_capability_and_the_redacted_inputs(config):
    artifact = CapabilityStore().load("member.read_savings_balance")
    rec = Recorder.start(
        artifact=artifact,
        inputs={"member_id": "10002"},
        config=config,
        tenant="base",
    )
    rec.finish({"status": "Success"})
    meta = json.loads((rec.dir / "meta.json").read_text(encoding="utf-8"))

    assert meta["capability"]["id"] == "member.read_savings_balance"
    assert meta["capability"]["version"] == "1.0.0"
    assert meta["capability"]["status"] == "approved"
    assert meta["inputs"]["member_id"] == "10002"
    assert meta["tenant"] == "base"
    assert meta["duration_ms"] >= 0


def test_run_ids_are_unique_and_name_their_capability():
    a = new_run_id("member.read_savings_balance")
    b = new_run_id("member.read_savings_balance")
    assert a != b
    assert "member-read_savings_balance" in a


def test_the_result_is_written_as_json(config, redactor):
    rec = Recorder.start(config=config, redactor=redactor)
    path = rec.finish({"outputs": {"savings_balance": "4821.55"}})
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["outputs"]["savings_balance"].startswith("[pii:")


# --- the property this whole layer exists for -----------------------------


def test_nothing_sensitive_reaches_any_file_in_the_bundle(tmp_path):
    """The load-bearing test. It walks every byte of a finished bundle, so it
    fails if redaction is ever routed around -- a writer that formats its own
    JSON, a debug dump added in a hurry. That is a stronger guarantee than
    checking the paths we happen to remember."""
    artifact = CapabilityStore().load("member.read_savings_balance")
    config = EvidenceConfig(root=tmp_path / "runs")
    rec = Recorder.start(
        artifact=artifact,
        inputs={"member_id": "10001"},
        config=config,
        redactor=Redactor.for_artifact(artifact, secrets=[PASSWORD]),
    )
    surface = FakeSurface()

    with rec.step("s4", "Read the member's name", "extract") as step:
        step.tier = 1
        step.extracted = {"member_name": MEMBER_NAME}
    rec.human_actions([{"name": "Password", "value": PASSWORD}])
    rec.event("note", detail=f"retrying after {PASSWORD} was rejected")
    rec.failure(surface, step_id="s5", reason=f"{MEMBER_NAME} has no savings row")
    rec.finish({"outputs": {"member_name": MEMBER_NAME}})

    for path in rec.dir.rglob("*"):
        if path.is_file() and path.suffix != ".png":
            body = path.read_text(encoding="utf-8")
            assert PASSWORD not in body, path
            assert MEMBER_NAME not in body, path
