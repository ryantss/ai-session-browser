<!--
  Harness LITE PR template — for diffs under ~100 lines or pure refactors.
  To offer both, GitHub supports a directory of templates: place the standard one
  at .github/PULL_REQUEST_TEMPLATE.md and this at
  .github/PULL_REQUEST_TEMPLATE/lite.md, then pick via ?template=lite.md.
-->

## What & why
<!-- One line. e.g. "Rename fetchUser → loadUser; no behavior change." Link issue if any. -->

## Type
- [ ] Behavior change
- [ ] Pure refactor
- [ ] Config / deps
- [ ] Docs

## Evidence
<!-- Required if "Behavior change" is checked. Otherwise link the green test run. -->
| Before | After |
|---|---|
| <!-- ![](url) or value --> | <!-- ![](url) or value --> |

Captured: <same viewport/input, how — or "no-op refactor, existing tests green">

## Verification
```
<test / lint output>
```

<!-- Agent-authored? One line, then keep the trailer. -->
🤖 <model> · autonomy: <…> · needs human eyes: <none | file:line>
<!-- harness:meta
authored_by: <model-id or "human">
diff_lines_net: <N>
contains_behavior_change: <true|false>
evidence_before_after: <provided | n/a-no-observable-change>
human_review_required: <true|false>
-->
