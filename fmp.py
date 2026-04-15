"""
FMP API client - uses /stable/ endpoints (required for this API key tier).
SEC EDGAR is used as a free fallback for balance-sheet data when FMP returns 402.
Yahoo Finance quoteSummary is used as a third fallback for balance sheets,
shares outstanding, and ETF holdings.

Call budget per update run (N tickers):
  N FMP quote calls                         (price, always fresh)
  0–N FMP shares-float calls                (cached 7 days)
  0–N FMP balance-sheet calls               (cached 90 days)
  0–N SEC EDGAR company-concept calls x4    (only for tickers FMP cannot serve)
  0–N Yahoo Finance quoteSummary calls      (fallback for EDGAR misses and ETF holdings)
"""

import json
import logging
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

FMP_BASE   = "https://financialmodelingprep.com/stable"
CACHE_FILE = Path(__file__).parent / "data" / "fmp_cache.json"

BS_TTL_DAYS           = 90   # balance sheets are quarterly
BS_MISS_TTL_DAYS      = 1    # retry failed/empty balance-sheet lookups after 1 day
FLOAT_TTL_DAYS        = 7    # shares-float changes slowly
PRICE_TTL_DAYS        = 1    # cache prices for 1 day (fallback when FMP is rate-limited)
ETF_HOLDINGS_TTL_DAYS = 7    # ETF holdings rebalance infrequently
ETF_MISS_TTL_DAYS     = 1    # retry failed ETF holdings lookups after 1 day
ETF_TOP_N             = 10   # enrich top-N holdings for CRI estimation

# Yahoo Finance free quoteSummary API (no key required)
YAHOO_BASE     = "https://query1.finance.yahoo.com/v10/finance/quoteSummary"
YAHOO_HEADERS  = {"User-Agent": "Mozilla/5.0 (compatible; ZakatCalculator/1.0)"}
YAHOO_TTL_DAYS = 1

# EDGAR N-PORT: free quarterly ETF holdings filings from the SEC
EDGAR_EFTS_SEARCH  = "https://efts.sec.gov/LATEST/search-index"
EDGAR_NPORT_CACHE  = Path(__file__).parent / "data" / "edgar_nport.json"
EDGAR_NPORT_TTL    = 30   # N-PORT is quarterly; cache 30 days
EDGAR_NAME_CACHE   = Path(__file__).parent / "data" / "edgar_name_ticker.json"
EDGAR_NAME_TTL     = 90

# Maps ETF ticker to (trust_cik, series_name_phrase) for direct EDGAR N-PORT lookup.
# Using the trust CIK avoids false positives from EDGAR full-text search (which returns
# any filing that mentions the ETF name, not just the ETF's own N-PORT).
_ETF_EDGAR_INFO: dict = {
    # SELECT SECTOR SPDR TRUST (CIK 1064641) - 11 series
    "XLK":  ("1064641", "Technology Select Sector"),
    "XLF":  ("1064641", "Financial Select Sector"),
    "XLE":  ("1064641", "Energy Select Sector"),
    "XLV":  ("1064641", "Health Care Select Sector"),
    "XLI":  ("1064641", "Industrial Select Sector"),
    "XLP":  ("1064641", "Consumer Staples Select Sector"),
    "XLU":  ("1064641", "Utilities Select Sector"),
    "XLY":  ("1064641", "Consumer Discretionary Select Sector"),
    "XLB":  ("1064641", "Materials Select Sector"),
    "XLC":  ("1064641", "Communication Services Select Sector"),
    "XLRE": ("1064641", "Real Estate Select Sector"),
    # Add more trusts/funds here as needed:
    # "SPY":  ("1222333", "SPDR S&P 500"),
}

# Hardcoded set of common ETF tickers. Used so we only flag real ETFs when
# FMP's profile endpoint (which carries the authoritative isEtf flag) is not
# available on the current API plan.
KNOWN_ETFS = {
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "VEA", "VWO", "BND", "AGG",
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLU", "XLY", "XLB", "XLC", "XLRE",
    "IVV", "VTV", "VUG", "VXUS", "VIG", "VYM", "VGT", "VHT", "VFH", "VIS", "VCR",
    "SCHD", "SCHX", "SCHB", "SCHG", "SCHV", "SCHA", "SCHF", "SCHE",
    "ARKK", "ARKQ", "ARKG", "ARKW", "ARKF", "ARKX",
    "EFA", "EEM", "VNQ", "TLT", "IEF", "SHY", "GLD", "SLV", "USO", "LQD", "HYG",
    "SMH", "KBE", "KRE", "SOXX", "XBI", "IBB", "GDX", "GDXJ",
    "TQQQ", "SQQQ", "UPRO", "SPXU", "SSO", "SDS", "UVXY", "VIXY",
    "FXI", "EWJ", "EWZ", "INDA", "MCHI", "ACWI", "ACWX",
    "BIL", "SHV", "VCIT", "VCSH", "MUB", "TIP",
}

# SEC EDGAR: completely free, covers all US-listed companies
EDGAR_BASE       = "https://data.sec.gov/api/xbrl/companyconcept"
EDGAR_TICKERS    = "https://www.sec.gov/files/company_tickers.json"
EDGAR_CIK_CACHE  = Path(__file__).parent / "data" / "edgar_cik.json"
EDGAR_USER_AGENT = "ZakatCalculator zakat@local.app"
EDGAR_TTL_DAYS   = 90


