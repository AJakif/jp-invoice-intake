"""Orchestrates the invoice intake pipeline end to end.

    extract -> verify -> targeted re-query -> register (clean/idempotent)
                                            -> review queue (blocking disputes)

Emits a per-run JSON report to reports/ summarizing what registered clean,
what registered with non-blocking warnings, what was skipped as an
already-registered duplicate, and what was routed to human review -- the
direct answer to "how would you find out if something was registered
incorrectly."

Usage:
    python pipeline.py                     # process invoices/*.{pdf,jpg,jpeg,png}
    python pipeline.py --invoices-dir DIR  # process a different directory
    python pipeline.py --process-review    # re-verify and register edited review/pending/ files
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import extract
import register
import review
import verify
from llm_client import LLMClient
from schema import ExtractedInvoice

REPORTS_DIR = Path("reports")
SUPPORTED_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png"}


def build_registration_payload(inv: ExtractedInvoice, result: "verify.VerificationResult", partner_code: str) -> dict:
    return {
        "partner_code": partner_code,
        "invoice_number": inv.invoice_number,
        "issue_date": inv.issue_date,
        "due_date": inv.due_date,
        "currency": "JPY",
        "lines": [
            {
                "description": line.description,
                "quantity": line.quantity,
                "unit": line.unit,
                "unit_price": line.unit_price,
                "amount": line.amount,
                "tax_code": line.tax_code,
            }
            for line in inv.lines
        ],
        # Send the recomputed values, not the printed ones: by the time we
        # get here reconciliation already passed (recomputed == printed), and
        # sending our own recomputation guarantees it matches what the API
        # recomputes on its side too.
        "subtotal": result.computed_subtotal,
        "tax_amount": result.computed_tax_amount,
        "total_amount": result.computed_total_amount,
    }


def process_one(
    source_file: Path,
    partners: list[dict],
    existing_keys: set[tuple[str, str]],
    existing_numbers_by_partner: dict[str, list[str]],
    client: register.AccountingAPIClient,
    llm: LLMClient,
) -> dict:
    entry = {"source_file": source_file.name}

    try:
        inv = extract.extract_invoice(source_file, partners, llm)
    except Exception as exc:  # noqa: BLE001 - a hard extraction failure is itself a reportable outcome
        entry.update(outcome="extraction_failed", error=str(exc))
        return entry

    result = verify.verify(inv, partners, existing_numbers_by_partner)

    # invoice_number is unconditionally re-queried on vision-sourced invoices
    # even when nothing was flagged; other fields are re-queried only when
    # already disputed. See verify.resolve_disputes_via_requery.
    result, requery_log = verify.resolve_disputes_via_requery(inv, result, source_file, llm)

    entry["extraction"] = inv.to_dict()
    entry["resolved_partner_code"] = result.resolved_partner_code
    entry["disputes"] = [d.to_dict() for d in result.disputes]
    entry["requery_log"] = requery_log

    if result.blocking_disputes:
        path = review.write_pending(inv, [d.to_dict() for d in result.disputes], requery_log)
        entry.update(outcome="review", review_path=str(path))
        return entry

    key = (result.resolved_partner_code, inv.invoice_number)
    if key in existing_keys:
        entry.update(outcome="skipped_duplicate", partner_code=result.resolved_partner_code)
        return entry

    payload = build_registration_payload(inv, result, result.resolved_partner_code)
    success, data = client.register_invoice(payload)
    if not success:
        # The API rejected an invoice our own verification thought was
        # clean -- surface the raw error rather than swallowing it, and
        # route to review since it clearly needs a human now.
        path = review.write_pending(
            inv,
            [d.to_dict() for d in result.disputes] + [{"field": "*", "reason": f"API rejected: {data}", "blocking": True, "category": "api_rejection", "details": data}],
            requery_log,
        )
        entry.update(outcome="review", api_error=data, review_path=str(path))
        return entry

    entry.update(
        outcome="registered_warning" if result.warnings else "registered",
        accounting_id=data["accounting_id"],
        partner_code=result.resolved_partner_code,
        warnings=[d.to_dict() for d in result.warnings],
    )
    existing_keys.add(key)
    existing_numbers_by_partner.setdefault(result.resolved_partner_code, []).append(inv.invoice_number)
    return entry


def run_pipeline(invoices_dir: Path) -> dict:
    client = register.AccountingAPIClient()
    client.health()
    partners = client.get_partners()
    existing_keys = register.existing_keys(client)
    existing_numbers_by_partner = register.existing_invoice_numbers_by_partner(client)
    llm = LLMClient()

    files = sorted(p for p in invoices_dir.iterdir() if p.suffix.lower() in SUPPORTED_SUFFIXES)
    results = [
        process_one(f, partners, existing_keys, existing_numbers_by_partner, client, llm) for f in files
    ]
    return _write_report(results)


def process_review(client: register.AccountingAPIClient | None = None) -> dict:
    client = client or register.AccountingAPIClient()
    client.health()
    partners = client.get_partners()
    existing_keys = register.existing_keys(client)
    existing_numbers_by_partner = register.existing_invoice_numbers_by_partner(client)

    results = []
    for path in review.list_pending():
        record = review.load_pending(path)
        inv = ExtractedInvoice.from_dict(record["editable"])
        result = verify.verify(inv, partners, existing_numbers_by_partner)
        diff = review.diff_editable(record)
        entry = {"source_file": inv.source_file, "review_file": str(path), "diff": diff}

        if result.blocking_disputes:
            record["editable"] = inv.to_dict()
            record["disputes"] = [d.to_dict() for d in result.disputes]
            path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
            entry.update(outcome="still_disputed", disputes=[d.to_dict() for d in result.disputes])
            results.append(entry)
            continue

        key = (result.resolved_partner_code, inv.invoice_number)
        if key in existing_keys:
            review.archive(path, record, "skipped_duplicate", diff)
            entry.update(outcome="skipped_duplicate")
            results.append(entry)
            continue

        payload = build_registration_payload(inv, result, result.resolved_partner_code)
        success, data = client.register_invoice(payload)
        if not success:
            record["disputes"] = [{"field": "*", "reason": f"API rejected: {data}", "blocking": True, "category": "api_rejection", "details": data}]
            path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
            entry.update(outcome="still_disputed", api_error=data)
            results.append(entry)
            continue

        review.archive(path, record, "registered", diff, detail={"accounting_id": data["accounting_id"]})
        existing_keys.add(key)
        existing_numbers_by_partner.setdefault(result.resolved_partner_code, []).append(inv.invoice_number)
        entry.update(outcome="registered", accounting_id=data["accounting_id"])
        results.append(entry)

    return _write_report(results, report_name="review_run")


def _write_report(results: list[dict], report_name: str = "run") -> dict:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for r in results:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    total = len(results)
    flagged = counts.get("review", 0) + counts.get("still_disputed", 0)
    report = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "counts": counts,
        "flag_rate": round(flagged / total, 3) if total else 0.0,
        "results": results,
    }
    path = REPORTS_DIR / f"{report_name}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    report["_path"] = str(path)
    return report


def _print_summary(report: dict) -> None:
    print(f"\nProcessed {report['total']} invoice(s). Report: {report['_path']}")
    for outcome, count in sorted(report["counts"].items()):
        print(f"  {outcome}: {count}")
    print(f"  flag rate: {report['flag_rate']:.0%}")
    for r in report["results"]:
        line = f"  - {r['source_file']}: {r['outcome']}"
        if r.get("accounting_id"):
            line += f" ({r['accounting_id']})"
        print(line)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--invoices-dir", default="invoices", type=Path)
    parser.add_argument("--process-review", action="store_true")
    args = parser.parse_args()

    if args.process_review:
        report = process_review()
    else:
        report = run_pipeline(args.invoices_dir)

    _print_summary(report)
    if report["counts"].get("extraction_failed"):
        sys.exit(1)


if __name__ == "__main__":
    main()
