#!/usr/bin/env python3
"""Cheap repo guards (stdlib only, ~instant). Run by CI; run locally with
`python3 scripts/check_repo.py`. Exits non-zero with one line per problem.

1. Every root-level test_*.py has a step in .github/workflows/test.yml
   (CLAUDE.md: otherwise it silently doesn't gate merges).
2. Every docs/design/*.md has a header with a valid status, an `issues` list,
   and (if approved) an approval date.
3. Every relative markdown link under docs/design resolves.
4. docs/design/README.md mentions every document under docs/design.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATUSES = {"approved", "reference", "draft", "superseded", "prototype"}
errors = []


def err(msg):
    errors.append(msg)


# 1. test files vs CI
workflow = (ROOT / ".github/workflows/test.yml").read_text()
for t in sorted(ROOT.glob("test_*.py")):
    if f"python {t.name}" not in workflow:
        err(f"{t.name}: no step in .github/workflows/test.yml")

# 2. design headers
design = ROOT / "docs/design"
for md in sorted(design.glob("*.md")):
    if md.name == "README.md":
        continue
    m = re.match(r"---\n(.*?)\n---\n", md.read_text(), re.S)
    if not m:
        err(f"{md.relative_to(ROOT)}: missing --- header")
        continue
    head = dict(
        (k.strip(), v.strip())
        for k, _, v in (l.partition(":") for l in m.group(1).splitlines() if ":" in l)
    )
    if head.get("status") not in STATUSES:
        err(f"{md.relative_to(ROOT)}: status must be one of {sorted(STATUSES)}")
    if not re.match(r"\[\d+(, *\d+)*\]$", head.get("issues", "")):
        err(f"{md.relative_to(ROOT)}: `issues: [..]` missing or malformed")
    if head.get("status") == "approved" and not re.match(r"\d{4}-\d\d-\d\d$", head.get("approved", "")):
        err(f"{md.relative_to(ROOT)}: approved designs need `approved: YYYY-MM-DD`")

# 3. relative links
for md in sorted(design.rglob("*.md")):
    for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", md.read_text()):
        if re.match(r"[a-z]+:", target):
            continue
        if not (md.parent / target).exists():
            err(f"{md.relative_to(ROOT)}: broken link -> {target}")

# 4. index completeness
index = (design / "README.md").read_text()
for md in sorted(design.rglob("*.md")):
    if md.name != "README.md" or md.parent != design:
        rel = md.relative_to(design).as_posix()
        # prototypes are linked via their folder README
        needle = rel if md.name != "README.md" else md.parent.relative_to(design).as_posix() + "/"
        if needle not in index and rel not in index:
            err(f"docs/design/README.md does not list {rel}")

if errors:
    print("\n".join(errors))
    sys.exit(1)
print("repo guards: ok")