# ─── cache ────────────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return {"balance_sheets": {}, "shares_float": {}, "prices": {}, "etf_holdings": {}}


def _save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_stale(fetched_at: Optional[str], ttl_days: int) -> bool:
    if not fetched_at:
        return True
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)
        return age > timedelta(days=ttl_days)
    except Exception:
        return True


# ─── public ───────────────────────────────────────────────────────────────────

async def enrich_positions(tickers: list, api_key: str) -> dict:
    """
    Returns { ticker: { cri_per_share, price, shares_outstanding, ... } }

    Calls made per ticker:
      1 quote (always fresh: price changes daily)
      1 shares-float (if cache stale, every 7 days)
      1 balance-sheet (if cache stale, every 90 days)
    """
    cache = _load_cache()
    now   = _now_iso()
    cache.setdefault("balance_sheets", {})
    cache.setdefault("shares_float", {})
    cache.setdefault("prices", {})
    cache.setdefault("etf_holdings", {})

    async with httpx.AsyncClient(timeout=30.0) as client:

        # ── quotes (always fresh) ─────────────────────────────────────────────
        # Try /stable/quote first; fall back to /stable/profile for tickers
        # that return 402 on the quote endpoint (subscription tier limitation).
        quotes = {}
        for ticker in tickers:
            q = await _get(client, f"{FMP_BASE}/quote", {"symbol": ticker, "apikey": api_key})
            item = (q or [None])[0] if isinstance(q, list) else None
            if item:
                quotes[ticker] = item

        # Profile fallback for any ticker that didn't get a price from /quote
        missing_price = [t for t in tickers if not quotes.get(t, {}).get("price")]
        for ticker in missing_price:
            p = await _get(client, f"{FMP_BASE}/profile", {"symbol": ticker, "apikey": api_key})
            item = (p or [None])[0] if isinstance(p, list) else None
            if item and item.get("price"):
                # Normalise to the same shape _compute_cri expects
                quotes[ticker] = {"price": item["price"]}

        # ── persist any fresh prices; fall back to cached price if FMP is rate-limited ──
        for ticker in tickers:
            live_price = quotes.get(ticker, {}).get("price")
            if live_price:
                # Got a fresh price: update the price cache
                cache["prices"][ticker] = {"price": live_price, "fetched_at": now}
            else:
                # FMP returned nothing useful (rate-limited / plan restriction).
                # Use cached price if available and not too stale.
                cached_price_entry = cache["prices"].get(ticker, {})
                if not _is_stale(cached_price_entry.get("fetched_at"), PRICE_TTL_DAYS):
                    cached_price = cached_price_entry.get("price")
                    if cached_price:
                        logger.info("Using cached price for %s: %s", ticker, cached_price)
                        quotes[ticker] = {"price": cached_price}

        # ── shares-float (cached 7 days) ──────────────────────────────────────
        stale_float = [t for t in tickers
                       if _is_stale(cache["shares_float"].get(t, {}).get("fetched_at"), FLOAT_TTL_DAYS)]
        for ticker in stale_float:
            data = await _get(client, f"{FMP_BASE}/shares-float", {"symbol": ticker, "apikey": api_key})
            item = (data or [None])[0] if isinstance(data, list) else None
            cache["shares_float"][ticker] = {"data": item or {}, "fetched_at": now}

        # ── balance sheets (cached 90 days) ───────────────────────────────────
        def _bs_stale(ticker):
            entry = cache["balance_sheets"].get(ticker, {})
            ttl = BS_TTL_DAYS if entry.get("data") else BS_MISS_TTL_DAYS
            return _is_stale(entry.get("fetched_at"), ttl)

        stale_bs = [t for t in tickers if _bs_stale(t)]
        if stale_bs:
            logger.info("Fetching balance sheets for %d ticker(s): %s", len(stale_bs), stale_bs)
        for ticker in stale_bs:
            data = await _get(client, f"{FMP_BASE}/balance-sheet-statement",
                              {"symbol": ticker, "limit": 1, "apikey": api_key})
            item = (data or [None])[0] if isinstance(data, list) else None
            cache["balance_sheets"][ticker] = {"data": item or {}, "fetched_at": now}

        # ── SEC EDGAR fallback for tickers FMP couldn't serve ─────────────────
        # Any ticker whose cached balance sheet is still empty gets a free
        # EDGAR lookup using the company-concept API (no auth required).
        no_bs = [t for t in tickers if not cache["balance_sheets"].get(t, {}).get("data")]
        if no_bs:
            logger.info("Trying SEC EDGAR for %d ticker(s) with no FMP balance sheet: %s", len(no_bs), no_bs)
            cik_map = await _edgar_cik_map(client)
            for ticker in no_bs:
                cik = cik_map.get(ticker.upper())
                if not cik:
                    continue
                edgar_bs = await _edgar_balance_sheet(cik, ticker, client)
                if edgar_bs:
                    logger.info("Got EDGAR balance sheet for %s", ticker)
                    cache["balance_sheets"][ticker] = {"data": edgar_bs, "fetched_at": now}

        # ── Yahoo Finance fallback for balance sheets EDGAR could not serve ──────
        # Covers international stocks (e.g. CCJ) and any other EDGAR misses.
        no_bs_yahoo = [t for t in tickers if not cache["balance_sheets"].get(t, {}).get("data")]
        if no_bs_yahoo:
            logger.info("Trying Yahoo Finance for %d ticker(s) with no balance sheet: %s",
                        len(no_bs_yahoo), no_bs_yahoo)
            for ticker in no_bs_yahoo:
                ybs = await _yahoo_balance_sheet(ticker, client)
                if ybs:
                    logger.info("Got Yahoo Finance balance sheet for %s", ticker)
                    cache["balance_sheets"][ticker] = {"data": ybs, "fetched_at": now}

        # ── Yahoo Finance shares supplement for tickers with BS but no shares ───
        # Happens when EDGAR has cash/receivables but its CSO concept returned 0.
        missing_shares = [
            t for t in tickers
            if cache["balance_sheets"].get(t, {}).get("data")
            and cache["balance_sheets"][t]["data"].get("commonStockSharesOutstanding") is None
            and not (cache["shares_float"].get(t, {}).get("data") or {}).get("outstandingShares")
        ]
        if missing_shares:
            logger.info("Fetching shares for %d ticker(s) with missing share count: %s",
                        len(missing_shares), missing_shares)
            # Build reverse cik map for lookup
            cik_map = {t: cik for t, cik in (await _edgar_cik_map(client)).items()}
            for ticker in missing_shares:
                shares = await _yahoo_shares(ticker, client)
                if not shares:
                    # Yahoo unavailable; parse R1.htm from the most recent EDGAR filing
                    cik = cik_map.get(ticker.upper())
                    if cik:
                        shares = await _edgar_shares_from_filing(cik, client)
                if shares:
                    logger.info("Got %d shares for %s", shares, ticker)
                    cache["balance_sheets"][ticker]["data"]["commonStockSharesOutstanding"] = shares

        # ── ETF CRI estimation for tickers still without balance sheet ─────────
        # ETFs have no meaningful balance sheet of their own.  Instead, fetch the
        # ETF's top holdings, enrich those underlying stocks, and compute a
        # weighted-average CRI ratio scaled by the ETF price.  Any BS-less
        # ticker is tried here; _compute_etf_cri returns None when there are no
        # holdings (which naturally excludes private companies).
        no_bs_final = [t for t in tickers if not cache["balance_sheets"].get(t, {}).get("data")]
        etf_cri_map: dict = {}
        if no_bs_final:
            for ticker in no_bs_final:
                etf_cri = await _compute_etf_cri(ticker, cache, now, client, api_key)
                if etf_cri is not None:
                    etf_cri_map[ticker] = etf_cri
                    logger.info("ETF CRI estimated for %s: %.6f per share", ticker, etf_cri)

    _save_cache(cache)

    results = {}
    for ticker in tickers:
        q   = quotes.get(ticker, {})
        sf  = cache["shares_float"][ticker]["data"] if ticker in cache["shares_float"] else {}
        bs  = cache["balance_sheets"][ticker]["data"] if ticker in cache["balance_sheets"] else {}
        r   = _compute_cri(ticker, bs, sf, q)

        # Mark the ticker as an ETF if we are reasonably sure it is one.
        if ticker.upper() in KNOWN_ETFS:
            r["is_etf"] = True

        if ticker in etf_cri_map:
            r["cri_per_share"]    = round(etf_cri_map[ticker], 6)
            r["is_etf_estimated"] = True
            r["is_etf"]           = True

        results[ticker] = r

    return results


