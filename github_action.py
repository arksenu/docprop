"""GitHub Actions adapter for the flag-only CLI; uses only the standard library."""

from __future__ import annotations

import copy
import html
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import docprop

MARKER = "<!-- docprop-review -->"


def markdown(report: dict | None, error: str = "", limit: int = 60000) -> str:
    if report is None:
        title, detail = "Check failed", error
    else:
        report = copy.deepcopy(report)
        report["repo"] = "."
        for item in report["links"]["items"]:
            if "acknowledge" in item:
                item["acknowledge"] = shlex.join(
                    ["doc-lattice", "reconcile", item["downstream"], "--ref", item["target_ref"]])
        for item in report["mirrors"]:
            if "sync" in item:
                parts = ["python", "/path/to/docprop/docprop.py", "sync", item["copy"], "--repo", "."]
                if item.get("copy_section"):
                    parts += ["--section", item["copy_section"]]
                item["sync"] = shlex.join(parts)
        title = "Needs review" if report["needs_review"] else "Nothing needs review"
        detail = docprop.render_text(report, diff_lines=40)
    prefix = f"{MARKER}\n## docprop: {title}\n\nFlag-only review; no documents were changed.\n\n<pre>"
    suffix = "</pre>\n"
    detail = html.escape(detail)
    budget = limit - len((prefix + suffix).encode()) - 160
    if len(detail.encode()) > budget:
        detail = detail.encode()[:budget].decode("utf-8", errors="ignore")
        detail += "\n… Report truncated. See the workflow job summary and report-path output.\n"
    return prefix + detail + suffix


def check(repo: str) -> None:
    destination = Path(tempfile.mkdtemp(prefix="docprop-", dir=os.environ["RUNNER_TEMP"]))
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("docprop.py")), "check",
         "--repo", repo, "--format", "json"], capture_output=True, text=True, check=False)
    status = result.returncode
    report, error = None, result.stderr or result.stdout
    if status in (0, 1):
        try:
            report = json.loads(result.stdout)
            if report["tool"] != "docprop" or report["needs_review"] is not (status == 1):
                raise ValueError("report does not match the CLI exit code")
            summary = markdown(report, limit=900000)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            report, status, error = None, 2, f"Invalid docprop report: {exc}"
    else:
        status = 2
    if report is None:
        summary = markdown(None, error or f"docprop exited {result.returncode}.")
    else:
        (destination / "report.json").write_text(result.stdout, encoding="utf-8")
    summary_path = destination / "summary.md"
    summary_path.write_text(markdown(report, error), encoding="utf-8")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
        stream.write(summary)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write(f"exit-code={status}\nneeds-review={str(status == 1).lower()}\n"
                     f"summary-path={summary_path}\n")
        if report is not None:
            stream.write(f"report-path={destination / 'report.json'}\n")
    print(f"docprop: {'check failed' if status == 2 else 'needs review' if status == 1 else 'clean'}")


def api(method: str, path: str, data: dict | None = None):
    request = Request(
        os.environ.get("GITHUB_API_URL", "https://api.github.com") + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                 "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                 "X-GitHub-Api-Version": "2022-11-28"}, method=method)
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def comment(summary_path: str) -> None:
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        print("docprop: no pull request event; report is in the job summary.")
        return
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    pr = event["pull_request"]
    repository = event["repository"]["full_name"]
    if (pr["head"].get("repo") or {}).get("full_name") != repository:
        print("docprop: fork pull request; report is in the job summary.")
        return
    if not os.environ.get("GITHUB_TOKEN"):
        print("::warning::docprop: no comment token; report is in the job summary.")
        return
    body = Path(summary_path).read_text(encoding="utf-8")
    issue_path = f"/repos/{repository}/issues/{pr['number']}/comments"
    try:
        page = 1
        while True:
            comments = api("GET", f"{issue_path}?per_page=100&page={page}")
            for existing in comments:
                if (existing.get("user", {}).get("login") == "github-actions[bot]"
                        and existing["body"].startswith(MARKER)):
                    if existing["body"] != body:
                        api("PATCH", f"/repos/{repository}/issues/comments/{existing['id']}",
                            {"body": body})
                    return
            if len(comments) < 100:
                break
            page += 1
        api("POST", issue_path, {"body": body})
    except HTTPError as exc:
        if exc.code != 403:
            raise
        print("::warning::docprop: comment forbidden; grant pull-requests: write. "
              "Report is in the job summary.")


if __name__ == "__main__":
    try:
        if sys.argv[1] == "check":
            check(sys.argv[2])
        elif sys.argv[1] == "comment":
            comment(sys.argv[2])
        else:
            raise ValueError("expected check or comment")
    except (OSError, ValueError, KeyError) as exc:
        print(f"docprop action: {exc}", file=sys.stderr)
        sys.exit(2)
