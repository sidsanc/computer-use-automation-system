"""Checks to run before pushing: no secrets, required report headings, schemas current.

    uv run python scripts/preflight.py
"""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cua.schema_export import export_schemas  # noqa: E402

REQUIRED_HEADINGS = [
    "## 1. Architecture",
    "## 2. Artifact schema",
    "## 3. Determinism & error handling",
    "## 4. Heterogeneity & multi-tenant",
    "## 5. Escalation & handoff",
    "## 6. Safety",
    "## 7. Cuts",
]
SECRET_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "Anthropic API key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "GitHub token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
]
BINARY = {".png", ".jpg", ".jpeg", ".zip", ".webm", ".ico", ".pdf"}


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [ROOT / line for line in out.splitlines() if line]


def main() -> int:
    problems: list[str] = []
    files = tracked_files()

    for path in files:
        if path.suffix.lower() in BINARY or not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern, label in SECRET_PATTERNS:
            if pattern.search(text):
                problems.append(f"{path.relative_to(ROOT)}: looks like a {label}")

    for unwanted in (".env", "Assignment A — Computer-Use Automation System.pdf"):
        if any(p.name == unwanted for p in files):
            problems.append(f"{unwanted} must not be tracked")
    if any(p.suffix.lower() == ".pdf" for p in files):
        problems.append("a PDF is tracked; the assignment brief should stay out of the repo")

    report = (ROOT / "REPORT.md").read_text(encoding="utf-8")
    missing = [h for h in REQUIRED_HEADINGS if h not in report]
    problems += [f"REPORT.md is missing the heading {h!r}" for h in missing]
    if REQUIRED_HEADINGS[0] in report:
        order = [report.index(h) for h in REQUIRED_HEADINGS if h in report]
        if order != sorted(order):
            problems.append("REPORT.md headings are out of order")

    with tempfile.TemporaryDirectory() as tmp:
        for generated in export_schemas(Path(tmp)):
            committed = ROOT / "schemas" / generated.name
            if not committed.exists() or json.loads(committed.read_text()) != json.loads(generated.read_text()):
                problems.append(f"schemas/{generated.name} is stale; run `uv run cua schema`")

    for required in ("README.md", "REPORT.md", "evidence/README.md", "evidence/index.json"):
        if not (ROOT / required).exists():
            problems.append(f"missing {required}")

    for problem in problems:
        print(f"FAIL  {problem}")
    if not problems:
        print(f"preflight OK: {len(files)} tracked files, no secrets, REPORT headings present, schemas current")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