# ─── HTTP helper ──────────────────────────────────────────────────────────────

async def _get(client: httpx.AsyncClient, url: str, params: dict):
    try:
        resp = await client.get(url, params=params)
        if resp.status_code in (402, 403, 429):
            logger.warning("%d on %s: endpoint not available on this plan or rate-limited",
                           resp.status_code, url)
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("FMP request failed (%s): %s", url, exc)
        return None


# ─── SEC EDGAR helpers ────────────────────────────────────────────────────────

async def _edgar_cik_map(client: httpx.AsyncClient) -> dict:
    """Return {TICKER: '0000320193', ...}, cached locally for 90 days."""
    if EDGAR_CIK_CACHE.exists():
        try:
            cached = json.loads(EDGAR_CIK_CACHE.read_text())
            if not _is_stale(cached.get("_fetched"), EDGAR_TTL_DAYS):
                return cached
        except Exception:
            pass
    try:
        resp = await client.get(EDGAR_TICKERS, headers={"User-Agent": EDGAR_USER_AGENT}, timeout=30.0)
        if resp.status_code != 200:
            return {}
        raw = resp.json()
        mapping: dict = {"_fetched": _now_iso()}
        for entry in raw.values():
            t = str(entry.get("ticker", "")).upper()
            cik = str(entry.get("cik_str", "")).zfill(10)
            if t:
                mapping[t] = cik
        EDGAR_CIK_CACHE.write_text(json.dumps(mapping))
        return mapping
    except Exception as exc:
        logger.warning("EDGAR CIK map fetch failed: %s", exc)
        return {}


