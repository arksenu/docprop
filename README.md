# docprop: flag stale docs, never rewrite them (v1 core, local)

When an upstream document changes, docprop tells you **which dependent documents need a look, what changed, and where to look first**. It never edits a linked document and never calls an AI model. (The v0 experiment had a model write the updates; only 2 of 9 were usable as written. See [EXPERIMENT.md](EXPERIMENT.md).)

It does two jobs:

| Job | What you declare | What docprop reports |
| --- | --- | --- |
| **Stale links** | "This doc derives from that doc (or section)," via [doc-lattice](https://github.com/Guardantix/doc-lattice) frontmatter | The upstream version you last reviewed, what changed since, and downstream passages that still use wording the change removed |
| **Exact copies** ("mirrors") | "This file is a verbatim copy of that one," in `.docprop.toml` | Whether the copy drifted, in which sections; `sync` makes it match with a plain copy |

## Roadmap status (v1: GitHub change-impact reviewer)

The v0 gate failed, so v1 is **flag-only**: per the plan, "summarizes" rewrites stay off until the prompts improve. This folder is the local core of v1. The GitHub half isn't built yet.

| Planned v1 item | Status |
| --- | --- |
| Stateless graph from frontmatter, built on doc-lattice | **Done.** Applied to latent-signals in [PR #1](https://github.com/arksenu/latent-signals/pull/1) (not merged) |
| "assumes" links: flag with explanation | **Done.** Shows the reviewed baseline, the upstream diff, and where to look first |
| "quotes" links: deterministic sync | **Partly done.** Whole-file mirrors work; section-level quotes don't yet |
| "summarizes" links: LLM rewrite | **Dropped by the v0 gate** (2 of 9 usable) |
| Link type and downstream section anchor in the schema | **Not started.** doc-lattice rejects unknown keys, so these must live in docprop's own config (a superset), not in doc-lattice headers |
| Meaning-change gate (skip wording-only edits) | **Not started** |
| GitHub Action on PRs, with one summary comment | **Not started.** The next step. It needs `fetch-depth: 0` so baselines can be found in history |
| Lint: reject cycles; block derived edits that contradict the source | **Not started.** doc-lattice has no cycle check |

## Setup

Needs Python 3.13+ and [uv](https://docs.astral.sh/uv/). `pip install doc-lattice` into any venv also works.

```bash
git clone https://github.com/arksenu/docprop && cd docprop
uv venv .venv --python '>=3.13'
uv pip install --python .venv/bin/python doc-lattice
```

docprop uses the `doc-lattice` installed next to the Python that runs it, so `.venv/bin/python docprop.py ...` works without activating the venv.

## Try it now

From the docprop folder, against a clone of [latent-signals](https://github.com/arksenu/latent-signals):

```bash
git clone https://github.com/arksenu/latent-signals ../latent-signals

# 1. Real drift: the 01_strategy product-brief copy no longer matches the root brief.
.venv/bin/python docprop.py check --repo ../latent-signals --config examples/latent-signals.docprop.toml

# 2. With dependency markings (PR #1 branch): user_flows.md went stale when the brief changed in d0ed023.
git -C ../latent-signals switch docprop/dependency-markings
.venv/bin/python docprop.py check --repo ../latent-signals
```

Both exit with code 1 ("needs review"). Neither changes any file.

## Reading a stale-link report

```text
STALE  downstream.md  <-  upstream
  Last reviewed against: 670ad5a (2026-10-03) "Baseline the candidate link"
  Upstream change since then: +13 / -13 lines
    -**Job 2 — Expansion:** "I have an existing product. Where should I expand?" ...
    +**Job 1 — Market Discovery (simpler variant):** ...
  Look here first: 11 downstream passage(s) still use wording the change removed, top 8 shown
    line 74 [Two Jobs to Be Done]: "Job 2 — Expansion", "existing product. Where should", ...
  Once reviewed (updated, or confirmed fine as is), record it:
    doc-lattice reconcile downstream --ref upstream
```

- **Last reviewed against** is the exact commit whose upstream text you approved. docprop finds it on its own: it has doc-lattice hash older revisions until one matches the link's stored baseline. You don't need to say where to diff from.
- **Upstream change** covers only the linked section (or the whole file for a whole-file link).
- **Look here first** lists downstream paragraphs, list items, or headings that repeat a 3-word phrase or a number/version that the change removed. These are pointers, not verdicts. If nothing repeats removed wording, docprop says so and tells you to skim. New meaning can matter even when no old words remain.
- **Record it** is the doc-lattice command that stores the new baseline after you've updated the doc (or decided it's fine). Commit after running it, so the next check can find this version.

## Using it on your own docs

### Stale links (doc-lattice)

1. Give every linked file an `id`, and add `derives_from` to the dependent one:

   ```yaml
   ---
   id: positioning
   derives_from:
     - ref: competitive-landscape#market-gaps   # a section; or just competitive-landscape
   ---
   ```

2. Add `.doc-lattice.yml` at the repo root. doc-lattice requires an `id` in **every** file under `docs_roots`, so list only annotated files or folders:

   ```yaml
   lattice_format: 2
   docs_roots: [latent-signals/01_strategy/positioning.md, latent-signals/01_strategy/competitive_landscape.md]
   ```

3. Record the starting baselines, then commit: `.venv/bin/doc-lattice reconcile --all`.
4. After later edits, run `docprop.py check --repo PATH`.

### Exact copies

Put `.docprop.toml` in the repo, or pass `--config FILE`. Paths are relative to the repo:

```toml
[[mirror]]
canonical = "product_brief.md"
copy = "latent-signals/01_strategy/product_brief.md"
```

`docprop.py sync COPY --repo PATH` replaces the copy's body with the canonical body. It keeps the copy's own header: frontmatter, plus leading HTML comments such as `<!-- Canonical version: ... -->`. Review with `git diff`, then commit.

### What to link (from the benchmark)

- **Link** documents that are derived from another: a summary, a spec built from a brief, a FAQ built from a pricing page.
- **Declare** verbatim copies as mirrors, not links. Comparing them needs no heuristics.
- **Don't link** chronological records such as dev logs and decision logs. They record what happened and don't derive from anything. Flags on them are noise, and v0 showed rewriting them invents history.

## Commands

| Command | Purpose |
| --- | --- |
| `docprop.py check [--repo P] [--config F]` | Report stale links and mirror drift |
| `  --format json` | Machine-readable report: full diffs, every passage, counts |
| `  --base REV` | Fallback when the reviewed version was never committed |
| `  --max-passages N`, `--diff-lines N` | Output size (defaults 8 and 40) |
| `  --output FILE` | Write the report to a file |
| `docprop.py sync COPY [--repo P] [--config F]` | Make a declared copy match its canonical file |

Exit codes: **0** nothing needs review, **1** something needs review (a stale link, a link without a baseline, a broken link, or mirror drift), **2** error. Exit code 1 makes `check` usable as a CI gate later.

## Evaluation on 24 real latent-signals edits

`check` ran on all 24 historical fixtures, and its "look here first" passages were compared with what the author actually changed downstream in the same commit. (The fixtures and evaluation script live in a local, git-ignored `.work/` folder.)

| Relationship (cases) | Baseline found | Passages flagged | Assessment |
| --- | --- | --- | --- |
| Exact copies (16, 24) | 2/2 | 13 | **All 13 correct**: each is a line that differs from the canonical brief. Covers 11 of the 15 stale lines in case 24. |
| Stubs (17–21) | 5/5 | 5 | All are lines the author changed, but only because the stubs share "Not yet written" boilerplate |
| Logs and summaries (01–15, 22, 23) | 17/17 | 29 | 9 matched real author fixes (13–15, the "v1 validated" wording cleanup). Case 06's 12 flags are old decision-log entries: history, not stale text. |

So: baselines were found 24/24, and pointers into copies and derived text are reliable. The pointers can't anticipate *new* content an author adds, such as a new log entry or a newly written section; 23 of the author's 43 changed lines were of that kind. Mirror checks need no heuristics and caught the real drift that's still in `latent-signals` today: four sections of the copy, including Two Jobs to Be Done and V1 Scope.

## Limits

- "Look here first" uses wording overlap only. It catches stale phrases, numbers, and versions, not reworded ideas. Always read the upstream change.
- The reviewed version must be in Git history: run reconcile, then commit. Otherwise use `--base`. History search covers the latest 100 commits that touched the upstream file and follows renames.
- Needs doc-lattice ≥ 7.4 and Python ≥ 3.13 (see Setup).
- docprop doesn't suggest links. You declare what depends on what.

## Files

| File | Role |
| --- | --- |
| `docprop.py` | v1 CLI (standard library + doc-lattice) |
| `test_docprop.py` | 15 tests, including end-to-end runs against real doc-lattice and Git |
| `examples/latent-signals.docprop.toml` | Mirror declaration for the real product-brief copy |
| `EXPERIMENT.md`, `doc_rewrite.py`, `benchmark.py`, `test_doc_rewrite.py` | Archived v0 rewrite experiment |

Run tests: `.venv/bin/python -B -m unittest -v test_docprop test_doc_rewrite`
