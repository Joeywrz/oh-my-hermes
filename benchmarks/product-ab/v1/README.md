# Product A/B: Hermes alone vs Hermes through OMH

`benchmarks/product-ab/v1` asks one question and answers it with four numbers:

> On this repository's own merged pull requests, graded by those pull requests'
> own tests, does the same model reach the same pass rate for less money, less
> wall clock, and fewer false completions when it is reached through OMH's
> coding delegation instead of Hermes alone?

It is a sibling of `benchmarks/live-model-tools/v1`, not a replacement. That
lane measures one prompt prefix on a synthetic corpus. This lane measures the
product: the route OMH resolves, the calibration that route selects, and the
verification gate, on tasks a maintainer of this repository would recognize.

## What one task is

One task is one merged `feat` or `fix` pull request.

| Piece | Where it comes from |
| --- | --- |
| Task text | the pull request's linked issue body, or the PR body's *Why This Exists* section when it closes no issue, with the solution half removed |
| Workspace | a detached git worktree at the pull request's merge base |
| Validator | the pull request's own test files, put on top of whatever the candidate left behind, run as `python -m unittest <those modules>` with `PYTHONPATH=tests` |
| Pass | those modules green **and** the pre-existing regression modules for the touched packages still green |

The validator takes the *result* of the pull request's test diff rather than
applying it as a patch: each test path the pull request touched is replaced
with the merge commit's version of that file, and a path the pull request
deleted is deleted. A candidate that edited the same test file can therefore
neither break the validator nor weaken it.

### Two subsets, and which one a headline may use

Where the task text came from decides what can be said about the result, so
every task records it as `task_source` and the two subsets are reported apart.

| Subset | Task text | What it can carry |
| --- | --- | --- |
| `linked_issue` | the body of the issue the pull request closes, written **before** the fix, by someone describing a problem | the headline. A sentence of the form "solved N% of this repository's own issues" may be written only from these tasks |
| `pull_request_body` | the *Why This Exists* section of the pull request, written **after** the fix, by its author | a secondary table, labelled as such |

The distinction is not fussiness. Cutting the solution half at its heading
removes the sections that prescribe the fix, and the corpus refuses any task
whose text quotes a line of the diff or names a definition the fix introduces.
None of that touches paraphrase, and a body written to explain a finished
change paraphrases constantly. A corpus that needs a caveat paragraph to be
read correctly will be quoted without the caveat.

Reading every merged pull request in this repository yields 41 issue-sourced
candidates, and the count stops growing at a read width of 800 -- 18 at width
250, 25 at 350, 33 at 450, 40 at 600, then flat. The probe therefore takes
issue-sourced candidates first, so that pull-request-body tasks cannot consume
slots the headline subset needs.

`analyze.py --task-source linked_issue` reports on the headline subset, and
prints the subset and its `n` above the table. `--leak-class` subsets the same
way on how much of the answer a task's text still carries.

### What this corpus is made of

Numbers describe the pinned corpus at digest `f6438aa2faf2`, and
every one of them is re-derived from `corpus/evaluation.json` by a test, so
they cannot drift away from the file they describe.

| | Count |
| --- | ---: |
| Merged pull requests read, newest first | 800 |
| Candidates probed | 48 |
| **Tasks kept** | **40** |
| — from a linked issue (the headline subset) | 35 |
| — from a pull request body (secondary) | 5 |

A reader who sees "40 tasks" without seeing what was probed and
dropped cannot judge the number, so the rejections are here rather than in the
corpus file alone.

| Rejected by the probe | Count |
| --- | ---: |
| Validator not green with the pull request's own fix | 3 |
| Verdict depends on the workspace path | 3 |
| Regression set already red at the merge base | 2 |

The first line means the corpus is selected partly on what this harness can
run, and that belongs in how any number from it is read. The concrete case:
PR-1488's validator asserts a filesystem-confinement receipt that cannot hold
in a temp-directory workspace, so that task is red before the fix and still red
after it, and it would have cost a call on every arm while deducting pass rate
for something no arm did.

How much of the answer each task's text still carries, worst class first:

| Leak class | All tasks | Issue-sourced |
| --- | ---: | ---: |
| `heading_prescriptive` | 0 | 0 |
| `names_new_identifier` | 0 | 0 |
| `names_changed_file` | 14 | 14 |
| `clean` | 26 | 21 |