async def _edgar_balance_sheet(cik: str, ticker: str, client: httpx.AsyncClient) -> dict:
    """
    Fetch the four CRI-relevant concepts from SEC EDGAR's company-concept API.
    Returns a dict shaped like FMP's balance sheet, or {} on failure.
    """
    headers = {"User-Agent": EDGAR_USER_AGENT}

    def _latest_usd(entries: list) -> tuple:
        """Most recent 10-Q or 10-K instant USD value.

        Prefers 10-Q entries over 10-K entries for the same end date to avoid
        cases where some 10-K filings report shares/values on a different scale.

        Returns (value, end_date) where end_date is the ISO date string or None.
        """
        candidates = [
            e for e in entries
            if e.get("form") in ("10-Q", "10-K") and "end" in e and e.get("val") is not None
            and e.get("val", 0) > 0
        ]
        if not candidates:
            return 0, None
        # Pick max end date
        max_end = max(e["end"] for e in candidates)
        at_max  = [e for e in candidates if e["end"] == max_end]
        # Prefer 10-Q over 10-K for the same end date (some 10-Ks use different scale)
        q_entries = [e for e in at_max if e.get("form") == "10-Q"]
        chosen = (q_entries or at_max)[0]
        return chosen.get("val", 0), chosen.get("end")

    def _latest_shares(entries: list) -> int:
        """Most recent 10-Q or 10-K instant share count.

        Prefers 10-Q entries over 10-K for the same period; some annual filings
        (e.g. NFLX) contain a value ~10x larger than the correct count reported
        in the quarterly filing for the same balance-sheet date.
        Additionally, if the most-recent 10-K value is >3x the most-recent 10-Q
        value (and both are recent), the 10-Q value is used as a sanity check.
        """
        candidates = [
            e for e in entries
            if e.get("form") in ("10-Q", "10-K") and "end" in e and e.get("val") is not None
            and e.get("val", 0) > 0
        ]
        if not candidates:
            return 0

        # Most recent 10-Q and most recent 10-K separately
        q_cands = sorted([e for e in candidates if e.get("form") == "10-Q"],
                         key=lambda x: x["end"])
        k_cands = sorted([e for e in candidates if e.get("form") == "10-K"],
                         key=lambda x: x["end"])

        latest_q = q_cands[-1]["val"] if q_cands else None
        latest_k = k_cands[-1]["val"] if k_cands else None

        # If we have both, use sanity check: if 10-K > 3× 10-Q, trust 10-Q
        if latest_q and latest_k:
            if latest_k > latest_q * 3:
                logger.debug(
                    "EDGAR shares anomaly for CIK %s: 10-K=%d vs 10-Q=%d; using 10-Q",
                    cik, latest_k, latest_q,
                )
                return latest_q
            # Otherwise use whichever is more recent
            if q_cands[-1]["end"] >= k_cands[-1]["end"]:
                return latest_q
            return latest_k

        return latest_q or latest_k or 0

    async def concept_usd(name: str) -> tuple:
        """Returns (value, end_date)."""
        url = f"{EDGAR_BASE}/CIK{cik}/us-gaap/{name}.json"
        data = await _get_edgar(client, url, headers)
        return _latest_usd((data or {}).get("units", {}).get("USD", []))

    async def concept_shares_us(name: str) -> int:
        """Fetch a us-gaap share concept."""
        url = f"{EDGAR_BASE}/CIK{cik}/us-gaap/{name}.json"
        data = await _get_edgar(client, url, headers)
        return _latest_shares((data or {}).get("units", {}).get("shares", []))

    # Cash: try combined field first, then sum the two components
    cash, cash_date = await concept_usd("CashCashEquivalentsAndShortTermInvestments")
    if not cash:
        cash_a, cash_a_date = await concept_usd("CashAndCashEquivalentsAtCarryingValue")
        cash_b, _           = await concept_usd("ShortTermInvestments")
        cash      = cash_a + cash_b
        cash_date = cash_a_date

    receivables, recv_date = await concept_usd("AccountsReceivableNetCurrent")
    if not receivables:
        receivables, recv_date = await concept_usd("ReceivablesNetCurrent")

    inventory, inv_date = await concept_usd("InventoryNet")

    # Shares outstanding: try CommonStockSharesOutstanding first, then fall back
    # to the weighted-average concept which more companies file consistently.
    # We also check freshness: if the most recent CSO filing is more than 2 years
    # old (e.g. DDOG only has 2018 pre-IPO data), we prefer WeightedAvg instead.
    cso_url  = f"{EDGAR_BASE}/CIK{cik}/us-gaap/CommonStockSharesOutstanding.json"
    cso_data = await _get_edgar(client, cso_url, headers)
    cso_entries = (cso_data or {}).get("units", {}).get("shares", [])
    cso_candidates = [
        e for e in cso_entries
        if e.get("form") in ("10-Q", "10-K") and e.get("val", 0) > 0 and "end" in e
    ]
    today = datetime.now(timezone.utc).date()
    two_years_ago = str(today.replace(year=today.year - 2))
    cso_is_fresh = any(e["end"] >= two_years_ago for e in cso_candidates)

    wavg_shares = await concept_shares_us("WeightedAverageNumberOfSharesOutstandingBasic")

    if cso_is_fresh:
        shares = _latest_shares(cso_entries)
        # If CSO sanity fails against WeightedAvg, WeightedAvg will be preferred
        # inside _latest_shares already via the 3× check.
    else:
        # CSO data is absent or stale; use weighted-average as primary source
        shares = wavg_shares or _latest_shares(cso_entries)

    if not cash and not receivables and not inventory:
        return {}   # no useful data, do not cache as success

    # Use the most recent end date seen across all CRI components as the reporting period.
    all_dates = [d for d in [cash_date, recv_date, inv_date] if d]
    reporting_date = max(all_dates) if all_dates else None

    return {
        "cashAndShortTermInvestments":  cash,
        "netReceivables":               receivables,
        "inventory":                    inventory,
        # Store None (not 0) when shares are unavailable so _compute_cri can
        # distinguish "we have a real zero" from "we never got data".
        "commonStockSharesOutstanding": shares if shares else None,
        "date":                         reporting_date,
        "source":                       "edgar",
    }


