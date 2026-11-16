"""End-to-end test with all network calls mocked (realistic payload shapes)."""
import io
import json
import os
import sys
import tempfile
import time
from datetime import date, timedelta

os.environ["CACHE_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openpyxl  # noqa: E402

import data_sources as ds  # noqa: E402

T = date.today()


def d(n):  # date n days from today, MM/DD/YYYY like nasdaq
    return (T + timedelta(days=n)).strftime("%m/%d/%Y")


class R:
    def __init__(self, status=200, content=b"", js=None):
        self.status_code = status
        self.content = content if js is None else json.dumps(js).encode()
        self.text = self.content.decode("utf-8", "ignore")
        self._js = js

    def json(self):
        if self._js is None:
            return json.loads(self.text)
        return self._js


def spdr_xlsx():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Fund Name:", "SPDR S&P 500 ETF Trust"])
    ws.append(["Ticker Symbol:", "SPY"])
    ws.append(["Holdings:", "As of " + T.strftime("%d-%b-%Y")])
    ws.append([])
    ws.append(["Name", "Ticker", "Identifier", "SEDOL", "Weight", "Sector", "Shares Held", "Local Currency"])
    ws.append(["APPLE INC", "AAPL", "037833100", "2046251", 7.0, "IT", 175000000, "USD"])
    ws.append(["MICROSOFT CORP", "MSFT", "594918104", "2588173", 6.5, "IT", 90000000, "USD"])
    ws.append(["COCA COLA CO", "KO", "191216100", "2206657", 0.5, "Staples", 150000000, "USD"])
    ws.append(["BERKSHIRE HATHAWAY INC CL B", "BRK.B", "084670702", "", 1.7, "Fin", 22000000, "USD"])
    ws.append(["US DOLLAR", "-", "", "", 0.1, "", 500000, "USD"])
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


NPORT = f"""<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/nport" xmlns:com="http://www.sec.gov/edgar/common">
 <formData>
  <genInfo><seriesName>Schwab U.S. Dividend Equity ETF</seriesName><seriesId>S000033459</seriesId>
   <repPdEnd>{(T - timedelta(days=60)).isoformat()}</repPdEnd><repPdDate>{(T - timedelta(days=60)).isoformat()}</repPdDate></genInfo>
  <fundInfo><totAssets>70000000000</totAssets><netAssets>69000000000</netAssets></fundInfo>
  <invstOrSecs>
   <invstOrSec><name>Coca-Cola Co/The</name><lei>x</lei><title>Coca-Cola Co/The</title><cusip>191216100</cusip>
     <identifiers><isin value="US1912161007"/><ticker value="KO"/></identifiers>
     <balance>25000000</balance><units>NS</units><curCd>USD</curCd><valUSD>1700000000</valUSD><pctVal>2.46</pctVal>
     <payoffProfile>Long</payoffProfile><assetCat>EC</assetCat><issuerCat>CORP</issuerCat><invCountry>US</invCountry></invstOrSec>
   <invstOrSec><name>PepsiCo Inc</name><lei>x</lei><title>PepsiCo Inc</title><cusip>713448108</cusip>
     <identifiers><isin value="US7134481081"/></identifiers>
     <balance>11000000</balance><units>NS</units><curCd>USD</curCd><valUSD>1650000000</valUSD><pctVal>2.39</pctVal>
     <payoffProfile>Long</payoffProfile><assetCat>EC</assetCat><issuerCat>CORP</issuerCat><invCountry>US</invCountry></invstOrSec>
   <invstOrSec><name>Exxon Mobil Corp</name><lei>x</lei><title>Exxon</title><cusip>30231G102</cusip>
     <identifiers><isin value="US30231G1022"/></identifiers>
     <balance>14000000</balance><units>NS</units><curCd>USD</curCd><valUSD>1600000000</valUSD><pctVal>2.3</pctVal>
     <payoffProfile>Long</payoffProfile><assetCat>EC</assetCat><issuerCat>CORP</issuerCat><invCountry>US</invCountry></invstOrSec>
   <invstOrSec><name>State Street Instl US Govt MMF</name><lei>x</lei><title>MMF</title><cusip>857492706</cusip>
     <identifiers><isin value="US8574927062"/></identifiers>
     <balance>50000000</balance><units>NS</units><curCd>USD</curCd><valUSD>50000000</valUSD><pctVal>0.07</pctVal>
     <payoffProfile>Long</payoffProfile><assetCat>STIV</assetCat><issuerCat>RF</issuerCat><invCountry>US</invCountry></invstOrSec>
  </invstOrSecs>
 </formData>
</edgarSubmission>""".encode()

ATOM = b"""<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><content type="text/xml"><accession-number>0001752724-25-111111</accession-number>
<filing-date>2025-05-28</filing-date><filing-href>https://www.sec.gov/Archives/edgar/data/1454889/000175272425111111/0001752724-25-111111-index.htm</filing-href><filing-type>NPORT-P</filing-type></content></entry>
<entry><content type="text/xml"><accession-number>0001752724-25-222222</accession-number>
<filing-date>2025-08-27</filing-date><filing-href>https://www.sec.gov/Archives/edgar/data/1454889/000175272425222222/0001752724-25-222222-index.htm</filing-href><filing-type>NPORT-P</filing-type></content></entry>
</feed>"""


def nasdaq_rows(amount, freq_days, first_offset, n=6, pay_lag=14):
    rows = []
    for i in range(n):
        ex = first_offset - i * freq_days
        rows.append({"exOrEffDate": d(ex), "type": "Cash", "amount": f"${amount:.2f}",
                     "declarationDate": d(ex - 20), "recordDate": d(ex), "paymentDate": d(ex + pay_lag), "currency": "USD"})
    return {"data": {"dividends": {"rows": rows}}, "message": None, "status": {"rCode": 200}}


NASDAQ = {
    "AAPL": nasdaq_rows(0.26, 91, 10),       # next ex in 10 days (declared)
    "MSFT": nasdaq_rows(0.91, 91, -5),       # paid ~9 days from now
    "KO": {"data": {"dividends": {"rows": None}}},
    "PEP": nasdaq_rows(1.4225, 91, -20),
    "BRK.B": {"data": {"dividends": {"rows": []}}},
    "SPY": nasdaq_rows(1.80, 91, -30),
    "SCHD": nasdaq_rows(0.26, 91, -55),
}
CALLS = []


def fake_get(url, headers=None, timeout=20, **kw):
    CALLS.append(url)
    if "ssga.com" in url:
        return R(200, spdr_xlsx()) if "-spy.xlsx" in url else R(404, b"<html>not found</html>")
    if "invesco.com" in url:
        return R(200, b"Fund Ticker,Security Identifier,Holding Ticker\n")
    if "vanguard.com" in url:
        return R(404, b"")
    if url.endswith("/files/company_tickers.json"):
        return R(js={"0": {"cik_str": 34088, "ticker": "XOM", "title": "EXXON MOBIL CORP"}, "1": {"cik_str": 1, "ticker": "KO", "title": "COCA COLA CO"}})
    if "company_tickers_mf.json" in url:
        return R(js={"fields": ["cik", "seriesId", "classId", "symbol"],
                     "data": [[1454889, "S000033459", "C000102850", "SCHD"]]})
    if "efts.sec.gov" in url:
        assert "S000033459" in url
        return R(js={"hits": {"hits": [
            {"_id": "0001752724-25-111111:primary_doc.xml", "_source": {"adsh": "0001752724-25-111111", "file_date": "2025-05-28", "form": "NPORT-P", "ciks": ["0001454889"]}},
            {"_id": "0001752724-25-222222:primary_doc.xml", "_source": {"adsh": "0001752724-25-222222", "file_date": "2025-08-27", "form": "NPORT-P", "ciks": ["0001454889"]}}]}})
    if "browse-edgar" in url:
        assert "S000033459" in url
        return R(200, ATOM)
    if url.endswith("000175272425222222/primary_doc.xml"):
        return R(200, NPORT)
    if "api.nasdaq.com" in url and "/dividends" in url:
        sym = url.split("/quote/")[1].split("/")[0]
        if sym == "XOM":
            return R(403, b"blocked")
        return R(js=NASDAQ.get(sym, {"data": None}))
    if "finance.yahoo.com" in url:
        sym = url.split("/chart/")[1].split("?")[0]
        params = kw.get("params") or {}
        prices = {"AAPL": 230.0, "MSFT": 420.0, "KO": 68.0, "BRK-B": 470.0, "SPY": 600.0, "SCHD": 27.5, "XOM": 115.0, "PEP": 150.0}
        res = {"meta": {"regularMarketPrice": prices.get(sym, 50.0)},
               "indicators": {"quote": [{"close": [prices.get(sym, 50.0) * 0.98, None]}]}}
        if sym in ("XOM", "KO") and params.get("events") == "div":
            base = time.time()
            res["events"] = {"dividends": {str(int(base - k * 91 * 86400)): {"amount": 0.99, "date": int(base - k * 91 * 86400 - 3 * 86400)} for k in range(5)}}
        return R(js={"chart": {"result": [res]}})
    return R(404, b"")


class FakePostResp(R):
    pass


def fake_post(url, json=None, headers=None, timeout=30):
    assert "openfigi" in url
    m = {"713448108": "PEP"}
    return FakePostResp(js=[{"data": [{"ticker": m[j["idValue"]], "exchCode": "US"}]} if j["idValue"] in m else {"warning": "No identifier found."} for j in json])


ds.http_get = fake_get
ds.requests.get = fake_get
ds._session.post = fake_post
_real_sleep = time.sleep
ds.time = type("T", (), {"sleep": staticmethod(lambda s: None), "time": staticmethod(time.time)})

import app as appmod  # noqa: E402


def run(ticker):
    c = appmod.app.test_client()
    r = c.post("/api/analyze", json={"ticker": ticker, "max_holdings": 600})
    assert r.status_code == 200, r.data
    jid = r.get_json()["job_id"]
    for _ in range(200):
        s = c.get(f"/api/job/{jid}").get_json()
        if s["status"] != "running":
            break
        time.sleep(0.05)
    assert s["status"] == "done", s
    return s["result"]


def test_spy():
    r = run("SPY")
    assert r["holdings_source"].startswith("State Street")
    tick = {h["ticker"]: h for h in r["holdings"]}
    assert set(tick) == {"AAPL", "MSFT", "KO", "BRK.B"}, tick.keys()
    assert abs(tick["AAPL"]["ttm_div_per_share"] - 0.26 * 4) < 1e-9, tick["AAPL"]
    assert tick["KO"]["div_source"] == "yahoo" and tick["KO"]["ttm_div_per_share"] > 0, tick["KO"]
    assert r["shares_outstanding"] and r["shares_outstanding"] > 0
    kinds = {e["kind"] for e in r["events"]}
    assert {"paid", "declared", "projected"} <= kinds, kinds
    # fund amount = amount * shares
    e = next(e for e in r["events"] if e["ticker"] == "MSFT")
    assert abs(e["fund_amount"] - 0.91 * 90000000) < 1
    print("SPY ok: SO=%.0f via %s; fund TTM=%.0f; per share=%.4f; ETF paid %.2f" % (
        r["shares_outstanding"], r["shares_outstanding_method"], r["fund_ttm_income"], r["per_share_ttm"], r["etf_ttm_distribution"]))
    return r


def test_schd():
    r = run("SCHD")
    assert "N-PORT" in r["holdings_source"], r["holdings_source"]
    tick = {h["ticker"]: h for h in r["holdings"]}
    assert set(tick) == {"KO", "PEP", "XOM"}, tick.keys()  # MMF excluded
    assert tick["XOM"]["div_source"] == "yahoo"
    assert any(e["pay_est"] for e in r["events"] if e["ticker"] == "XOM")
    expected_so = 69e9 / (27.5 * 0.98)
    assert abs(r["shares_outstanding"] - expected_so) / expected_so < 1e-6
    print("SCHD ok: SO=%.0f; fund TTM=%.0f; per share=%.4f; warnings=%s" % (
        r["shares_outstanding"], r["fund_ttm_income"], r["per_share_ttm"], r["warnings"]))
    return r


if __name__ == "__main__":
    spy = test_spy()
    schd = test_schd()
    c = appmod.app.test_client()
    assert c.get("/").status_code == 200
    assert c.post("/api/analyze", json={"ticker": "bad ticker!"}).status_code == 400
    json.dump({"SPY": spy, "SCHD": schd}, open(os.path.join(os.path.dirname(__file__), "sample_results.json"), "w"))
    print("all tests passed")
