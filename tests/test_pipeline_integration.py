import json

import pipeline
import register
import review
import verify
from fixtures import PARTNERS, invoice_12


def test_clean_registration_against_real_api(clean_api):
    inv = invoice_12()
    client = register.AccountingAPIClient(base_url=clean_api)
    result = verify.verify(inv, PARTNERS)
    assert result.is_clean

    payload = pipeline.build_registration_payload(inv, result, result.resolved_partner_code)
    success, data = client.register_invoice(payload)
    assert success, data
    assert data["partner_code"] == "P-1005"
    assert data["total_amount"] == 594000


def test_idempotent_rerun_skips_via_get_first_check(clean_api):
    inv = invoice_12()
    client = register.AccountingAPIClient(base_url=clean_api)
    result = verify.verify(inv, PARTNERS)
    payload = pipeline.build_registration_payload(inv, result, result.resolved_partner_code)

    success, _ = client.register_invoice(payload)
    assert success

    # Second pass: the pipeline's idempotency check (fetch existing keys,
    # skip before calling POST) should catch this without ever hitting
    # DUPLICATE_INVOICE.
    existing = register.existing_keys(client)
    key = (result.resolved_partner_code, inv.invoice_number)
    assert key in existing


def test_review_queue_round_trip(clean_api, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = register.AccountingAPIClient(base_url=clean_api)

    inv = invoice_12()
    inv.invoice_number = None  # force a blocking dispute so it queues for review
    result = verify.verify(inv, PARTNERS)
    assert not result.is_clean

    path = review.write_pending(inv, [d.to_dict() for d in result.disputes])
    assert path.exists()

    # Simulate a human editing the pending file to fix the missing field.
    record = review.load_pending(path)
    record["editable"]["invoice_number"] = "MIT-2026-014"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    report = pipeline.process_review(client=client)
    assert report["counts"].get("registered") == 1

    archived = list(review.ARCHIVE_DIR.glob("*.json"))
    assert len(archived) == 1
    archived_record = json.loads(archived[0].read_text(encoding="utf-8"))
    assert archived_record["diff"]["invoice_number"] == {"before": None, "after": "MIT-2026-014"}
    assert not path.exists()  # moved out of pending