async def _get_edgar(client: httpx.AsyncClient, url: str, headers: dict):
    """Quiet GET for EDGAR. 404 is normal (concept does not exist for this company)."""
    try:
        resp = await client.get(url, headers=headers, timeout=20.0)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.debug("EDGAR concept fetch failed (%s): %s", url, exc)
        return None


# ─── EDGAR filing-level shares fallback ──────────────────────────────────────

async def _edgar_shares_from_filing(cik: str, client: httpx.AsyncClient) -> Optional[int]:
    """
    Parse EntityCommonStockSharesOutstanding from EDGAR's R1.htm viewer file
    of the most recent 10-Q or 10-K.  Used when the company-concept API returns
    stale or no shares data (e.g. Visa, which files shares only in the cover page
    but not in the tagged us-gaap CommonStockSharesOutstanding concept).
    """
    headers = {"User-Agent": EDGAR_USER_AGENT}
    try:
        # Get most recent 10-Q or 10-K accession
        sub_resp = await client.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers=headers, timeout=15.0,
        )
        sub  = sub_resp.json()
        recent = sub.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accs  = recent.get("accessionNumber", [])
        filing_acc = next(
            (accs[i] for i, f in enumerate(forms) if f in ("10-Q", "10-K")), None
        )
        if not filing_acc:
            return None

        cik_plain = cik.lstrip("0")
        acc_nodash = filing_acc.replace("-", "")
        r1_url = (f"https://www.sec.gov/Archives/edgar/data/"
                  f"{cik_plain}/{acc_nodash}/R1.htm")
        resp   = await client.get(r1_url, headers=headers, timeout=15.0)
        if resp.status_code != 200:
            return None

        # Parse the tagged share count out of the XBRL viewer table.
        # The R1.htm structure puts the number in a class="nump" cell that
        # follows the concept anchor: EntityCommonStockSharesOutstanding -> ... -> nump cell
        match = re.search(
            r"EntityCommonStockSharesOutstanding[^>]+>[^<]+</a></td>"
            r".*?<td[^>]*nump[^>]*>([\d,]+)<",
            resp.text, re.IGNORECASE | re.DOTALL,
        )
        if not match:
            # Broader fallback for non-standard layouts
            match = re.search(
                r"Shares\s+Outstanding[^<]*</a></td>"
                r".*?<td[^>]*nump[^>]*>([\d,]+)<",
                resp.text, re.IGNORECASE | re.DOTALL,
            )
        if match:
            shares = int(match.group(1).replace(",", ""))
            if shares > 1_000_000:   # sanity: must be at least 1M shares
                logger.info("Got %d shares for CIK %s from R1.htm", shares, cik)
                return shares
    except Exception as exc:
        logger.debug("R1.htm shares fetch failed for CIK %s: %s", cik, exc)
    return None


# ─── Yahoo Finance helpers ────────────────────────────────────────────────────

async def _yahoo_get(client: httpx.AsyncClient, ticker: str, modules: str) -> dict:
    """Fetch Yahoo Finance quoteSummary. Returns result[0] dict or {} on failure."""
    try:
        resp = await client.get(
            f"{YAHOO_BASE}/{ticker}",
            params={"modules": modules},
            headers=YAHOO_HEADERS,
            timeout=15.0,
        )
        if resp.status_code != 200:
            logger.debug("Yahoo Finance %d for %s (%s)", resp.status_code, ticker, modules)
            return {}
        data = resp.json()
        results = (data.get("quoteSummary") or {}).get("result") or []
        return results[0] if results else {}
    except Exception as exc:
        logger.debug("Yahoo Finance fetch failed for %s: %s", ticker, exc)
        return {}


def _yraw(obj) -> float:
    """Extract the raw numeric value from a Yahoo Finance {raw, fmt} object."""
    if isinstance(obj, dict):
        return obj.get("raw") or 0
    return obj or 0


async def _yahoo_balance_sheet(ticker: str, client: httpx.AsyncClient) -> dict:
    """
    Fetch balance sheet and shares outstanding from Yahoo Finance.
    Returns a dict compatible with _compute_cri, or {} on failure.
    Works for US and international tickers (e.g. CCJ).
    """
    data = await _yahoo_get(client, ticker, "balanceSheetHistory,defaultKeyStatistics")

    bs_list = (data.get("balanceSheetHistory") or {}).get("balanceSheetStatements") or []
    bs      = bs_list[0] if bs_list else {}
    stats   = data.get("defaultKeyStatistics") or {}

    cash        = _yraw(bs.get("cash")) + _yraw(bs.get("shortTermInvestments"))
    receivables = _yraw(bs.get("netReceivables"))
    inventory   = _yraw(bs.get("inventory"))
    shares      = _yraw(stats.get("sharesOutstanding"))

    if not cash and not receivables and not inventory:
        return {}

    return {
        "cashAndShortTermInvestments": cash,
        "netReceivables":              receivables,
        "inventory":                   inventory,
        "commonStockSharesOutstanding": int(shares) if shares else None,
        "source":                      "yahoo",
    }


