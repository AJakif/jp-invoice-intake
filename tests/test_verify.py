import copy

import verify
from fixtures import PARTNERS, invoice_09, invoice_12
from schema import LineItem


def test_invoice_09_rounding_mismatch_is_caught_as_blocking_dispute():
    """The API's own floor-rounding logic would reject invoice_09's printed
    total (147,497 vs recomputed 147,496) -- verify() must catch this
    before ever calling the API, not silently pass it through."""
    result = verify.verify(invoice_09(), PARTNERS)
    total_disputes = [d for d in result.disputes if d.field == "total_amount"]
    assert total_disputes, "expected a total_amount dispute"
    assert total_disputes[0].blocking
    assert total_disputes[0].category == "reconciliation"
    assert result.computed_total_amount == 147496
    assert not result.is_clean


def test_invoice_12_clean_path_with_edge_case_lines():
    """Lump-sum (null qty/unit_price) and negative discount lines must not
    themselves trigger reconciliation disputes when the math is correct."""
    result = verify.verify(invoice_12(), PARTNERS)
    assert result.is_clean
    assert result.resolved_partner_code == "P-1005"
    assert result.computed_subtotal == 540000
    assert result.computed_tax_amount == 54000
    assert result.computed_total_amount == 594000


def test_digit_misread_detection():
    inv = invoice_12()
    inv.total_amount = 595000  # corrupted by +1000
    result = verify.verify(inv, PARTNERS)
    assert not result.is_clean
    assert any(d.field == "total_amount" and d.blocking for d in result.disputes)


def test_partner_mismatch_does_not_silently_resolve():
    inv = invoice_12()
    inv.supplier_name_printed = "謎の会社株式会社"  # garbled / unknown name
    inv.partner_code_guess = None
    result = verify.verify(inv, PARTNERS)
    assert result.resolved_partner_code is None
    assert any(d.field == "partner_code" and d.blocking for d in result.disputes)


def test_partner_llm_and_fuzzy_disagreement_blocks():
    inv = invoice_12()
    inv.partner_code_guess = "P-1001"  # LLM guesses wrong supplier entirely
    result = verify.verify(inv, PARTNERS)
    assert result.resolved_partner_code is None
    assert any(d.field == "partner_code" and d.blocking for d in result.disputes)


def test_blocking_vs_nonblocking_confidence_fields():
    inv = invoice_12()
    inv.field_confidence = {
        "lines[0].description": "faint print",
        "invoice_number": "smudged digit",
    }
    result = verify.verify(inv, PARTNERS)
    desc_dispute = next(d for d in result.disputes if d.field == "lines[0].description")
    number_dispute = next(d for d in result.disputes if d.field == "invoice_number")
    assert not desc_dispute.blocking
    assert number_dispute.blocking
    # a non-blocking-only invoice should still be considered clean overall
    inv2 = invoice_12()
    inv2.field_confidence = {"lines[0].description": "faint print"}
    result2 = verify.verify(inv2, PARTNERS)
    assert result2.is_clean
    assert len(result2.warnings) == 1


def test_tax_base_mismatch_is_nonblocking():
    inv = invoice_12()
    inv.tax_base_by_code = {"T10": 999999}  # printed base disagrees with line sum
    result = verify.verify(inv, PARTNERS)
    tax_base_disputes = [d for d in result.disputes if d.field.startswith("tax_base")]
    assert tax_base_disputes
    assert not tax_base_disputes[0].blocking
    assert result.is_clean  # non-blocking alone doesn't stop registration


def test_due_date_before_issue_date_blocks():
    inv = invoice_12()
    inv.issue_date, inv.due_date = "2026-05-01", "2026-04-01"
    result = verify.verify(inv, PARTNERS)
    assert any(d.field == "due_date" and d.blocking for d in result.disputes)


def test_variant_collision_probe_flags_ocr_confusable_duplicate():
    inv = invoice_12()
    inv.invoice_number = "MIT-2O26-014"  # 'O' instead of '0', a classic OCR confusion
    existing = {"P-1005": ["MIT-2026-014"]}
    result = verify.verify(inv, PARTNERS, existing)
    dup_disputes = [d for d in result.disputes if d.category == "duplicate"]
    assert dup_disputes and dup_disputes[0].blocking


def test_variant_collision_probe_no_false_positive_on_unrelated_number():
    inv = invoice_12()
    existing = {"P-1005": ["MIT-2026-999"]}
    result = verify.verify(inv, PARTNERS, existing)
    assert not [d for d in result.disputes if d.category == "duplicate"]
