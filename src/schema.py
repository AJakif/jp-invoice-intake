"""Structured invoice schema shared by extraction, verification, and registration."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class LineItem:
    description: str
    quantity: Optional[int]
    unit: Optional[str]
    unit_price: Optional[int]
    amount: int
    tax_code: str

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "LineItem":
        return LineItem(
            description=d["description"],
            quantity=d.get("quantity"),
            unit=d.get("unit"),
            unit_price=d.get("unit_price"),
            amount=d["amount"],
            tax_code=d["tax_code"],
        )


@dataclass
class ExtractedInvoice:
    """What the extraction pass produces, before any verification."""

    source_file: str
    supplier_name_printed: str
    partner_code_guess: Optional[str]
    invoice_number: Optional[str]
    issue_date: Optional[str]
    due_date: Optional[str]
    lines: list[LineItem]
    subtotal: Optional[int]
    tax_amount: Optional[int]
    total_amount: Optional[int]
    tax_base_by_code: dict[str, int] = field(default_factory=dict)
    field_confidence: dict[str, str] = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["lines"] = [line.to_dict() for line in self.lines]
        return d

    @staticmethod
    def from_dict(d: dict) -> "ExtractedInvoice":
        return ExtractedInvoice(
            source_file=d["source_file"],
            supplier_name_printed=d.get("supplier_name_printed", ""),
            partner_code_guess=d.get("partner_code_guess"),
            invoice_number=d.get("invoice_number"),
            issue_date=d.get("issue_date"),
            due_date=d.get("due_date"),
            lines=[LineItem.from_dict(x) for x in d.get("lines", [])],
            subtotal=d.get("subtotal"),
            tax_amount=d.get("tax_amount"),
            total_amount=d.get("total_amount"),
            tax_base_by_code=d.get("tax_base_by_code", {}),
            field_confidence=d.get("field_confidence", {}),
            notes=d.get("notes", ""),
        )


# The JSON schema handed to the LLM as its structured-output contract.
EXTRACTION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "supplier_name_printed": {
            "type": "string",
            "description": "Supplier company name exactly as printed on the invoice (the issuer, not the recipient).",
        },
        "partner_code_guess": {
            "type": ["string", "null"],
            "description": "Best-guess partner_code from the supplied partner master, or null if no confident match.",
        },
        "invoice_number": {"type": ["string", "null"]},
        "issue_date": {
            "type": ["string", "null"],
            "description": "ISO format YYYY-MM-DD. Convert from Japanese era/date formats as needed.",
        },
        "due_date": {"type": ["string", "null"], "description": "ISO format YYYY-MM-DD."},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": ["integer", "null"]},
                    "unit": {
                        "type": "string",
                        "description": (
                            "Always required by the accounting system, even when quantity "
                            "and unit_price are null. If no unit is printed (e.g. a "
                            "lump-sum line), use '式' as a placeholder."
                        ),
                    },
                    "unit_price": {"type": ["integer", "null"]},
                    "amount": {
                        "type": "integer",
                        "description": "Line amount in JPY, integer. Negative for discount lines.",
                    },
                    "tax_code": {
                        "type": "string",
                        "description": "T10 for 10% consumption tax, T08 for 8%. Infer from the printed rate.",
                    },
                },
                "required": ["description", "unit", "amount", "tax_code"],
            },
        },
        "subtotal": {"type": ["integer", "null"], "description": "Printed subtotal (小計), integer JPY."},
        "tax_amount": {"type": ["integer", "null"], "description": "Printed tax amount (消費税), integer JPY."},
        "total_amount": {"type": ["integer", "null"], "description": "Printed total (合計 / 御請求金額), integer JPY."},
        "tax_base_by_code": {
            "type": "object",
            "description": (
                "Printed taxable base per tax code, e.g. from a line like "
                "'消費税10%（対象 134,088）' -> {\"T10\": 134088}. Omit codes not printed separately."
            ),
            "additionalProperties": {"type": "integer"},
        },
        "field_confidence": {
            "type": "object",
            "description": (
                "Only include entries for fields you are NOT confident about. "
                "Key is the field name (e.g. 'invoice_number', 'total_amount', 'lines[0].description'), "
                "value is a short reason (e.g. 'handwritten', 'smudged', 'ambiguous era conversion')."
            ),
            "additionalProperties": {"type": "string"},
        },
        "notes": {
            "type": "string",
            "description": "Free text for anything visually unusual: handwriting, stamps, corrections, overlaps.",
        },
    },
    "required": ["supplier_name_printed", "lines"],
}