The first two are zero by construction: a task naming a definition the fix
introduces is excluded outright, and a surviving solution heading would mean
the cut missed one. They are counted anyway, because a corpus that assumes its
own filter is exhaustive has no way to discover that it is not.

### What is excluded, and why

| Excluded | Reason |
| --- | --- |
| A title that is not `feat` or `fix` | a release, a calibration, or a multi-capability roll-up is not one task |
| No test module touched | no maintainer-written validator exists |
| No `src/**.py` touched | nothing for a candidate to build |
| Non-test, non-generated change over 400 lines | too large to be one task at one attempt |
| Only generated artifacts changed outside `tests/` | the work is a regeneration and its tests pin bytes, not behaviour |
| Only `.github/`, packaging, or dependency files changed | maintenance without a behavioural validator |
| Task text under 200 characters | not a brief |
| Task text quoting the diff | see *Leak rules* below |
| Task text naming a definition the fix introduces | see *Leak rules* below |
| Validator already green at the merge base | nothing has to be built to pass it |
| Validator still red with the fix that shipped | the task is unsolvable here; see *Both directions* below |
| Regression set already red at the merge base | every arm would fail for a reason no arm caused |
| Validator disagrees with itself between two checkout paths | see *Two-path probe* below |

The 400-line cap counts the non-test, non-generated change, because that is
the work a candidate has to produce. Test lines are the validator, and a
thorough test file does not make a task harder.

### Both directions

Proving a task red at its merge base closes one direction only: nothing has to
be built to pass it. It says nothing about whether anything *can* be. So the
probe also applies the pull request's own non-test change -- the reference
solution -- and requires the validator and the regression modules to come back
green. A task that stays red under the change that actually shipped depends on
something outside the candidate's reach; it would cost a call on every arm and
pull the published pass rate down for a reason no arm caused.

No arm ever sees the reference solution. It is pinned in the corpus under
`solution_blobs` and read only by `corpus --probe`.

### Two-path probe

`bench.py corpus --probe` runs each candidate's validator twice, under two
parent directories that differ in spelling, in depth, and in length. It keeps
the task only if both runs reached the *same verdict*: the same status and the
same ran, failure, and error counts.

That check is there because a candidate disagreed with itself. PR-1502 fixes a
fixture that built a verification command out of the checkout path and compared
it against a 240-character cap, so at its own merge base the fixture's verdict
turned on how long the workspace path happened to be — that pull request's own
body reports it passing from one worktree and erroring from another on the same
commit. Without the second pass it would have entered the corpus, and every
arm's result on it would have been decided by the directory the harness
happened to pick rather than by anything the arm did.

Two earlier versions of this check let PR-1502 straight through, each for its
own reason:

| Version | Why it missed |
| --- | --- |
| A second pass that renamed the leaf inside the same parent | the path length barely moved, so the two passes agreed on every task |
| A second pass under a different root, compared on status alone | both roots were long enough to trip the cap, so both passes came back red and the comparison saw a match |

Comparing the counts is what closes it. Under the two roots the probe uses,
PR-1502's validator came back red with two errors and no failures from one and
red with one failure and no errors from the other: the same colour, a different
verdict, and that disagreement is the path dependence itself. The candidate is
rejected as `verdict_depends_on_workspace_path` and counted under that name in
the corpus's `selection.probe_rejected`, so a dropped task is never a silent
one.

The probe also stops as soon as the corpus is full, working from the newest
pull request backwards. Each probe runs the validator twice and the regression
set once, which costs minutes on this repository's larger modules, and sweeping
candidates past a full corpus buys nothing.

### Leak rules

Two assertions keep the answer out of the question, and both are enforced in
code rather than promised in prose:

1. **The task text may not quote the diff.** Every added line of the pull
   request's non-test diff that is at least 40 characters long is searched for,
   whitespace-normalized, in the task text. A hit excludes the pull request
   (`corpus.leaked_solution_lines`).
