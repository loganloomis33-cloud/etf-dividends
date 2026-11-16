"""
Data fetchers.

Holdings (exact shares held), tried in order:
  1. SPDR / State Street daily holdings file (SPY, DIA, SDY, XL* sector funds, ...)
  2. Invesco daily holdings CSV (QQQ, SPHD, ...)
  3. Vanguard holdings API (VYM, VOO, VTI, ...)
  4. SEC EDGAR N-PORT filing (any US-registered ETF; iShares, Schwab, etc.)
     -> official, exact share counts, but published with a lag (quarterly).

Dividends per company:
  1. Nasdaq dividend history API (ex date, record date, pay date, declared date, amount)
  2. Yahoo Finance chart API (ex date + amount only; pay date then estimated)

Everything is cached in SQLite so repeated lookups are fast and polite to the sources.
"""
import csv
import io
import logging
import os
import re
import statistics
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

import requests

import cache

log = logging.getLogger("etf.data")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
# The SEC requires a descriptive User-Agent with contact info.
SEC_UA = os.environ.get("SEC_USER_AGENT", "ETF Dividend Tracker contact@example.com")
OPENFIGI_KEY = os.environ.get("OPENFIGI_API_KEY", "").strip()

HOLDINGS_TTL = 6 * 3600
NPORT_TTL = 24 * 3600
DIVIDENDS_TTL = 12 * 3600
PRICE_TTL = 3600
FIGI_TTL = 90 * 24 * 3600

_session = requests.Session()
_session.headers.update({"User-Agent": BROWSER_UA, "Accept": "*/*"})


class SourceError(Exception):
    pass


