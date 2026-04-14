"""
Zakat Calculator - FastAPI backend
Run: python main.py   (opens http://127.0.0.1:8000)

Position data flow:
  1. User clicks the bookmarklet while on their Fidelity Positions page.
  2. Bookmarklet extracts only {ticker, shares} and POSTs to /api/positions.
  3. /api/update then fetches CRI data from FMP for those tickers.
"""

import json
import logging
import math
import os
import uuid
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from calculator import calculate_zakat
from fmp import enrich_positions

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ─── paths ────────────────────────────────────────────────────────────────────
BASE = Path(__file__).parent
STATIC_DIR = BASE / "static"
DATA_DIR = BASE / "data"
DATA_DIR.mkdir(exist_ok=True)

PORTFOLIO_FILE = DATA_DIR / "portfolio.json"
INTENTS_FILE   = DATA_DIR / "intents.json"
SETTINGS_FILE  = DATA_DIR / "settings.json"
CASH_FILE      = DATA_DIR / "cash.json"


def _load(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def _save(path: Path, data):
    path.write_text(json.dumps(data, indent=2, allow_nan=False))


# ─── app ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="Zakat Calculator")

# CORS is required so the bookmarklet (running on fidelity.com) can POST here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # local-only app, no security risk
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# In-memory job tracker  { job_id: { status, message, done, error } }
_jobs: dict = {}
_JOBS_MAX = 50   # keep at most this many completed jobs in memory


# ─── routes ───────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/portfolio")
async def get_portfolio():
    portfolio = _load(PORTFOLIO_FILE, [])
    intents   = _load(INTENTS_FILE,   {})

    results = []
    for pos in portfolio:
        ticker = pos["ticker"]
        intent = intents.get(ticker, "fundamental")
        calc   = calculate_zakat(
            ticker=ticker,
            shares=pos.get("shares", 0),
            market_price=pos.get("price", 0),
            cri_per_share=pos.get("cri_per_share", 0),
            intent=intent,
        )
        results.append({**pos, **calc, "intent": intent})

    equity_zakat       = sum(r["zakat_due"]    for r in results)
    total_market_value = sum(r["market_value"] for r in results)

    # Use per-entry rounded values so the sum matches the cash table display exactly.
    cash_entries     = _load(CASH_FILE, [])
    total_cash_zakat = sum(round(e.get("amount", 0) * 0.025, 2) for e in cash_entries)

    return {
        "positions":          results,
        "total_zakat":        round(equity_zakat + total_cash_zakat, 2),
        "total_market_value": round(total_market_value, 2),
        "total_cash_zakat":   round(total_cash_zakat, 2),
    }


# ─── bookmarklet pushes positions here ───────────────────────────────────────

class RawPosition(BaseModel):
    ticker:  str
    shares:  float
    price:   float = 0.0   # price scraped from Fidelity page (0 = not available)
    account: str   = ""    # account name scraped from Fidelity page


@app.post("/api/positions")
async def receive_positions(positions: list[RawPosition], background_tasks: BackgroundTasks):
    """
    Called by the bookmarklet. Accepts ticker, shares, and optionally price and account.
    Immediately kicks off FMP enrichment in the background.
    """
    if not positions:
        raise HTTPException(status_code=400, detail="No positions received.")

    settings = _load(SETTINGS_FILE, {})
    api_key  = settings.get("fmp_api_key") or os.getenv("FMP_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="FMP API key not configured.")

    raw = [{"ticker": p.ticker.upper(), "shares": p.shares,
            "fidelity_price": p.price,   # stored separately so refresh never confuses it with an old FMP price
            "account": p.account.strip()} for p in positions]
    logger.info("Received %d position(s) from bookmarklet: %s",
                len(raw), [p["ticker"] for p in raw])

    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {"status": "running", "message": "Fetching FMP data…", "done": False, "error": None}
    background_tasks.add_task(_enrich_and_save, job_id, raw, api_key)

    return {"job_id": job_id, "count": len(raw)}


# ─── "Refresh FMP prices" button: re-enriches existing positions ──────────────

@app.post("/api/update")
async def refresh_fmp(background_tasks: BackgroundTasks):
    """Re-runs FMP enrichment on the last-known positions (no scraping needed)."""
    portfolio = _load(PORTFOLIO_FILE, [])
    if not portfolio:
        raise HTTPException(status_code=400, detail="No positions loaded yet. Use the bookmarklet first.")

    settings = _load(SETTINGS_FILE, {})
    api_key  = settings.get("fmp_api_key") or os.getenv("FMP_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="FMP API key not configured.")

    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {"status": "running", "message": "Refreshing FMP prices…", "done": False, "error": None}
    background_tasks.add_task(_enrich_and_save, job_id, portfolio, api_key)
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return _jobs[job_id]


# ─── settings ────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    s   = _load(SETTINGS_FILE, {})
    key = s.get("fmp_api_key", "")
    return {"fmp_api_key": key[:4] + "****" if len(key) > 4 else key, "has_key": bool(key)}


class SettingsBody(BaseModel):
    fmp_api_key: str


@app.post("/api/settings")
async def save_settings(body: SettingsBody):
    current = _load(SETTINGS_FILE, {})
    current["fmp_api_key"] = body.fmp_api_key.strip()
    _save(SETTINGS_FILE, current)
    return {"status": "ok"}


# ─── intent ───────────────────────────────────────────────────────────────────

class IntentBody(BaseModel):
    intent: str  # "fundamental" | "active"


@app.patch("/api/intent/{ticker}")
async def set_intent(ticker: str, body: IntentBody):
    if body.intent not in ("fundamental", "active"):
        raise HTTPException(status_code=400, detail="intent must be 'fundamental' or 'active'")
    intents = _load(INTENTS_FILE, {})
    intents[ticker.upper()] = body.intent
    _save(INTENTS_FILE, intents)
    return {"status": "ok"}


# ─── cash accounts ────────────────────────────────────────────────────────────

class CashEntry(BaseModel):
    name: str
    amount: float


@app.get("/api/cash")
async def get_cash():
    entries = _load(CASH_FILE, [])
    enriched = [
        {**e, "zakat_due": round(e.get("amount", 0) * 0.025, 2)}
        for e in entries
    ]
    total = sum(e.get("amount", 0) for e in entries)
    return {
        "entries":      enriched,
        "total_amount": round(total, 2),
        "total_zakat":  round(total * 0.025, 2),
    }


@app.post("/api/cash")
async def add_cash(entry: CashEntry):
    name = entry.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name must not be empty.")
    if not math.isfinite(entry.amount) or entry.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be a positive finite number.")
    entries = _load(CASH_FILE, [])
    new_entry = {
        "id":     str(uuid.uuid4())[:8],
        "name":   name,
        "amount": round(entry.amount, 2),
    }
    entries.append(new_entry)
    _save(CASH_FILE, entries)
    return new_entry


@app.delete("/api/cash/{entry_id}")
async def delete_cash(entry_id: str):
    entries = _load(CASH_FILE, [])
    new_entries = [e for e in entries if e.get("id") != entry_id]
    if len(new_entries) == len(entries):
        raise HTTPException(status_code=404, detail="Cash entry not found.")
    _save(CASH_FILE, new_entries)
    return {"status": "ok"}


# ─── shared enrichment task ───────────────────────────────────────────────────

async def _enrich_and_save(job_id: str, raw_positions: list, api_key: str):
    def log(msg: str):
        logger.info("[job %s] %s", job_id, msg)
        _jobs[job_id]["message"] = msg

    try:
        # Deduplicate tickers for enrichment (same stock can appear in multiple accounts)
        unique_tickers = list(dict.fromkeys(p["ticker"] for p in raw_positions))
        log(f"Fetching FMP data for {len(unique_tickers)} unique ticker(s)…")

        cri_map = await enrich_positions(unique_tickers, api_key)

        enriched = []
        for pos in raw_positions:
            ticker = pos["ticker"]
            cri    = cri_map.get(ticker, {"cri_per_share": 0, "cri_value": 0})
            # fidelity_price is only ever set by the bookmarklet; it persists across
            # refresh cycles and always wins over FMP's price (which returns 0 on the free plan).
            fidelity_price = pos.get("fidelity_price") or 0
            fmp_price      = cri.get("price") or 0
            merged         = {**pos, **cri}
            merged["price"] = fidelity_price or fmp_price
            enriched.append(merged)

        _save(PORTFOLIO_FILE, enriched)
        _jobs[job_id] = {
            "status":  "done",
            "message": f"Updated {len(enriched)} position(s).",
            "done":    True,
            "error":   None,
        }

    except Exception as exc:
        logger.exception("Enrichment failed")
        _jobs[job_id] = {
            "status":  "error",
            "message": f"Error: {exc}",
            "done":    True,
            "error":   str(exc),
        }

    finally:
        # Prune completed jobs so the dict does not grow without bound.
        if len(_jobs) > _JOBS_MAX:
            done_ids = [k for k, v in list(_jobs.items()) if v.get("done")]
            for k in done_ids[:len(_jobs) - _JOBS_MAX]:
                _jobs.pop(k, None)


# ─── entrypoint ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False, log_level="warning", access_log=False)