1. **The task text may not name what the fix introduces.** A pull request body
   is written after the change, by its author, and it paraphrases rather than
   quotes -- so it can name the function to write without reproducing a line of
   it, and the rule above never fires. A name counts as a leak when the pull
   request defines it, the task text uses it, and `git grep` finds it nowhere
   under `src/` at the merge base. That last condition is what separates
   handing over the answer from naming something the candidate could have read
   for itself (`corpus.introduced_names_in_task_text`).
1. **The solution half of the body is cut at its heading.** Headings match by
   prefix, not equality: this repository's own template writes `Implementation
   (boundary level)`, and an equality test kept that entire section. A `#` line
   inside a fenced code block is a comment, not a section boundary.
1. **Naming one of the pull request's own changed files is recorded, not
   excluded.** Pointing at the file that misbehaves is what an ordinary bug
   report does and hands over no part of the fix. Every task carries
   `task_text_names_source_paths` so a reader can subset the corpus rather than
   trust a sentence about it.
2. **The workspace may not contain the validator.** Before a candidate is
   started, every pinned test blob is compared against the workspace's copy; a
   match aborts the run rather than grading it (`grading.validator_paths_already_present`).
   The same check runs inside `doctor`.

## The arms

All three arms run the same Hermes execution path, so the only differences
between the first two are the ones OMH owns.

| Arm | Model and effort | Prompt | Verification gate |
| --- | --- | --- | --- |
| `hermes` | the manifest's control model and effort | the bare task plus the completion contract | none |
| `omh` | the same control model and effort | the delegation prompt OMH composes, including the calibration `omh coding model-route` resolves | yes |
| `omh_mixture` | the model the complexity routing resolves from the category mixture | the same delegation prompt | yes |

**`omh_mixture` is excluded from the published run**, and for a reason that is
not methodological. It exists to keep the cost number attributable, and it
cannot: the complexity routing resolves `glm-5.3-ultrafast` for part of this
corpus, and the shipped price table has no rate for that alias, so the arm's
cost column reads `unknown`. An arm whose cost is unknown cannot serve as a
cost attribution aid -- it would be three arms of spend to produce two arms of
answer. No rate was invented for it: every rate in that table carries the
vendor page and the month it was read, which is the only reason the table is
worth anything.

The arm stays in the lane and stays working. To publish it, add a documented
rate for `glm-5.3-ultrafast` to `APPROX_PRICE_PER_MTOK` and run with
`--arm omh_mixture`. That is the whole of what is missing.

