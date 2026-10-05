#!/usr/bin/env python3
"""docprop v1: flag documents that may be stale after an upstream change.

Flag-only by design. docprop never rewrites a linked document and never calls an
LLM: in the v0 experiment (EXPERIMENT.md) only 2 of 9 model-written edits were
usable unmodified.

Links and baselines belong to doc-lattice. For each stale link, docprop:
  1. finds the exact upstream revision the reviewer last acknowledged, by having
     doc-lattice hash historical revisions until one matches the stored ``seen``;
  2. shows what changed in the linked upstream section since then;
  3. lists downstream passages that still repeat wording the change removed or
     replaced, so the reviewer knows where to look first.

Exact copies ("mirrors") are declared in ``.docprop.toml`` and compared text for
text; ``sync`` overwrites a copy's body with its canonical body.

Exit codes: 0 nothing needs review, 1 something needs review, 2 error.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import difflib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
from typing import Any

EXIT_CLEAN, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 2
PROBE_ID = "docprop-baseline-probe"
MAX_HISTORY = 100
DEFAULT_LATTICE = Path(__file__).resolve().parent / ".work/tools/venv/bin/doc-lattice"

WORD = re.compile(r"[A-Za-z0-9]+(?:[.'\u2019/-][A-Za-z0-9]+)*%?")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s{0,3}(```|~~~)")
ITEM_START = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|\|)")
STOPWORDS = frozenset(
    "a an and are as at be been but by can could did do does for from had has have if in "
    "into is it its may might must no not of on or our over per should so than that the "
    "their them then there these they this those to under via vs was we were what when "
    "which while who will with would you your".split()
)


class DocpropError(Exception):
    """An expected failure, reported without a traceback (exit 2)."""


# --------------------------------------------------------------------------- helpers

def run(args: list[str], cwd: Path, allowed: tuple[int, ...] = (0,)) -> str:
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise DocpropError(f"Cannot run {args[0]}: {exc}") from exc
    if result.returncode not in allowed:
        detail = (result.stderr.strip() or result.stdout.strip())[:600]
        raise DocpropError(f"{Path(args[0]).name} {' '.join(args[1:2])} exited "
                           f"{result.returncode}: {detail}")
    return result.stdout


def normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read(path: Path) -> str:
    try:
        return normalize(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise DocpropError(f"Cannot read {path}: {exc}") from exc


def display(repo: Path, path: Path | None) -> str | None:
    if path is None:
        return None
    return path.relative_to(repo).as_posix() if path.is_relative_to(repo) else str(path)


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def default_lattice() -> str:
    """doc-lattice installed next to this Python (a venv), else the local workspace, else PATH."""
    for candidate in (Path(sys.executable).with_name("doc-lattice"), DEFAULT_LATTICE):
        if candidate.is_file():
            return str(candidate)
    return "doc-lattice"


def lattice_json(binary: str, cwd: Path, subcommand: str) -> dict[str, Any]:
    raw = run([binary, subcommand, "--format", "json"], cwd,
              allowed=(0, 1) if subcommand == "check" else (0,))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DocpropError(f"doc-lattice {subcommand} did not return JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise DocpropError(f"doc-lattice {subcommand} returned an unexpected payload")
    return data


# ----------------------------------------------------------------- document structure

def metadata_end(text: str) -> int:
    """Offset just past a doc-lattice metadata envelope, or 0 when there is none."""
    for opener, closer in (("---\n", "\n---\n"), ("<!-- doc-lattice\n", "\n-->\n")):
        if text.startswith(opener):
            end = text.find(closer, len(opener) - 1)
            if end < 0:
                raise DocpropError("Unclosed metadata block at the top of a document")
            return end + len(closer)
    return 0


def split_header(text: str) -> tuple[str, str]:
    """Split metadata plus leading HTML comments (e.g. a 'synced copy' note) from the body."""
    position = metadata_end(text)
    while True:
        rest = text[position:]
        stripped = rest.lstrip("\n")
        if not stripped.startswith("<!--"):
            break
        close = stripped.find("-->")
        if close < 0:
            break
        position += len(rest) - len(stripped) + close + 3
        if text.startswith("\n", position):
            position += 1
    return text[:position], text[position:]


def canonical_lines(text: str) -> list[str]:
    lines = [line.rstrip() for line in normalize(text).split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def section_text(text: str, target_ref: str) -> str:
    """The body (whole-file ref) or one section (``id#anchor``), as doc-lattice addresses it."""
    body = text[metadata_end(text):]
    if "#" not in target_ref:
        return body
    anchor = target_ref.split("#", 1)[1]
    try:
        from doc_lattice.markdown_compat import anchor_ids
        from doc_lattice.sections import build_toc, section_spans, split_body_lines
        from doc_lattice.sections import section_text as span_text
    except ImportError as exc:
        raise DocpropError("Run docprop with the Python environment that has doc-lattice "
                           "installed (e.g. .work/tools/venv/bin/python)") from exc
    headings = build_toc(body)
    ids = anchor_ids(headings)
    matches = [index for index, heading_id in enumerate(ids) if heading_id == anchor]
    if len(matches) != 1:
        raise DocpropError(f"Section {target_ref} is not uniquely addressable in this revision")
    spans = section_spans(headings, len(split_body_lines(body)))
    return span_text(body, spans[matches[0]])


def safe_section(text: str, target_ref: str) -> str | None:
    try:
        return section_text(text, target_ref)
    except DocpropError:
        return None


@dataclass
class Passage:
    line: int
    heading: str
    text: str


def passages(text: str) -> list[Passage]:
    """Split a document into reviewable passages: paragraphs, list items, table rows,
    headings and fenced blocks, each with its 1-based line number and nearest heading."""
    start = metadata_end(text)
    first_line = text[:start].count("\n") + 1
    result: list[Passage] = []
    heading, block, block_line, fence = "", [], 0, None

    def flush() -> None:
        nonlocal block
        if block:
            result.append(Passage(block_line, heading, "\n".join(block)))
            block = []

    for offset, line in enumerate(text[start:].split("\n")):
        number = first_line + offset
        if fence:
            block.append(line)
            if line.strip().startswith(fence):
                flush()
                fence = None
            continue
        if match := FENCE.match(line):
            flush()
            fence, block_line, block = match.group(1), number, [line]
            continue
        if match := HEADING.match(line):
            flush()
            heading = match.group(1)
            result.append(Passage(number, heading, line))
            continue
        if not line.strip():
            flush()
            continue
        if ITEM_START.match(line) or not block:
            flush()
            block_line, block = number, [line]
        else:
            block.append(line)
    flush()
    return result


# ------------------------------------------------------------------ change signals

def words(text: str) -> list[str]:
    return [match.group(0).lower() for match in WORD.finditer(text)]


def trigrams(sequence: list[str]) -> set[tuple[str, ...]]:
    return {tuple(sequence[i:i + 3]) for i in range(len(sequence) - 2)}


def informative(gram: tuple[str, ...]) -> bool:
    """At least two content words, so filler such as 'that the pipeline' never matches."""
    return sum(word not in STOPWORDS and len(word) > 1 for word in gram) >= 2


def removed_signals(old: str, new: str) -> tuple[set[tuple[str, ...]], set[str]]:
    """Wording the change took away: 3-word phrases from removed/replaced upstream lines that
    no longer occur anywhere in the new text, plus numbers and versions that disappeared."""
    old_lines, new_lines = old.split("\n"), new.split("\n")
    new_words = words(new)
    new_grams, new_vocabulary = trigrams(new_words), set(new_words)
    phrases: set[tuple[str, ...]] = set()
    numbers: set[str] = set()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, _, _ in matcher.get_opcodes():
        if tag not in ("replace", "delete"):
            continue
        removed = words("\n".join(old_lines[i1:i2]))
        phrases |= {gram for gram in trigrams(removed) if gram not in new_grams and informative(gram)}
        numbers |= {word for word in removed if len(word) > 1 and any(c.isdigit() for c in word)
                    and word not in new_vocabulary}
    return phrases, numbers


def matched_spans(text: str, phrases: set[tuple[str, ...]], numbers: set[str]) -> tuple[list[str], int]:
    """Original-text spans of a passage that repeat removed wording, and how many words matched."""
    found = list(WORD.finditer(text))
    sequence = [match.group(0).lower() for match in found]
    covered = [False] * len(sequence)
    for i in range(len(sequence) - 2):
        if tuple(sequence[i:i + 3]) in phrases:
            covered[i] = covered[i + 1] = covered[i + 2] = True
    for i, word in enumerate(sequence):
        if word in numbers:
            covered[i] = True
    spans: list[str] = []
    i = 0
    while i < len(sequence):
        if not covered[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(sequence) and covered[j + 1]:
            j += 1
        span = " ".join(re.sub(r"[*`]+", "", text[found[i].start():found[j].end()]).split())
        if span not in spans:
            spans.append(span)
        i = j + 1
    return spans, sum(covered)


def unified(old: str, new: str, old_name: str, new_name: str) -> list[str]:
    return list(difflib.unified_diff(old.split("\n"), new.split("\n"), old_name, new_name,
                                     n=1, lineterm=""))


# ---------------------------------------------------------------------- baselines

@dataclass
class Baseline:
    commit: str
    short: str
    date: str
    subject: str
    text: str


def in_git(repo: Path) -> bool:
    probe = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo,
                           capture_output=True, text=True, check=False)
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def history(repo: Path, relative: str) -> list[tuple[str, str]]:
    """(commit, path at that commit) for commits touching a file, newest first; follows renames."""
    out = run(["git", "log", f"-n{MAX_HISTORY}", "--follow", "--format=%x00%H", "--name-only",
               "--", relative], repo)
    revisions = []
    for chunk in out.split("\x00")[1:]:
        lines = [line for line in chunk.split("\n") if line.strip()]
        if lines:
            revisions.append((lines[0], lines[-1] if len(lines) > 1 else relative))
    return revisions


class BaselineFinder:
    """Locate the committed upstream revision whose target hash equals a link's ``seen``.

    doc-lattice computes every hash (section hashes include the parent-heading chain), so
    docprop never re-implements its algorithm: each candidate revision is checked in a
    throwaway two-file doc-lattice project. Results are cached per blob.
    """

    def __init__(self, repo: Path, binary: str) -> None:
        self.repo, self.binary = repo, binary
        self.hashes: dict[tuple[str, str], str | None] = {}

    def target_hash(self, text: str, target_ref: str) -> str | None:
        # Hashes cover only the body, so give the revision a fresh minimal header. This also
        # handles revisions from before the file had an id (markings added later) and drops
        # the revision's own links, whose targets are absent from the probe project.
        try:
            body = normalize(text)[metadata_end(normalize(text)):]
        except DocpropError:
            return None
        target_id = target_ref.split("#", 1)[0]
        with tempfile.TemporaryDirectory(prefix="docprop-probe-") as tmp:
            root = Path(tmp)
            (root / "upstream.md").write_text(f"---\nid: {json.dumps(target_id)}\n---\n{body}",
                                              encoding="utf-8")
            (root / f"{PROBE_ID}.md").write_text(
                f"---\nid: {PROBE_ID}\nderives_from:\n  - ref: {json.dumps(target_ref)}\n---\n"
                "probe\n", encoding="utf-8")
            (root / ".doc-lattice.yml").write_text(
                f"lattice_format: 2\ndocs_roots: [upstream.md, {PROBE_ID}.md]\n", encoding="utf-8")
            try:
                data = lattice_json(self.binary, root, "check")
            except DocpropError:
                return None  # that revision is not loadable by doc-lattice
        for edge in data.get("edges", []):
            if isinstance(edge, dict) and edge.get("source_id") == PROBE_ID:
                actual = edge.get("actual")
                return actual if isinstance(actual, str) else None
        return None

    def find(self, upstream: Path, target_ref: str, expected: str) -> Baseline | None:
        relative = upstream.relative_to(self.repo).as_posix()
        for commit, path in history(self.repo, relative):
            blob = subprocess.run(["git", "rev-parse", "--verify", "-q", f"{commit}:{path}"],
                                  cwd=self.repo, capture_output=True, text=True, check=False)
            if blob.returncode != 0:
                continue  # e.g. the commit deleted the file
            key = (blob.stdout.strip(), target_ref)
            if key not in self.hashes:
                text = run(["git", "cat-file", "blob", key[0]], self.repo)
                self.hashes[key] = self.target_hash(text, target_ref)
            if self.hashes[key] == expected:
                meta = run(["git", "show", "-s", "--format=%h%x09%as%x09%s", commit],
                           self.repo).strip().split("\t", 2)
                text = normalize(run(["git", "cat-file", "blob", key[0]], self.repo))
                short, date, subject = (meta + ["", "", ""])[:3]
                return Baseline(commit, short, date, subject, text)
        return None


# ------------------------------------------------------------------- link checks

def lattice_enabled(repo: Path) -> bool:
    return (repo / ".doc-lattice.yml").is_file() or (repo / "docs").is_dir()


def command_in(repo: Path, parts: list[str]) -> str:
    rendered = " ".join(shlex.quote(part) for part in parts)
    return rendered if repo == Path.cwd().resolve() else f"cd {shlex.quote(str(repo))} && {rendered}"


def explain_stale(repo: Path, finder: BaselineFinder | None, paths: dict[str, Path],
                  edge: dict[str, Any], max_passages: int, base: str | None) -> dict[str, Any]:
    source, target = edge["source_id"], edge["target_ref"]
    upstream, downstream = paths.get(target.split("#", 1)[0]), paths.get(source)
    out: dict[str, Any] = {"baseline": None, "upstream_diff": [], "added": 0, "removed": 0,
                           "passages": [], "passages_total": 0}
    if upstream is None or downstream is None:
        out["note"] = "Link endpoint is not in the doc-lattice graph."
        return out
    relative = display(repo, upstream)
    old_text = None
    expected = edge.get("expected")
    baseline = finder.find(upstream, target, expected) if finder and isinstance(expected, str) else None
    if baseline:
        out["baseline"] = {"commit": baseline.commit, "short": baseline.short,
                           "date": baseline.date, "subject": baseline.subject,
                           "how": "matched the link's seen hash"}
        old_text = baseline.text
    elif base and finder:
        try:
            old_text = normalize(run(["git", "show", f"{base}:{relative}"], repo))
            out["baseline"] = {"commit": base, "how": "--base (reviewed version not in history)"}
        except DocpropError:
            out["note"] = f"{relative} does not exist at --base {base}."
    if old_text is None:
        out.setdefault("note", "The reviewed upstream version is not in Git history (it may never "
                               "have been committed), so no upstream diff is shown. Pass --base "
                               "REV to compare against a chosen revision.")
        return out
    old_section, new_section = safe_section(old_text, target), safe_section(read(upstream), target)
    if old_section is None or new_section is None:
        out["note"] = f"Could not extract {target} from both versions."
        return out
    diff = unified(old_section, new_section, f"{relative} (reviewed)", f"{relative} (current)")
    out["upstream_diff"] = diff
    out["added"] = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    out["removed"] = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    if canonical_lines(old_section) == canonical_lines(new_section):
        out["note"] = ("The section text is unchanged; the link went stale because a parent "
                       "heading or the section's position changed.")
    phrases, numbers = removed_signals(old_section, new_section)
    scored = []
    for passage in passages(read(downstream)):
        spans, score = matched_spans(passage.text, phrases, numbers)
        if spans:
            scored.append((score, passage, spans))
    scored.sort(key=lambda item: (-item[0], item[1].line))
    shown = sorted(scored[:max_passages], key=lambda item: item[1].line)
    out["passages_total"] = len(scored)
    out["passages"] = [{"line": p.line, "heading": p.heading, "matched": spans[:4],
                        "excerpt": clip(p.text, 300)} for _, p, spans in shown]
    return out


def check_links(repo: Path, binary: str, max_passages: int, base: str | None) -> dict[str, Any]:
    if not lattice_enabled(repo):
        return {"enabled": False, "items": [], "summary": {},
                "note": "No .doc-lattice.yml (or docs/ folder) in this repo, so link checks were "
                        "skipped."}
    graph = lattice_json(binary, repo, "graph")
    paths: dict[str, Path] = {}
    for node in graph.get("nodes", []):
        if isinstance(node, dict) and isinstance(node.get("id"), str) and isinstance(node.get("path"), str):
            path = Path(node["path"])
            paths[node["id"]] = (path if path.is_absolute() else repo / path).resolve()
    report = lattice_json(binary, repo, "check")
    finder = BaselineFinder(repo, binary) if in_git(repo) else None
    items = []
    for edge in report.get("edges", []):
        if not isinstance(edge, dict) or edge.get("state") == "OK":
            continue
        state, source, target = edge.get("state"), edge.get("source_id"), edge.get("target_ref")
        if not isinstance(source, str) or not isinstance(target, str):
            raise DocpropError(f"Unexpected doc-lattice edge: {edge}")
        item: dict[str, Any] = {
            "state": state, "downstream": source, "target_ref": target,
            "downstream_path": display(repo, paths.get(source)),
            "upstream_path": display(repo, paths.get(target.split("#", 1)[0])),
        }
        reconcile = command_in(repo, [binary, "reconcile", source, "--ref", target])
        if state == "STALE":
            item.update(explain_stale(repo, finder, paths, edge, max_passages, base))
            item["acknowledge"] = reconcile
        elif state == "UNRECONCILED":
            item["advice"] = "This link has no baseline yet. Check the pair once, then record it."
            item["acknowledge"] = reconcile
        else:
            item["advice"] = ("The link target is missing or ambiguous; fix the ref "
                              "(`doc-lattice check` shows details).")
            if edge.get("collision"):
                item["collision"] = edge["collision"]
        items.append(item)
    return {"enabled": True, "items": items, "summary": report.get("summary", {})}


# ------------------------------------------------------------------------ mirrors

@dataclass
class Mirror:
    canonical: str
    copy: str


def inside(repo: Path, value: str) -> Path:
    candidate = (repo / value).resolve()
    if not candidate.is_relative_to(repo) or candidate.suffix.lower() != ".md":
        raise DocpropError(f"Mirror path must be a Markdown file inside the repo: {value}")
    return candidate


def load_config(repo: Path, path: Path | None) -> list[Mirror]:
    config = path or repo / ".docprop.toml"
    if not config.is_file():
        if path:
            raise DocpropError(f"Config file not found: {path}")
        return []
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise DocpropError(f"Cannot read {config}: {exc}") from exc
    unknown = set(data) - {"mirror"}
    if unknown:
        raise DocpropError(f"Unknown key(s) in {config}: {', '.join(sorted(unknown))}")
    entries = data.get("mirror", [])
    if not isinstance(entries, list):
        raise DocpropError(f"'mirror' in {config} must be an array of tables ([[mirror]])")
    mirrors: list[Mirror] = []
    copies: set[Path] = set()
    for number, entry in enumerate(entries, 1):
        if (not isinstance(entry, dict) or set(entry) != {"canonical", "copy"}
                or not all(isinstance(value, str) for value in entry.values())):
            raise DocpropError(f"[[mirror]] #{number} in {config} needs exactly the string keys "
                               "'canonical' and 'copy'")
        canonical, copy = inside(repo, entry["canonical"]), inside(repo, entry["copy"])
        if canonical == copy:
            raise DocpropError(f"[[mirror]] #{number}: canonical and copy are the same file")
        if copy in copies:
            raise DocpropError(f"[[mirror]] #{number}: {entry['copy']} is already declared as a copy")
        copies.add(copy)
        mirrors.append(Mirror(display(repo, canonical), display(repo, copy)))
    return mirrors


def heading_before(lines: list[str], index: int) -> str:
    for line in reversed(lines[: min(index, len(lines) - 1) + 1]):
        if match := HEADING.match(line):
            return match.group(1)
    return "(top of document)"


def mirror_status(repo: Path, mirror: Mirror) -> dict[str, Any]:
    item: dict[str, Any] = {"canonical": mirror.canonical, "copy": mirror.copy}
    missing = [name for name in (mirror.canonical, mirror.copy) if not (repo / name).is_file()]
    if missing:
        return {**item, "state": "MISSING", "missing": missing}
    canonical = canonical_lines(split_header(read(repo / mirror.canonical))[1])
    copy = canonical_lines(split_header(read(repo / mirror.copy))[1])
    if canonical == copy:
        return {**item, "state": "IN_SYNC"}
    sections: list[str] = []
    changed = 0
    matcher = difflib.SequenceMatcher(None, copy, canonical, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed += max(i2 - i1, j2 - j1)
        name = heading_before(canonical, j1)
        if name not in sections:
            sections.append(name)
    diff = unified("\n".join(copy), "\n".join(canonical), f"{mirror.copy} (copy)",
                   f"{mirror.canonical} (canonical)")
    return {**item, "state": "DRIFTED", "sections": sections, "changed_lines": changed, "diff": diff}


def sync_mirror(repo: Path, mirrors: list[Mirror], copy_arg: str) -> str:
    target = inside(repo, copy_arg)
    mirror = next((m for m in mirrors if (repo / m.copy).resolve() == target), None)
    if mirror is None:
        raise DocpropError(f"{copy_arg} is not a declared mirror copy (see .docprop.toml)")
    try:
        raw_copy = target.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DocpropError(f"Cannot read {target}: {exc}") from exc
    header, _ = split_header(normalize(raw_copy))
    _, body = split_header(read(repo / mirror.canonical))
    updated = header + (body.lstrip("\n") if header else body)
    if "\r\n" in raw_copy:
        updated = updated.replace("\n", "\r\n")
    if updated == raw_copy:
        return f"{mirror.copy} already matches {mirror.canonical}; nothing written."
    target.write_bytes(updated.encode("utf-8"))
    return (f"Synced {mirror.copy} from {mirror.canonical} (header kept). Review the change with "
            "`git diff`; if the copy is doc-lattice tracked, run `docprop.py check` again.")


# ------------------------------------------------------------------------ report

def build_report(repo: Path, mirrors: list[Mirror], args: argparse.Namespace) -> dict[str, Any]:
    links = check_links(repo, args.lattice_bin, args.max_passages, args.base)
    mirror_items = [mirror_status(repo, mirror) for mirror in mirrors]
    script = Path(__file__).resolve()
    for item in mirror_items:
        if item["state"] == "DRIFTED":
            parts = [sys.executable, str(script), "sync", item["copy"], "--repo", str(repo)]
            if args.config:
                parts += ["--config", str(Path(args.config).expanduser().resolve())]
            item["sync"] = " ".join(shlex.quote(part) for part in parts)
    states = [item["state"] for item in links["items"]]
    counts = {
        "stale": states.count("STALE"),
        "unbaselined": states.count("UNRECONCILED"),
        "broken": states.count("BROKEN") + states.count("AMBIGUOUS"),
        "mirrors_drifted": sum(item["state"] != "IN_SYNC" for item in mirror_items),
        "mirrors_in_sync": sum(item["state"] == "IN_SYNC" for item in mirror_items),
    }
    needs_review = any(counts[key] for key in ("stale", "unbaselined", "broken", "mirrors_drifted"))
    return {"tool": "docprop", "version": 1, "repo": str(repo), "links": links,
            "mirrors": mirror_items, "counts": counts, "needs_review": needs_review}


def render_link(item: dict[str, Any], diff_lines: int) -> list[str]:
    out = [f"{item['state']}  {item['downstream_path'] or item['downstream']}  <-  {item['target_ref']}"]
    if item["state"] != "STALE":
        out.append(f"  {item['advice']}")
    else:
        baseline = item.get("baseline")
        if baseline and baseline.get("short"):
            out.append(f"  Last reviewed against: {baseline['short']} ({baseline['date']}) "
                       f"\"{clip(baseline['subject'], 70)}\"")
        elif baseline:
            out.append(f"  Compared with --base {baseline['commit']}")
        if item.get("note"):
            out.append(f"  Note: {item['note']}")
        body = [line for line in item.get("upstream_diff", []) if not line.startswith(("---", "+++"))]
        if body:
            out.append(f"  Upstream change since then: +{item['added']} / -{item['removed']} lines")
            out += [f"    {clip(line, 150)}" for line in body[:diff_lines]]
            if len(body) > diff_lines:
                out.append(f"    ... {len(body) - diff_lines} more diff lines "
                           "(--diff-lines N, or --format json for everything)")
        if item.get("passages"):
            total, shown = item["passages_total"], len(item["passages"])
            extra = f", top {shown} shown" if total > shown else ""
            out.append(f"  Look here first: {total} downstream passage(s) still use wording the "
                       f"change removed{extra}")
            for passage in item["passages"]:
                context = f" [{clip(passage['heading'], 40)}]" if passage["heading"] else ""
                quoted = ", ".join(f'"{clip(span, 60)}"' for span in passage["matched"])
                out.append(f"    line {passage['line']}{context}: {quoted}")
                out.append(f"      {clip(passage['excerpt'], 140)}")
        elif body:
            out.append("  No downstream passage repeats wording the change removed. New or "
                       "reworded meaning can still")
            out.append("  matter: read the change above and skim the document.")
    if item.get("acknowledge"):
        out.append("  Once reviewed (updated, or confirmed fine as is), record it:")
        out.append(f"    {item['acknowledge']}")
    return out + [""]


def render_mirror(item: dict[str, Any], diff_lines: int) -> list[str]:
    if item["state"] == "IN_SYNC":
        return [f"MIRROR OK  {item['copy']} matches {item['canonical']}", ""]
    if item["state"] == "MISSING":
        return [f"MIRROR MISSING  {', '.join(item['missing'])} (declared in .docprop.toml)", ""]
    out = [f"MIRROR DRIFTED  {item['copy']}  (copy of {item['canonical']})",
           f"  {item['changed_lines']} line(s) differ, in: {'; '.join(item['sections'])}"]
    body = [line for line in item["diff"] if not line.startswith(("---", "+++"))]
    out += [f"    {clip(line, 150)}" for line in body[:diff_lines]]
    if len(body) > diff_lines:
        out.append(f"    ... {len(body) - diff_lines} more diff lines")
    out += ["  To make the copy match the canonical file (plain copy, header kept):",
            f"    {item['sync']}", ""]
    return out


def render_text(report: dict[str, Any], diff_lines: int) -> str:
    out = [f"docprop check: {report['repo']}", ""]
    links = report["links"]
    if not links["enabled"]:
        out += [links["note"], ""]
    for item in links["items"]:
        out += render_link(item, diff_lines)
    for item in report["mirrors"]:
        out += render_mirror(item, diff_lines)
    counts = report["counts"]
    if not links["enabled"] and not report["mirrors"]:
        out.append("Nothing to check: add doc-lattice links, or declare mirrors in .docprop.toml.")
    out.append(f"Summary: {counts['stale']} stale link(s), {counts['unbaselined']} without a "
               f"baseline, {counts['broken']} broken/ambiguous, {counts['mirrors_drifted']} "
               f"mirror(s) drifted, {counts['mirrors_in_sync']} in sync.")
    out.append("Needs review." if report["needs_review"] else "Nothing needs review.")
    return "\n".join(out) + "\n"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="docprop", description="Flag documents that may be stale after an upstream change. "
                                    "Never rewrites linked documents.")
    sub = p.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="Report stale links and mirror drift "
                                         "(exit 1 when something needs review)")
    check.add_argument("--base", help="Fallback revision when the reviewed upstream version "
                                      "is not in Git history")
    check.add_argument("--lattice-bin", default=default_lattice(),
                       help="doc-lattice executable (default: next to this Python, else PATH)")
    check.add_argument("--format", choices=("text", "json"), default="text")
    check.add_argument("--max-passages", type=int, default=8,
                       help="Passages listed per stale link (default 8)")
    check.add_argument("--diff-lines", type=int, default=40,
                       help="Diff lines shown per item in text output (default 40)")
    check.add_argument("--output", type=Path, help="Write the report to a file instead of stdout")
    sync = sub.add_parser("sync", help="Overwrite a declared mirror copy's body with its "
                                       "canonical body (header kept)")
    sync.add_argument("copy", help="The copy's path, relative to --repo")
    for command in (check, sync):
        command.add_argument("--repo", type=Path, default=Path("."),
                             help="Documentation repository (default: current directory)")
        command.add_argument("--config", type=Path,
                             help="docprop config (default: REPO/.docprop.toml)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        repo = args.repo.expanduser().resolve()
        if not repo.is_dir():
            raise DocpropError(f"Not a directory: {args.repo}")
        config = args.config.expanduser().resolve() if args.config else None
        mirrors = load_config(repo, config)
        if args.command == "sync":
            print(sync_mirror(repo, mirrors, args.copy))
            return EXIT_CLEAN
        if args.max_passages < 1 or args.diff_lines < 1:
            raise DocpropError("--max-passages and --diff-lines must be at least 1")
        report = build_report(repo, mirrors, args)
        if args.format == "json":
            text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        else:
            text = render_text(report, args.diff_lines)
        if args.output:
            args.output.write_text(text, encoding="utf-8")
            print(f"Report written to {args.output}", file=sys.stderr)
        else:
            sys.stdout.write(text)
        return EXIT_FINDINGS if report["needs_review"] else EXIT_CLEAN
    except DocpropError as exc:
        print(f"docprop: error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
