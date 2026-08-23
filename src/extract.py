"""Extraction: turns a source invoice file into an ExtractedInvoice.

Hybrid by source type, per the PRD:
- Native-text PDFs (real embedded text layer, threshold-checked): extract
  text directly with pdfplumber, structure it with a cheap text-only LLM
  call. Multi-page invoices have all pages' text concatenated first.
- Scanned PDFs (image-only) and image files (JPG/PNG): render to an image
  and pass to a vision LLM call.
"""

from __future__ import annotations

import io
from pathlib import Path

import pdfplumber
import pymupdf as fitz

from llm_client import ImagePart, LLMClient
from schema import EXTRACTION_JSON_SCHEMA, ExtractedInvoice, LineItem

# A page whose extracted text is shorter than this is treated as "no real
# text layer" even if pdfplumber found a handful of stray embedded labels.
NATIVE_TEXT_MIN_CHARS = 40

SYSTEM_PROMPT = """You are an expert at reading Japanese business invoices (請求書) \
for accounting data entry. Extract every field precisely as printed. Do not guess \
values you cannot read; instead mark them in field_confidence with a short reason.

Field notes:
- supplier_name_printed is the company ISSUING the invoice (who is asking to be paid) \
  -- NOT the recipient. The recipient is always "株式会社サンプル商事" (Sample Trading \
  Co., the client receiving every one of these invoices) and is marked with "御中" \
  (an honorific meaning "addressed to") right after or near its name -- never extract \
  this company as the supplier. The real supplier is the OTHER company name on the \
  document, the one associated with a street address, a TEL number, and a 登録番号 \
  (tax registration number starting with "T"). When the text was extracted from a \
  PDF, the recipient block and the supplier block were printed side by side but the \
  text layer linearizes them top-to-bottom in an arbitrary order -- do not assume the \
  first company name you see is the supplier; find the one next to 登録番号 and TEL.
- Dates: convert to ISO YYYY-MM-DD. Japanese invoices may use "2026年1月7日" or \
  "2026/01/07" formats.
- tax_code: T10 for 10% consumption tax (消費税10%), T08 for 8% (消費税8%, usually \
  food/beverage per Japan's reduced-rate rules). Infer per line from the printed rate; \
  a single invoice may mix both.
- amount is required for every line; quantity and unit_price may be null for lump-sum \
  or freight/packing lines.
- tax_base_by_code: if the invoice prints a taxable base per rate, e.g. \
  "消費税10%（対象 134,088）", capture {"T10": 134088}.
- Read handwritten annotations, stamps, and corrections; note them in `notes` but do \
  not let them override the printed invoice fields unless they clearly are the \
  correction.
- Use the supplied partner master to guess partner_code by matching supplier_name_printed \
  against name/aliases. If no confident match, leave partner_code_guess null.
"""


def _partner_master_text(partners: list[dict]) -> str:
    lines = ["Partner master (partner_code: name [aliases]):"]
    for p in partners:
        aliases = ", ".join(p.get("aliases", []))
        lines.append(f"- {p['partner_code']}: {p['name']} [{aliases}]")
    return "\n".join(lines)


def _has_native_text(pdf_path: Path) -> tuple[bool, str]:
    with pdfplumber.open(pdf_path) as pdf:
        texts = [page.extract_text() or "" for page in pdf.pages]
    combined = "\n".join(texts).strip()
    return len(combined) >= NATIVE_TEXT_MIN_CHARS, combined


def _render_pdf_page_to_png(pdf_path: Path, page_index: int = 0, zoom: float = 2.0) -> bytes:
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_index]
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat)
        return pix.tobytes("png")
    finally:
        doc.close()


def extract_invoice(source_file: Path, partners: list[dict], llm: LLMClient) -> ExtractedInvoice:
    source_file = Path(source_file)
    partner_text = _partner_master_text(partners)

    if source_file.suffix.lower() == ".pdf":
        has_text, combined_text = _has_native_text(source_file)
        if has_text:
            result = _extract_from_text(combined_text, partner_text, llm)
        else:
            png_bytes = _render_pdf_page_to_png(source_file)
            result = _extract_from_image_bytes(png_bytes, "image/png", partner_text, llm)
    else:
        media_type = "image/png" if source_file.suffix.lower() == ".png" else "image/jpeg"
        result = _extract_from_image_bytes(
            source_file.read_bytes(), media_type, partner_text, llm
        )

    return _to_extracted_invoice(source_file.name, result)


