# jp-invoice-intake

**An LLM pipeline that reads Japanese supplier invoices (native PDFs, scans, photos, handwriting) and registers them in an accounting system — without ever trusting a single model read for anything that can cause a wrong payment.**

![Python](https://img.shields.io/badge/python-3.x-blue) ![LLM](https://img.shields.io/badge/LLM-Claude%20%7C%20OpenAI-8A2BE2) ![Tests](https://img.shields.io/badge/tests-offline%20pytest-brightgreen)

> Built as a time-boxed (8-hour) engineering exercise for a fictional client, "Sample Trading Co.", against a mock accounting API (`accounting_api.py`).

---

## The problem behind the problem

The client asked for speed: *"have AI read invoices and enter them automatically."* But their own example of the pain was a **near-duplicate payment caused by a typo** — a trust problem, not a speed problem.

A pipeline that reads fast but occasionally posts a wrong amount, a wrong supplier, or a duplicate just moves the source of a bad payment from a tired human to a confidently wrong model. So the goal became:

> **Automate as much as can be automated *safely*.** Every field that could cause a wrong or duplicate payment gets an independent second check. Anything that fails is set aside for a human — never guessed.

## Results on the 12 sample invoices

| Outcome | Count | Notes |
|---|---|---|
| ✅ Registered | 8 | Accepted by the accounting API on the **first attempt** — zero `AMOUNT_MISMATCH` / `VALIDATION_ERROR` responses |
| ⏭️ Skipped as duplicate | 1 | A phone photo of an invoice already registered as a PDF — the client's exact failure mode, caught |
| 🧑‍💼 Routed to human review | 3 | All for genuine reasons (below) |

The three review cases were all real problems, not model noise:

- **A misread tax total** — the extracted combined tax (¥9,260) didn't match the sum of the document's own per-rate tax lines (¥8,936). Caught by arithmetic reconciliation before the API ever saw it.
- **A source document that doesn't add up** — printed subtotal + tax = ¥147,496, printed total = ¥147,497. A ¥1 rounding error *in the invoice itself*.
- **An unknown supplier** — not in the partner master. Flagged rather than auto-onboarded, because creating a payee is a bigger decision than flagging one.

**Flag rate went from 83% on the first live run to 25% on the final run** — see [the debugging story](#a-bug-confidence-scores-would-never-have-caught).

## How it works

```mermaid
flowchart LR
    A[invoices/<br/>PDF · JPG · PNG] --> B{Native text<br/>layer?}
    B -- yes --> C[pdfplumber text<br/>+ text LLM]
    B -- no --> D[pymupdf render<br/>+ vision LLM]
    C --> E[verify.py<br/>zero-LLM checks]
    D --> E
    E --> F[Targeted re-query<br/>stronger model,<br/>single field]
    F --> G{All payment-critical<br/>checks pass?}
    G -- yes --> H[Idempotent register<br/>fetch-then-skip]
    G -- no --> I[review/pending/*.json<br/>human edits or rejects]
    I -- --process-review --> E
    H --> J[reports/*.json<br/>audit trail + flag rate]
    I --> J
```

1. **Extract** (`src/extract.py`) — Native-text PDFs go through `pdfplumber` plus a cheap text-LLM call (character-perfect, no OCR). Scans, photos, and handwriting are rendered with `pymupdf` and read by a vision model. Multi-page PDFs are concatenated first.
2. **Verify** (`src/verify.py`) — Independent checks that cost zero LLM calls:
   - Recomputes subtotal / tax / total from the line items using **the exact logic the accounting API uses server-side** (floor-rounded tax per tax code), so mismatches are caught locally, before the network call.
   - Resolves the supplier from **two independent signals** — the model's guess and a `rapidfuzz` match against the partner master. Only agreement resolves it.
   - Cross-checks the printed per-rate taxable base (`消費税10%（対象 …）`) against the line items tagged with that rate, catching mixed 8% / 10% mistakes.
   - Sanity-checks dates (e.g. due before issue).
   - Probes for **duplicates via OCR-confusion variants** of the invoice number (e.g. `0`↔`O`, `1`↔`l`) against already-registered numbers.
3. **Targeted re-query** — On vision-sourced invoices, `invoice_number` is *always* re-read by a stronger model with a narrow single-field prompt; dates and totals only when already disputed. Cross-field arithmetic failures are not re-queried — a second read can't fix a document whose own numbers don't add up.
4. **Route** (`src/pipeline.py`) — Only fields that affect **payment amount or payment identity** block registration (`verify.BLOCKING_CONFIDENCE_FIELDS`). Uncertainty in free-text line descriptions becomes a non-blocking warning. Clean invoices are registered idempotently; disputed ones become editable JSON in `review/pending/` with the original extraction, an editable copy, and the reasons. `--process-review` re-verifies edits, registers what now reconciles, and archives a before/after diff.
5. **Report** — Every run writes `reports/*.json`: registered, registered-with-warnings, skipped-duplicate, routed-to-review (with reasons), and the aggregate flag rate. Every registered record traces back to its source file.

## A bug confidence scores would never have caught

The first live run flagged a supplier dispute on almost every invoice — an **83% flag rate**.

The cause: every invoice is addressed to the same recipient (`株式会社サンプル商事 御中`), printed next to the actual supplier. `pdfplumber`'s linearised text drops the left/right page layout, so the two company names ended up adjacent with no visual cue — and the model **consistently** extracted the recipient as the supplier.

The fix was in the prompt, not the model: name the recipient explicitly, and anchor the supplier to an unambiguous marker — the company printed next to a TEL number and a `登録番号` (tax registration number). Together with a schema fix for lump-sum lines, the final run flags **3 of 12 (25%)**, each for a genuine reason.

The lesson: the model wasn't *uncertain*, it was *consistently wrong in the same way*. A per-field confidence score would have passed every one of those reads. Systematic errors need structural checks.

## Cost and where it breaks first

Measured from real calls (OpenAI path: `gpt-4o-mini` for extraction, `gpt-4o` for re-query):

| Call | Input tokens | Output tokens | Cost |
|---|---|---|---|
| Vision extraction (1 scan) | 38,181 | 240 | ~$0.0059 |
| Text extraction (1 native PDF) | 2,107 | 1,623 | ~$0.0013 |
| Single-field re-query | 1,217 | 9 | ~$0.0031 |

- **≈ $0.008 per invoice** blended (3 native / 9 vision), **≈ $8–13 / month at 1,000 invoices**.
- **The bottleneck is not the LLM — it's the human review queue.** At a 25% flag rate, 1,000 invoices/month means ~250 human reviews; that labour costs an order of magnitude more than the API. Flag rate, not model choice, decides whether this pays for itself — which is why every run reports it.

## Design decisions

| Chose | Over | Why |
|---|---|---|
| Vision LLM + targeted re-query | A second OCR engine (Tesseract / cloud OCR) | Extra setup cost, and weakest exactly where help is needed: Japanese handwriting. Limitation acknowledged: the re-query is same-family, not fully independent. |
| Plain Python control flow | LangGraph / agent framework | The branching (dispute → re-query → review → register) is small; a graph framework adds indirection without value here. |
| Zero tolerance on reconciliation mismatches | A guessed tolerance band | With 12 samples, any band is a guess. Tolerance is a client policy decision, deferred explicitly in code. |
| File-based review queue | A web review UI | Fit the time budget after the verification logic; the UI is the top follow-up. |
| Flag unknown suppliers | Auto-create partner records | Auto-onboarding opens a path to paying an unverified account. |
| Provider abstraction (`src/llm_client.py`) | Hard-coding one vendor | Switching Anthropic ↔ OpenAI is one env var. |

## Quick start

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...        # or set LLM_PROVIDER=openai and OPENAI_API_KEY
# put your invoice PDFs / images in invoices/
./run.sh
```

`run.sh` starts the mock accounting API, health-checks it, and runs the pipeline over `invoices/`. To re-process human-edited review files:

```bash
./run.sh --process-review
```

On Windows, use Git Bash (`bash run.sh`) or run the two steps by hand:

```powershell
python accounting_api.py
python src/pipeline.py --invoices-dir invoices
```

> Sample invoices are not included in this repository. `invoices/` is git-ignored so real documents are never committed by accident.

### Configuration

| Env var | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `openai` |
| `ANTHROPIC_MODEL_PRIMARY` / `_STRONG` | `claude-haiku-4-5-20251001` / `claude-sonnet-5` | Extraction / re-query tiers |
| `OPENAI_MODEL_PRIMARY` / `_STRONG` | `gpt-4o-mini` / `gpt-4o` | Extraction / re-query tiers |
| `ACCOUNTING_API_URL` | `http://localhost:8080` | Mock accounting API |
| `ACCOUNTING_API_KEY` | `demo-key-1234` | Mock API key (demo value) |

### Tests

```bash
python -m pytest tests/ -q
```

Offline — no live LLM calls. Tests use hand-built extraction fixtures with the real printed numbers from two sample invoices (the ¥1-discrepancy document, and one with a lump-sum line plus a negative discount line) and run against a real `accounting_api.py` subprocess. They assert observable behaviour, not prompt contents or call counts.

## Project layout

```
src/
  extract.py      text vs. vision extraction
  verify.py       reconciliation, supplier resolution, duplicate probe, re-query
  pipeline.py     orchestration, routing, reporting, CLI
  register.py     idempotent accounting-API client
  review.py       file-based human review queue
  schema.py       extraction schema
  llm_client.py   Anthropic / OpenAI abstraction
tests/            offline tests + fixtures
DOCS/PRD.md       full design rationale and scope decisions
accounting_api.py mock accounting API (the system of record)
run.sh            single-command start
```

## What I'd do next

1. **A reconciliation tolerance policy**, decided with the client and backed by flag-rate data from a larger real sample — the highest-consequence open decision in either direction.
2. **A minimal review UI** — disputed fields beside the source image, with edit-and-resubmit, replacing "open the JSON, open the image, cross-reference by hand".
3. **Concurrency and rate-limit handling** for month-end catch-up batches.
