"""
One-off script — writes the correct headers into an existing Google Sheet.

Usage:
  1. Create a blank Google Sheet in your Drive
  2. Share it with the service account email (Editor access)
  3. Copy the Sheet ID from the URL and paste it into .env as GOOGLE_SHEET_ID=...
  4. Run: docker compose run --rm --no-deps app python create_sheet.py
"""

import os
import sys

import gspread
from google.oauth2.service_account import Credentials

CREDENTIALS_FILE = "google_credentials.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = [
    "full_name",
    "email",
    "company_name",
    "job_title",
    "company_size",
    "budget_range",
    "message",
    "lead_score",
    "priority_tier",
    "intent_summary",
    "suggested_opener",
    "red_flags",
    "manual_review",
    "prior_context_used",
]


def main():
    """Opens the existing sheet by ID, writes bold headers into row 1."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if not sheet_id:
        print("Error: GOOGLE_SHEET_ID is not set in your .env file.")
        print("Create a blank Google Sheet, copy the ID from the URL, and add it to .env.")
        sys.exit(1)

    credentials   = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    sheets_client = gspread.authorize(credentials)

    spreadsheet = sheets_client.open_by_key(sheet_id)
    sheet        = spreadsheet.sheet1

    sheet.clear()
    sheet.append_row(HEADERS, value_input_option="USER_ENTERED")
    sheet.format("A1:N1", {"textFormat": {"bold": True}})

    print()
    print("Headers written successfully.")
    print(f"  Sheet ID : {sheet_id}")
    print(f"  URL      : https://docs.google.com/spreadsheets/d/{sheet_id}/edit")
    print(f"  Columns  : {', '.join(HEADERS)}")
    print()


if __name__ == "__main__":
    main()