async def _yahoo_etf_holdings(ticker: str, client: httpx.AsyncClient) -> list:
    """
    Return [{asset, weightPercentage}, ...] from Yahoo Finance topHoldings.
    weightPercentage is in the 0-100 range.
    """
    data = await _yahoo_get(client, ticker, "topHoldings")
    holdings = (data.get("topHoldings") or {}).get("holdings") or []
    result = []
    for h in holdings:
        symbol = h.get("symbol", "").upper()
        pct    = _yraw(h.get("holdingPercent")) * 100   # Yahoo uses 0-1 scale
        if symbol and pct > 0:
            result.append({"asset": symbol, "weightPercentage": pct})
    return result


async def _yahoo_shares(ticker: str, client: httpx.AsyncClient) -> Optional[int]:
    """Return shares outstanding for a ticker from Yahoo Finance, or None."""
    data   = await _yahoo_get(client, ticker, "defaultKeyStatistics")
    shares = _yraw((data.get("defaultKeyStatistics") or {}).get("sharesOutstanding"))
    return int(shares) if shares else None


# ─── EDGAR N-PORT ETF holdings ────────────────────────────────────────────────

def _normalize_company_name(name: str) -> str:
    """Strip legal suffixes and whitespace for fuzzy company-name matching."""
    stop = (r"\b(incorporated|inc|corporation|corp|limited|ltd|company|co|plc|llc|lp|"
            r"trust|group|holdings|international|technologies|technology|systems|"
            r"solutions|communications|enterprises|industries|services|global|"
            r"semiconductor|pharmaceuticals|pharmaceutical|therapeutics|biosciences|"
            r"financial|bancorporation|bancshares|bancorp)\b")
    n = re.sub(stop, "", name.lower(), flags=re.IGNORECASE)
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


async def _edgar_name_ticker_map(client: httpx.AsyncClient) -> dict:
    """
    Build normalized company-name -> ticker lookup from SEC company_tickers.json.
    Cached locally for EDGAR_NAME_TTL days.
    """
    if EDGAR_NAME_CACHE.exists():
        try:
            cached = json.loads(EDGAR_NAME_CACHE.read_text())
            if not _is_stale(cached.get("_fetched"), EDGAR_NAME_TTL):
                return cached
        except Exception:
            pass
    try:
        resp = await client.get(
            EDGAR_TICKERS, headers={"User-Agent": EDGAR_USER_AGENT}, timeout=30.0
        )
        raw = resp.json()
        mapping: dict = {"_fetched": _now_iso()}
        for entry in raw.values():
            t     = str(entry.get("ticker", "")).upper().strip()
            title = str(entry.get("title", "")).strip()
            if t and title:
                norm = _normalize_company_name(title)
                if norm:
                    mapping[norm] = t
        EDGAR_NAME_CACHE.write_text(json.dumps(mapping))
        logger.info("Built EDGAR name->ticker map with %d entries", len(mapping) - 1)
        return mapping
    except Exception as exc:
        logger.warning("EDGAR name->ticker map failed: %s", exc)
        return {}


async def _edgar_nport_holdings(ticker: str, client: httpx.AsyncClient) -> list:
    """
    Fetch ETF holdings from SEC EDGAR N-PORT-P filing (free, no key required).
    Returns [{asset: ticker, weightPercentage: float}, ...] or [].

    Uses trust CIK from _ETF_EDGAR_INFO to avoid false positives from EDGAR
    full-text search (which returns any fund that holds the ETF, not the ETF itself).
    Scans the trust's recent N-PORT filings to find the right series by name.
    """
    info = _ETF_EDGAR_INFO.get(ticker.upper())
    if not info:
        return []
    trust_cik, series_phrase = info

    # Load N-PORT cache
    nport_cache: dict = {}
    if EDGAR_NPORT_CACHE.exists():
        try:
            nport_cache = json.loads(EDGAR_NPORT_CACHE.read_text())
        except Exception:
            pass

    cached = nport_cache.get(ticker, {})
    if cached.get("holdings") and not _is_stale(cached.get("fetched_at"), EDGAR_NPORT_TTL):
        logger.info("Using cached N-PORT holdings for %s (%d entries)",
                    ticker, len(cached["holdings"]))
        return cached["holdings"]

    headers = {"User-Agent": EDGAR_USER_AGENT}
    cik_padded = trust_cik.zfill(10)

    try:
        # Step 1: get recent N-PORT accession numbers for this trust
        resp_sub = await client.get(
            f"https://data.sec.gov/submissions/CIK{cik_padded}.json",
            headers=headers, timeout=15.0,
        )
        sub  = resp_sub.json()
        filings  = sub.get("filings", {}).get("recent", {})
        all_forms = filings.get("form", [])
        all_accs  = filings.get("accessionNumber", [])
        nport_accs = [all_accs[i] for i, f in enumerate(all_forms) if f == "NPORT-P"]

        if not nport_accs:
            logger.warning("No N-PORT filings found for trust CIK %s", trust_cik)
            return []

        # Step 2: scan filings (most recent first) to find the right series
        cik_plain = trust_cik.lstrip("0")
        target_xml = None
        for acc in nport_accs[:30]:   # check up to 30 filings (trust may have many series)
            acc_clean = acc.replace("-", "")
            xml_url   = (f"https://www.sec.gov/Archives/edgar/data/"
                         f"{cik_plain}/{acc_clean}/primary_doc.xml")
            try:
                resp_x = await client.get(xml_url, headers=headers, timeout=15.0)
                xml    = resp_x.text
                series_names = re.findall(r"<seriesName>([^<]+)</seriesName>", xml)
                if any(series_phrase.lower() in s.lower() for s in series_names):
                    logger.info("Found N-PORT for %s: %s (series: %s)",
                                ticker, acc, series_names)
                    target_xml = xml
                    break
            except Exception:
                continue

        if not target_xml:
            logger.warning("Could not find N-PORT series '%s' for %s", series_phrase, ticker)
            return []

        # Step 3: parse equity holdings from the matched XML
        blocks = re.findall(r"<invstOrSec>(.*?)</invstOrSec>", target_xml, re.DOTALL)
        raw_holdings = []
        for block in blocks:
            cat_m = re.search(r"<assetCat>([^<]+)</assetCat>", block)
            if not cat_m or cat_m.group(1) != "EC":   # equity only
                continue
            name_m = re.search(r"<name>([^<]+)</name>", block)
            pct_m  = re.search(r"<pctVal>([^<]+)</pctVal>", block)
            if not name_m or not pct_m:
                continue
            raw_holdings.append({
                "name": name_m.group(1).strip(),
                "pct":  float(pct_m.group(1)),
            })

        logger.info("N-PORT for %s: %d equity holdings found", ticker, len(raw_holdings))

        # Step 4: map company names to tickers using EDGAR company_tickers.json
        name_map  = await _edgar_name_ticker_map(client)
        results   = []
        unmatched = []
        for h in raw_holdings:
            norm   = _normalize_company_name(h["name"])
            symbol = name_map.get(norm)
            if not symbol:
                # Try progressively shorter prefixes
                parts = norm.split()
                for n_words in range(len(parts) - 1, 0, -1):
                    symbol = name_map.get(" ".join(parts[:n_words]))
                    if symbol:
                        break
            if symbol:
                results.append({"asset": symbol, "weightPercentage": h["pct"]})
            else:
                unmatched.append(h["name"])

        if unmatched:
            logger.debug("N-PORT %s: %d unmatched: %s", ticker, len(unmatched), unmatched[:5])
        logger.info("N-PORT %s: mapped %d/%d holdings", ticker, len(results), len(raw_holdings))

        # Cache and return
        nport_cache[ticker] = {"holdings": results, "fetched_at": _now_iso()}
        EDGAR_NPORT_CACHE.write_text(json.dumps(nport_cache, indent=2))
        return results

    except Exception as exc:
        logger.warning("N-PORT fetch failed for %s: %s", ticker, exc)
        return []


