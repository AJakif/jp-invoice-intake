"""Verification: independently checks an ExtractedInvoice before it is ever
sent to the accounting API, and decides whether disputes block registration.

Design (see DOCS/PRD.md for the full rationale):

- Arithmetic reconciliation mirrors accounting_api.py's own recomputation
  exactly (same floor-rounding, same per-tax-code grouping), so we fail the
  same way the API would, before making the network call.
- Every field that's hard to read gets an independent second signal:
    * amounts -> arithmetic reconciliation
    * supplier -> LLM guess + fuzzy match against the partner master
    * disputed / identity-critical fields -> a targeted model re-query
      (see resolve_disputes below, called from pipeline.py)
  Disagreement between the two signals routes to human review; agreement
  resolves the dispute; neither signal alone is trusted.
- Blocking vs. non-blocking: only fields that affect payment amount or
  payment identity block registration. Everything else uncertain (mainly
  free-text descriptions) is recorded as a non-blocking warning instead.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from rapidfuzz import fuzz

from schema import ExtractedInvoice

TAX_RATES = {"T10": 0.10, "T08": 0.08}

# Fields that guard money movement or payment identity. A dispute on any of
# these blocks registration and routes to human review. This is a single
# named constant, not a config system: the automation/review line is a
# client policy call, deliberately kept to one line to change.
BLOCKING_CONFIDENCE_FIELDS = {
    "invoice_number",
    "issue_date",
    "due_date",
    "total_amount",
    "subtotal",
    "tax_amount",
    "partner_code",
}

# Fields eligible for a targeted second-model re-query when disputed (or,
# for invoice_number, unconditionally) on vision-sourced invoices.
REQUERYABLE_FIELDS = {"invoice_number", "issue_date", "due_date", "total_amount"}

# A plausible invoice date window. A date outside this almost always means a
# misread era conversion or a transposed digit, not a real invoice.
PLAUSIBLE_DATE_MIN = date(2000, 1, 1)
PLAUSIBLE_DATE_MAX_FUTURE = timedelta(days=730)

FUZZY_MATCH_THRESHOLD = 80

# Common OCR/typo confusions used by the duplicate variant-collision probe.
_CONFUSION_PAIRS = [("O", "0"), ("l", "1"), ("I", "1"), ("S", "5"), ("B", "8"), ("Z", "2")]


DISPUTE_CATEGORIES = {
    "reconciliation",  # printed value disagrees with a value computed from other fields
    "confidence",  # extraction self-reported low confidence
    "missing",  # required field wasn't read at all
    "date",  # date sanity check
    "supplier",  # partner resolution disagreement
    "duplicate",  # variant-collision probe
}


@dataclass
class Dispute:
    field: str
    reason: str
    blocking: bool
    details: dict = field(default_factory=dict)
    category: str = "confidence"
    resolved_by_requery: bool = False

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "reason": self.reason,
            "blocking": self.blocking,
            "details": self.details,
            "category": self.category,
            "resolved_by_requery": self.resolved_by_requery,
        }


@dataclass
class VerificationResult:
    resolved_partner_code: str | None
    computed_subtotal: int | None
    computed_tax_amount: int | None
    computed_total_amount: int | None
    disputes: list[Dispute] = field(default_factory=list)

    @property
    def blocking_disputes(self) -> list[Dispute]:
        return [d for d in self.disputes if d.blocking]

    @property
    def warnings(self) -> list[Dispute]:
        return [d for d in self.disputes if not d.blocking]

    @property
    def is_clean(self) -> bool:
        return len(self.blocking_disputes) == 0


def reconcile_amounts(inv: ExtractedInvoice) -> tuple[int, int, int, list[Dispute]]:
    """Recomputes subtotal/tax/total from line items using the accounting
    API's exact logic (floor-rounded tax per tax code) and compares against
    the printed values the extraction read. Returns the recomputed values
    plus any mismatch disputes."""
    disputes: list[Dispute] = []

    computed_subtotal = sum(line.amount for line in inv.lines)

    unknown_codes = sorted({l.tax_code for l in inv.lines if l.tax_code not in TAX_RATES})
    if unknown_codes:
        disputes.append(
            Dispute(
                field="tax_code",
                reason=f"Unknown tax code(s) on line items: {', '.join(unknown_codes)}",
                blocking=True,
                details={"unknown_tax_codes": unknown_codes},
                category="reconciliation",
            )
        )

    subtotal_by_code: dict[str, int] = {}
    for line in inv.lines:
        subtotal_by_code[line.tax_code] = subtotal_by_code.get(line.tax_code, 0) + line.amount
    tax_by_code = {
        code: math.floor(sub * TAX_RATES[code])
        for code, sub in subtotal_by_code.items()
        if code in TAX_RATES
    }
    computed_tax = sum(tax_by_code.values())
    computed_total = computed_subtotal + computed_tax

    if inv.subtotal is None:
        disputes.append(Dispute("subtotal", "Subtotal was not read from the document", True, category="missing"))
    elif inv.subtotal != computed_subtotal:
        disputes.append(
            Dispute(
                "subtotal",
                "Printed subtotal does not match the sum of line amounts",
                True,
                {"printed": inv.subtotal, "computed": computed_subtotal},
                category="reconciliation",
            )
        )

    if inv.tax_amount is None:
        disputes.append(Dispute("tax_amount", "Tax amount was not read from the document", True, category="missing"))
    elif inv.tax_amount != computed_tax:
        disputes.append(
            Dispute(
                "tax_amount",
                "Printed tax amount does not match tax recalculated from the lines",
                True,
                {"printed": inv.tax_amount, "computed": computed_tax, "computed_by_code": tax_by_code},
                category="reconciliation",
            )
        )

    if inv.total_amount is None:
        disputes.append(Dispute("total_amount", "Total amount was not read from the document", True, category="missing"))
    elif inv.total_amount != computed_total:
        disputes.append(
            Dispute(
                "total_amount",
                "Printed total does not match subtotal + recalculated tax",
                True,
                {"printed": inv.total_amount, "computed": computed_total},
                category="reconciliation",
            )
        )

    return computed_subtotal, computed_tax, computed_total, disputes


def check_tax_base(inv: ExtractedInvoice) -> list[Dispute]:
    """Non-blocking: the sum of line amounts assigned to a tax code should
    equal the printed taxable base for that code, if the document printed
    one (e.g. '消費税10%（対象 134,088）'). This is an independent,
    zero-extra-cost check on the model's per-line tax-code assignment,
    catching mixed 8%/10% invoices where a line was tagged wrong."""
    if not inv.tax_base_by_code:
        return []
    subtotal_by_code: dict[str, int] = {}
    for line in inv.lines:
        subtotal_by_code[line.tax_code] = subtotal_by_code.get(line.tax_code, 0) + line.amount

    disputes = []
    for code, printed_base in inv.tax_base_by_code.items():
        computed_base = subtotal_by_code.get(code, 0)
        if computed_base != printed_base:
            disputes.append(
                Dispute(
                    field=f"tax_base[{code}]",
                    reason=(
                        f"Sum of {code} line amounts ({computed_base}) does not match "
                        f"the printed taxable base ({printed_base}) for that rate"
                    ),
                    blocking=False,
                    details={"printed_base": printed_base, "computed_base": computed_base, "tax_code": code},
                    category="reconciliation",
                )
            )
    return disputes


def check_dates(inv: ExtractedInvoice) -> list[Dispute]:
    disputes = []
    issue = _parse_date(inv.issue_date)
    due = _parse_date(inv.due_date)

    if inv.issue_date is None:
        disputes.append(Dispute("issue_date", "Issue date was not read from the document", True, category="missing"))
    elif issue is None:
        disputes.append(Dispute("issue_date", f"Issue date is not a valid ISO date: {inv.issue_date!r}", True, category="date"))

    if inv.due_date is None:
        disputes.append(Dispute("due_date", "Due date was not read from the document", True, category="missing"))
    elif due is None:
        disputes.append(Dispute("due_date", f"Due date is not a valid ISO date: {inv.due_date!r}", True, category="date"))

    if issue and due and due < issue:
        # The accounting API rejects this outright (DUE_DATE_BEFORE_ISSUE_DATE);
        # failing it locally is a guaranteed API rejection either way.
        disputes.append(
            Dispute(
                "due_date",
                "Due date precedes issue date",
                True,
                {"issue_date": inv.issue_date, "due_date": inv.due_date},
                category="date",
            )
        )

    today = date.today()
    for field_name, d in (("issue_date", issue), ("due_date", due)):
        if d is not None and not (PLAUSIBLE_DATE_MIN <= d <= today + PLAUSIBLE_DATE_MAX_FUTURE):
            disputes.append(
                Dispute(
                    field_name,
                    f"{field_name} is outside a plausible invoice date range "
                    "(likely a misread era conversion or transposed digit)",
                    False,
                    {"value": getattr(inv, field_name)},
                    category="date",
                )
            )
    return disputes


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def resolve_partner(inv: ExtractedInvoice, partners: list[dict]) -> tuple[str | None, list[Dispute]]:
    """Dual independent check: the extraction pass's own partner_code_guess
    must agree with an independent fuzzy-string match of the printed name
    against the partner master. Only agreement resolves the supplier."""
    llm_guess = inv.partner_code_guess
    fuzzy_code, fuzzy_score, fuzzy_name = _fuzzy_match_partner(inv.supplier_name_printed, partners)

    if llm_guess and fuzzy_code and llm_guess == fuzzy_code:
        return llm_guess, []

    disputes = [
        Dispute(
            field="partner_code",
            reason="Supplier resolution disagreement: LLM guess and fuzzy-name match did not agree",
            blocking=True,
            details={
                "supplier_name_printed": inv.supplier_name_printed,
                "llm_guess": llm_guess,
                "fuzzy_match": fuzzy_code,
                "fuzzy_match_name": fuzzy_name,
                "fuzzy_score": fuzzy_score,
            },
            category="supplier",
        )
    ]
    return None, disputes


def _fuzzy_match_partner(name: str, partners: list[dict]) -> tuple[str | None, float, str | None]:
    if not name:
        return None, 0.0, None
    best_code, best_score, best_name = None, 0.0, None
    for p in partners:
        candidates = [p["name"], *p.get("aliases", [])]
        score = max(fuzz.token_sort_ratio(name, c) for c in candidates)
        if score > best_score:
            best_code, best_score, best_name = p["partner_code"], score, p["name"]
    if best_score >= FUZZY_MATCH_THRESHOLD:
        return best_code, best_score, best_name
    return None, best_score, best_name


def classify_confidence_disputes(inv: ExtractedInvoice) -> list[Dispute]:
    """Self-reported low-confidence fields from extraction become disputes,
    classified blocking/non-blocking by BLOCKING_CONFIDENCE_FIELDS."""
    disputes = []
    for field_name, reason in inv.field_confidence.items():
        base_field = field_name.split("[")[0]
        blocking = base_field in BLOCKING_CONFIDENCE_FIELDS
        disputes.append(
            Dispute(
                field=field_name,
                reason=f"Extraction flagged low confidence: {reason}",
                blocking=blocking,
                details={},
            )
        )
    return disputes


def variant_collision_probe(
    invoice_number: str | None, partner_code: str | None, existing_numbers: list[str]
) -> Dispute | None:
    """Generates common OCR/typo confusions of invoice_number and checks
    whether any variant collides with an already-registered invoice number
    for the same partner. A hit is a suspected duplicate under a different
    misread key, routed to review. Only catches collisions against invoices
    already registered (see DOCS/PRD.md for the known limitation)."""
    if not invoice_number or not existing_numbers:
        return None
    variants = _generate_variants(invoice_number)
    existing_set = set(existing_numbers)
    if invoice_number in existing_set:
        return None  # handled as an exact duplicate elsewhere (skip-and-idempotent path)
    hits = sorted(variants & existing_set)
    if hits:
        return Dispute(
            field="invoice_number",
            reason="Invoice number is a likely OCR/typo variant of an already-registered invoice number",
            blocking=True,
            details={"partner_code": partner_code, "extracted": invoice_number, "matched_existing": hits},
            category="duplicate",
        )
    return None


def _generate_variants(number: str) -> set[str]:
    variants = {number}
    for a, b in _CONFUSION_PAIRS:
        for src, dst in ((a, b), (b, a)):
            if src in number:
                variants.add(number.replace(src, dst))
    return variants


def verify(
    inv: ExtractedInvoice,
    partners: list[dict],
    existing_numbers_by_partner: dict[str, list[str]] | None = None,
) -> VerificationResult:
    computed_subtotal, computed_tax, computed_total, amount_disputes = reconcile_amounts(inv)
    partner_code, partner_disputes = resolve_partner(inv, partners)
    date_disputes = check_dates(inv)
    tax_base_disputes = check_tax_base(inv)
    confidence_disputes = classify_confidence_disputes(inv)

    disputes = [*amount_disputes, *partner_disputes, *date_disputes, *tax_base_disputes, *confidence_disputes]

    if not inv.invoice_number:
        disputes.append(Dispute("invoice_number", "Invoice number was not read from the document", True, category="missing"))

    if existing_numbers_by_partner and partner_code:
        dup = variant_collision_probe(
            inv.invoice_number, partner_code, existing_numbers_by_partner.get(partner_code, [])
        )
        if dup:
            disputes.append(dup)

    return _finish(partner_code, computed_subtotal, computed_tax, computed_total, disputes)


def _finish(partner_code, computed_subtotal, computed_tax, computed_total, disputes) -> VerificationResult:
    return VerificationResult(
        resolved_partner_code=partner_code,
        computed_subtotal=computed_subtotal,
        computed_tax_amount=computed_tax,
        computed_total_amount=computed_total,
        disputes=disputes,
    )


REQUERY_SYSTEM = (
    "You are re-reading exactly one specific field from a scanned Japanese invoice image, "
    "independently of any prior extraction pass. Read only what is printed on the document. "
    "If the field is illegible or absent, return null rather than guessing."
)

_REQUERY_SCHEMA = {
    "invoice_number": {
        "type": "object",
        "properties": {"value": {"type": ["string", "null"]}},
        "required": ["value"],
    },
    "issue_date": {
        "type": "object",
        "properties": {"value": {"type": ["string", "null"], "description": "ISO YYYY-MM-DD"}},
        "required": ["value"],
    },
    "due_date": {
        "type": "object",
        "properties": {"value": {"type": ["string", "null"], "description": "ISO YYYY-MM-DD"}},
        "required": ["value"],
    },
    "total_amount": {
        "type": "object",
        "properties": {"value": {"type": ["integer", "null"]}},
        "required": ["value"],
    },
}

_REQUERY_PROMPTS = {
    "invoice_number": "What is the invoice number (請求書番号) printed on this invoice?",
    "issue_date": "What is the issue date (発行日) printed on this invoice? Respond in ISO YYYY-MM-DD.",
    "due_date": "What is the payment due date (お支払期日) printed on this invoice? Respond in ISO YYYY-MM-DD.",
    "total_amount": "What is the total amount (合計 / 御請求金額) printed on this invoice, in integer JPY?",
}


def resolve_disputes_via_requery(inv: ExtractedInvoice, result: VerificationResult, source_file, llm) -> tuple[VerificationResult, list[dict]]:
    """Targeted second read for a subset of identity-critical fields on
    vision-sourced invoices. invoice_number is always re-queried
    unconditionally; issue_date/due_date/total_amount are re-queried only
    when already disputed (and only for disputes that aren't a cross-field
    arithmetic mismatch, which a second single-field read can't fix).

    Agreement with the original reading resolves (un-blocks) the dispute.
    Disagreement is recorded on the dispute with both readings and the
    dispute stays blocking.
    """
    import extract as extract_module  # local import: avoids a hard dependency for callers that don't need it

    log: list[dict] = []
    if not extract_module.is_vision_sourced(source_file):
        return result, log

    disputed_fields = {d.field: d for d in result.disputes if d.field in REQUERYABLE_FIELDS and d.category != "reconciliation"}
    fields_to_check = set(disputed_fields) | {"invoice_number"}

    image_bytes, media_type = extract_module.render_for_requery(source_file)
    original_values = {
        "invoice_number": inv.invoice_number,
        "issue_date": inv.issue_date,
        "due_date": inv.due_date,
        "total_amount": inv.total_amount,
    }

    remaining_disputes = list(result.disputes)
    for field_name in fields_to_check:
        requeried = _requery_one_field(field_name, image_bytes, media_type, llm)
        original = original_values[field_name]
        agrees = requeried == original
        log.append({"field": field_name, "original": original, "requery": requeried, "agrees": agrees})

        existing = disputed_fields.get(field_name)
        if agrees:
            if existing is not None:
                existing.blocking = False
                existing.resolved_by_requery = True
        else:
            if existing is not None:
                existing.details["requery_value"] = requeried
                existing.reason += " (confirmed by independent re-query: readings disagree)"
            else:
                # Unconditional invoice_number check found a confident
                # mismatch the original pass never flagged as low-confidence
                # -- exactly the "typo, not a garbled scan" failure mode.
                remaining_disputes.append(
                    Dispute(
                        field=field_name,
                        reason="Independent re-query disagrees with the original extraction for this field",
                        blocking=True,
                        details={"original": original, "requery_value": requeried},
                        category="confidence",
                    )
                )

    return _finish(
        result.resolved_partner_code,
        result.computed_subtotal,
        result.computed_tax_amount,
        result.computed_total_amount,
        remaining_disputes,
    ), log


def _requery_one_field(field_name: str, image_bytes: bytes, media_type: str, llm):
    import tempfile
    from pathlib import Path as _Path

    from llm_client import ImagePart

    suffix = ".png" if media_type == "image/png" else ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(image_bytes)
        tmp_path = _Path(f.name)
    try:
        result = llm.structured_call(
            tier="strong",
            system=REQUERY_SYSTEM,
            text=_REQUERY_PROMPTS[field_name],
            json_schema=_REQUERY_SCHEMA[field_name],
            schema_name=f"requery_{field_name}",
            images=[ImagePart(path=tmp_path, media_type=media_type)],
            max_tokens=256,
        )
        return result.get("value")
    finally:
        tmp_path.unlink(missing_ok=True)
