# ETF Dividend Look-Through

Enter an ETF ticker to see:

- every company it holds and the **exact number of shares** of each
- each company's dividends (ex-date, record date, pay date, amount)
- a **day-by-day calendar** of how much dividend cash the fund receives, in total and **per ETF share**
- a **portfolio mode**: enter your ETFs and share counts to see which companies your dividends come from and when

## Where the data comes from

| What | Source | Notes |
|---|---|---|
| Holdings (shares held) | State Street SPDR daily file | SPY, DIA, SDY, XLK, XLF and other SPDR funds |
| | Invesco daily holdings CSV | QQQ, SPHD, RSP and other Invesco funds |
| | Vanguard holdings API | VYM, VOO, VTI and other Vanguard funds |
| | SEC EDGAR N-PORT filings (fallback) | Works for almost any US ETF (iShares, Schwab/SCHD, JPMorgan, etc.). Share counts are exact but can be 2–5 months old. |
| Ticker lookup for N-PORT | OpenFIGI | Maps CUSIPs to tickers when the filing has no ticker |
| Dividends | Nasdaq dividend history | Includes ex, record, declared and pay dates (Nasdaq-listed stocks only) |
| | StockAnalysis (NYSE and anything Nasdaq doesn't cover) | Ex, record and pay dates, including declared upcoming dividends |
| | Yahoo Finance (last resort) | Ex-date and amount only. Pay date is estimated as ex-date + 14 days and marked with * |
| Prices / shares outstanding | Yahoo Finance, Nasdaq | Shares outstanding = net assets ÷ ETF price. You can edit it in the UI. |

All lookups are cached in SQLite. Holdings are cached for 6 hours and dividends for 12.

### How the numbers are calculated
- **Fund receives** = dividend per share × shares the fund holds now.
- **Per ETF share** = fund receives ÷ ETF shares outstanding.
- **Your look-through amount** = fund receives × (your ETF shares ÷ ETF shares outstanding).
- **Projected** payments repeat last year's regular dividends when the next one hasn't been declared yet.

These figures are **gross**. The amount an ETF actually distributes is lower because of its expense ratio and foreign withholding, and the timing differs because funds pool income and pay it out monthly or quarterly. The app shows the ETF's real distributions next to the estimate so you can compare them.

## Deploy to Render (free)

1. Create a new GitHub repository and push this folder to it:
   ```bash
   git remote add origin https://github.com/<you>/etf-dividends.git
   git push -u origin main
   ```
2. On https://dashboard.render.com click **New → Blueprint**, pick the repo, and Render will read `render.yaml`.
   (Or use **New → Web Service** with build `pip install -r requirements.txt` and start
   `gunicorn app:app --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT`.)
3. Set environment variables when prompted:
   - `SEC_USER_AGENT`: **required by the SEC.** Use something like `ETF Dividend Tracker yourname@youremail.com`.
   - `OPENFIGI_API_KEY`: optional and free from openfigi.com. It makes N-PORT lookups much faster.
4. Open the URL Render gives you.

Note: Render's free tier sleeps after 15 minutes idle, so the first load can take about 30 seconds. Its disk is also temporary, so the cache resets on each deploy or restart. Keep `--workers 1`, because analysis jobs are tracked in memory.

## Run locally
```bash
pip install -r requirements.txt
SEC_USER_AGENT="ETF Dividend Tracker you@email.com" python app.py
# open http://localhost:5000
python tests/test_mock.py   # offline tests with mocked data
```

## Limits
- A first-time analysis of a big fund takes a while. 500 holdings means about 500 dividend lookups, roughly 1–3 minutes. After that it's cached. Use "Max holdings" to cap it (the largest holdings are analyzed first).
- Foreign-listed holdings, such as those in international ETFs, often have no US ticker, so they show under "Not included".
- Nasdaq and Yahoo are unofficial endpoints and can rate-limit or change. The code falls back between them, but if both block your server's IP, dividend data will be missing.
