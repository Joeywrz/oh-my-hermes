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

## Safety and claim boundary

- Offline by default. `export`, `deterministic` and `score` spend nothing.
- `run` spends money and refuses without `--allow-paid-live`,
  `--max-paid-calls` and `--confirm`. The library refuses a second time in
  process, so importing it and calling `run_batch` cannot spend anything
  either.
- The paid path is `omh coding hermes-child dispatch --confirm-dispatch`. The
  prompt goes on stdin and is never persisted; a run record carries the exit
  code and the reason an answer file is missing, never model text.
- `hermes_current_session` runs the caller's authenticated Hermes profile. It
  is labelled in every record and is not the isolated child boundary.
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
  --batch-size 20 --allow-paid-live --max-paid-calls 36 --confirm
```

`--answers` also accepts a directory of `route_question_answer/v1` records, the
shape an answer recorded on a live route is written in; those join a corpus
item by question digest.

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

`omh chat route-questions score` is the authority on what a row is worth. It
names every malformed row by line, counts it, and never drops it silently.

## Latest measured status

No live arm has been run. The deterministic arm is the only measurement this
lane carries, and it is reproduced by `bench.py deterministic` on any checkout.
Running a model arm is an operator decision, because it spends money.
