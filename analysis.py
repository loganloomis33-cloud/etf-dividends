"""Turns holdings + dividend histories into a fund dividend calendar."""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import data_sources as ds

log = logging.getLogger("etf.analysis")

WORKERS = 6
EST_PAY_LAG_DAYS = 14
LOOKBACK_DAYS = 400


def _shift_year(iso):
    d = date.fromisoformat(iso)
    try:
        return d.replace(year=d.year + 1).isoformat()
    except ValueError:  # Feb 29
        return (d + timedelta(days=365)).isoformat()


def _rank_key(h):
    return -(h.get("value") or 0), -(h.get("weight") or 0), -(h.get("shares") or 0)


def shares_outstanding(h, etf):
    """Best-effort ETF shares outstanding. Returns (shares, method, etf_price)."""
    price_now = ds.get_price(etf)
    if h.get("net_assets") and h.get("as_of"):
        p = ds.get_close_on(etf, h["as_of"]) or price_now
        if p:
            return h["net_assets"] / p, f"net assets ${h['net_assets']:,.0f} ÷ ETF price ${p:,.2f} on {h['as_of']}", price_now or p
    if not price_now:
        return None, "ETF price unavailable", None
    vals = [x for x in h["holdings"] if x.get("value")]
    if len(vals) > 0.8 * len(h["holdings"]) and vals:
        total = sum(x["value"] for x in vals)
        wsum = sum(x.get("weight") or 0 for x in vals)
        if 50 < wsum < 150:
            total = total / (wsum / 100.0)
        return total / price_now, f"holdings market value ${total:,.0f} ÷ ETF price ${price_now:,.2f}", price_now
    total = ds.estimate_total_net_assets_from_weights(h["holdings"])
    if total:
        return total / price_now, f"estimated net assets ${total:,.0f} (from top holdings' weights) ÷ ETF price ${price_now:,.2f}", price_now
    return None, "could not estimate", price_now


