"""Rewrite the GitHub issues from docs/PLAN.md.

Each `## Issue N (#M) - Title` section of the plan becomes the body of GitHub
issue M. Keeping the issues generated from the plan means the two cannot drift.
"""
import re
import subprocess
import sys
from pathlib import Path

GH = r"C:\Program Files\GitHub CLI\gh.exe"
REPO = "vamsiman/computer-use-automation"
PLAN = Path(r"C:\Users\THANM\computer-use-automation\docs\PLAN.md")

DEPS = {
    3: "Issue 1 (#2)", 4: "Issue 2 (#3)", 5: "Issues 1 (#2), 2 (#3)",
    6: "Issues 1 (#2), 4 (#5)", 7: "Issues 1 (#2), 4 (#5)",
    8: "Issues 1 (#2), 5 (#6)", 9: "Issues 1 (#2), 5 (#6)",
    10: "Issues 4 (#5), 5 (#6), 6 (#7), 7 (#8), 8 (#9)",
    11: "Issues 5 (#6), 9 (#10)",
    12: "Issues 5 (#6), 6 (#7), 7 (#8), 8 (#9)",
    13: "Issues 3 (#4), 11 (#12)",
    14: "Issues 6 (#7), 11 (#12), 12 (#13)",
    15: "Issue 13 (#14)",
    16: "Issues 10 (#11), 11 (#12), 14 (#15)",
    17: "Issues 11 (#12), 15 (#16)",
    18: "Issues 12 (#13), 13 (#14), 15 (#16)",
    19: "Issues 11 (#12), 12 (#13), 13 (#14)",
    20: "Issue 17 (#18)",
}

BRIEF = {
    3: "Section 4 (target application is our call)",
    4: "Section 3.3 (runtime errors and exceptional states)",
    5: "Sections 3.1, 3.7 (surface abstraction seam)",
    6: "Section 3.2 (structured artifact) - focal point of the evaluation",
    7: "Section 3.6 (the seam pause/cede/resume implies)",
    8: "Section 3.4 (allowlist, risky actions)",
    9: "Sections 3.4, 3.5 (redaction, evidence)",
    10: "Sections 3.1, 4 (the discovery run has to be real)",
    11: "Section 3.2 (artifact decoupled from the raw transcript)",
    12: "Section 3.3 (deterministic replay, the production path)",
    13: "Section 3.3 (business outcome vs recoverable vs hard failure)",
    14: "Section 3.6 (escalation and handoff) - 'not just a TODO'",
    15: "Section 3.6 (operator surface may be mocked; mechanism must be real)",
    16: "Section 8 stretch (agent-facing capability interface)",
    17: "Sections 3.7, 8 (multi-tenant reuse, canonicalisation)",
    18: "Section 6 deliverable 3 (/evidence/)",
    19: "Section 7 (code quality, tested where it counts)",
    20: "Section 6 deliverables 1 and 2",
}


def gh(*args):
    r = subprocess.run([GH, *args], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        print("ERR:", " ".join(args[:4]), r.stderr[:400])
        sys.exit(1)
    return r.stdout.strip()


text = PLAN.read_text(encoding="utf-8")
pattern = re.compile(r"^## Issue (\d+) \(#(\d+)\) — (.+?)$", re.M)
marks = list(pattern.finditer(text))
if len(marks) != 18:
    print(f"expected 18 issue sections, found {len(marks)}")
    sys.exit(1)

for i, m in enumerate(marks):
    board, gh_no, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
    end = marks[i + 1].start() if i + 1 < len(marks) else text.find("\n## Verification")
    section = text[m.end():end].strip().rstrip("-").strip()

    header = (
        f"> Part of #1. Board item **{board}**.\n"
        f"> **Depends on:** {DEPS[gh_no]}\n"
        f"> **Brief:** {BRIEF[gh_no]}\n"
        f"> Full plan with cross-cutting invariants: [`docs/PLAN.md`]"
        f"(https://github.com/{REPO}/blob/master/docs/PLAN.md)\n"
    )
    body = f"{header}\n---\n\n{section}\n"
    full_title = f"{board}. {title}"

    existing = gh("issue", "view", str(gh_no), "-R", REPO, "--json", "number") \
        if gh_no <= 18 else ""
    if existing:
        gh("issue", "edit", str(gh_no), "-R", REPO, "-t", full_title, "-b", body)
        print(f"edited  #{gh_no}  {full_title}")
    else:
        url = gh("issue", "create", "-R", REPO, "-t", full_title, "-b", body)
        print(f"created {url}  {full_title}")
