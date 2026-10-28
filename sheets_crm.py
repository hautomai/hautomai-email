from __future__ import annotations

import time

from googleapiclient.discovery import build

from config import config
from gmail_client import GoogleAuth, SHEETS_READ_LIMITER, _retry

COLUMNS = [
    "agency_name",
    "contact_name",
    "contact_title",
    "email",
    "website",
    "status",
    "thread_id",
    "icebreaker",
    "first_sent_at",
    "followup_1_at",
    "reply_at",
    "reply_class",
    "notes",
    "run_id",
    "country",
    "verified_at",
    "icebreaker_client",
    "verified_version",
]

# Raw-tab mode: when `sheets.column_map` is set, the sheet holds a raw lead
# schema (e.g. a scraper export). CRM fields are read from those source
# columns and pipeline-owned fields are written into columns appended *after*
# the existing data (never overwriting the source header).
COLUMN_MAP = config["sheets"].get("column_map") or {}
TRACKING_COLUMNS = config["sheets"].get("tracking_columns") or []
RAW_MODE = bool(COLUMN_MAP)
HEALTH_NOTE_RANGE = config["sheets"].get("health_note_range") or "S1:T1"
SEND_VERIFICATION = {str(v).strip().lower() for v in config["sheets"].get("send_verification", ["good"])}

SHEET_NAME = config["sheets"]["sheet_name"]

_META_CACHE: dict[str, tuple[float, list[str]]] = {}
_META_TTL_SECONDS = 30 * 60


def _col_letter(index: int) -> str:
    letters = ""
    while index >= 0:
        letters = chr(65 + index % 26) + letters
        index = index // 26 - 1
    return letters