# ─── ETF CRI estimation ───────────────────────────────────────────────────────

async def _compute_etf_cri(
    ticker: str,
    cache: dict,
    now: str,
    client,
    api_key: str,
) -> Optional[float]:
    """
    Estimate CRI per share for an ETF by inspecting its top holdings.

    Returns the estimated CRI per share (float), or None if the ticker is not
    an ETF or insufficient data is available.

    Algorithm:
      1. Fetch holdings from /stable/etf-holder (cached ETF_HOLDINGS_TTL_DAYS days).
      2. Take the top ETF_TOP_N holdings by weight.
      3. Enrich those stocks (quote + float + balance-sheet) using the shared cache.
      4. Compute a weighted-average CRI ratio = Σ(w_i * cri_per_share_i / price_i).
      5. Extrapolate to the full fund and scale by the ETF price.
    """
    # ── 1. Fetch / cache ETF holdings ────────────────────────────────────────
    # Use a short TTL for misses so we retry with the fallback endpoint sooner.
    cached_h = cache["etf_holdings"].get(ticker, {})
    has_data  = bool(cached_h.get("data"))
    etf_ttl   = ETF_HOLDINGS_TTL_DAYS if has_data else ETF_MISS_TTL_DAYS
    if _is_stale(cached_h.get("fetched_at"), etf_ttl):
        # Try etf-holder first, then etf-holdings as fallback (plan differences)
        raw = await _get(client, f"{FMP_BASE}/etf-holder",
                         {"symbol": ticker, "apikey": api_key})
        if not (isinstance(raw, list) and raw):
            raw = await _get(client, f"{FMP_BASE}/etf-holdings",
                             {"symbol": ticker, "apikey": api_key})
        if not (isinstance(raw, list) and raw):
            # FMP endpoints unavailable; try Yahoo Finance topHoldings
            yahoo_h = await _yahoo_etf_holdings(ticker, client)
            if yahoo_h:
                raw = yahoo_h
        if not (isinstance(raw, list) and raw):
            # Yahoo also unavailable; use SEC EDGAR N-PORT (free, quarterly filings)
            nport_h = await _edgar_nport_holdings(ticker, client)
            if nport_h:
                raw = nport_h
        if isinstance(raw, list) and raw:
            cache["etf_holdings"][ticker] = {"data": raw, "fetched_at": now}
        else:
            cache["etf_holdings"][ticker] = {"data": [], "fetched_at": now}

    holdings = cache["etf_holdings"].get(ticker, {}).get("data", [])
    if not holdings:
        return None   # not an ETF or holdings data unavailable

    # ── 2. Top N holdings by weight ───────────────────────────────────────────
    top_h = sorted(holdings, key=lambda h: h.get("weightPercentage", 0), reverse=True)
    top_h = [h for h in top_h if h.get("asset")][:ETF_TOP_N]
    sub_tickers = [h["asset"] for h in top_h]
    if not sub_tickers:
        return None

    logger.info("ETF %s: enriching top-%d holdings for CRI: %s", ticker, len(sub_tickers), sub_tickers)

    # ── 3. Enrich sub-tickers using the shared cache ──────────────────────────
    # Quotes (try /quote then /profile as fallback, same as main enrichment)
    sub_quotes: dict = {}
    for st in sub_tickers:
        cached_p = cache["prices"].get(st, {})
        if not _is_stale(cached_p.get("fetched_at"), PRICE_TTL_DAYS) and cached_p.get("price"):
            sub_quotes[st] = {"price": cached_p["price"]}
        else:
            q = await _get(client, f"{FMP_BASE}/quote", {"symbol": st, "apikey": api_key})
            item = (q or [None])[0] if isinstance(q, list) else None
            if not (item and item.get("price")):
                # fallback to profile endpoint
                p = await _get(client, f"{FMP_BASE}/profile", {"symbol": st, "apikey": api_key})
                item = (p or [None])[0] if isinstance(p, list) else None
            if item and item.get("price"):
                sub_quotes[st] = {"price": item["price"]}
                cache["prices"][st] = {"price": item["price"], "fetched_at": now}

    # Shares float
    stale_float = [t for t in sub_tickers
                   if _is_stale(cache["shares_float"].get(t, {}).get("fetched_at"), FLOAT_TTL_DAYS)]
    for st in stale_float:
        data = await _get(client, f"{FMP_BASE}/shares-float", {"symbol": st, "apikey": api_key})
        item = (data or [None])[0] if isinstance(data, list) else None
        cache["shares_float"][st] = {"data": item or {}, "fetched_at": now}

    # Balance sheets
    def _bs_stale(t: str) -> bool:
        entry = cache["balance_sheets"].get(t, {})
        ttl = BS_TTL_DAYS if entry.get("data") else BS_MISS_TTL_DAYS
        return _is_stale(entry.get("fetched_at"), ttl)

    stale_bs = [t for t in sub_tickers if _bs_stale(t)]
    for st in stale_bs:
        data = await _get(client, f"{FMP_BASE}/balance-sheet-statement",
                          {"symbol": st, "limit": 1, "apikey": api_key})
        item = (data or [None])[0] if isinstance(data, list) else None
        cache["balance_sheets"][st] = {"data": item or {}, "fetched_at": now}

    # EDGAR fallback for sub-tickers still missing a balance sheet
    no_sub_bs = [t for t in sub_tickers if not cache["balance_sheets"].get(t, {}).get("data")]
    if no_sub_bs:
        cik_map = await _edgar_cik_map(client)
        for st in no_sub_bs:
            cik = cik_map.get(st.upper())
            if cik:
                edgar_bs = await _edgar_balance_sheet(cik, st, client)
                if edgar_bs:
                    cache["balance_sheets"][st] = {"data": edgar_bs, "fetched_at": now}

    # ── 4. Weighted-average CRI ratio ─────────────────────────────────────────
    covered_weight   = 0.0
    weighted_ratio   = 0.0

    for h in top_h:
        st     = h["asset"]
        weight = h.get("weightPercentage", 0) / 100.0
        sf     = cache["shares_float"].get(st, {}).get("data", {})
        bs     = cache["balance_sheets"].get(st, {}).get("data", {})
        q      = sub_quotes.get(st, {})

        cri_data = _compute_cri(st, bs, sf, q)
        price    = cri_data.get("price", 0)
        cri_ps   = cri_data.get("cri_per_share", 0)

        if price > 0:
            weighted_ratio += weight * (cri_ps / price)
            covered_weight += weight

    if covered_weight == 0:
        return None

    # Extrapolate from covered weight to full fund
    full_ratio = weighted_ratio / covered_weight

    # ── 5. Scale by ETF price ─────────────────────────────────────────────────
    etf_price = (cache["prices"].get(ticker, {}).get("price") or 0)
    if not etf_price:
        return None

    return full_ratio * etf_price