`omh_mixture` is labelled separately on purpose: it is the only arm where the
routing is allowed to move, so a cost difference stays attributable to routing
rather than to calibration. Read it as one bundled change, not as a model
swap: the resolved route can carry a different provider (`og` rather than the
control's) and a different effort per task as well as a different model, and
all three move together. The `model` block on every record says exactly what
that task got.

Arm order rotates one position per task, so no arm is systematically first.
One task runs at a time.

### How the OMH arm is composed

* `omh coding delegate --executor hermes --stdin` produces the complexity
  tier, the model class, and the resolved chain head. The task text goes in on
  stdin, never on argv, and only the deterministic routing fields are kept.
* `omh coding model-route --executor hermes --model … --effort … --json`
  resolves the route.
* The prompt is composed from the *shipped* protocol constants in
  `omh.coding.unit_prompt_protocol` — goal echo, verification stop, failure
  kind, structural search discipline, tool batching, the numbered completion
  criteria, and `calibration_for_route` — so a calibration this repository
  revises is the calibration the next run measures.
* `calibration_for_route` returns nothing outside the high effort tier, so the
  manifest pins the control effort at `high`. At `medium` the arm carried no
  calibration on any task while the mixture arm, routed at `high`, did get a
  block — which put calibration only where the model also changed, inverting
  the reason that arm is labelled separately. `doctor` fails when the pinned
  control effort resolves to an empty calibration, so the sentence above
  cannot quietly become false again.
* The fanout-transport criteria are filtered out of the composed prompt. The
  shipped `completion_criteria_for_unit` always appends "the work is committed
  on the unit branch", which exists so a dispatched worktree can be collected;
  this lane has no collector, and left in it reached the model in the same
  prompt as "do not commit". They are derived from the protocol rather than
  matched by wording, so a rewording upstream stays filtered.
* After the run, the arm executes the task's declared verification commands.
  When they fail, a completion claim is withdrawn, and the manifest's
  `omh_repair_attempts` decides whether the arm gets one more turn with the
  failing check named. The repair turn's tokens and seconds count against the
  arm; nothing is free.

The verification commands are a compile gate plus the pre-existing regression
modules for the touched packages. The pull request's own tests are never among
them: they are the hidden validator, and a gate that ran them would hand the
candidate the answer.

Before either half of the grade is run, the regression modules are restored
from the merge base. They are chosen to be modules the pull request did *not*
touch, so the validator never restores them, and they used to be graded from
whatever the candidate left behind -- while the OMH arm's prompt named those
exact modules and another criterion permitted edits under `tests/`. A candidate
that weakened one passed that half undetected, and the bare Hermes arm, told
none of this, could not have done the same even by accident.

## What this lane deliberately does not measure

* **Fanout.** `omh coding fanout dispatch` does not spawn a Hermes-owned unit:
  `DISPATCH_COMMAND_TEMPLATES` in `src/coding/fanout_dispatch.py` has no
  `hermes` entry, and such a unit returns `unsupported_for_local_dispatch`
  before a worktree is created. Measuring fanout would mean changing the
  executor, which would break the one-model control this comparison rests on.
  The multi-unit path is therefore out of scope here and is named, not
  silently omitted.
* **The isolated child boundary.** `omh coding hermes-child dispatch` is the
  other Hermes execution boundary, and it is deliberately cut off from the
  caller's profile: it points HOME and HERMES_HOME at throwaway directories and
  forwards only the named provider's documented environment variables. A
  machine whose models are reached through a subscription login or a gateway
  registration cannot authenticate through it, which is why every measured run
  in the sibling lane uses the profile path too. `doctor` refuses a manifest
  naming any execution path other than the one the lane runs, so that field can
  never claim a path that is not implemented.
* **A prepared decision.** `prepared_not_observed` is never a pass. Only a
  validator that actually ran decides a task.
* **Any model's general capability.** The corpus is this repository, these
  tasks, this model, this effort, and this machine's provider routes.
* **Pass-rate headroom beyond this corpus.** A number here describes these
  pull requests.

## The four numbers

| Number | Definition |
| --- | --- |
| Pass rate | validator green, over the tasks in the corpus |
| Cost per pass | total cost ÷ passes, reported only when every run in the arm carries a price. The host's `estimated_cost_usd` is recorded when it reports one; the comparable column is OMH's own shipped list-price table applied to each run's input, output, and cache-read tokens, so a subscription route and a metered route are still on one scale |
| Wall clock per goal | seconds from dispatch to the last attempt's exit, repair turns **and the verification gate** included; reported as a median and a total, with `model_seconds` and `verification_gate_seconds` kept separately |
| False completion | the run wrote a completion claim of `complete` and the validator is red |

The false-completion column is not a neutral observation of two identical
systems: on the OMH arms the verification gate can withdraw a claim its own
declared checks contradict, and that withdrawal is exactly the product
behaviour under test. Read the column as "how often this product handed back a
wrong *done*", not as "how often the model was wrong". Every withdrawal is
recorded on the run, so the two readings can be separated afterwards.

Tool calls and API turns ride along as secondary columns, read from the
Hermes usage file.

A run that nothing could price is not a free run, and the arm it belongs to has
no cost. An unpriced run used to collapse to `0.0`, sum into the arm total, and
divide into cost per pass: resolving the mixture routing over this corpus
dispatches an alias with no price-table entry, so the table rendered a 100%
cost saving with a tight confidence interval, manufactured out of unknowns.

Now an arm with any unpriced run reports `cost_usd_total: null`, the table
prints `unknown` rather than a dollar figure, an `Unpriced` column shows the
coverage, and a footnote names the models that have no entry. The paired cost
delta is refused by name rather than computed over whichever pairs happened to
be priced. `_cost()` raises rather than substitute a zero, so a future caller
that forgets to check cannot reintroduce the same number.

Wall clock counts the verification gate. The gate runs a compile pass and up to
six unittest modules, on the OMH arms only, so charging it to nobody would take
minutes off exactly one side of the comparison and hand them to the "faster"
headline.

Pairing is per task. `analyze.py` computes a paired bootstrap CI95 on each
delta and an exact McNemar test on pass rate, using `percentile`,
`exact_mcnemar`, and `holm` imported from
`benchmarks/live-model-tools/v1/lib/statistics.py`. A delta whose interval
spans zero is reported as **no measurable difference** by name, never rounded
into a direction it does not have.

## Safety and artifacts

* **No paid call happens without `--allow-paid-live` and `--max-paid-calls`.**
  Without them the harness runs the whole pipeline — worktrees, routing,
  validator, grading — and never invokes Hermes.
* **The budget counts invocations, not runs.** `--max-paid-calls` is compared
  against the worst case, which is one call per scheduled run plus
  `omh_repair_attempts` more for each OMH arm, because a verification gate that
  fails buys the arm another turn. Forty tasks over all three arms schedule 120
  runs and can launch 200 calls, so a budget of 120 refuses that run rather
  than starting something it cannot pay for. The two published arms are 80
  runs and 120 calls. Budgeting on the worst case can
  refuse a run that would have come in under the limit, which is the direction
  a spending limit should err in. `smoke` enforces the identical check: it
  calls into the runner directly rather than through the matrix, and for a
  while accepted the flag, echoed it back on the receipt, and ignored it.
* OMH core makes no LLM, API, or network call. This lane is benchmark tooling
  under `benchmarks/`, and it is allowed to run `gh`, `git`, and `hermes` as
  subprocesses. The only network reads are in `lib/github.py`, and they happen
  only at corpus build time. After the corpus is written every run is offline
  except for the model calls themselves.
* Records are metadata only: task ids, digests, counts, statuses, timings.
  Prompts, transcripts, stdout, stderr, credentials, and absolute paths are
  never persisted; `lane.artifact_is_safe` refuses a record that carries one.
* Run outputs go to `artifacts/`, which is gitignored. Only the manifest, the
  corpus, and the code are committed.
* Candidate worktrees are detached and removed however the run exits, so a
  crashed run leaks no branch.

## Commands

`doctor` fails before a paid token is spent when the manifest does not pin a
control model, the corpus is missing or its digests drifted, a task's merge
base is not in this checkout, `hermes`/`omh`/`git` are not on PATH, the active
Hermes profile is not linked to the providers the arms need, the control route
does not resolve, or a candidate worktree cannot be created — or already
contains the validator.

```bash
# Readiness, before a paid token is spent.
python benchmarks/product-ab/v1/bench.py doctor

# Re-derive every pinned digest from the local object store (offline).
python benchmarks/product-ab/v1/bench.py corpus --verify

# Rebuild the corpus from GitHub, then re-prove every task offline.
python benchmarks/product-ab/v1/bench.py corpus --build --pull-request-limit 250
python benchmarks/product-ab/v1/bench.py corpus --probe --max-tasks 40

# The whole pipeline with no model call at all.
python benchmarks/product-ab/v1/bench.py smoke

# The measured run. Both flags are required and neither has a default.
# 40 tasks over the two published arms schedules 80 runs and can launch 120
# calls, because the OMH arm may take a repair turn. The budget has to cover
# 120 or the run is refused before it starts.
python benchmarks/product-ab/v1/bench.py run \
  --arm hermes --arm omh \
  --allow-paid-live --max-paid-calls 120 \
  --output benchmarks/product-ab/v1/artifacts/runs.jsonl

# The headline table, on the subset a sentence about our own issues may use.
python benchmarks/product-ab/v1/analyze.py \
  --records benchmarks/product-ab/v1/artifacts/runs.jsonl \
  --task-source linked_issue --table

# The full table, both subsets together, labelled as such.
python benchmarks/product-ab/v1/analyze.py \
  --records benchmarks/product-ab/v1/artifacts/runs.jsonl --table
```

## Measured

No measured run has been published yet. The lane, its corpus, and its offline
pilot are in place; the table lands here, and in `MODEL_OPTI.md`, only after a
run whose records this repository can point at. Nothing above is a result.

When it does land, two tables land together and they are not interchangeable.
The headline sentence is written from the issue-sourced subset with its `n`
stated beside the number, because only those tasks were described before the
fix existed. The full table follows, labelled, and carries both subsets. A
number quoted from the full table is a number about a corpus that is partly
its own authors' description of work they had already finished.
