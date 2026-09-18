"""Regenerate /evidence from the deterministic tour (`cua demo` runs the same scenarios).

    uv run python scripts/make_evidence.py

Discovery evidence is NOT produced here: it comes from real `cua discover` runs and is copied in
as-is. Everything here is the replay path, so it needs no API key and is reproducible.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cua.demo import run_tour  # noqa: E402

EVIDENCE = ROOT / "evidence"
WITH_VIDEO = frozenset({"handoff_operator_takes_control"})


def copy_run(result, scenario) -> dict:
    target = EVIDENCE / scenario.name
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(result.evidence_dir, target)
    for recording in (target / "video").glob("*.webm"):  # Playwright names videos by page hash
        recording.rename(target / f"{scenario.name}.webm")
        shutil.rmtree(target / "video", ignore_errors=True)
    return {
        "run": scenario.name, "headline": scenario.headline, "note": scenario.note,
        "status": result.status, "exit_code": result.exit_code,
        "outcome": result.outcome.code if result.outcome else None,
        "failure": result.failure.category if result.failure else None,
        "policy_rule": result.policy.rule if result.policy else None,
        "recoveries": [r.handler for r in result.recoveries_applied],
        "human_actions": len(result.human_actions),
        "locator_ranks": result.locator_ranks, "tenant": result.tenant,
        "overlay": result.capability.overlay,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-root", default=str(ROOT / "runs" / "evidence"))
    parser.add_argument("--no-video", action="store_true", help="Skip the recorded handoff run.")
    args = parser.parse_args()
    runs_root = Path(args.runs_root)
    shutil.rmtree(runs_root, ignore_errors=True)

    index = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        videos = frozenset() if args.no_video else WITH_VIDEO
        for scenario, result in run_tour(browser, ROOT / "config", ROOT / "capabilities", runs_root, videos):
            index.append(copy_run(result, scenario))
            print(f"{scenario.name:<40} {result.status:<16} exit={result.exit_code}")
        browser.close()

    (EVIDENCE / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {len(index)} runs to {EVIDENCE}")


if __name__ == "__main__":
    main()
