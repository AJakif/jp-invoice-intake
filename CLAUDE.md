# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A take-home assignment (`TAKE_HOME.md`) to automate invoice data entry for a
Japanese company: read supplier invoices (native-text PDFs, scanned PDFs,
scanned images, some with handwriting), turn them into structured data,
verify what was read, and register clean results into a fixed mock
accounting API. `DOCS/PRD.md` documents the full design rationale — read it
before making architectural changes, it explains *why* each verification
check exists, not just what it does.

`accounting_api.py` is a frozen spec (see TAKE_HOME.md section 8) — its
behavior, error codes, and HTTP statuses must never be changed. All other
code treats it as an external system to integrate with, not something to
edit around.

## Commands

```bash
# Single-command start: mock accounting API + full pipeline against invoices/
./run.sh
./run.sh --process-review        # re-verify and register edited review/pending/ files

# Manual two-step equivalent (e.g. on native PowerShell)
python accounting_api.py
python src/pipeline.py --invoices-dir invoices
python src/pipeline.py --process-review

# Tests (offline, no live LLM calls; spins up a real accounting_api.py subprocess)
python -m pytest tests/ -q
```

Requires an LLM key in `.env` at the repo root (gitignored) — `ANTHROPIC_API_KEY`
or `OPENAI_API_KEY` with `LLM_PROVIDER` set accordingly. See `README.md` for
the full env var table.

## Architecture

Pipeline: `extract.py` → `verify.py` → `verify.resolve_disputes_via_requery`
→ `register.py` (clean) or `review.py` (blocking disputes), orchestrated by
`pipeline.py`.

- **`src/schema.py`** — `ExtractedInvoice`/`LineItem` dataclasses plus
  `EXTRACTION_JSON_SCHEMA`, the JSON-schema contract handed to the LLM as a
  forced tool call. `unit` is required non-null on every line even when
  `quantity`/`unit_price` are null — the accounting API rejects a null
  `unit` (`VALIDATION_ERROR`), even for lump-sum lines.
- **`src/llm_client.py`** — provider-agnostic `LLMClient`. Dispatches to
  Anthropic or OpenAI by `LLM_PROVIDER`, each with a `primary` (extraction)
  and `strong` (re-query) model tier. Structured output is forced via
  Anthropic tool-use / OpenAI JSON mode so extraction always parses.
- **`src/extract.py`** — hybrid by source type: native-text PDFs (real
  embedded text layer, threshold-checked, multi-page text concatenated) go
  through `pdfplumber` + a text-only LLM call; scanned PDFs/images render via
  `pymupdf` and go through a vision LLM call. `_clean_nullable()` normalizes
  the literal string `"null"` (which models sometimes emit instead of JSON
  null) back to `None` — this matters because equality checks in
  `verify.resolve_partner` depend on it.
- **`src/verify.py`** — the core policy module. Recomputes
  subtotal/tax/total from line items using the *exact* logic
  `accounting_api.py` uses server-side (floor-rounded tax per tax code), so
  mismatches are caught before the network call. Resolves supplier via two
  independent signals (LLM guess + `rapidfuzz` fuzzy match) that must agree.
  `BLOCKING_CONFIDENCE_FIELDS` is the single-constant automation/review
  boundary: only fields affecting payment amount or identity block
  registration (see PRD "Blocking vs. non-blocking checks"). Every
  `Dispute` carries a `category` (`reconciliation`, `missing`, `confidence`,
  `date`, `supplier`, `duplicate`) — `resolve_disputes_via_requery` uses
  this to decide what's eligible for a targeted second read (cross-field
  arithmetic disputes are never re-queried; a second read of one field can't
  fix numbers that don't add up on their own).
- **`src/register.py`** — thin accounting API client. Registration is
  idempotent by fetching existing `(partner_code, invoice_number)` pairs
  first and skipping matches, not by catching `DUPLICATE_INVOICE` after the
  fact.
- **`src/review.py`** — file-based human review queue (`review/pending/`).
  Each queued file holds the untouched original extraction alongside an
  editable copy; a human edits the file or deletes it to reject. Resolution
  produces a `{field: {before, after}}` diff, archived to `review/archive/`.
- **`src/pipeline.py`** — orchestration + per-run JSON report (`reports/`)
  summarizing registered/warned/skipped-duplicate/reviewed counts and flag
  rate.

## Known gotchas hit during development

- **Extraction prompt must explicitly disambiguate recipient vs. supplier.**
  Every invoice's recipient is the same company ("株式会社サンプル商事",
  marked with the honorific "御中"), printed near the supplier's name.
  Linearized PDF text loses the original left/right layout, and models will
  confuse the two without an explicit instruction pointing at 登録番号/TEL
  as the supplier's disambiguator (see the `supplier_name_printed` note in
  `extract.py`'s `SYSTEM_PROMPT`). Without this, supplier resolution fails
  on most invoices, not a handful.
- **Windows console can't print Japanese text** (`cp1252` codec). Never
  `print()` extracted Japanese text directly in a Windows shell; write to a
  UTF-8 file or wrap stdout (`io.TextIOWrapper(sys.stdout.buffer,
  encoding="utf-8")`) instead.
- **`python3` may exist on PATH but fail to execute** (Windows Store alias
  shim). `run.sh` checks `python3 --version` actually succeeds, not just
  `command -v`, before falling back to `python`.
- pytest's default temp dir can hit `PermissionError` in this environment;
  `pytest.ini` sets `--basetemp=.pytest_tmp` to avoid it.

## Remaining before this satisfies the take-home submission requirements

`SUBMISSION.md` (the take-home's required write-up: scoping decisions,
questions-and-assumptions, cost/risk analysis, etc.) has not been written —
its content is the author's own reasoning and priorities, not something to
generate on their behalf. A demo video/screenshots (submission requirement
3) are also outstanding.
