"""
FMP API client - uses /stable/ endpoints (required for this API key tier).
SEC EDGAR is used as a free fallback for balance-sheet data when FMP returns 402.

Call budget per update run (N tickers):
  N FMP quote calls                         (price, always fresh)
  0–N FMP shares-float calls                (cached 7 days)
  0–N FMP balance-sheet calls               (cached 90 days)
  0–N SEC EDGAR company-concept calls ×4    (only for tickers FMP can't serve)
"""

import json
import logging
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
ETF_TOP_N             = 10   # enrich top-N holdings for CRI estimation

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

        # ── ETF CRI estimation for tickers still without balance sheet ─────────
        # ETFs have no meaningful balance sheet of their own.  Instead, fetch the
        # ETF's top holdings, enrich those underlying stocks, and compute a
        # weighted-average CRI ratio scaled by the ETF price.
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

        if ticker in etf_cri_map:
            r["cri_per_share"]    = round(etf_cri_map[ticker], 6)
            r["is_etf_estimated"] = True

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
    cached_h = cache["etf_holdings"].get(ticker, {})
    if _is_stale(cached_h.get("fetched_at"), ETF_HOLDINGS_TTL_DAYS):
        raw = await _get(client, f"{FMP_BASE}/etf-holder",
                         {"symbol": ticker, "apikey": api_key})
        if isinstance(raw, list) and raw:
            cache["etf_holdings"][ticker] = {"data": raw, "fetched_at": now}
        else:
            cache["etf_holdings"][ticker] = {"data": [], "fetched_at": now}

    holdings = cache["etf_holdings"].get(ticker, {}).get("data", [])
    if not holdings:
        return None   # not an ETF (or no holdings data)

    # ── 2. Top N holdings by weight ───────────────────────────────────────────
    top_h = sorted(holdings, key=lambda h: h.get("weightPercentage", 0), reverse=True)
    top_h = [h for h in top_h if h.get("asset")][:ETF_TOP_N]
    sub_tickers = [h["asset"] for h in top_h]
    if not sub_tickers:
        return None

    logger.info("ETF %s: enriching top-%d holdings for CRI: %s", ticker, len(sub_tickers), sub_tickers)

    # ── 3. Enrich sub-tickers using the shared cache ──────────────────────────
    # Quotes
    sub_quotes: dict = {}
    for st in sub_tickers:
        cached_p = cache["prices"].get(st, {})
        if not _is_stale(cached_p.get("fetched_at"), PRICE_TTL_DAYS) and cached_p.get("price"):
            sub_quotes[st] = {"price": cached_p["price"]}
        else:
            q = await _get(client, f"{FMP_BASE}/quote", {"symbol": st, "apikey": api_key})
            item = (q or [None])[0] if isinstance(q, list) else None
            if item and item.get("price"):
                sub_quotes[st] = item
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
