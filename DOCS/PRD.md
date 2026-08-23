# PRD: Automated Invoice Intake for Sample Trading Co.

> Synthesized from planning discussion + implementation to date.
> No issue tracker connector is available in this environment — this document
> is the artifact in place of a published ticket. Copy into your tracker of
> choice if needed.
>
> **Revision 2** adds the outcome of a gap-analysis pass done after an
> alignment audit against the take-home's scored requirements. Sections
> below marked *(session 2)* reflect decisions made in that pass; the
> corresponding code changes are designed but **not yet implemented** — see
> Further Notes.

## Problem Statement

Sample Trading Co.'s accounting staff key every supplier invoice into the
accounting system by hand, one at a time, as they arrive each month. This
turns month-end close into overtime, and manual entry is error-prone enough
that a typo nearly caused a duplicate payment last month. Invoices arrive in
inconsistent formats depending on supplier — native-text PDFs, scanned PDFs,
scanned images from the office copier, and some with handwritten
annotations — and all of it is in Japanese.

The client's own framing ("could an AI read them and enter them
automatically?") describes the mechanism, not the actual problem worth
solving. The real problem is trust under uncertainty: any automation here is
only useful if the accounting staff can trust what gets entered without
re-checking every field by hand — otherwise the overtime just moves from
typing to auditing. The system therefore has to be honest about what it
doesn't know, not just fast at what it does.

## Solution

An automated pipeline that reads each invoice, extracts structured data,
independently verifies that data against arithmetic and business rules the
accounting system itself enforces, and registers only what reconciles
cleanly. Anything that doesn't reconcile — a misread digit, an unresolvable
supplier name, a genuinely inconsistent source document — is routed to a
human-editable review queue instead of being silently entered or silently
dropped.

The guiding design pattern, applied uniformly rather than as one-off
special cases: **every field that's hard to read gets an independent second
signal, and disagreement between the two routes to a human** — never a
silent guess and never a hard stop. This applies to amounts (self-reported
model confidence + arithmetic reconciliation), disputed fields (targeted
re-query on a stronger model), and supplier identity (LLM guess + fuzzy
string match against the partner master).

## User Stories

1. As an accounting staff member, I want invoices to be entered into the
   accounting system automatically, so that I don't spend hours each
   month-end retyping supplier invoices.
2. As an accounting staff member, I want to be confident that nothing enters
   the accounting system with a wrong amount, so that I don't risk a
   duplicate or incorrect payment.
3. As an accounting staff member, I want invoices the system isn't sure
   about to be set aside for me to check, rather than either guessed at or
   silently skipped, so that I know exactly what still needs my attention.
4. As an accounting staff member reviewing a flagged invoice, I want to see
   *why* it was flagged (which field, what the disagreement was), so that I
   know what to actually check on the source document instead of re-reading
   the whole invoice from scratch.
5. As an accounting staff member, I want to correct a flagged invoice and
   have it re-enter the system without redoing the whole intake process, so
   that fixing one invoice doesn't mean rerunning everything.
6. As an accounting staff member, I want to be able to rerun the intake
   process safely (e.g. after fixing something), so that I don't
   accidentally double-enter an invoice I already registered.
7. As the CEO, I want to see something working on the last two months of
   real supplier invoices, so that I can judge whether this is worth
   investing in further before committing budget.
8. As the CEO, I want to know what this would cost to run every month at
   real volume, so that I can weigh it against the overtime cost it's meant
   to replace.
9. As the CEO, I want to know where this breaks or needs a human even after
   it's built, so that I don't roll it out expecting it to be fully
   hands-off.
10. As an engineer maintaining this system, I want the verification logic to
    mirror the accounting API's own validation rules, so that invoices we
    think are "clean" don't get rejected by the API for reasons we could
    have caught ourselves.
11. As an engineer maintaining this system, I want a single command to start
    the whole pipeline (accounting API + intake), so that running or
    demoing it doesn't require multiple manual steps.
12. As an engineer extending this system, I want the LLM provider to be
    swappable, so that a change in model pricing or availability doesn't
    require rewriting the extraction logic.
13. As an engineer debugging a bad extraction, I want a concrete record of
    what was extracted, what was disputed, and why, so that I can tell
    whether a failure is an OCR/extraction problem or a genuine
    inconsistency in the source document.
