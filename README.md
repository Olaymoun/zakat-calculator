# Zakat Calculator

A local web app for calculating Zakat on equity portfolios using the **CRI (Cash, Receivables, Inventory) method**. Integrates directly with Fidelity's positions page via a browser bookmarklet and pulls financial data from Financial Modeling Prep (FMP) and SEC EDGAR.

---

## Features

- **One-click Fidelity import:** a browser bookmarklet extracts your ticker symbols and share counts from Fidelity's positions page without storing any account information
- **CRI method:** calculates Zakat on the liquid portion of each equity holding, not the full market value
- **ETF CRI estimation:** for ETFs (e.g. XLK, QQQ), CRI is estimated from the weighted average of the top holdings since ETFs have no meaningful balance sheet of their own
- **Cash and bank accounts:** manually enter bank balances; Zakat (2.5%) is calculated on each and included in the total
- **Intent toggle:** switch each holding between Long-Term (CRI method) and Short-Term (full market value) intent per Islamic finance guidelines
- **Dual data sources:** FMP for prices and balance sheets; SEC EDGAR as a free fallback for any ticker FMP cannot serve
- **Smart caching:** balance sheets cached 90 days (quarterly filings), prices cached 1 day, shares float cached 7 days, ETF holdings cached 7 days
- **Portfolio summary:** total portfolio value, total Zakat due (equities + cash), and per-holding breakdown

---

## Zakat Calculation: CRI Method

### Intent-Based Classification

| Intent | Zakatable Base |
|--------|---------------|
| **Long-Term / Fundamental** | `CRI per share x shares owned` |
| **Short-Term / Active** | `Market price x shares owned` |

### CRI Formula

```
CRI = Cash & Short-Term Investments + Net Receivables + Inventory
CRI per Share = CRI / Shares Outstanding
Zakat Due = Zakatable Base x 2.5%
```

### Data Sources

| Field | Primary | Fallback |
|-------|---------|---------|
| Price | FMP `/stable/quote` | FMP `/stable/profile` |
| Shares Outstanding | FMP `/stable/shares-float` | SEC EDGAR `CommonStockSharesOutstanding` |
| Balance Sheet | FMP `/stable/balance-sheet-statement` | SEC EDGAR company-concept API |
| ETF Holdings | FMP `/stable/etf-holder` | (none) |

### ETF CRI Estimation

ETFs do not file their own balance sheets, so the standard CRI lookup returns no data. For any ticker where balance sheet data is unavailable, the app automatically checks whether it is an ETF by requesting its holdings list. If holdings are found:

1. The top 10 holdings by portfolio weight are selected
2. Each underlying stock is enriched with its own price, shares float, and balance sheet (using the shared cache)
3. A weighted-average CRI ratio is computed: `sum(weight_i x CRI_per_share_i / price_i)`
4. The ratio is extrapolated to the full fund and scaled by the ETF price

The result is displayed with an **ETF est.** badge in the UI. Holdings data is cached for 7 days.

---

## Prerequisites

