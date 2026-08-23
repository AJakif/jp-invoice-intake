"""Hand-built ExtractedInvoice fixtures standing in for extraction output.

These use the real printed numbers from the sample invoices so the
reconciliation tests exercise genuine document data, not synthetic
round-trip-safe numbers. The LLM call itself is never exercised here --
these fixtures are what extract.py would have produced.
"""

from schema import ExtractedInvoice, LineItem

PARTNERS = [
    {"partner_code": "P-1001", "name": "株式会社山田製作所", "aliases": ["ヤマダ製作所", "山田製作所"], "registration_no": "T1010001000101"},
    {"partner_code": "P-1002", "name": "有限会社佐藤商店", "aliases": ["佐藤商店"], "registration_no": "T2020002000202"},
    {"partner_code": "P-1003", "name": "東京フーズ株式会社", "aliases": ["東京フーズ"], "registration_no": "T3030003000303"},
    {"partner_code": "P-1004", "name": "大阪機械工業株式会社", "aliases": ["大阪機械", "大阪機械工業"], "registration_no": "T4040004000404"},
    {"partner_code": "P-1005", "name": "みらいITソリューションズ株式会社", "aliases": ["みらいIT", "みらいITソリューションズ"], "registration_no": "T5050005000505"},
]


def invoice_09() -> ExtractedInvoice:
    """OSK-26-0128 / 大阪機械工業株式会社. The document's own printed total
    (147,497) doesn't reconcile with subtotal + tax (134,088 + 13,408 =
    147,496) -- a 1-yen rounding artifact in the source document itself."""
    return ExtractedInvoice(
        source_file="invoice_09.pdf",
        supplier_name_printed="大阪機械工業株式会社",
        partner_code_guess="P-1004",
        invoice_number="OSK-26-0128",
        issue_date="2026-01-28",
        due_date="2026-02-20",
        lines=[
            LineItem("シャフト加工", 37, "個", 2733, 101121, "T10"),
            LineItem("熱処理", 37, "個", 891, 32967, "T10"),
        ],
        subtotal=134088,
        tax_amount=13408,
        total_amount=147497,  # printed; correct recompute is 147496
        tax_base_by_code={"T10": 134088},
    )


def invoice_12() -> ExtractedInvoice:
    """MIT-2026-014 / みらいITソリューションズ株式会社. Lump-sum line (null
    quantity/unit_price) and a negative discount line; clean reconciliation."""
    return ExtractedInvoice(
        source_file="invoice_12.jpg",
        supplier_name_printed="みらいITソリューションズ株式会社",
        partner_code_guess="P-1005",
        invoice_number="MIT-2026-014",
        issue_date="2026-02-10",
        due_date="2026-03-31",
        lines=[
            LineItem("業務システム改修", None, "式", None, 450000, "T10"),
            LineItem("追加ライセンス", 5, "本", 24000, 120000, "T10"),
            LineItem("値引き", None, "式", None, -30000, "T10"),
        ],
        subtotal=540000,
        tax_amount=54000,
        total_amount=594000,
        tax_base_by_code={"T10": 540000},
    )