def analyze(etf, max_holdings=600, progress=lambda done, total, msg: None):
    etf = etf.upper().strip()
    today = date.today()
    today_s = today.isoformat()
    cutoff = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    ttm_start = (today - timedelta(days=365)).isoformat()
    next12_end = (today + timedelta(days=365)).isoformat()

    progress(0, 1, "Fetching holdings…")
    h = ds.get_holdings(etf)
    warnings = []

    eq, skipped = [], []
    for x in h["holdings"]:
        if not x.get("shares") or x["shares"] <= 0 or (x.get("asset") == "cash"):
            continue
        if not x.get("ticker"):
            skipped.append({"name": x["name"], "shares": x["shares"], "reason": "no US ticker (foreign listing or unmapped)"})
            continue
        eq.append(x)
    # merge duplicate tickers
    merged = {}
    for x in eq:
        m = merged.get(x["ticker"])
        if m:
            m["shares"] += x["shares"]
            m["value"] = (m.get("value") or 0) + (x.get("value") or 0) or None
            m["weight"] = (m.get("weight") or 0) + (x.get("weight") or 0) or None
        else:
            merged[x["ticker"]] = dict(x)
    eq = sorted(merged.values(), key=_rank_key)
    if len(eq) > max_holdings:
        for x in eq[max_holdings:]:
            skipped.append({"name": x["name"], "ticker": x["ticker"], "shares": x["shares"], "reason": f"beyond top {max_holdings} holdings limit"})
        warnings.append(f"Only the top {max_holdings} of {len(eq)} holdings were analyzed (raise the limit to include all).")
        eq = eq[:max_holdings]

    # ---- dividends, concurrently
    total = len(eq) + 2
    done = 0
    divs = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(ds.get_dividends, x["ticker"]): x["ticker"] for x in eq}
        for f in as_completed(futs):
            t = futs[f]
            try:
                divs[t] = f.result()
            except Exception as e:  # pragma: no cover
                divs[t] = {"source": None, "events": [], "error": str(e)}
            done += 1
            if done % 3 == 0 or done == len(eq):
                progress(done, total, f"Dividend data {done}/{len(eq)} ({t})")

    progress(done, total, "Estimating ETF shares outstanding…")
    so, so_method, etf_price = shares_outstanding(h, etf)
    done += 1
    progress(done, total, "Fetching the ETF's own distributions…")
    etf_div = ds.get_dividends(etf, "etf")
    if not etf_div.get("events"):
        etf_div = ds.get_dividends(etf, "stocks")

    # ---- events
    events, holdings_out = [], []
    src_count = {"nasdaq": 0, "yahoo": 0, None: 0}
    for x in eq:
        d = divs.get(x["ticker"]) or {"events": [], "source": None}
        src_count[d.get("source")] = src_count.get(d.get("source"), 0) + 1
        evs = [e for e in d["events"] if e["ex_date"] >= cutoff]
        tick_events = []
        for e in evs:
            pay = e.get("pay_date")
            est = False
            if not pay:
                pay = (date.fromisoformat(e["ex_date"]) + timedelta(days=EST_PAY_LAG_DAYS)).isoformat()
                est = True
            kind = "paid" if pay <= today_s else "declared"
            tick_events.append({
                "ticker": x["ticker"], "name": x["name"], "ex_date": e["ex_date"], "pay_date": pay,
                "pay_est": est, "record_date": e.get("record_date"), "decl_date": e.get("decl_date"),
                "amount": e["amount"], "type": e.get("type") or "Cash", "kind": kind,
                "fund_amount": e["amount"] * x["shares"],
            })
        # projections: repeat last year's regular payments that haven't been declared yet
        max_ex = max([e["ex_date"] for e in tick_events], default=None)
        for e in list(tick_events):
            if e["ex_date"] < ttm_start or "special" in e["type"].lower():
                continue
            nex = _shift_year(e["ex_date"])
            if nex <= today_s or (max_ex and nex <= (date.fromisoformat(max_ex) + timedelta(days=20)).isoformat()):
                continue
            if nex > next12_end:
                continue
            tick_events.append({**e, "ex_date": nex, "pay_date": _shift_year(e["pay_date"]),
                                "record_date": None, "decl_date": None, "kind": "projected"})
        events.extend(tick_events)

        ttm = sum(e["amount"] for e in tick_events if e["kind"] != "projected" and ttm_start <= e["ex_date"] <= today_s)
        upcoming = sorted([e for e in tick_events if e["pay_date"] > today_s and e["kind"] != "projected"], key=lambda e: e["pay_date"])
        holdings_out.append({
            "ticker": x["ticker"], "name": x["name"], "shares": x["shares"],
            "weight": x.get("weight"), "value": x.get("value"),
            "div_source": d.get("source"), "ttm_div_per_share": ttm,
            "fund_ttm_income": ttm * x["shares"],
            "next_ex": upcoming[0]["ex_date"] if upcoming else None,
            "next_pay": upcoming[0]["pay_date"] if upcoming else None,
            "next_amount": upcoming[0]["amount"] if upcoming else None,
        })

    if src_count.get("yahoo"):
        warnings.append(f"{src_count['yahoo']} holdings used Yahoo data, which has no pay dates — those pay dates are estimated (ex-date + {EST_PAY_LAG_DAYS} days).")
    if src_count.get(None):
        warnings.append(f"{src_count[None]} holdings had no dividend data available (many simply don't pay dividends).")
    if "N-PORT" in h.get("source", ""):
        warnings.append(f"Holdings come from the fund's latest SEC N-PORT filing (portfolio as of {h.get('as_of')}). Share counts may have drifted since.")

    fund_ttm = sum(e["fund_amount"] for e in events if e["kind"] != "projected" and ttm_start <= e["ex_date"] <= today_s)
    etf_events = [e for e in (etf_div.get("events") or []) if e["ex_date"] >= cutoff]
    etf_ttm = sum(e["amount"] for e in etf_events if ttm_start <= e["ex_date"] <= today_s)

    return {
        "etf": etf,
        "fund_name": h.get("fund_name"),
        "holdings_source": h.get("source"),
        "holdings_as_of": h.get("as_of"),
        "filing_url": h.get("filing_url"),
        "etf_price": etf_price,
        "shares_outstanding": so,
        "shares_outstanding_method": so_method,
        "analyzed_count": len(eq),
        "total_positions": len(h["holdings"]),
        "fund_ttm_income": fund_ttm,
        "per_share_ttm": (fund_ttm / so) if so else None,
        "etf_distributions": etf_events,
        "etf_ttm_distribution": etf_ttm,
        "events": sorted(events, key=lambda e: (e["pay_date"], e["ticker"])),
        "holdings": sorted(holdings_out, key=lambda r: -r["fund_ttm_income"]),
        "skipped": skipped,
        "warnings": warnings,
        "generated": today_s,
    }