# ─── CRI computation ──────────────────────────────────────────────────────────

def _compute_cri(ticker: str, bs: dict, sf: dict, quote: dict) -> dict:
    # Balance-sheet CRI components (null treated as 0 per spec).
    # Use explicit None checks so a legitimate zero value is not treated as missing.
    raw_cash = bs.get("cashAndShortTermInvestments")
    if raw_cash is None:
        raw_cash = (bs.get("cashAndCashEquivalents") or 0) + (bs.get("shortTermInvestments") or 0)
    cash        = raw_cash
    receivables = bs.get("netReceivables") or 0
    inventory   = bs.get("inventory")     or 0
    cri_value   = cash + receivables + inventory

    # Shares outstanding: prefer shares-float, fall back to balance sheet.
    # Note: bs.get("commonStockSharesOutstanding") stores None (not 0) when EDGAR
    # couldn't find shares data, so `or` correctly skips it.
    shares_outstanding = (sf.get("outstandingShares") or
                          bs.get("commonStockSharesOutstanding") or
                          None)

    price            = quote.get("price") or 0
    reporting_period = bs.get("date")

    if shares_outstanding:
        cri_per_share = cri_value / shares_outstanding
    else:
        cri_per_share = 0
        logger.warning("No shares outstanding data for %s; cri_per_share will be 0", ticker)

    return {
        "cri_value":             round(cri_value, 2),
        "cri_per_share":         round(cri_per_share, 6),
        "shares_outstanding":    shares_outstanding,
        "price":                 round(float(price), 4),
        "cash_component":        cash,
        "receivables_component": receivables,
        "inventory_component":   inventory,
        "reporting_period":      reporting_period,
    }