- Python 3.8+
- A free [Financial Modeling Prep API key](https://financialmodelingprep.com/developer/docs)
- Google Chrome or Firefox

---

## Installation

```bash
git clone https://github.com/Olaymoun/zakat-calculator.git
cd zakat-calculator
cp .env.example .env
# Add your FMP API key to .env
./run.sh
```

Then open **http://127.0.0.1:8000** in your browser.

### First run

`run.sh` will:
1. Create a Python virtual environment
2. Install all dependencies
3. Start the server

---

## Usage

### 1. Add your FMP API key

Either add it to `.env`:
```
FMP_API_KEY=your_key_here
```
Or paste it in the **Settings** panel in the app.

### 2. Install the bookmarklet (one time only)

1. Open the app at `http://127.0.0.1:8000`
2. Show your bookmarks bar: `Cmd+Shift+B`
3. Click **"Copy bookmarklet code"**
4. Right-click your bookmarks bar and select **Add Page...**
5. Name: `Send to Zakat`, URL: paste (`Cmd+V`) and save

### 3. Import your Fidelity positions

1. Log in to Fidelity and navigate to **Accounts and Trade > Portfolio > Positions**
2. Click **Send to Zakat** in your bookmarks bar
3. An alert confirms how many positions were found, then the app opens in a new tab
4. The app fetches CRI data from FMP and SEC EDGAR automatically

### 4. Add cash and bank accounts

In the **Cash and Bank Accounts** section, enter each account name and its current balance and click **Add**. The app calculates 2.5% Zakat on each balance and includes it in the total. Entries persist across sessions and can be removed at any time.

### 5. Set intent per holding

Each equity row has a **Long-Term / Short-Term** toggle:
- **Long-Term:** you hold this stock as a business ownership stake; only the liquid (CRI) fraction is Zakatable
- **Short-Term:** you bought this to flip it; the full market value is Zakatable

Intent is saved and persists across updates.

### 6. Refresh prices

Click **Refresh FMP Prices** in the header to re-fetch current prices and recalculate without re-running the bookmarklet. For ETF holdings, this also re-uses cached underlying stock data.

---

## Project Structure

```
zakat-calculator/
├── main.py          # FastAPI server - all REST endpoints
├── fmp.py           # FMP and SEC EDGAR data client with caching - ETF CRI estimation
├── calculator.py    # Zakat calculation logic (CRI method)
├── static/
│   └── index.html   # Single-page frontend
├── data/            # Runtime data (gitignored)
│   ├── portfolio.json
│   ├── intents.json
│   ├── settings.json
│   ├── cash.json        # Cash and bank account entries
│   └── fmp_cache.json   # Prices, balance sheets, float, ETF holdings
├── requirements.txt
├── run.sh           # One-command setup and launch
└── .env.example
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serve the frontend |
| `GET` | `/api/portfolio` | Get all positions with calculated Zakat (includes cash Zakat in total) |
| `POST` | `/api/positions` | Receive positions from bookmarklet |
| `POST` | `/api/update` | Re-run FMP enrichment on existing positions |
| `GET` | `/api/status/{job_id}` | Poll background job status |
| `PATCH` | `/api/intent/{ticker}` | Update intent for a ticker |
| `GET` | `/api/cash` | Get all cash account entries |
| `POST` | `/api/cash` | Add a cash account entry `{name, amount}` |
| `DELETE` | `/api/cash/{entry_id}` | Remove a cash account entry |
| `GET` | `/api/settings` | Get current settings |
| `POST` | `/api/settings` | Save FMP API key |

---

## Data Privacy

The bookmarklet extracts **only ticker symbols and share quantities** from Fidelity's page. No account numbers, balances, personal information, or credentials are collected or transmitted. All data stays on your local machine; the app runs entirely on `localhost`.

---

## Caching Strategy

| Data | TTL | Reason |
|------|-----|--------|
| Stock prices | 1 day | Changes daily |
| Shares float | 7 days | Changes slowly |
| Balance sheets | 90 days | Quarterly filings |
| Failed lookups | 1 day | Retry tomorrow |
| ETF holdings | 7 days | Rebalances infrequently |
| SEC EDGAR CIK map | 90 days | Rarely changes |

---

## Limitations

- **Private companies** (e.g. pre-IPO holdings) have no SEC filings or FMP data; CRI will be `$0`. Use Short-Term intent for these.
- **FMP free tier** supports a limited set of endpoints. Prices come from `/stable/quote` or `/stable/profile`. Balance sheets fall back to SEC EDGAR when FMP returns 402.
- **Non-US equities** may not have SEC EDGAR filings. CRI will fall back to FMP only.
- **ETF CRI is an estimate:** only the top 10 holdings by weight are used, extrapolated to the full fund. Accuracy improves for concentrated ETFs and decreases for very broad funds.
- **Nested ETFs:** if an ETF holds other ETFs among its top-10 holdings, those inner ETFs will show a CRI of `$0` (single-level lookup only).

---

## License

MIT