14. As an engineer processing PDFs, I want native-text PDFs handled via
    direct text extraction rather than vision, so that the 3 out of 12
    invoices that have a real text layer get character-perfect, cheaper
    extraction instead of being OCR'd unnecessarily.
15. As an engineer registering invoices, I want registration to check the
    accounting system for existing records before posting, so that reruns
    are idempotent without relying on catching a duplicate error after the
    fact.
16. As an engineer evaluating cost, I want to know the per-invoice and
    per-month cost breakdown at realistic volume, so that I can flag where
    the pipeline would need re-architecting (e.g. batching, cheaper models,
    caching) if volume grows.
17. *(session 2)* As an accounting staff member, I want the invoice number
    itself to be double-checked, not just the amounts, so that the exact
    incident that prompted this project — a typo nearly causing a duplicate
    payment — can't happen silently through a misread invoice number
    disabling the accounting system's own duplicate check.
18. *(session 2)* As the CEO, I want duplicate-payment protection to live in
    one place (the accounting system I'm keeping), not be reimplemented
    separately by this pipeline, so that the two can't drift out of sync
    with each other.
19. *(session 2)* As an accounting staff member, I want to know when an
    invoice registered successfully but with a caveat (e.g. a tax-base
    mismatch, a low-confidence description), so that "it went through
    clean" and "it went through with something worth a second look" aren't
    indistinguishable outcomes.
20. *(session 2)* As an engineer or the CEO reviewing pipeline output, I
    want a per-run report of what registered, what registered with
    warnings, what was skipped as a duplicate, and what needed a human, so
    that I can answer "how would I find out if something was registered
    incorrectly" without reading logs line by line.

## Implementation Decisions

**Extraction — hybrid by source type**
- Native-text PDFs (has a real embedded text layer, threshold-checked, not
  "any text at all" since some scans carry stray embedded labels): extract
  text directly (`pdfplumber`), structure it with a cheap text-only LLM call.
- Scanned PDFs (image-only) and image files (JPG/PNG): render/pass to a
  vision LLM call.
- Primary extraction model: a cheap/fast tier (`gpt-4o-mini` in the intended
  production configuration). Decided against a full OCR-engine pipeline
  (Tesseract or a cloud OCR service) as the primary path — current
  vision-LLMs handle Japanese business documents well, and a second OCR
  engine was judged not worth its setup cost and Japanese-handwriting
  weakness (particularly Tesseract) for a 12-invoice job.

**Structured output schema**
- Every extraction returns: invoice identity fields, supplier name as
  printed + an LLM-guessed `partner_code`, line items (description,
  quantity, unit, unit_price, amount, tax_code — quantity/unit_price
  nullable, amount always required, negative amounts supported for
  discount lines), printed subtotal/tax/total, a `field_confidence` map
  (only low-confidence fields need an entry), and free-text notes for
  anything visually unusual (handwriting, stamps, corrections).

**Verification — arithmetic reconciliation mirrors the accounting API exactly**
- Recompute `subtotal` = sum of line amounts, `tax` = per-tax-code
  floor-rounded subtotal × rate, `total` = subtotal + tax — the identical
  logic the accounting API itself uses to validate `AMOUNT_MISMATCH`. Any
  mismatch between the document's printed totals and the recomputed values
  becomes a dispute, checked client-side before ever calling the API.
- Self-reported low-confidence fields from the extraction pass are treated
  as disputes, **but not all with equal weight** *(session 2 revision)* —
  see "Blocking vs. non-blocking checks" below.
- Missing fields required by the API (`invoice_number`, `issue_date`,
  `due_date`) are disputes.
- *(session 2)* Printed tax bases are captured and reconciled: Japanese
  invoices print a taxable-base figure per rate (e.g. `消費税10%（対象
  134,088）`). The sum of line amounts assigned to a given `tax_code` must
  equal the corresponding printed base — this is an independent,
  zero-extra-cost check on the model's per-line tax-code assignment
  (relevant because a mixed 8%/10% invoice is a normal Japanese business
  scenario, e.g. a food supplier, and tax-code assignment currently has no
  other check). Non-blocking (see below).
- *(session 2)* Cheap date sanity checks added: `due_date ≥ issue_date`
  (the API enforces this anyway, so failing it locally is a guaranteed
  rejection) and range plausibility (a date far outside a normal invoice
  period usually indicates a misread era conversion or transposed digit).
  No LLM cost.

**Blocking vs. non-blocking checks — the automation/review boundary,
made explicit *(session 2)***
- Not every failed check should stop an invoice from registering. The
  policy adopted: **blocking checks guard money movement, non-blocking
  checks guard data quality.** Concretely, only
  `invoice_number, issue_date, due_date, total_amount, subtotal,
  tax_amount, partner_code` (and the arithmetic/tax-base reconciliations
  that feed them) block registration and route to human review.
  Everything else that's uncertain — principally free-text `description`
  fields, which have no consequence for what gets paid — is recorded as a
  warning in the per-run report (below) instead of stopping the invoice.
- This directly answers "where you drew the line between automation and
  human review": the line is drawn at whether the field affects a payment
  amount or a payment identity, not at whether the model happened to be
  unsure.
- Implemented as a single named constant (a `BLOCKING_CONFIDENCE_FIELDS`
  set), not a config system — deliberately: it's a judgment call that
  belongs to the client, not to this pipeline, and it should be one line
  to change once he weighs in, rather than requiring a settings UI nobody
  asked for.
- The reconciliation-mismatch policy (invoice_09's ¥1 discrepancy) is
  currently set to **always block / always route to a human**, with an
  explicit note that this is provisional: it was chosen without knowing
  how *frequently* source documents fail to reconcile, and if that turns
  out to be common across the full 12-invoice set, "always human" would
  push the flag rate high enough to undercut the automation's value. A
  tolerance band (e.g. auto-accept mismatches within ±¥1 per tax code,
  which is what pure floor-rounding produces) is the likely next step, but
  setting the tolerance is a client policy decision, not an engineering
  one — deferred rather than guessed at.

**Reconciliation reporting *(session 2)***
- Every pipeline run emits a JSON report (not just console output) summarizing:
  what registered clean, what registered with non-blocking warnings (and
  which), what was routed to review (and why), what was skipped as
  already-registered, and the aggregate flag rate for the run.
- This is the direct answer to the take-home's "how would you find out if
  something was registered incorrectly" question, and it's also the
  instrument that would tell you whether the always-block reconciliation
  policy above is actually sustainable at real volume.
- JSON chosen over Markdown as the artifact (better fit for "at scale
  you'd pipe this into monitoring"); the existing console summary already
  covers the human-readable case, so no second format was built.

**Disputed-field resolution — targeted re-query, not a second OCR pipeline**
- For vision-sourced invoices, a subset of fields that are meaningfully
  re-readable in isolation (`invoice_number`, `issue_date`, `due_date`,
  `total_amount`) get an independent second read: the same image is
  re-sent to a stronger model tier (`gpt-4o` in production) with a fresh,
  narrowly-scoped prompt, not the original extraction prompt. Agreement
  between the two reads resolves the dispute; disagreement is recorded with
  both readings and still routed to review.
- Cross-field arithmetic disputes (subtotal/tax/total not reconciling) are
  *not* re-queried — a second read of one field can't fix a document whose
  own printed numbers don't add up. These route straight to review.
- Decided against bounding-box image cropping for the re-query (fiddly to
  get right without model-provided coordinates); the re-query re-sends the
  full image with a different, narrower prompt instead.
- *(session 2)* `invoice_number` is re-queried **unconditionally**, not only
  when already disputed — it's the one field where belt-and-braces was
  judged worth it, because a *confident* misread here is exactly the
  failure mode the client described (a typo, not a garbled scan) and every
  other check in the pipeline would let it through untouched.

**Duplicate detection — ownership boundary *(session 2)***
- The accounting API already rejects exact `(partner_code, invoice_number)`
  collisions (`DUPLICATE_INVOICE`). Decided **not** to build a second,
  parallel duplicate-detection layer in the pipeline — the client is
  explicitly keeping his existing accounting system, and a second
  implementation of the same concept risks drifting from the system of
  record rather than reinforcing it.
- This makes `invoice_number` accuracy load-bearing for the client's
  actual reported incident (a near-duplicate payment from a typo): if the
  pipeline feeds the API a misread number, dedup doesn't degrade, it's
  silently disabled, because the API sees two different keys. The
  unconditional re-query above exists specifically to close this.
- Additionally, a **variant-collision probe** runs against already-registered
  invoice numbers for the same partner: generate common OCR/typo confusions
  of the extracted number (`O`↔`0`, `l`↔`1`, `S`↔`5`, `B`↔`8`, etc.) and
  check whether any variant matches an existing record. A hit is a
  suspected duplicate, routed to review. This needs no invoice history to
  bootstrap (unlike a learned per-supplier format check, see Out of Scope)
  and directly targets the reported failure mode rather than a proxy for
  it. Known limitation: it only catches collisions against invoices already
  registered — two misread copies of the same invoice arriving in the same
  batch, before either is registered, would both go through on the first
  pass (though the second would then collide with the first on any
  subsequent run).

**Supplier resolution — dual independent check**
- The extraction prompt is given the full partner master (names + aliases)
  and asked to guess a `partner_code` directly.
- Independently, a code-level fuzzy match (`rapidfuzz`, token-sort ratio,
  threshold 80) matches the printed name against the same master.
- Only agreement between the two resolves the supplier; disagreement (or
  either one failing to find a confident match) is a dispute.

**Human review — file-based queue, not a UI**
- Disputed invoices are written as editable JSON to a `review/pending/`
  directory, including the original (unedited) extraction, the list of
  disputes with reasons, and instructions.
- A human edits the file directly to correct fields, or deletes it to
  reject the invoice outright.
- Reprocessing (`--process-review`) re-verifies edited files and registers
  what now reconciles. On resolution, a diff of `{field: {before, after}}`
  is logged and archived, so a rerun's effect is visible even without a UI
  (mitigation for the weaker demo this choice implies — deliberate scoping
  trade-off, not an oversight).
- Decided against a Streamlit/web review screen and against LangGraph-based
  orchestration for the pipeline as a whole — both were judged not worth
  their build cost against an 8-hour budget for a 12-invoice job, and
  plain Python functions make the branching logic (dispute → re-query →
  review → register) easier to read and explain than a graph framework
  would for something this size.

**Registration — idempotent by design**
- Before registering, fetch all currently-registered
  `(partner_code, invoice_number)` pairs from the accounting API and skip
  any invoice already present, rather than relying on catching the API's
  `DUPLICATE_INVOICE` error after the fact. Makes reruns (explicitly
  supported by the take-home's mock API) safe by construction.
- All accounting-API error codes are surfaced through to the result object
  for the caller/log to see, not swallowed.

**Provider abstraction**
- A single `LLMClient` class dispatches to either an Anthropic or OpenAI
  backend based on an environment variable, with two named tiers
  (`primary`, `strong`) per provider. This sandbox can only reach
  `api.anthropic.com` for live testing (network egress is allowlisted);
  the OpenAI path is fully implemented but untested live here, and is the
  intended configuration for the actual submission, run locally with the
  candidate's own API key.

**Startup**
- `run.sh` starts the accounting API, health-checks it, then runs the
  pipeline against `invoices/`, all from one command — satisfies the
  take-home's explicit "single command to start it" requirement.

## Testing Decisions

Tests should exercise **observable pipeline behavior** — what gets
registered, what gets disputed and why, what the review queue contains —
not internal function call counts or prompt contents. There's no prior art
in this repo (greenfield project); the test suite establishes the pattern.

What's tested (offline, without live LLM calls, using hand-built fixtures
standing in for extraction output — deliberate, since the LLM call itself
can't be exercised live in this sandbox):

- **Arithmetic reconciliation correctness**, using the real numbers from
  the two sample invoices available. This surfaced a genuine finding, not
  a bug: `invoice_09`'s own printed total (¥147,497) doesn't reconcile with
  its printed subtotal + tax (¥134,088 + ¥13,408 = ¥147,496) — a 1-yen
  rounding artifact in the source document itself. The test asserts this
  is correctly caught as a dispute rather than silently passed through,
  since the real accounting API's identical floor-rounding logic would
  reject it too.
- **Clean-path registration**, using `invoice_12` (which includes a
  lump-sum line with null quantity/unit_price and a negative discount
  line) to confirm those schema edge cases are handled correctly through
  to a successful `POST /invoices`.
- **Idempotent rerun**: registering the same invoice twice is confirmed to
  short-circuit via the GET-first check (`skipped_duplicate`) without ever
  hitting the API's `DUPLICATE_INVOICE` error path.
- **Digit-misread detection**: a corrupted total (off by ¥1,000) is
  confirmed to produce a dispute via arithmetic mismatch.
- **Partner-mismatch handling**: a garbled supplier name is confirmed to
  fail to resolve (neither LLM guess nor fuzzy match succeeds, or they
  disagree) rather than silently matching the wrong partner.
- **Review-queue round trip**: a disputed invoice is queued, "corrected" by
  simulating a human edit, reprocessed via `--process-review`, confirmed to
  register successfully, and confirmed to produce an accurate before/after
  diff log. (This caught and fixed a real bug during development: the
  original queue format stored only the editable copy, so if the edit
  target field was the same field being diffed, before == after trivially.
  Fixed by persisting the original extraction separately from the editable
  copy.)

## Out of Scope

- A true independent OCR engine (cloud or local) as a cross-check —
  explicitly considered and decided against; the targeted same-model
  re-query is the chosen mitigation, with its limitation (not a truly
  independent read) noted rather than hidden.
- LangGraph or any agentic-graph orchestration framework — the pipeline's
  branching is expressed as plain Python control flow.
- A web/Streamlit review UI — the file-based queue is the shipped
  mechanism; a UI is a candidate "next 8 hours" item, not in this scope.
- Bounding-box image cropping for the re-query step.
- Batch concurrency / throughput optimization — the 12-invoice sample
  volume doesn't warrant it; noted as a first thing to revisit at
  production scale (see cost/limits section of SUBMISSION.md).
- Monitoring, alerting, or a production deployment target (this is a
  take-home prototype, not a shipped service).
- Handling currencies other than JPY, or multi-page invoices with line
  items spanning multiple pages (not present in the sample set).
- *(session 2)* A **learned per-supplier invoice-number format check**
  (regex/pattern inferred from each supplier's known numbers) — considered
  as a way to harden `invoice_number`, decided against in favor of the
  variant-collision probe. With ~12 invoices across 5 suppliers, there's
  only 2–3 examples per supplier to learn a format from; a check built on
  that little evidence risks false-flagging legitimate variation, which
  directly worsens the flag-rate problem it was meant to help with. The
  variant-collision probe needs no history to bootstrap and targets the
  actual failure mode (a duplicate slipping through) rather than a proxy
  for it (does this look like the supplier's usual format).
- *(session 2)* A **configurable/settings-driven blocking-fields policy**
  — the blocking/non-blocking line is a single code constant, not a
  runtime setting, on the reasoning that it's a client policy decision
  that should require the client's sign-off to change, not routine
  self-service.

## Further Notes

- This PRD documents a system built against a **fixed, non-negotiable
  mock API spec** (`accounting_api.py`, provided by the take-home,
  behavior not modifiable). Several design decisions above exist
  specifically because that API recomputes and validates amounts
  server-side — the client-side verification logic was written to fail
  the same way the API would, before ever making the network call.
- Remaining build items as of this document: finish `run.sh` polish,
  write `README.md`, implement the session 2 changes below (not yet in
  code as of this revision), run all 12 invoices through the live
  pipeline end-to-end (currently only 2 of 12 are available in this
  environment; tested via fixtures + `invoice_09.pdf`/`invoice_12.jpg`
  render paths), and write `SUBMISSION.md` proper (this PRD is scoping
  documentation, not the take-home's required submission format — they
  serve different purposes and both are needed).
- The cost-estimate section of the eventual submission should use real
  current OpenAI pricing for `gpt-4o-mini` / `gpt-4o`, looked up at
  write-time rather than assumed from training data. (Done separately —
  see cost analysis from the alignment-audit pass: the mini/4o cost gap
  narrows substantially on image input due to token-multiplier
  differences, and the dominant real cost at any realistic volume is
  human review minutes, not tokens — which is why flag rate, not model
  choice, is the metric the reporting above is built to surface.)
- **Session 2 changes not yet implemented in code**: unconditional
  `invoice_number` re-query, the variant-collision duplicate probe,
  printed tax-base reconciliation, date sanity checks, the
  `BLOCKING_CONFIDENCE_FIELDS` reclassification (descriptions currently
  still block via the old "any low-confidence field disputes" logic in
  `verify.py`), and the per-run JSON reconciliation report. All were
  designed and agreed in a follow-up gap-analysis session but require
  code changes to `schema.py`, `verify.py`, and `pipeline.py` before the
  test suite and a live run can confirm they work as designed.