class SheetsCrm:
    def __init__(self, spreadsheet_id: str, credentials=None):
        self.spreadsheet_id = spreadsheet_id
        self.credentials = credentials or GoogleAuth.token()
        self._sheets = None

    def _service(self):
        if self._sheets is None:
            self.credentials = GoogleAuth.refresh(self.credentials)
            self._sheets = build("sheets", "v4", credentials=self.credentials, cache_discovery=False)
        return self._sheets

    def _read(self, fn) -> dict:
        SHEETS_READ_LIMITER.wait()
        return _retry(fn)

    def _sheet_titles(self) -> list[str]:
        key = self.spreadsheet_id
        cached = _META_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < _META_TTL_SECONDS:
            return cached[1]
        meta = self._read(
            lambda: self._service().spreadsheets().get(spreadsheetId=self.spreadsheet_id).execute()
        )
        titles = [s["properties"]["title"] for s in meta["sheets"]]
        _META_CACHE[key] = (time.monotonic(), titles)
        return titles

    def ensure_sheet(self) -> None:
        if SHEET_NAME not in self._sheet_titles():
            _retry(
                lambda: self._service()
                .spreadsheets()
                .batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"requests": [{"addSheet": {"properties": {"title": SHEET_NAME}}}]},
                )
                .execute()
            )
            del _META_CACHE[self.spreadsheet_id]
        if RAW_MODE:
            self._ensure_raw_tracking()
            return
        existing = self._read(
            lambda: self._service().spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"{SHEET_NAME}!A1:{_col_letter(len(COLUMNS) - 1)}",
            ).execute()
        )
        values = existing.get("values", [])
        missing: list[dict] = []
        for idx, col in enumerate(COLUMNS):
            if idx >= len(values[0]) or values[0][idx] != col:
                missing.append({"range": f"{SHEET_NAME}!{_col_letter(idx)}1", "values": [[col]]})
        if len(values) == 0 or missing:
            _retry(
                lambda: self._service().spreadsheets().values().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"valueInputOption": "RAW", "data": missing or [{"range": f"{SHEET_NAME}!A1", "values": [COLUMNS]}]},
                ).execute()
            )

    def _sheet_spec(self) -> dict:
        meta = self._read(
            lambda: self._service().spreadsheets().get(spreadsheetId=self.spreadsheet_id).execute()
        )
        for sheet in meta["sheets"]:
            if sheet["properties"]["title"] == SHEET_NAME:
                return sheet["properties"]
        return {}

    def _ensure_raw_tracking(self) -> None:
        """Append pipeline-owned tracking columns after the raw data columns,
        expanding the grid first if the raw schema leaves no room."""
        data = self._read(
            lambda: self._service().spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=f"{SHEET_NAME}!1:1"
            ).execute()
        )
        values = data.get("values", [])
        header = [h.lower() for h in (values[0] if values else [])]
        # reserve 2 spare columns for the health note after the tracking block
        needed = len(header) + len([c for c in TRACKING_COLUMNS if c.lower() not in header]) + 2
        spec = self._sheet_spec()
        if needed > int(spec.get("gridProperties", {}).get("columnCount", 26)):
            _retry(
                lambda: self._service().spreadsheets().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"requests": [{
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": spec.get("sheetId"),
                                "gridProperties": {"columnCount": needed},
                            },
                            "fields": "gridProperties.columnCount",
                        }
                    }]},
                ).execute()
            )
        updates: list[dict] = []
        next_index = len(header)
        for col in TRACKING_COLUMNS:
            if col.lower() in header:
                continue
            updates.append({"range": f"{SHEET_NAME}!{_col_letter(next_index)}1", "values": [[col]]})
            header.append(col)
            next_index += 1
        if updates:
            _retry(
                lambda: self._service().spreadsheets().values().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"valueInputOption": "RAW", "data": updates},
                ).execute()
            )

    def _raw_columns(self) -> dict[str, int]:
        """header-name (lowercased) -> column index for the raw sheet."""
        data = self._read(
            lambda: self._service().spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=f"{SHEET_NAME}!1:1"
            ).execute()
        )
        values = data.get("values", [])
        header = [h.lower() for h in (values[0] if values else [])]
        return {name: i for i, name in enumerate(header)}

    def rows(self) -> list[dict]:
        if RAW_MODE:
            return self._raw_rows()
        data = self._read(
            lambda: self._service().spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"{SHEET_NAME}!A1:{_col_letter(len(COLUMNS) - 1)}",
            ).execute()
        )
        values = data.get("values", [])
        if not values:
            return []
        header = [h.lower() for h in values[0]]
        rows = []
        for idx, row in enumerate(values[1:], start=2):
            record = {"_row": idx}
            for col, value in zip(header, row):
                record[col] = value
            for col in COLUMNS:
                record.setdefault(col, "")
            rows.append(record)
        return rows

    def _raw_rows(self) -> list[dict]:
        data = self._read(
            lambda: self._service().spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id, range=f"{SHEET_NAME}!A1:ZZ"
            ).execute()
        )
        values = data.get("values", [])
        if not values:
            return []
        header = [h.lower() for h in values[0]]
        col_to_idx = {name: i for i, name in enumerate(header)}
        rows = []
        for idx, row in enumerate(values[1:], start=2):
            if not any(str(cell).strip() for cell in row):
                continue
            record = {"_row": idx}
            for crm_field, source in COLUMN_MAP.items():
                i = col_to_idx.get(source.lower())
                if i is not None and i < len(row):
                    record[crm_field] = row[i]
            if "first_name" in col_to_idx and "last_name" in col_to_idx:
                i = col_to_idx["first_name"]
                j = col_to_idx["last_name"]
                first = row[i] if i < len(row) else ""
                last = row[j] if j < len(row) else ""
                record.setdefault("contact_name", f"{first} {last}".strip())
            for col in TRACKING_COLUMNS:
                i = col_to_idx.get(col.lower())
                record[col] = row[i] if i is not None and i < len(row) else ""
            for col in COLUMNS:
                record.setdefault(col, "")
            verification = (record.get("verification") or "").strip().lower()
            if (
                not (record.get("status") or "").strip()
                and (record.get("email") or "").strip()
                and verification in SEND_VERIFICATION
            ):
                record["status"] = "new"
            rows.append(record)
        return rows

    def add_rows(self, records: list[dict]) -> int:
        if not records:
            return 0
        if RAW_MODE:
            return self._raw_add_rows(records)
        values = [[r.get(col, "") for col in COLUMNS] for r in records]
        _retry(
            lambda: self._service().spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id,
                range=f"{SHEET_NAME}!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            ).execute()
        )
        return len(records)

    def _raw_add_rows(self, records: list[dict]) -> int:
        col_to_idx = self._raw_columns()
        next_index = len(col_to_idx)
        tracking_idx: dict[str, int] = {}
        for col in TRACKING_COLUMNS:
            if col.lower() not in col_to_idx:
                tracking_idx[col] = next_index
                next_index += 1
        values = []
        for rec in records:
            width = max(len(col_to_idx), next_index)
            row = [""] * width
            for crm_field, source in COLUMN_MAP.items():
                i = col_to_idx.get(source.lower())
                if i is not None and crm_field in rec:
                    row[i] = rec[crm_field]
            if "first_name" in col_to_idx and "last_name" in col_to_idx and (rec.get("contact_name") or ""):
                parts = str(rec["contact_name"]).split(None, 1)
                first = col_to_idx["first_name"]
                last = col_to_idx["last_name"]
                row[first] = parts[0]
                if len(parts) > 1:
                    row[last] = parts[1]
            for col in TRACKING_COLUMNS:
                i = tracking_idx.get(col)
                if i is None:
                    i = col_to_idx.get(col.lower())
                if i is not None and col in rec:
                    if i < len(row):
                        row[i] = rec[col]
            values.append(row)
        _retry(
            lambda: self._service().spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id,
                range=f"{SHEET_NAME}!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            ).execute()
        )
        return len(records)

    def update_row(self, row_idx: int, updates: dict) -> None:
        data = []
        for col, value in updates.items():
            if col == "_row":
                continue
            if RAW_MODE:
                if col not in TRACKING_COLUMNS:
                    continue
                col_to_idx = self._raw_columns()
                i = col_to_idx.get(col.lower())
                if i is None:
                    continue
                data.append({"range": f"{SHEET_NAME}!{_col_letter(i)}{row_idx}", "values": [[value]]})
                continue
            if col not in COLUMNS:
                continue
            index = COLUMNS.index(col)
            data.append({"range": f"{SHEET_NAME}!{_col_letter(index)}{row_idx}", "values": [[value]]})
        if data:
            _retry(
                lambda: self._service().spreadsheets().values().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"valueInputOption": "RAW", "data": data},
                ).execute()
            )

    def summary(self) -> dict:
        counts = {}
        for row in self.rows():
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return counts

    @staticmethod
    def create_spreadsheet(title: str) -> str:
        credentials = GoogleAuth.refresh(GoogleAuth.token(), persist=False)
        sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        created = _retry(lambda: sheets.spreadsheets().create(body={"properties": {"title": title}}).execute())
        return created["spreadsheetId"]