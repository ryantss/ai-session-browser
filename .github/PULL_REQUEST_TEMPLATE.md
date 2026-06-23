<!--
  Harness PR template. Installed into target repos as .github/PULL_REQUEST_TEMPLATE.md
  by scripts/init-new-repo.sh and scripts/adopt-existing-repo.sh.

  Reviewers read top-down and should be able to approve WITHOUT scrolling into the
  agent section. The agent section exists to route human attention and make the
  agent's reasoning auditable, not to bury the lede.

  For small / mechanical PRs (< ~100 lines or pure refactor) use the lite template.
-->

## Why
<!-- 1-2 sentences. The problem, not the solution. Link the SPEC / sprint contract / issue. -->

Closes: <!-- #issue or docs/sprints/... -->

## What changed (at the seam level)
<!-- Describe behavior and boundaries, not a file list. -->
- <component>: <what it now does differently>
- <component>: ...

## Evidence: before → after
<!--
  REQUIRED for any behavior / UI / UX / performance / data change.
  Capture the BEFORE state first, before writing implementation code — you cannot
  reconstruct it afterward. State HOW each was captured so it is reproducible, not
  cherry-picked. Same input, same env, same viewport on both sides.
  Delete the subsections that don't apply. Pure-internal refactor with no observable
  change: write "No observable change — pure refactor" and link the green test run.
-->

### Visual (UI/UX changes)
| Before | After |
|---|---|
| <!-- ![before](url) --> | <!-- ![after](url) --> |

<!-- For flows/interactions attach a GIF instead of stills. -->

### Metrics / performance
| Metric | Before | After | Δ | How measured |
|---|---|---|---|---|
| <!-- p95 latency --> | <!-- 840 ms --> | <!-- 310 ms --> | <!-- -63% --> | <!-- k6, 500 rps, staging, n=10k --> |

### Behavior / data (API, logic, jobs)
**Input:** `<request / scenario>`
- Before: `<old output / error / row count>`
- After: `<new output>`

### Reproduce it yourself
```
<exact commands or steps a reviewer runs to see before → after>
```
- [ ] Evidence is from the **same** input/env/viewport on both sides
- [ ] Not cherry-picked: edge / failure cases also shown, or "N/A"

## Reviewer's guide (read in this order)
1. `path/to/core.ext` — the real logic. Start here.
2. `path/to/caller.ext` — how it's wired in.
3. Everything else is tests / mechanical / generated. Skim.

## Risk & rollback
- Blast radius: <what breaks if this is wrong>
- Flag / guard: <feature flag, config, or "none">
- Rollback: <clean revert? migrations? data backfill?>

## Deliberately not done
<!-- Scope cuts and follow-ups, so reviewers don't flag missing work as a bug. -->

---
<!-- ╔═══ AI AUTHORING SECTION — fill in only if an agent authored this PR. Delete for human-authored PRs. ═══╗ -->
## 🤖 Agent disclosure

**Provenance**
- Authored by: <model / harness sprint id, e.g. claude-opus-4-8 via /harness-sprint>
- Autonomy: <fully autonomous | human-steered | human-reviewed pre-PR>
- Contract: <docs/sprints/...>

**Verification I actually ran** (evidence, not claims)
```
<paste the real command output: test run, typecheck, lint, build, dry-run>
```
- [ ] Tests pass (output above)
- [ ] Typecheck / lint clean
- [ ] Ran the actual feature end-to-end, not just unit tests: <how, or "N/A">

**Where I want human eyes** (confidence map)
- 🔴 Low confidence / needs judgment: `file:line` — <ambiguous spec, business rule I guessed, perf assumption>
- 🟡 Inferred, not verified: <library API I assumed exists, edge case I didn't test>
- 🟢 Mechanical / high confidence: <renames, boilerplate — safe to skim>

**Assumptions made** (where the spec was silent)
- <assumption> → <what I chose and why>

**What I could NOT verify**
- <external dependency, prod-only behavior, missing fixture>

<!-- Machine-readable trailer — parsed by the harness (evaluator / coderabbit-liaison / merge-captain). Leave intact. -->
<!-- harness:meta
sprint: <docs/sprints/...>
authored_by: <model-id or "human">
autonomy: <autonomous | human-steered | human>
diff_lines_net: <N>
contains_behavior_change: <true|false>
contains_migration: <true|false>
evidence_before_after: <provided | n/a-no-observable-change>
verified:
  tests: <pass|fail|n/a>
  typecheck: <pass|fail|n/a>
  ran_feature: <true|false>
low_confidence:
  - file: <path>
    line: <N>
    reason: <one line>
human_review_required: <true|false>
-->
<!-- ╚═══════════════════════════════════════════════════════════════════════╝ -->
