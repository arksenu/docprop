#!/usr/bin/env python3
"""Prepare, run, and review a proposal-only latent-signals history benchmark.

Every case replays one real Markdown file edit in an isolated Git repository.
Dependency links are *candidate annotations* for the experiment, not historical
facts recorded by the original repository. The source clone is never modified.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "inputs/latent-signals"
DEFAULT_OUT = ROOT / ".work/benchmarks/latent-24"
LATTICE = ROOT / ".work/tools/venv/bin/doc-lattice"
RUNNER = ROOT / "doc_rewrite.py"

# One plausible doc-level link per source. These are hypotheses to be reviewed,
# especially chronological logs, and not proof that every edit requires a rewrite.
PAIRS = {
    "latent-signals/05_validation/results/backtest_summary.md": (
        "latent-signals/04_decisions/decision_log.md", "medium",
        "Decisions may rely on validation outcomes; chronological entries may stay unchanged."),
    "latent-signals/04_decisions/decision_log.md": (
        "latent-signals/dev_log.md", "medium",
        "Development log sometimes records decisions, but old entries should not be retroactively rewritten."),
    "latent-signals/dev_log.md": (
        "latent-signals/04_decisions/decision_log.md", "low",
        "Decision log may reflect development findings; this reverse link is exploratory."),
    "product_brief.md": (
        "latent-signals/01_strategy/product_brief.md", "high",
        "The nested copy explicitly names the root brief as canonical."),
    "latent-signals/02_requirements/scoring_function_spec.md": (
        "latent-signals/03_architecture/data_pipeline.md", "medium",
        "Pipeline design includes scoring; not every formula edit affects pipeline prose."),
    "latent-signals/02_requirements/design_constraints.md": (
        "latent-signals/03_architecture/infrastructure.md", "medium",
        "Infrastructure choices may rely on design constraints."),
    "latent-signals/01_strategy/competitive_landscape.md": (
        "latent-signals/01_strategy/positioning.md", "medium",
        "Positioning may depend on competitor analysis."),
    "latent-signals/03_architecture/data_pipeline.md": (
        "latent-signals/03_architecture/infrastructure.md", "medium",
        "Infrastructure may depend on pipeline requirements."),
    "latent-signals/01_strategy/positioning.md": (
        "latent-signals/06_business/gtm_plan.md", "medium",
        "Go-to-market plans may depend on positioning."),
}


def run(args: list[str], cwd: Path, allowed: tuple[int, ...] = (0,)) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode not in allowed:
        raise RuntimeError(f"{' '.join(args[:3])} failed ({result.returncode}): "
                           f"{result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def git(*args: str) -> str:
    return run(["git", *args], SOURCE)


def make_cases() -> list[dict[str, str | bool]]:
    revisions = git("rev-list", "--reverse", "HEAD").splitlines()
    cases: list[dict[str, str | bool]] = []
    for sha in revisions:
        parents = git("rev-list", "--parents", "-n", "1", sha).split()[1:]
        if not parents:
            continue
        parent = parents[0]
        previous = set(git("ls-tree", "-r", "--name-only", parent).splitlines())
        changed = set(git("diff", "--name-only", parent, sha, "--", "*.md").splitlines())
        for upstream, (downstream, confidence, note) in PAIRS.items():
            if upstream not in changed or upstream not in previous or downstream not in previous:
                continue
            slug = re.sub(r"[^A-Za-z0-9]+", "-", Path(upstream).stem).strip("-")[:36]
            cases.append({
                "id": f"{len(cases) + 1:02d}-{sha[:7]}-{slug}",
                "commit": sha, "parent": parent, "upstream": upstream,
                "downstream": downstream, "confidence": confidence, "link_note": note,
                "downstream_changed_same_commit": downstream in changed,
            })
    return cases


def historical(revision: str, path: str) -> str:
    return git("show", f"{revision}:{path}")


def fixture(case: dict[str, str | bool], out: Path) -> dict[str, str | bool]:
    case_dir = out / "cases" / str(case["id"])
    repo = case_dir / "repo"
    repo.mkdir(parents=True, exist_ok=False)
    run(["git", "init", "-q"], repo)
    run(["git", "config", "user.name", "Docprop benchmark"], repo)
    run(["git", "config", "user.email", "benchmark@example.invalid"], repo)
    (repo / ".doc-lattice.yml").write_text(
        "lattice_format: 2\ndocs_roots: [upstream.md, downstream.md]\n", encoding="utf-8")
    (repo / "upstream.md").write_text(
        "---\nid: upstream\n---\n" + historical(str(case["parent"]), str(case["upstream"])),
        encoding="utf-8")
    (repo / "downstream.md").write_text(
        "---\nid: downstream\nderives_from:\n  - ref: upstream\n---\n" +
        historical(str(case["parent"]), str(case["downstream"])), encoding="utf-8")
    run([str(LATTICE), "reconcile", "--all"], repo)
    run(["git", "add", "."], repo)
    run(["git", "commit", "-qm", "Baseline the candidate link"], repo)
    (repo / "upstream.md").write_text(
        "---\nid: upstream\n---\n" + historical(str(case["commit"]), str(case["upstream"])),
        encoding="utf-8")
    run(["git", "commit", "-qam", "Apply an actual historical upstream edit"], repo)
    check = json.loads(run([str(LATTICE), "check", "--format", "json"], repo, (0, 1)))
    if check["summary"]["STALE"] != 1:
        raise RuntimeError(f"Expected one stale edge for {case['id']}: {check['summary']}")
    if case["downstream_changed_same_commit"]:
        # Historical downstream diff is context, not a perfect correctness oracle:
        # a same-commit downstream edit might have other causes too.
        actual = git("diff", str(case["parent"]), str(case["commit"]), "--",
                     str(case["downstream"]))
        (case_dir / "historical_downstream.diff").write_text(actual, encoding="utf-8")
    return {**case, "repo": str(repo), "status": "prepared"}


def prepare(out: Path) -> None:
    if not SOURCE.is_dir() or not LATTICE.is_file() or not RUNNER.is_file():
        raise RuntimeError("Source clone, runner, or task-local doc-lattice executable is missing")
    cases = make_cases()
    if not 20 <= len(cases) <= 30:
        raise RuntimeError(f"Expected 20–30 candidate edits; found {len(cases)}")
    if out.exists():
        raise RuntimeError(f"Benchmark directory already exists: {out}; do not overwrite it")
    (out / "cases").mkdir(parents=True)
    prepared: list[dict[str, str | bool]] = []
    for case in cases:
        print(f"Prepare {case['id']}: {case['upstream']} -> {case['downstream']}", flush=True)
        prepared.append(fixture(case, out))
    (out / "manifest.json").write_text(json.dumps({
        "source_commit": git("rev-parse", "HEAD").strip(),
        "method": "one real upstream file edit per case; candidate whole-file link baselined in a disposable Git fixture",
        "limitations": "Links are hypotheses; historical downstream edits are context, not automatic ground truth",
        "cases": prepared,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Prepared {len(prepared)} cases at {out}; source clone untouched")


def load(out: Path) -> list[dict[str, str | bool]]:
    manifest = out / "manifest.json"
    if not manifest.is_file():
        raise RuntimeError(f"No prepared benchmark manifest: {manifest}")
    return json.loads(manifest.read_text(encoding="utf-8"))["cases"]


def execute(out: Path, *, limit: int, model: str, only: list[str]) -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY must be set in this Terminal; do not paste it into chat")
    cases = load(out)
    if only:
        cases = [case for case in cases if any(str(case["id"]).startswith(p) for p in only)]
        if not cases:
            raise RuntimeError(f"No cases match --case {' '.join(only)}")
    completed = 0
    for case in cases:
        repo = Path(str(case["repo"]))
        proposals = out / "cases" / str(case["id"]) / "proposals"
        log = proposals / "proposals.jsonl"
        if log.is_file() and log.read_text(encoding="utf-8").strip():
            continue
        if limit and completed >= limit:
            break
        print(f"Review {case['id']} ({case['confidence']} confidence link)", flush=True)
        # Deliberately no --apply. A failed request stops the batch; successful
        # logs persist and are skipped on the next invocation.
        result = subprocess.run(
            [sys.executable, "-B", str(RUNNER), "replay", "--repo", str(repo),
             "--commit", "HEAD", "--lattice-bin", str(LATTICE),
             "--proposals-dir", str(proposals), "--model", model],
            cwd=ROOT, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Case {case['id']} failed; correct it and rerun (finished cases skip)")
        completed += 1
    print(f"New proposals this invocation: {completed}. Use `benchmark.py report` to review.")


def report(out: Path) -> None:
    cases = load(out)
    review_path = out / "review.csv"
    existing: dict[str, dict[str, str]] = {}
    if review_path.exists():
        with review_path.open(newline="", encoding="utf-8") as stream:
            existing = {row["case_id"]: row for row in csv.DictReader(stream)}
    rows = []
    for case in cases:
        proposals = out / "cases" / str(case["id"]) / "proposals"
        log = proposals / "proposals.jsonl"
        entry = json.loads(log.read_text(encoding="utf-8").splitlines()[-1]) if log.is_file() else {}
        kept = existing.get(str(case["id"]), {})
        rows.append({
            "case_id": case["id"], "source_commit": str(case["commit"])[:7],
            "upstream": case["upstream"], "downstream": case["downstream"],
            "link_confidence": case["confidence"],
            "actual_downstream_changed": case["downstream_changed_same_commit"],
            "verdict": entry.get("verdict", "pending"), "decision": kept.get("decision", ""),
            "notes": kept.get("notes", ""),
            "diff": entry.get("proposal", ""), "reason": entry.get("reason", ""),
        })
    with review_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    counts = {state: sum(row["verdict"] == state for row in rows)
              for state in ("edit", "unaffected", "conflict", "pending")}
    decisions = [row for row in rows if row["verdict"] == "edit"]
    reviewed = [row for row in decisions if row["decision"] in ("accept", "modify", "reject")]
    accepted = sum(row["decision"] == "accept" for row in reviewed)
    print(f"Cases: {len(rows)}; verdicts: {counts}; review file: {review_path}")
    if decisions:
        print(f"Unmodified acceptance: {accepted}/{len(decisions)} proposed edits "
              f"({len(decisions) - len(reviewed)} still unreviewed)")
    print("Reject or modify is not the same as a false positive; inspect original context and diff.")
    print("Do not treat candidate links as verified ground truth without reviewing their semantics.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    for name in ("prepare", "run", "report"):
        command = sub.add_parser(name)
        command.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
        if name == "run":
            command.add_argument("--limit", type=int, default=0,
                                 help="Maximum new cases this invocation; 0 means all")
            command.add_argument("--model", default="gpt-4.1-mini")
            command.add_argument("--case", action="append", default=[], dest="only",
                                 help="Run only case IDs starting with this prefix (repeatable)")
    args = parser.parse_args()
    out = args.out_dir.resolve()
    try:
        if args.mode == "prepare":
            prepare(out)
        elif args.mode == "run":
            execute(out, limit=args.limit, model=args.model, only=args.only)
        else:
            report(out)
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