def http_get(url, headers=None, timeout=20, **kw):
    """GET with small retry/backoff. Returns requests.Response (raises on failure)."""
    last = None
    for attempt in range(3):
        try:
            r = _session.get(url, headers=headers or {}, timeout=timeout, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                last = SourceError(f"HTTP {r.status_code} from {url}")
                time.sleep(1.5 * (attempt + 1))
                continue
            return r
        except requests.RequestException as e:  # network error
            last = e
            time.sleep(1.0 * (attempt + 1))
    raise SourceError(str(last))


# --------------------------------------------------------------------------- utils

def norm_ticker(t):
    """Normalize tickers to 'BRK.B' style."""
    if not t:
        return ""
    t = str(t).strip().upper()
    t = re.sub(r"\s+(US|UN|UW|UQ|UR|UA|UP)$", "", t)  # bloomberg-ish suffixes
    t = t.replace("/", ".").replace(" ", ".").replace("-", ".")
    if t in ("-", "--", "N/A", "NA", "CASH", "USD", ".", "NONE"):
        return ""
    return t


def to_float(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace("$", "").replace(",", "").replace("%", "")
    if s in ("", "-", "--", "N/A", "NA"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(s):
    """Parse many date formats to ISO yyyy-mm-dd (or None)."""
    if not s:
        return None
    if isinstance(s, datetime):
        return s.date().isoformat()
    if isinstance(s, date):
        return s.isoformat()
    s = str(s).strip()
    if s in ("N/A", "--", "-", ""):
        return None
    s = re.sub(r"^as of\s*", "", s, flags=re.I)
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d-%b-%Y", "%b %d, %Y", "%m/%d/%y", "%d %b %Y", "%B %d, %Y",
                "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _pick(d, *names):
    """Case/space-insensitive dict lookup."""
    norm = {re.sub(r"[^a-z0-9]", "", str(k).lower()): v for k, v in d.items()}
    for n in names:
        v = norm.get(re.sub(r"[^a-z0-9]", "", n.lower()))
        if v not in (None, ""):
            return v
    return None


def _holding(name, ticker, shares, value=None, weight=None, cusip=None, isin=None, asset=None):
    return {
        "name": (name or "").strip(),
        "ticker": norm_ticker(ticker),
        "shares": shares,
        "value": value,
        "weight": weight,
        "cusip": cusip,
        "isin": isin,
        "asset": asset,
    }


# --------------------------------------------------------------------------- issuers

def fetch_spdr(etf):
    url = ("https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/"
           f"etfs/us/holdings-daily-us-en-{etf.lower()}.xlsx")
    r = http_get(url)
    if r.status_code != 200 or len(r.content) < 500 or r.content[:2] != b"PK":
        raise SourceError("not a SPDR fund")
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    fund_name, as_of, header_idx = None, None, None
    for i, row in enumerate(rows[:30]):
        cells = [c for c in row if c is not None]
        if not cells:
            continue
        first = str(cells[0]).strip()
        if first.lower().startswith("fund name") and len(cells) > 1:
            fund_name = str(cells[1])
        if first.lower().startswith("holdings") and len(cells) > 1:
            as_of = parse_date(cells[1])
        joined = " | ".join(str(c) for c in cells).lower()
        if "shares held" in joined and "name" in joined:
            header_idx = i
            break
    if header_idx is None:
        raise SourceError("SPDR file format not recognized")
    header = [str(c).strip() if c is not None else "" for c in rows[header_idx]]
    out = []
    for row in rows[header_idx + 1:]:
        if not row or all(c is None for c in row):
            continue
        d = dict(zip(header, row))
        shares = to_float(_pick(d, "Shares Held"))
        if shares is None:
            continue
        out.append(_holding(
            _pick(d, "Name"), _pick(d, "Ticker"), shares,
            weight=to_float(_pick(d, "Weight")),
            cusip=_pick(d, "Identifier"),
        ))
    if not out:
        raise SourceError("SPDR file had no holdings")
    return {"source": "State Street SPDR daily holdings", "fund_name": fund_name, "as_of": as_of,
            "holdings": out}


def fetch_invesco(etf):
    url = ("https://www.invesco.com/us/financial-products/etfs/holdings/main/holdings/0"
           f"?audienceType=Investor&action=download&ticker={etf.upper()}")
    r = http_get(url)
    if r.status_code != 200 or "," not in r.text[:500]:
        raise SourceError("not an Invesco fund")
    reader = csv.DictReader(io.StringIO(r.text.lstrip("﻿")))
    out, as_of, fund_t = [], None, None
    for d in reader:
        fund_t = fund_t or _pick(d, "Fund Ticker")
        shares = to_float(_pick(d, "Shares/Par Value", "Shares", "Par Value"))
        if shares is None:
            continue
        cls = (_pick(d, "Class of Shares") or "").lower()
        as_of = as_of or parse_date(_pick(d, "Date"))
        out.append(_holding(
            _pick(d, "Name"), _pick(d, "Holding Ticker", "Ticker"), shares,
            value=to_float(_pick(d, "MarketValue", "Market Value")),
            weight=to_float(_pick(d, "Weight")),
            cusip=_pick(d, "Security Identifier"),
            asset="cash" if "cash" in cls or "currency" in cls else None,
        ))
    if not out or (fund_t and fund_t.strip().upper() != etf.upper()):
        raise SourceError("Invesco returned no holdings")
    return {"source": "Invesco daily holdings", "fund_name": None, "as_of": as_of, "holdings": out}


def fetch_vanguard(etf):
    base = f"https://investor.vanguard.com/investment-products/etfs/profile/api/{etf.upper()}/portfolio-holding/stock"
    out, start, as_of = [], 1, None
    for _ in range(40):  # paginate
        r = http_get(f"{base}?start={start}&count=500", headers={"Accept": "application/json"})
        if r.status_code != 200:
            break
        try:
            j = r.json()
        except ValueError:
            break
        fund = j.get("fund") or {}
        ents = fund.get("entity") or j.get("entity") or []
        as_of = as_of or parse_date(fund.get("asOfDate") or j.get("asOfDate"))
        for e in ents:
            shares = to_float(e.get("sharesHeld"))
            if shares is None:
                continue
            out.append(_holding(e.get("longName") or e.get("shortName"), e.get("ticker"), shares,
                                value=to_float(e.get("marketValue")),
                                weight=to_float(e.get("percentWeight")),
                                cusip=e.get("cusip"), isin=e.get("isin")))
        total = j.get("size") or fund.get("size") or 0
        if not ents or len(out) >= int(total or 0):
            break
        start += len(ents)
    if not out:
        raise SourceError("not a Vanguard fund")
    return {"source": "Vanguard holdings", "fund_name": None, "as_of": as_of, "holdings": out}


# --------------------------------------------------------------------------- SEC N-PORT

def _sec_get(url):
    # SEC gets its own plain requests (no shared browser-like session), which it handles reliably.
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"}, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                last = SourceError(f"SEC HTTP {r.status_code}")
                time.sleep(2 * (attempt + 1))
                continue
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise SourceError(f"SEC request failed: {last}")


def sec_series_for_ticker(etf):
    data = cache.get("sec:mf_tickers", 7 * 24 * 3600)
    if data is None:
        r = _sec_get("https://www.sec.gov/files/company_tickers_mf.json")
        if r.status_code != 200:
            raise SourceError(f"SEC ticker list HTTP {r.status_code}")
        j = r.json()
        fields = j.get("fields") or ["cik", "seriesId", "classId", "symbol"]
        data = {}
        for row in j.get("data", []):
            rec = dict(zip(fields, row))
            data[str(rec.get("symbol", "")).upper()] = [rec.get("cik"), rec.get("seriesId"), rec.get("classId")]
        cache.set("sec:mf_tickers", data)
    rec = data.get(etf.upper())
    if not rec:
        raise SourceError(f"{etf} not found in SEC fund ticker list (it may be a UIT, ETN, or non-US fund)")
    return rec  # [cik, seriesId, classId]


def _strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
        for k in list(el.attrib):
            if "}" in k:
                el.attrib[k.split("}", 1)[1]] = el.attrib.pop(k)
    return root


def latest_nport_url(series_id, cik=None):
    # 1) EDGAR full-text search (fast, reliable from cloud hosts)
    try:
        r = _sec_get(f"https://efts.sec.gov/LATEST/search-index?q=%22{series_id}%22&forms=NPORT-P")
        if r.status_code == 200:
            hits = ((r.json().get("hits") or {}).get("hits")) or []
            best = None
            for h in hits:
                src = h.get("_source") or {}
                hid = h.get("_id") or ""
                adsh = src.get("adsh") or hid.split(":")[0]
                fdate = src.get("file_date") or ""
                if not adsh or (src.get("form") or "NPORT-P") not in ("NPORT-P", "NPORT-P/A"):
                    continue
                if best is None or fdate > best[0]:
                    c = cik or (src.get("ciks") or ["0"])[0]
                    best = (fdate, adsh, int(str(c)))
            if best:
                fdate, adsh, c = best
                return f"https://www.sec.gov/Archives/edgar/data/{c}/{adsh.replace('-', '')}/primary_doc.xml", fdate
    except Exception as e:
        log.info("EDGAR full-text search failed for %s: %s", series_id, e)
    # 2) legacy browse-edgar atom feed
    url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
           f"&CIK={series_id}&type=NPORT-P&dateb=&owner=include&count=10&output=atom")
    r = _sec_get(url)
    if r.status_code != 200:
        raise SourceError(f"EDGAR filing index HTTP {r.status_code}")
    root = _strip_ns(ET.fromstring(r.content))
    best = None
    for entry in root.iter("entry"):
        content = entry.find("content")
        if content is None:
            continue
        href = (content.findtext("filing-href") or "").strip()
        fdate = (content.findtext("filing-date") or "").strip()
        ftype = (content.findtext("filing-type") or "").strip()
        if not href or not ftype.startswith("NPORT-P"):
            continue
        if best is None or fdate > best[0]:
            best = (fdate, href)
    if not best:
        raise SourceError("no N-PORT filings found for this fund")
    return best[1].rsplit("/", 1)[0] + "/primary_doc.xml", best[0]


def parse_nport(xml_bytes):
    root = _strip_ns(ET.fromstring(xml_bytes))
    gen = root.find(".//genInfo")
    fund = root.find(".//fundInfo")
    info = {
        "series_name": gen.findtext("seriesName") if gen is not None else None,
        "series_id": gen.findtext("seriesId") if gen is not None else None,
        "rep_date": parse_date(gen.findtext("repPdDate")) if gen is not None else None,
        "net_assets": to_float(fund.findtext("netAssets")) if fund is not None else None,
    }
    holdings = []
    for sec in root.iter("invstOrSec"):
        units = (sec.findtext("units") or "").strip()
        asset_cat = (sec.findtext("assetCat") or "").strip()
        if asset_cat == "" and sec.find("assetConditional") is not None:
            asset_cat = sec.find("assetConditional").get("assetCat", "")
        ids = sec.find("identifiers")
        ticker = isin = None
        if ids is not None:
            t = ids.find("ticker")
            ticker = t.get("value") if t is not None else None
            i = ids.find("isin")
            isin = i.get("value") if i is not None else None
        cusip = (sec.findtext("cusip") or "").strip()
        if cusip in ("N/A", "000000000"):
            cusip = None
        h = _holding(
            sec.findtext("name") or sec.findtext("title"), ticker,
            to_float(sec.findtext("balance")),
            value=to_float(sec.findtext("valUSD")),
            weight=to_float(sec.findtext("pctVal")),
            cusip=cusip, isin=isin, asset=asset_cat,
        )
        h["units"] = units
        h["country"] = sec.findtext("invCountry")
        holdings.append(h)
    return info, holdings


def fetch_nport(etf):
    cik, series_id, _cls = sec_series_for_ticker(etf)
    key = f"nport:{series_id}"
    cached = cache.get(key, NPORT_TTL)
    if cached:
        return cached
    url, filed = latest_nport_url(series_id, cik)
    r = _sec_get(url)
    if r.status_code != 200:
        raise SourceError(f"N-PORT document HTTP {r.status_code}")
    info, raw = parse_nport(r.content)
    holdings, other = [], 0
    for h in raw:
        # equity positions denominated in shares
        if h.get("units") == "NS" and (h.get("asset") in ("EC", "EP", "") or h.get("ticker")):
            holdings.append(h)
        else:
            other += 1
    map_missing_tickers(holdings)
    res = {
        "source": f"SEC N-PORT filing (filed {filed})",
        "fund_name": info["series_name"],
        "as_of": info["rep_date"],
        "net_assets": info["net_assets"],
        "holdings": holdings,
        "non_equity_positions": other,
        "filing_url": url,
    }
    cache.set(key, res)
    return res


def map_missing_tickers(holdings):
    """Fill missing tickers via OpenFIGI using CUSIP/ISIN (US listings only)."""
    todo = []
    for h in holdings:
        if h["ticker"]:
            continue
        ident = h.get("cusip") or h.get("isin")
        if not ident:
            continue
        c = cache.get(f"figi:{ident}", FIGI_TTL)
        if c is not None:
            h["ticker"] = c
        else:
            todo.append(h)
    batch = 100 if OPENFIGI_KEY else 10
    headers = {"Content-Type": "application/json"}
    if OPENFIGI_KEY:
        headers["X-OPENFIGI-APIKEY"] = OPENFIGI_KEY
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        jobs = []
        for h in chunk:
            if h.get("cusip"):
                jobs.append({"idType": "ID_CUSIP", "idValue": h["cusip"], "exchCode": "US"})
            else:
                jobs.append({"idType": "ID_ISIN", "idValue": h["isin"], "exchCode": "US"})
        try:
            r = _session.post("https://api.openfigi.com/v3/mapping", json=jobs, headers=headers, timeout=30)
            if r.status_code == 429:
                time.sleep(6)
                r = _session.post("https://api.openfigi.com/v3/mapping", json=jobs, headers=headers, timeout=30)
            res = r.json() if r.status_code == 200 else []
        except (requests.RequestException, ValueError):
            res = []
        for h, item in zip(chunk, res):
            data = (item or {}).get("data") or []
            t = norm_ticker(data[0].get("ticker")) if data else ""
            h["ticker"] = t
            cache.set(f"figi:{h.get('cusip') or h.get('isin')}", t)
        if i + batch < len(todo):
            time.sleep(0.3 if OPENFIGI_KEY else 2.6)  # stay under free rate limit


# --------------------------------------------------------------------------- holdings entry

def get_holdings(etf):
    etf = etf.upper().strip()
    key = f"holdings:{etf}"
    cached = cache.get(key, HOLDINGS_TTL)
    if cached:
        return cached
    errors = []
    for fn in (fetch_spdr, fetch_invesco, fetch_vanguard, fetch_nport):
        try:
            res = fn(etf)
            res["etf"] = etf
            res.setdefault("errors", errors)
            cache.set(key, res)
            return res
        except Exception as e:  # try next source
            errors.append(f"{fn.__name__}: {e}")
            log.info("holdings source %s failed for %s: %s", fn.__name__, etf, e)
    raise SourceError("Could not find holdings for %s. Tried: %s" % (etf, "; ".join(errors)))


# --------------------------------------------------------------------------- dividends

def _nasdaq_headers():
    return {
        "User-Agent": BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/",
    }


def nasdaq_dividends(ticker, assetclass="stocks"):
    url = f"https://api.nasdaq.com/api/quote/{ticker}/dividends?assetclass={assetclass}"
    r = http_get(url, headers=_nasdaq_headers(), timeout=15)
    if r.status_code != 200:
        raise SourceError(f"nasdaq HTTP {r.status_code}")
    j = r.json()
    data = j.get("data")
    if not data:
        raise SourceError("nasdaq: no data")
    rows = ((data.get("dividends") or {}).get("rows")) or []
    out = []
    for row in rows:
        amt = to_float(row.get("amount"))
        ex = parse_date(row.get("exOrEffDate"))
        if amt is None or not ex:
            continue
        out.append({
            "ex_date": ex,
            "pay_date": parse_date(row.get("paymentDate")),
            "record_date": parse_date(row.get("recordDate")),
            "decl_date": parse_date(row.get("declarationDate")),
            "amount": amt,
            "type": (row.get("type") or "Cash").strip(),
            "currency": row.get("currency") or "USD",
        })
    return out


def yahoo_chart(ticker, rng="2y", interval="1mo", period1=None, period2=None):
    sym = ticker.replace(".", "-")
    params = {"interval": interval, "events": "div"}
    if period1 is not None:
        params.update(period1=int(period1), period2=int(period2))
    else:
        params["range"] = rng
    last = None
    for host in ("query1", "query2"):
        try:
            r = http_get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}", params=params, timeout=15)
            if r.status_code == 200:
                res = (r.json().get("chart") or {}).get("result")
                if res:
                    return res[0]
            last = SourceError(f"yahoo HTTP {r.status_code}")
        except (SourceError, ValueError) as e:
            last = e
    raise SourceError(f"yahoo: {last}")


def yahoo_dividends(ticker):
    res = yahoo_chart(ticker, "2y", "1mo")
    divs = ((res.get("events") or {}).get("dividends") or {}).values()
    out = []
    for d in divs:
        ex = datetime.fromtimestamp(d["date"], tz=timezone.utc).date().isoformat()
        out.append({"ex_date": ex, "pay_date": None, "record_date": None, "decl_date": None,
                    "amount": float(d["amount"]), "type": "Cash", "currency": "USD"})
    return out


def get_dividends(ticker, assetclass="stocks"):
    """Returns {'source':..., 'events':[...]} sorted by ex_date ascending."""
    key = f"div:{assetclass}:{ticker}"
    cached = cache.get(key, DIVIDENDS_TTL)
    if cached is not None:
        return cached
    res = None
    try:
        ev = nasdaq_dividends(ticker, assetclass)
        res = {"source": "nasdaq", "events": ev}
    except Exception as e:
        log.info("nasdaq dividends failed for %s: %s", ticker, e)
        try:
            ev = yahoo_dividends(ticker)
            res = {"source": "yahoo", "events": ev}
        except Exception as e2:
            log.info("yahoo dividends failed for %s: %s", ticker, e2)
            return {"source": None, "events": [], "error": f"{e}; {e2}"}
    res["events"].sort(key=lambda x: x["ex_date"])
    cache.set(key, res)
    return res


# --------------------------------------------------------------------------- prices

def get_price(ticker):
    key = f"price:{ticker}"
    c = cache.get(key, PRICE_TTL)
    if c is not None:
        return c
    price = None
    try:
        res = yahoo_chart(ticker, "5d", "1d")
        price = to_float((res.get("meta") or {}).get("regularMarketPrice"))
    except Exception:
        pass
    if price is None:
        for ac in ("etf", "stocks"):
            try:
                r = http_get(f"https://api.nasdaq.com/api/quote/{ticker.replace('.', '/')}/info?assetclass={ac}",
                             headers=_nasdaq_headers(), timeout=15)
                pd = ((r.json().get("data") or {}).get("primaryData") or {})
                price = to_float(pd.get("lastSalePrice"))
                if price:
                    break
            except Exception:
                pass
    if price is not None:
        cache.set(key, price)
    return price


def get_close_on(ticker, iso_date):
    """Closing price on (or just before) a date."""
    key = f"close:{ticker}:{iso_date}"
    c = cache.get(key, None)
    if c is not None:
        return c
    d = datetime.fromisoformat(iso_date).replace(tzinfo=timezone.utc)
    try:
        res = yahoo_chart(ticker, period1=(d - timedelta(days=7)).timestamp(),
                          period2=(d + timedelta(days=1)).timestamp(), interval="1d")
        closes = (((res.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
        closes = [c for c in closes if c is not None]
        if closes:
            cache.set(key, closes[-1])
            return closes[-1]
    except Exception:
        pass
    return None


def estimate_total_net_assets_from_weights(holdings, max_n=10):
    """Uses shares*price / weight on the biggest positions to estimate fund net assets."""
    top = sorted([h for h in holdings if h.get("weight") and h["weight"] > 0.2 and h.get("ticker")],
                 key=lambda h: -h["weight"])[:max_n]
    ests = []
    for h in top:
        p = get_price(h["ticker"])
        if p:
            ests.append(h["shares"] * p / (h["weight"] / 100.0))
    if len(ests) >= 3:
        return statistics.median(ests)
    return None
