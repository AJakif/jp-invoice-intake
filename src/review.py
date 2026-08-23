"""File-based human review queue.

Disputed invoices are written as editable JSON under review/pending/. A
human edits the "editable" copy directly to correct fields (the
"original_extraction" copy is left untouched so a before/after diff is
possible), or deletes the file to reject the invoice outright.

--process-review re-verifies each edited file and registers what now
reconciles cleanly. On resolution, a {field: {before, after}} diff is
logged and the file is moved to review/archive/.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from schema import ExtractedInvoice

PENDING_DIR = Path("review/pending")
ARCHIVE_DIR = Path("review/archive")

INSTRUCTIONS = (
    "This invoice could not be registered automatically. Edit the fields under "
    "'editable' to correct them, then rerun with --process-review. "
    "'original_extraction' is left untouched as a reference -- do not edit it. "
    "Delete this file entirely to reject the invoice (it will be skipped on the "
    "next --process-review run)."
)


def _stem_for(source_file: str) -> str:
    return Path(source_file).stem


def pending_path(source_file: str) -> Path:
    return PENDING_DIR / f"{_stem_for(source_file)}.json"


def write_pending(
    inv: ExtractedInvoice, disputes: list[dict], requery_log: list[dict] | None = None
) -> Path:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "instructions": INSTRUCTIONS,
        "source_file": inv.source_file,
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "disputes": disputes,
        "requery_log": requery_log or [],
        "original_extraction": inv.to_dict(),
        "editable": inv.to_dict(),
    }
    path = pending_path(inv.source_file)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def list_pending() -> list[Path]:
    if not PENDING_DIR.exists():
        return []
    return sorted(PENDING_DIR.glob("*.json"))


def load_pending(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def diff_editable(record: dict) -> dict[str, dict]:
    """Top-level {field: {before, after}} diff between original_extraction
    and editable. Lines are compared as a whole (any change to the list
    counts as one 'lines' diff entry) since per-index diffing of line items
    adds complexity a human reviewer doesn't need -- they edited the file
    and know what they changed."""
    original = record["original_extraction"]
    edited = record["editable"]
    diff = {}
    for key in edited:
        if edited.get(key) != original.get(key):
            diff[key] = {"before": original.get(key), "after": edited.get(key)}
    return diff


def archive(path: Path, record: dict, outcome: str, diff: dict, detail: dict | None = None) -> Path:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        **record,
        "outcome": outcome,
        "diff": diff,
        "detail": detail or {},
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }
    archive_path = ARCHIVE_DIR / f"{path.stem}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
    archive_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    path.unlink()
    return archive_path
