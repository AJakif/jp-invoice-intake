# Submission

- Name: [FILL IN]
- Submission date (YYYY-MM-DD): [FILL IN]
- Hours actually spent: [FILL IN]
- Repository / how to run it: this repo. `pip install -r requirements.txt`, add an LLM key to `.env` (see `.env` / `README.md`), then `./run.sh`.

## 1. Understanding the request

> Reading the client's email, what did you decide was the problem worth solving?
> State both the problem the client described and the problem you actually set out to solve.

The client described the problem as speed: manual entry causes overtime, and
he wants AI to "read invoices and enter them automatically." Taken
literally, that's an OCR-and-post pipeline.

The problem underneath it is trust, not speed: the CEO's own example of the
pain this causes is a *near-duplicate payment from a typo*, not a slow
month-end close. An automation that reads fast but occasionally posts a
wrong amount, a wrong supplier, or a duplicate invoice doesn't reduce risk —
it just moves the source of a bad payment from a tired human to a
confidently-wrong model, with nobody checking either. So the problem I set
out to solve was: *how much of this can be automated safely*, where "safely"
means every field that could cause a wrong or duplicate payment gets an
independent second check before anything is trusted, and everything that
fails that check is set aside for a human rather than guessed at.

[FILL IN — anything you'd add here about your own framing/priorities]

## 2. What you would have asked the client

> List the questions you wanted to ask, assuming you could not ask them.
> For each one, state **the assumption you made instead of waiting for an answer**,
> and why you made it. We are looking for questions paired with assumptions, not questions alone.

| What you wanted to ask | The assumption you made | Why |
|---|---|---|
| The mock API rejects reconciliation mismatches down to the yen (invoice_09's own printed total is off by ¥1 from its printed subtotal+tax). Real supplier invoices will have this kind of rounding noise — what tolerance, if any, should auto-accept? | Zero tolerance: any mismatch blocks and routes to review. | I don't know the mismatch *rate* across real volume yet — only 12 samples, 1 of which already hits this. Guessing a tolerance band without knowing the real distribution risks quietly waving through genuine errors. Deferred as a client policy decision (see `verify.py`'s comment on this), not an engineering guess. |
| What should happen when a supplier isn't in the partner master at all (invoice_10, `新星ロジスティクス株式会社`) — is this a new supplier to onboard, or a sign the invoice shouldn't be paid from this system? | Treat it as an unresolvable dispute, route to human review, take no further action (no auto-onboarding of new partners). | Auto-creating a partner record is a much bigger decision (opens a path to paying an unverified account) than flagging one for a human to decide. |
| Is there a threshold flag rate above which this stops being worth running unattended (e.g. if half of every batch needs a human anyway)? | None assumed — the per-run JSON report exists specifically so this can be measured empirically instead of guessed. | Flag rate depends on document quality this pipeline hasn't seen yet; better to instrument and observe than commit to a number now. |
| Should line-item descriptions be corrected to match the client's own internal item catalog/wording, or is verbatim-from-invoice sufficient for the accounting system's purposes? | Verbatim is sufficient — `description` has no payment consequence and isn't validated by the accounting API beyond "non-empty". | The accounting API's own shape check treats `description` as free text; nothing downstream depends on it matching an internal catalog. |
| [FILL IN — add any question specific to your own build/experience] | | |

## 3. Scoping decisions

> This assignment does not fit in 8 hours. What did you include, what did you cut,
> and why in that order?

**What you built**

- Hybrid extraction: native-text PDFs via `pdfplumber` + a text LLM call;
  scanned PDFs/images via `pymupdf` rendering + a vision LLM call.
- Client-side verification that mirrors the accounting API's own
  reconciliation logic exactly (floor-rounded tax per tax code), so mismatches
  are caught before the network call, not after.
- Dual-signal supplier resolution (LLM guess + fuzzy match against the
  partner master) — only agreement resolves the supplier.
- A blocking/non-blocking policy (`verify.BLOCKING_CONFIDENCE_FIELDS`) that
  draws the automation/review line at "does this affect a payment amount or
  identity," not at "was the model unsure about anything."
- Targeted single-field re-query on a stronger model tier for
  identity-critical fields on vision-sourced invoices — `invoice_number`
  unconditionally, others only when already disputed.
- A duplicate variant-collision probe (OCR/typo confusions of the invoice
  number checked against already-registered numbers) — this is what caught
  invoice_07 as a duplicate of invoice_01 in the real run.
- Idempotent registration (fetch-then-skip, not catch-the-error).
- A file-based human review queue with before/after diffing on resolution.
- A per-run JSON report (registered / warned / skipped-duplicate / reviewed,
  and flag rate).
- Offline tests against hand-built fixtures using the real numbers from
  invoice_09 and invoice_12, run against a real `accounting_api.py`
  subprocess.
- `./run.sh` as the single start command.

**What you left out, and why**

- *A human review UI.* The file-based queue (`review/pending/*.json`) is
  the shipped mechanism. A web screen is higher-value than most other
  remaining items but didn't fit the budget after the verification/dispute
  logic — see section 8.
- *A true independent OCR engine as a cross-check.* Considered and rejected:
  a second OCR pass (Tesseract or a cloud OCR API) adds real setup cost and
  is weak specifically on Japanese handwriting, which is exactly the case
  it would need to help with. The targeted re-query on a stronger model tier
  is the mitigation instead, with its limitation (not a truly independent
  read, same model family) noted rather than hidden.
- *A learned per-supplier invoice-number format check.* With ~12 invoices
  across 5 suppliers there's only 2-3 examples per supplier — not enough to
  learn a format from without risking false-flagging legitimate variation.
- *Batch concurrency.* The pipeline processes invoices sequentially. Fine at
  12; would need revisiting well before 1,000/month (see section 7).
- *A configurable blocking-fields policy.* `BLOCKING_CONFIDENCE_FIELDS` is a
  single code constant, deliberately not a settings UI — it's a client
  policy call that should require sign-off to change.
- [FILL IN — anything you cut that isn't listed above, and your own ordering rationale]

## 4. Design and technology choices

> Describe the flow end to end and why you chose your main components.
> A diagram helps but is not required.
> Say what you chose **and what you decided against**.
> Include which LLM or OCR service you used and why — a free tier or local model is a valid answer.

```
extract.py  ->  verify.py  ->  verify.resolve_disputes_via_requery  ->  register.py / review.py
```

1. **Extract**: native-text PDFs (text layer above a length threshold, so a
   scan with a few stray embedded labels doesn't get misclassified) go
   through `pdfplumber` text extraction, cheaper and character-perfect
   compared to OCR/vision. Everything else (scanned PDFs, JPG/PNG, including
   handwritten annotations) is rendered to an image and read by a vision
   model call. Multi-page native-text PDFs (invoice_02, discovered while
   building — the take-home's own "no multi-page" note turned out not to
   hold for the sample set) have all pages' text concatenated before
   extraction, which handles it for free.
2. **Verify**: independent, zero-LLM-cost checks run against the extraction
   output before anything touches the accounting API — arithmetic
   reconciliation (identical logic to `accounting_api.py`'s own
   recalculation), a printed-tax-base cross-check, date sanity checks, and
   dual-signal supplier resolution.
3. **Targeted re-query**: rather than a second full OCR pass, a narrow,
   single-field prompt re-reads specific fields on the same image with a
   stronger model. Decided against bounding-box cropping (fiddly without
   model-provided coordinates) — the re-query just re-sends the full image
   with a different prompt.
4. **Route**: clean invoices register via a thin API client that fetches
   existing records first (idempotent by construction); disputed invoices
   become editable JSON files in `review/pending/`.

**LLM choice**: OpenAI (`gpt-4o-mini` for extraction, `gpt-4o` for
re-query), selected via `LLM_PROVIDER=openai` in `.env`. [FILL IN — say
why you picked OpenAI specifically: pricing, an existing account/credits,
familiarity, etc.] The provider is abstracted behind `src/llm_client.py`
(`LLMClient`) so swapping to Anthropic is a one-line env var change, not a
rewrite — the extraction/verification logic doesn't know which provider
answered it.

**Decided against**:
- A second OCR engine (Tesseract / a cloud OCR API) as the primary read
  path — current vision LLMs handle Japanese business documents well
  enough that a second engine's setup cost and Japanese-handwriting
  weakness weren't worth it for 12 invoices.
- LangGraph or an agentic-graph framework for orchestration — the
  branching (dispute → re-query → review → register) is plain Python
  control flow in `pipeline.py`; a graph framework adds indirection this
  size of job doesn't need.
- A Streamlit/web review screen — file-based queue instead (see section 3).

## 5. How you used AI, and how you checked it

> Which parts of the work did you hand to AI, and how did you instruct it?
> Then: **where did you not trust the output**, and how did you check it?
> We care more about the reasoning here than the implementation.

**What you delegated to AI**

[FILL IN — your own account of what you asked Claude Code (or whatever
tooling you used) to do: e.g. scaffolding the module structure, drafting
the extraction/verification logic against the PRD, writing the test
fixtures, debugging the live run.]

**How you verified the output**

Every extraction the LLM produces is independently re-checked before it's
trusted, by design — that's the whole point of `verify.py` — but the
pipeline *code itself* also needed checking, separately from what it
verifies at runtime:

- Ran the pipeline against all 12 real sample invoices rather than trusting
  the offline test fixtures alone. The first real run had an 83% flag rate
  — high enough to be a signal that something was systematically wrong,
  not that the invoices were systematically hard.
- Traced the disputes back to source: opened the actual invoice images
  next to what got extracted, rather than assuming a "review" outcome meant
  the document was genuinely ambiguous.
- [FILL IN — your own verification steps beyond what's captured above]

**A case where the AI got it wrong**

The first live run flagged a `partner_code` dispute on nearly every
invoice. The cause: every invoice's *recipient* is the same company
(`株式会社サンプル商事`, marked with the honorific `御中`), printed close to
the actual *supplier's* name. `pdfplumber`'s linearized text extraction
loses the original left/right page layout, so the two company names ended
up adjacent in the text with no visual cue distinguishing them — and the
model was consistently extracting the recipient as `supplier_name_printed`
instead of the supplier.

I fixed it by making the extraction prompt name the recipient explicitly
(it's always the same company) and pointing at an unambiguous
disambiguator instead — the supplier is the company printed next to a TEL
number and a `登録番号` (tax registration number). Flag rate dropped from
83% to 42% on the next run purely from that prompt fix, and the remaining
disputes on rerun were all genuine (see section 6). This is exactly the
kind of error the design is meant to catch even when it's *systematic*
rather than occasional — but it's also a reminder that "the model is
usually confidently wrong in the same way" is worth checking for
specifically, since a per-invoice confidence score wouldn't have caught it
(the model wasn't uncertain, it was consistently wrong).

## 6. Integrating with the accounting system

> How did you handle the API's constraints?
> If any invoice could not be registered, explain how your design handles it.

| Invoice | Result | How you handled it |
|---|---|---|
| invoice_01.pdf | Registered (ACC-0001) | Clean reconciliation. |
| invoice_02.pdf | Registered (ACC-0002) | Multi-page native-text PDF; all pages' text concatenated before extraction. |
| invoice_03.pdf | Registered (ACC-0003) | Clean reconciliation. |
| invoice_04.jpg | Registered (ACC-0004) | Clean reconciliation; handwritten annotation on the document noted but didn't affect any registered field. |
| invoice_05.jpg | Registered (ACC-0005) | Clean reconciliation. |
| invoice_06.jpg | Registered (ACC-0006) | Clean reconciliation. |
| invoice_07.jpg | Skipped as duplicate | A scanned photo of the exact same physical invoice as invoice_01.pdf (identical invoice number, supplier, and amounts). Caught by the idempotent fetch-then-skip check before ever calling `POST /invoices` — this is the client's own reported failure mode (a near-duplicate payment) working as intended. |
| invoice_08.jpg | Routed to review | Printed tax total (¥9,260) doesn't match the tax recomputed from the document's own two printed per-rate lines (8% + 10%, which independently sum to ¥8,936) — a genuine misread of the combined total, caught by arithmetic reconciliation before the API ever saw it. |
| invoice_09.pdf | Routed to review | The document's own printed total (¥147,497) doesn't reconcile with its printed subtotal + tax (¥134,088 + ¥13,408 = ¥147,496) — a 1-yen rounding artifact in the source document itself, not an extraction error. Correctly caught rather than silently passed through, since the real accounting API's identical floor-rounding logic would reject it too. |
| invoice_10.jpg | Routed to review | Supplier (`新星ロジスティクス株式会社`) genuinely does not appear in the partner master (`GET /partners` only lists 5 suppliers) — correctly unresolvable, not a misread. |
| invoice_11.jpg | Registered (ACC-0007) | Clean reconciliation. |
| invoice_12.jpg | Registered (ACC-0008) | Lump-sum line (null quantity/unit_price) and a negative discount line; clean reconciliation through the schema edge cases. |

Every invoice that reached `POST /invoices` was accepted by the real API on
the first attempt — no `AMOUNT_MISMATCH` or `VALIDATION_ERROR` responses
during the live run, because `verify.py` mirrors the API's own validation
before that call is ever made. The one gap found during the live run (the
API requires `unit` to be a non-null string on every line, even lump-sum
lines with null quantity/unit_price) was in the extraction schema, not the
API integration, and was fixed before this table was produced.

## 7. Cost, limits, and risk in production

> Rough numbers are fine, but show your reasoning.

Measured directly from a real call against the sample invoices
(`gpt-4o-mini` for extraction, `gpt-4o` for re-query), not estimated:

| Call | Input tokens | Output tokens | Cost |
|---|---|---|---|
| Vision extraction (1 scanned image, `gpt-4o-mini`) | 38,181 | 240 | ~$0.0059 |
| Text extraction (1 native-text PDF, `gpt-4o-mini`) | 2,107 | 1,623 | ~$0.0013 |
| Single-field re-query (`gpt-4o`) | 1,217 | 9 | ~$0.0031 |

(Pricing: `gpt-4o-mini` $0.15/$0.60 per 1M input/output tokens; `gpt-4o`
$2.50/$10.00 per 1M input/output tokens, as of this writing.) The
vision-call token count looks disproportionate next to the text call, but
this is expected and documented by OpenAI — `gpt-4o-mini` uses a higher
per-image token multiplier than `gpt-4o`, which is why the mini/4o cost gap
narrows substantially on image input specifically.

- **Cost per invoice**: native-text PDFs cost ~$0.001-0.002 (one text
  call, no re-query needed — `invoice_number` re-query only applies to
  vision-sourced invoices). Vision-sourced invoices cost ~$0.006 (extraction)
  + ~$0.003 per re-queried field (`invoice_number` always, others only if
  disputed) — roughly $0.009-0.013 depending on how many fields get
  re-queried. Blended across a mix like the sample set (3 native / 9
  vision), that's roughly **$0.008 per invoice**.
- **Monthly cost at 1,000 invoices/month**: **~$8-13/month** in LLM API
  cost at this mix. This is small enough that it isn't the number worth
  optimizing.
- **Processing time per invoice**: dominated by model latency, not local
  compute (PDF text extraction and image rendering are both sub-second) —
  roughly 5-10 seconds for a native-text PDF, 10-20 seconds for a
  vision-sourced invoice with a re-query, sequential (no concurrency yet).
  At 1,000 invoices/month that's a non-issue for a monthly batch job, but
  would matter if this needed to run near-real-time.
- **Where this breaks first**: not cost, and not raw LLM throughput — the
  **human review queue**. The sample run's flag rate was 25% (3 of 12,
  after fixing the supplier-extraction bug — see section 5), all for
  genuine reasons. If that rate holds at 1,000 invoices/month, that's
  ~250 invoices/month needing a few minutes of human attention each — call
  it 250 × 4 min ÷ 60 × a modest hourly rate, and the review-labor cost
  is already an order of magnitude above the ~$10/month LLM cost. Flag
  rate, not model choice, is the number that actually determines whether
  this pays for itself, which is exactly why the per-run JSON report
  tracks it. Secondary bottleneck: the pipeline is currently sequential,
  so a large batch (e.g. catching up a whole month at once) would be slow
  wall-clock, and OpenAI per-minute rate limits would start to matter
  before 1,000/month became a problem if run as one big batch rather than
  spread through the month as invoices arrive.
- **How you would find out if something was registered incorrectly**:
  every run writes a JSON report (`reports/*.json`) linking each source
  file to its outcome and, for registered invoices, the `accounting_id`
  the API assigned — so a registered record can always be traced back to
  its source document and the disputes (if any, non-blocking) it carried.
  The accounting system itself remains the system of record for what's
  actually paid; this pipeline's report is the audit trail for *why* each
  entry got there, not a second copy of the ledger.

## 8. What you would do with another 8 hours

> Up to three items, in priority order, and why that order.

1. **A tolerance band for the reconciliation-mismatch policy** (see
   section 2) — currently always-block, and the real run already hit this
   once in 12 invoices (invoice_09). Getting this wrong in either direction
   (too strict = high flag rate erodes the automation's value; too loose =
   silently paying a wrong amount) is the highest-consequence open
   decision, but it needs the client's policy input and, ideally, flag-rate
   data across a bigger real sample, not another guess.
2. **A minimal review UI.** The file-based queue works but doesn't scale to
   someone doing this daily — a small web screen showing the disputed
   fields, the source document image, and an edit-and-resubmit action would
   directly replace the "open the JSON, open the image, cross-reference by
   hand" workflow the file queue currently requires.
3. [FILL IN — your own third priority]
