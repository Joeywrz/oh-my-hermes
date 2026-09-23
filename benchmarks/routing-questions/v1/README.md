# OMH routing questions

`omh_routing_question_benchmark/v1` asks an arm the questions OMH's own router
answers, on the cases OMH's own router is gated on.

`src/quality/routing_precision.py` holds two shipped corpora: negative controls
that must not be hijacked into a workflow, and intervention cases that must
reach one. `omh chat route-questions export` projects both into typed
questions, one item per case:

- one **Choice** over the router's own candidate shortlist plus `none`, which
  picks *which* workflow;
- one **yes/no** question per candidate, which decides *whether* that one
  workflow is what the request asks for.

The two carry no invariant between them. The Choice is relative and always
returns one of its options; each yes/no is absolute and answerable on its own.
An arm may pick a candidate and still say no to every fit, and that is reported
rather than repaired.

## Arms

| Arm | What answers | Live | How |
| --- | --- | --- | --- |
| `deterministic` | the OMH router | no | computed from the corpus; always in every report |
| model | a model through the explicit child boundary | yes | `bench.py run --harness omh` |
| external | any tool the operator likes | no | an answers file scored offline |

The deterministic arm's verdicts are the routing-precision corpus's own, read
through `precision_case_verdict` and `intervention_case_verdict`. It is not
re-derived here, which is what keeps this lane and `omh chat routing-precision`
from disagreeing about the same router on the same cases.

## What a live arm can be scored on

A live route attaches a question only where it could not decide, and it builds
that question from the candidate handoff. The corpus carries a question for
every case, so an offline arm answers all of them; only the handoff cases
(`question_source: candidate_handoff`, `live_joinable: true`) are ones a live
route ever asks. An arm answering recorded live routes is therefore denominated
on those alone, and the rest are named `not_live_joinable` in each rate's
`excluded` list rather than counted as questions it failed to answer. The
export summary reports both counts, and a score report carries
`live_joinable_case_count` beside `case_count`.

## Safety and claim boundary

- Offline by default. `export`, `deterministic` and `score` spend nothing.
- `run` spends money and refuses without `--allow-paid-live`,
  `--max-paid-calls` and `--confirm`. The library refuses a second time in
  process, so importing it and calling `run_batch` cannot spend anything
  either.
- The paid path is `omh coding hermes-child dispatch --confirm-dispatch`. The
  prompt goes on stdin there and is never persisted; a run record carries the
  exit code, the reason an answer file is missing, and the answer rows narrowed
  to the documented answer keys, never free model text.
- `hermes_current_session` runs the caller's authenticated Hermes profile. It
  is labelled in every record and is not the isolated child boundary. Its
  prompt goes on argv, because `--oneshot` takes it positionally, so it is
  visible to `ps` while the batch runs. Its process directory and its
  `TERMINAL_CWD` are both pinned to the batch workspace, so the `file` toolset
  resolves against the workspace and not the directory `bench.py` was launched
  from.
- Answer rows are written as each batch returns. A run that stops early leaves
  the answers it already bought on disk, and they score as answers for those
  cases and unanswered for the rest.
- An arm that wrote no answer file answered nothing. It is scored as
  unanswered, never as correct, and every unanswered case is excluded from the
  denominator by name.
- A score describes these corpora at this revision. It is not evidence about
  any model beyond them, and an answer is a routing judgment, not execution,
  review, CI, or merge evidence.

## Commands

```bash
# Offline: build the corpus and read the router's own arm.
python benchmarks/routing-questions/v1/bench.py export
python benchmarks/routing-questions/v1/bench.py deterministic

# Offline: check an operator-supplied answer file before scoring it.
python benchmarks/routing-questions/v1/bench.py score \
  --answers /path/to/answers.jsonl --preflight

# Offline: score any arm next to the deterministic one.
python benchmarks/routing-questions/v1/bench.py score \
  --answers /path/to/answers.jsonl --output artifacts/score.json

# Paid: answer the corpus with a model arm, in batches, through the child boundary.
python benchmarks/routing-questions/v1/bench.py run \
  --arm my-model --model <alias> --provider <alias> --reasoning <alias> \
  --batch-size 20 --allow-paid-live --max-paid-calls <batches> --confirm
```

`<batches>` is the corpus size divided by `--batch-size`, rounded up. The cap
is checked before the first dispatch, and the refusal names the batch count it
measured, so an under-set cap costs nothing.

`--answers` also accepts a directory of `route_question_answer/v1` records, the
shape an answer recorded on a live route is written in. Those join a corpus
item by `message_sha256` first and by question digest only as a fallback. The
digest covers the candidate shortlist as well as the request, and the shortlist
is cut per surface -- `omh chat route-hint` asks about two candidates where a
full route asks about three -- so the same request asked on two surfaces
produces two digests. The message does not move, so it is the key; the digest
then reports whether the shortlist was the same one, as `digest_match`. A
mismatch is scored, not dropped, and counted per arm as `digest_mismatch`. A
record whose message or digest still reaches more than one corpus item is
reported as ambiguous and scored against none of them.

Export at `--limit 3`, which is the default. The limit cuts only a question
built from the router's recommendations; a question built from an undecidable
route's candidate handoff is not cut, because the live route does not cut it
either.

## Producing an external arm

Any tool that can read the exported corpus and write
`routing_question_answers/v1` rows is an arm. One row per case:

```json
{"schema_version": "routing_question_answers/v1",
 "case_id": "<case id from the corpus>",
 "arm": "<the name this arm carries in the report>",
 "answers": {"route_choice": {"choice": "<option id or none>"},
             "fits::<workflow>": {"noul": 0.0}}}
```

An installed Jev plugin (for example one exposing `jev_decide`, or
`nerve_decide` after the Nerve rename) becomes an arm the same way, and the
operator runs it: OMH never calls the plugin. For each item in `export`'s
`items`, send the tool the item's `message` and one question from
`question.questions` at a time (its `instructions`, plus `options` for
`route_choice`), and never the item's `expected` or `deterministic` fields.
Write the returned option id or `noul` value under the same question key in
that case's row, then `score --preflight` the file before scoring it.

`omh chat route-questions score` is the authority on what a row is worth. It
names every malformed row by line, counts it, and never drops it silently.

`deterministic` is not an arm name a row may claim. Every report computes that
arm from the corpus itself, so a supplied row naming it is refused as malformed
rather than merged into a tally the report then replaces.

## Latest measured status

No live arm has been run. The deterministic arm is the only measurement this
lane carries, and it is reproduced by `bench.py deterministic` on any checkout.
Running a model arm is an operator decision, because it spends money.
