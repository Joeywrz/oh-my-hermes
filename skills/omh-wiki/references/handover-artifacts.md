# Handover Artifacts

Three artifacts close a long piece of work: a **deep guide**, an **ELI5 pass**,
and a **quiz**. They are written for whoever opens the work next - the same
person after the context is gone, or a model with no memory of the session.
Not a teammate being onboarded, not an outsider, and not publication prose.

`wiki`'s interview asks whether an agent is one of the readers. Here the answer
is always yes, so write for recall: short, named, and searchable over polished.

## Assemble from the record, not from the conversation

The other three sections depend on this one.

By the time these artifacts are written the early reasoning is no longer in
context - compaction took it - and a model asked to explain a decision it can
no longer see will reconstruct one. The reconstruction is fluent, it agrees
with the diff, and it is invented. It is also the worst thing to leave behind,
because nothing downstream can tell it from the reason that was actually there.

The reasons are not gone. They were written down while they were still true.

| Record | Read these | What it supplies |
| --- | --- | --- |
| Plan record (`omh_todo/v1` item) | `state`, `phase`, `blocked_reason` | what was done, which stage it belonged to, and what was skipped with its reason |
| Verification gate (`verification-gate`) | `observed_check_results/v1`, `claim_verdict/v1` | which command actually ran, its exit status, and which checks are missing or failed |
| Review (`code-review`) | `ranked findings per axis` | what a reader misses unless someone tells them |
| QA (`ultraqa`) | `pass/fail evidence` | what nobody thought of the first time |

Every sentence in all three artifacts either restates one of those fields or is
marked as the writer's own inference. There is no third category. Quote the
field rather than paraphrasing it; a paraphrase of a reason is where the drift
starts.

An empty field is an answer. No item carrying a `blocked_reason` means nothing
was blocked - it does not mean the reason is somewhere in the transcript.
Write `none` and move on.

This repository already applies the rule one stage earlier. `blocked_reason`
is a field because the stop criterion used to be inferred from item text, and
the inference was wrong in both directions on ordinary input. Reading a record
instead of a sentence is that same fix, applied at the end of the work instead
of the middle.

## Deep guide

The next reader has the diff. What they do not have is why it looks like that,
and that is all the deep guide carries.

- One section per done item, in `phase` order. A done item absent from the
  guide is a gap: either write it or name it as deliberately omitted.
- Each section answers three questions from three fields. **What changed** -
  the item's own text. **Why this way** - the `blocked_reason` of what was not
  taken, plus the review findings that landed. **What proves it** - the
  observed check rows, by command and exit status.
- Delete any sentence `git show` would have told the reader. A guide that
  narrates the diff costs a read and returns nothing.
- Name the file and the symbol. Never the line number and never a count: both
  drift, and a pointer that drifts sends the next reader hunting for a string
  that is no longer there.
- A decision with no recorded reason is written as having no recorded reason.
  That sentence is worth more than a plausible one.

## ELI5 pass

Level `very_easy`, the same word `paper-learning` records, so one idea keeps
one name. The ELI5 pass is the deep guide at that level - not a second
document and not a second source.

- It is a projection. A claim the deep guide does not make was invented at the
  moment of simplifying.
- `very_easy` here means: expand every repo-internal term on first use, one idea
  per sentence, and the reason before the mechanism.
- `very_easy` never means dropping a boundary, a refusal, or a `blocked_reason`.
  Simplification removes vocabulary; it never removes a claim. That is the
  coverage-preserving constraint `paper-learning` already holds, applied to a
  change instead of a paper.
- No deep guide, no ELI5 pass. An easy explanation with nothing behind it is a
  guess that reads as an authority.

## Quiz

The quiz is a completeness check on the deep guide. It is not a study aid and
nobody is being graded.

**Every question cites one record entry, and a question that cannot cite one is
not written.** Three entry kinds are admissible and no others.

| Admissible entry | Where it comes from | What it proves |
| --- | --- | --- |
| A review finding | `code-review` ranked findings | a reader misses this unless told |
| A failed check | `ultraqa` pass/fail evidence, or a HOLD/BLOCK `claim_verdict/v1` | nobody thought of it the first time |
| A `blocked_reason` | the plan record | a judgement was made and needs explaining |

Each one is a record of something that actually went wrong or was actually
decided. That is the entire admission test, and it is what stops the quiz
becoming "what does this change do" - a question whose answer is in the diff,
which tests nothing and passes always.

- Carry the citation with the question: the entry kind and the entry's own
  identifier - finding id, check name, or the item the reason hangs on.
- Answer from the deep guide only. **A question the deep guide cannot answer is
  a hole in the deep guide.** Record it as a gap and fix the guide. Do not
  soften the question, and never answer it from the transcript - answering
  from the transcript is the invention this page exists to prevent.
- One question per admissible entry, and no padding. Two findings, no failed
  checks and nothing blocked is a two-question quiz, and two is the right
  answer rather than a thin one.
- Zero admissible entries is `no_admissible_entries`, written as that. Not an
  empty quiz and not an invented one: a change nothing caught, nothing failed
  on, and nothing was skipped in has no completeness check to run.

## Boundary

The three artifacts are prepared retained knowledge. They are not execution,
verification, review, CI, merge-readiness, or merge evidence. A deep guide
restating a PASS verdict has not re-proved it, and writing all three closes
nothing that was not already closed.
