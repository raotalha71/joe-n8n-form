import asyncio
import os
from datetime import datetime, timezone

import gspread
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

app = FastAPI()

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


# Delivery toggles so you can run one or both destinations.
ENABLE_N8N = _env_flag("ENABLE_N8N", "true")
ENABLE_GOOGLE_SHEETS = _env_flag("ENABLE_GOOGLE_SHEETS", "false")

N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "").strip()
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
GOOGLE_WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "Sheet1").strip()
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()

# Keep Google Sheets columns in this exact order.
SHEET_FIELD_ORDER = [
    "name",
    "email",
    "clientname",
    "brandname",
    "ccEmail",
    "priority",
    "vertical",
    "campaign",
    "mainUrl",
    "briefInfo",
    "channels",
    "sizes",
    "types",
    "campaignPeriod",
    "estimatedVolume",
]

SHEET_HEADERS = ["submitted_at", *SHEET_FIELD_ORDER]


@app.get("/", response_class=HTMLResponse)
async def get_form(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


def _normalize_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        if not value.strip():
            return []
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(value)]


def _serialize_cell(value):
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _build_payload(raw_data: dict) -> dict:
    payload = {
        "name": raw_data.get("name", ""),
        "email": raw_data.get("email", ""),
        "clientname": raw_data.get("clientname", ""),
        "brandname": raw_data.get("brandname", ""),
        "ccEmail": raw_data.get("ccEmail", ""),
        "priority": raw_data.get("priority", ""),
        "vertical": raw_data.get("vertical", ""),
        "campaign": raw_data.get("campaign", ""),
        "mainUrl": raw_data.get("mainUrl", ""),
        "briefInfo": raw_data.get("briefInfo", ""),
        "channels": _normalize_list(raw_data.get("channels", raw_data.get("channel", []))),
        "sizes": _normalize_list(raw_data.get("sizes", raw_data.get("size", []))),
        "types": _normalize_list(raw_data.get("types", raw_data.get("type", []))),
        "campaignPeriod": raw_data.get("campaignPeriod", ""),
        "estimatedVolume": raw_data.get("estimatedVolume", ""),
    }

    payload["submitted_at"] = datetime.now(timezone.utc).isoformat()
    return payload


async def _send_to_n8n(payload: dict) -> None:
    if not N8N_WEBHOOK_URL:
        raise RuntimeError("N8N_WEBHOOK_URL is empty")

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(N8N_WEBHOOK_URL, json=payload)
        response.raise_for_status()


def _append_to_google_sheet(payload: dict) -> None:
    if not GOOGLE_SERVICE_ACCOUNT_FILE or not GOOGLE_SHEET_ID:
        raise RuntimeError("Google Sheets configuration is incomplete")

    gc = gspread.service_account(filename=GOOGLE_SERVICE_ACCOUNT_FILE)
    worksheet = gc.open_by_key(GOOGLE_SHEET_ID).worksheet(GOOGLE_WORKSHEET_NAME)

    first_row = worksheet.row_values(1)
    if not first_row:
        worksheet.append_row(SHEET_HEADERS, value_input_option="USER_ENTERED")

    row = [payload.get("submitted_at", "")]
    row.extend(_serialize_cell(payload.get(field, "")) for field in SHEET_FIELD_ORDER)
    worksheet.append_row(row, value_input_option="USER_ENTERED")


@app.post("/submit")
async def submit_form(request: Request):
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        raw_data = await request.json()
    else:
        form_data = await request.form()
        raw_data = dict(form_data)

    payload = _build_payload(raw_data)

    missing = [key for key in ("name", "email", "campaign", "mainUrl") if not payload.get(key)]
    if missing:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "Missing required fields", "fields": missing},
        )

    tasks = {}
    if ENABLE_N8N:
        tasks["n8n"] = _send_to_n8n(payload)
    if ENABLE_GOOGLE_SHEETS:
        tasks["google_sheets"] = asyncio.to_thread(_append_to_google_sheet, payload)

    if not tasks:
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": "No destination enabled. Set ENABLE_N8N or ENABLE_GOOGLE_SHEETS.",
            },
        )

    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    destination_status = {}
    all_ok = True
    for name, result in zip(tasks.keys(), results):
        if isinstance(result, Exception):
            all_ok = False
            destination_status[name] = f"failed: {result}"
        else:
            destination_status[name] = "success"

    status_code = 200 if all_ok else 207
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": all_ok,
            "destinations": destination_status,
            "data": payload,
        },
    )
