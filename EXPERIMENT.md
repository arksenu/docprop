# docprop v0: document rewrite-quality experiment (archived)

> **Superseded.** The gate below failed (2/9 edits accepted unmodified), so v1 is flag-only: see [README.md](README.md). `doc_rewrite.py`, `benchmark.py` and `test_doc_rewrite.py` are kept only to reproduce this experiment.

A **local Python CLI**, not a GitHub Action or service. It delegates graph construction, stale-edge classification, section addressing, and reconciliation to [doc-lattice](https://github.com/Guardantix/doc-lattice). It invokes one structured LLM request per stale edge, in dependency order, then writes a unified diff and JSONL review record. Without `--apply`, tracked documents are not edited.

**Status:** Working files are in the local `docprop/` folder, not `autoclaude`. A clean clone of `latent-signals` is under `inputs/latent-signals/`. doc-lattice 7.4.1 is installed under `.work/tools/venv/`. Six offline unit tests pass, and a temporary linked copy of real `latent-signals` docs passed a historical-edit integration test of stale detection, section extraction, an edit/reconcile cascade, and detached-worktree replay (with the LLM response stubbed). The user also ran one **live proposal-only replay** of that historical edit; its diff changed only the relevant downstream section to match the revised upstream text. A separate **24-case exploratory benchmark is prepared, not yet run**. The original `latent-signals` clone and `autoclaude` remain unchanged. The user's Terminal had a model credential for the smoke test, but this task's execution shell has none.

## What the link and baseline mean

These are **annotations supplied for the experiment**, not existing files the user was expected to have. For example, a temporary copy of an overview might contain:

```yaml
---
id: overview
derives_from:
  - ref: product-brief#two-jobs-to-be-done
---
```

This declares that the overview relies on a section of the product brief. Running `doc-lattice reconcile overview` after checking that the overview is current automatically records a `seen` hash on that edge: the **baseline**. When the brief changes, `check` compares that hash with its current section. The old *text* comes separately from Git at `--base`. We can add annotations and baseline them in **temporary copies**, as the integration fixture does, without changing the `latent-signals` clone or requiring the user to know the syntax.

## Requirements

- Python 3.13+; Git; an installed `doc-lattice` executable and importable `doc_lattice` Python package in the same interpreter as this script. Targeted against doc-lattice 7.4.1. Its [frontmatter format](https://github.com/Guardantix/doc-lattice#frontmatter-reference) and `.doc-lattice.yml` configuration belong to the documentation repository.
- `OPENAI_API_KEY` supplied in the local environment. Default model `gpt-4.1-mini` and endpoint `https://api.openai.com/v1/chat/completions`; override with `--model` and `--api-url` if necessary. **Entire downstream docs and upstream sections are sent to that endpoint.** Review the privacy and cost implications before running on sensitive docs.
- A Git repository with at least one commit. For each existing `derives_from` link, doc-lattice's `seen` value should have been baselined using `doc-lattice reconcile --all` **before** the upstream edit under evaluation. Reconcile is an acknowledgement of review, not a staleness detector.

Example (using the task-local install already present here):

```sh
export OPENAI_API_KEY='your-key' # do not commit or paste it into a chat
.work/tools/venv/bin/python doc_rewrite.py run \
  --repo /path/to/linked-docs-repo \
  --lattice-bin "$(pwd)/.work/tools/venv/bin/doc-lattice" --base HEAD
```

The actual doc-lattice JSON flags are `check --format json` and `graph --format json`, **not** `check --json`. The CLI invokes those commands itself. To use a non-default executable, pass `--lattice-bin /path/to/doc-lattice`. Use the same Python environment as the executable so section extraction imports its pinned package.

## Commands

```sh
# Current checkout's upstream changes versus HEAD; write proposals only.
python3 doc_rewrite.py run --repo /path/to/docs-repo --base HEAD

# Explicitly accept every valid edit verdict; writes docs and reconciles only edited edges.
python3 doc_rewrite.py run --repo /path/to/docs-repo --base HEAD --apply

# Recreate the docs state at an older commit (first parent is the base).
python3 doc_rewrite.py replay --repo /path/to/docs-repo --commit <upstream-edit-sha>
```

`--config`, `--proposals-dir`, `--log`, `--model`, `--api-url`, and `--lattice-bin` are available in either mode. By default, proposals go under the target repo's `proposals/`; replay uses `proposals/replay-<sha>/`. `--apply` in replay affects only a temporary detached worktree; the user's checkout stays untouched. A replay requires the doc-lattice configuration and already-tracked edges to exist **at that historical commit**. A root commit has no first-parent baseline and cannot be replayed.

The script first reads doc-lattice's file-level graph and rejects cycles or ambiguous targets; `check` supplies the **actual per-section stale edges**. It sorts affected downstream files topologically, then passes the old upstream target at `--base`, the new upstream target, and the complete downstream file to the LLM. One answer is required: `unaffected`, `edit` (complete replacement document), or `conflict` (human decision). It validates that edits preserve doc-lattice frontmatter byte-for-byte. Each edge gets a `.diff` (empty when there is no edit) and a JSONL record including the edge, verdict, explanation, prompt/completion/total token counts, and `"decision": ""` for you to fill with `accept`, `modify`, or `reject`. A nonzero exit is a failure, not a clean review.

With `--apply`, after **each** edited edge the script invokes `doc-lattice reconcile DOWNSTREAM --ref UPSTREAM`; newly stale child edges are then discovered on the next check. `unaffected` and `conflict` edges remain stale and are **not** silently reconciled. Without `--apply`, edits are only proposals, so a child that becomes stale *because of a proposed but unapplied parent edit* is not reviewed until a later run. Multiple proposals to the same downstream file are independent; review and apply them sequentially rather than blindly combining patches. `--apply` is an all-edit-verdict switch, not an individual approval UI.

## Evaluation gate

On a **real** doc set, baseline the graph; observe 20–30 authentic upstream edits; review each resulting diff; record `accept` only for an edit adopted *without modification*. Measure `accept / proposed edit verdicts`, and separately note missed necessary edits and false-positive edits. If fewer than roughly half of proposed edits are accepted unmodified, keep flag-only change impact and defer automatic rewriting. Replay does **not** manufacture edits or an evaluation result. `latent-signals` has 14 total commits and 66 changed-Markdown-file events, but those historical commits **do not contain doc-lattice metadata**, so `replay` cannot be pointed directly at them without first constructing a separate linked evaluation history.

### Prepared exploratory history set

`benchmark.py prepare` has already constructed **24 distinct actual upstream Markdown file edits**, each in an isolated Git fixture under `.work/benchmarks/latent-24/`. It added a candidate whole-file `derives_from` link to a *previous-revision* downstream document, reconciled that link, then replayed the historical upstream change. These annotations are **hypotheses**, not links the original authors declared: 2 high-confidence (canonical copy), 15 medium-confidence, 7 low-confidence (especially chronological log relationships). Several edits occurred in the same original commit but are distinct changed files. Review link validity before using an item's verdict in the quality gate. A same-commit original downstream diff, where present, is saved for context but is not automatic ground truth.

In the Terminal where `OPENAI_API_KEY` was available for the one-edit smoke test, from `docprop/`:

```sh
# Three new proposal-only cases first (three model calls); review before continuing.
.work/tools/venv/bin/python -B benchmark.py run --limit 3
.work/tools/venv/bin/python -B benchmark.py report

# If the first cases look useful, run the remaining cases. Completed cases are skipped.
.work/tools/venv/bin/python -B benchmark.py run
.work/tools/venv/bin/python -B benchmark.py report
```

**No `--apply` is used** by the harness. `report` writes `.work/benchmarks/latent-24/review.csv` with links to the diffs. Inspect each relevant source and diff; fill `decision` with `accept`, `modify`, or `reject` for **edit** verdicts, then rerun `report` to preserve and summarize those decisions. An `unaffected` verdict can still miss a necessary edit: review those too, separately. The script sends the complete downstream document and upstream text to the API **once per case**, so running the full set entails up to 24 model requests and provider usage. A request failure stops the batch; successful cases remain for a subsequent run. Do not present this convenience set as conclusive quality evidence until its hypothesized links and reviewer decisions are checked.

`--case PREFIX` (repeatable) runs only matching case IDs. `report` preserves both the `decision` and `notes` columns.

### Results (13 of 24 cases run; 11 log-to-log cases skipped on purpose)

| Case | Relationship | Verdict | Review |
| --- | --- | --- | --- |
| 01 | dev log → decision log | unaffected | Correct |
| 02 | decision log → dev log | edit | **Reject**: invented a session entry; reworded unrelated old entries |
| 03 | dev log → decision log | unaffected | Reasonable |
| 04 | decision log → dev log | edit | **Reject**: invented a session with the wrong number and content |
| 05 | dev log → decision log | unaffected | Reasonable |
| 06 | backtest summary → decision log | edit | **Modify**: accurate facts, but invented a formal decision entry and a stray heading |
| 16 | canonical brief → synced copy | edit | **Accept**: copy now identical to canonical; also fixed a line the author missed |
| 17 | scoring spec → pipeline stub | edit | **Reject**: hallucinated a full doc citing five source files that do not exist |
| 18 | constraints → infrastructure stub | unaffected | Reasonable |
| 19 | landscape → positioning stub | edit | **Reject**: wrote new strategy into a doc still marked "Not yet written" |
| 20 | pipeline → infrastructure stub | edit | **Reject**: unnecessary pointer; nothing depended on the change |
| 21 | positioning → GTM stub | edit | **Modify**: right intent (defer), but contradicts "V1 is a validated prototype" |
| 24 | canonical brief → synced copy | edit | **Accept**: synced two stale sections the author never synced |

**Unmodified acceptance: 2/9 proposed edits (22%)**; 2/6 when only non-log relationships are counted. Both are below the ~50% bar. The sample is small (13 cases, fewer than the planned 20–30), but the failure pattern is consistent:

- **Copies: works.** When the downstream is a mirror of a canonical file, the model synced it exactly (cases 16, 24). That is mechanical, though, and a deterministic copy would do it without an LLM.
- **Logs: fails.** Chronological records (dev log, decision log) do not derive from other files. Asked to keep them in sync, the model invents history.
- **Stubs and summaries: fails.** When the downstream must be *written* rather than updated, the model fills it with plausible but unsupported content: nonexistent files, unsanctioned strategy, unneeded edits.
- **"Unaffected" verdicts were all reasonable** (4/4). No necessary edit was clearly missed.

**Gate result: do not build automatic rewriting for v1.** Build flag-only change impact: detect stale links (doc-lattice already does this) and show the reviewer what changed upstream and which downstream passages it touches. Optionally add a deterministic "mirror" link type for canonical copies. Remaining log-to-log cases (07–15, 22, 23) were skipped because they repeat the failure mode already seen.

**Real drift found in passing:** `latent-signals/01_strategy/product_brief.md` claims to be a synced copy of the root `product_brief.md`, but at the current HEAD its "Two Jobs to Be Done" and "V1 Scope" sections are still stale. They show the old Discovery-primary framing; the canonical brief was updated in `d0ed023`. This is exactly what flag-only detection would catch. It has not been changed here, since the clone is read-only input.

A bug that merged the last diff line when a file lacked a trailing newline was fixed after case 06 (regression test added).

## Validation and boundaries

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest -v test_doc_rewrite
```

Unit tests stub the doc-lattice CLI and LLM boundaries. The integration test at `.work/scripts/run_integration.py` exercises the installed package against a temporary linked copy of a real historical edit, with a stubbed model. The 24-case benchmark is prepared but no bulk model calls have been made. This prototype makes no meaning-change gate, link suggestions, claim extraction, GitHub PR automation, or backend. LLM output is untrusted: review proposals, especially before using `--apply`. Concurrent modifications are checked before each edit, but this is not a transactional multi-file merge tool.