def _extract_from_text(text: str, partner_text: str, llm: LLMClient) -> dict:
    prompt = (
        f"{partner_text}\n\n"
        "Below is the text layer extracted from a native-text PDF invoice "
        "(page breaks are not marked, but multi-page content is concatenated "
        "in order). Extract the structured invoice data.\n\n"
        f"--- INVOICE TEXT ---\n{text}\n--- END ---"
    )
    return llm.structured_call(
        tier="primary",
        system=SYSTEM_PROMPT,
        text=prompt,
        json_schema=EXTRACTION_JSON_SCHEMA,
        schema_name="invoice_extraction",
    )


def _extract_from_image_bytes(
    image_bytes: bytes, media_type: str, partner_text: str, llm: LLMClient
) -> dict:
    import tempfile

    suffix = ".png" if media_type == "image/png" else ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(image_bytes)
        tmp_path = Path(f.name)
    try:
        prompt = (
            f"{partner_text}\n\n"
            "The attached image is a scanned Japanese invoice. Extract the "
            "structured invoice data. Read carefully around any stamps or "
            "handwriting; note anything that obscures a field."
        )
        return llm.structured_call(
            tier="primary",
            system=SYSTEM_PROMPT,
            text=prompt,
            json_schema=EXTRACTION_JSON_SCHEMA,
            schema_name="invoice_extraction",
            images=[ImagePart(path=tmp_path, media_type=media_type)],
        )
    finally:
        tmp_path.unlink(missing_ok=True)


def _clean_nullable(value):
    """Some models emit the literal string 'null'/'none' instead of JSON
    null for optional fields. Normalize those to a real None so equality
    checks downstream (e.g. supplier resolution agreement) aren't fooled."""
    if isinstance(value, str) and value.strip().lower() in ("null", "none", ""):
        return None
    return value


def _to_extracted_invoice(source_name: str, result: dict) -> ExtractedInvoice:
    lines = [
        LineItem(
            description=item.get("description", ""),
            quantity=_clean_nullable(item.get("quantity")),
            unit=_clean_nullable(item.get("unit")) or "式",
            unit_price=_clean_nullable(item.get("unit_price")),
            amount=item.get("amount", 0),
            tax_code=item.get("tax_code", ""),
        )
        for item in result.get("lines", [])
    ]
    return ExtractedInvoice(
        source_file=source_name,
        supplier_name_printed=result.get("supplier_name_printed", ""),
        partner_code_guess=_clean_nullable(result.get("partner_code_guess")),
        invoice_number=_clean_nullable(result.get("invoice_number")),
        issue_date=_clean_nullable(result.get("issue_date")),
        due_date=_clean_nullable(result.get("due_date")),
        lines=lines,
        subtotal=result.get("subtotal"),
        tax_amount=result.get("tax_amount"),
        total_amount=result.get("total_amount"),
        tax_base_by_code=result.get("tax_base_by_code", {}),
        field_confidence=result.get("field_confidence", {}),
        notes=result.get("notes", ""),
    )


def is_vision_sourced(source_file: Path) -> bool:
    """True if this file went (or would go) through the vision path, used
    by the disputed-field re-query step to decide whether a re-render is
    possible."""
    source_file = Path(source_file)
    if source_file.suffix.lower() != ".pdf":
        return True
    has_text, _ = _has_native_text(source_file)
    return not has_text


def render_for_requery(source_file: Path) -> tuple[bytes, str]:
    """Returns (image_bytes, media_type) for a vision-sourced file, for the
    targeted re-query step in verify.py."""
    source_file = Path(source_file)
    if source_file.suffix.lower() == ".pdf":
        return _render_pdf_page_to_png(source_file), "image/png"
    media_type = "image/png" if source_file.suffix.lower() == ".png" else "image/jpeg"
    return source_file.read_bytes(), media_type
