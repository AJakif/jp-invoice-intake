"""Thin client for the mock accounting API (accounting_api.py).

Registration is idempotent by construction: the caller is expected to fetch
existing invoices via list_invoices() and skip anything already present
before calling register_invoice(), rather than relying on catching
DUPLICATE_INVOICE after the fact. All API error codes are surfaced to the
caller, never swallowed.
"""

from __future__ import annotations

import os
from typing import Any

import requests

BASE_URL = os.environ.get("ACCOUNTING_API_URL", "http://localhost:8080")
API_KEY = os.environ.get("ACCOUNTING_API_KEY", "demo-key-1234")


class AccountingAPIClient:
    def __init__(self, base_url: str = BASE_URL, api_key: str = API_KEY):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    def health(self) -> dict[str, Any]:
        response = requests.get(f"{self.base_url}/health", timeout=10)
        response.raise_for_status()
        return response.json()

    def get_partners(self) -> list[dict]:
        response = requests.get(f"{self.base_url}/partners", headers=self.headers, timeout=10)
        response.raise_for_status()
        return response.json()["data"]["partners"]

    def get_tax_codes(self) -> list[dict]:
        response = requests.get(f"{self.base_url}/tax-codes", headers=self.headers, timeout=10)
        response.raise_for_status()
        return response.json()["data"]["tax_codes"]

    def list_invoices(self) -> list[dict]:
        response = requests.get(f"{self.base_url}/invoices", headers=self.headers, timeout=10)
        response.raise_for_status()
        return response.json()["data"]["invoices"]

    def register_invoice(self, payload: dict) -> tuple[bool, dict]:
        """Returns (success, data_or_error). Never raises for a well-formed
        4xx/409/422 API response -- the caller decides how to handle a
        rejection. Raises only on transport failure."""
        response = requests.post(
            f"{self.base_url}/invoices", headers=self.headers, json=payload, timeout=30
        )
        body = response.json()
        if body["success"]:
            return True, body["data"]
        return False, body["error"]

    def delete_all(self) -> int:
        response = requests.delete(f"{self.base_url}/invoices", headers=self.headers, timeout=10)
        response.raise_for_status()
        return response.json()["data"]["removed"]


def existing_keys(client: AccountingAPIClient) -> set[tuple[str, str]]:
    return {(r["partner_code"], r["invoice_number"]) for r in client.list_invoices()}


def existing_invoice_numbers_by_partner(client: AccountingAPIClient) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for r in client.list_invoices():
        result.setdefault(r["partner_code"], []).append(r["invoice_number"])
    return result
