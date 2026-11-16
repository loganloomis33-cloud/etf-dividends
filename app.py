import logging
import os
import re
import threading
import time
import uuid

from flask import Flask, jsonify, request, send_from_directory

import analysis
import cache

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("etf.app")

app = Flask(__name__, static_folder="static", static_url_path="/static")

RESULT_TTL = 6 * 3600
MAX_LIMIT = int(os.environ.get("MAX_HOLDINGS_LIMIT", "4000"))
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

_jobs = {}
_jobs_lock = threading.Lock()
_running = {}  # (etf, limit) -> job_id, so duplicate requests share one job


def _run(job_id, etf, limit):
    job = _jobs[job_id]

    def progress(done, total, msg):
        job.update(done=done, total=max(total, 1), message=msg)

    try:
        res = analysis.analyze(etf, limit, progress)
        cache.set(f"result:{etf}:{limit}", res)
        job.update(status="done", result=res, message="Done")
    except Exception as e:
        log.exception("analysis failed for %s", etf)
        job.update(status="error", message=str(e))
    finally:
        with _jobs_lock:
            _running.pop((etf, limit), None)


@app.post("/api/analyze")
def start():
    body = request.get_json(silent=True) or {}
    etf = str(body.get("ticker", "")).upper().strip()
    if not TICKER_RE.match(etf):
        return jsonify(error="Enter a valid ETF ticker"), 400
    try:
        limit = max(1, min(int(body.get("max_holdings") or 600), MAX_LIMIT))
    except (TypeError, ValueError):
        limit = 600
    refresh = bool(body.get("refresh"))

    if not refresh:
        cached = cache.get(f"result:{etf}:{limit}", RESULT_TTL)
        if cached:
            jid = uuid.uuid4().hex
            _jobs[jid] = {"status": "done", "result": cached, "done": 1, "total": 1, "message": "Cached", "ts": time.time()}
            return jsonify(job_id=jid)
    else:
        cache.set(f"holdings:{etf}", None)

    with _jobs_lock:
        existing = _running.get((etf, limit))
        if existing and existing in _jobs:
            return jsonify(job_id=existing)
        jid = uuid.uuid4().hex
        _jobs[jid] = {"status": "running", "done": 0, "total": 1, "message": "Starting…", "ts": time.time()}
        _running[(etf, limit)] = jid
    threading.Thread(target=_run, args=(jid, etf, limit), daemon=True).start()
    _gc()
    return jsonify(job_id=jid)


@app.get("/api/job/<jid>")
def job(jid):
    j = _jobs.get(jid)
    if not j:
        return jsonify(error="Unknown job (the server may have restarted) — please run it again."), 404
    out = {k: v for k, v in j.items() if k != "result"}
    if j.get("status") == "done":
        out["result"] = j["result"]
    return jsonify(out)


def _gc():
    cutoff = time.time() - 3600
    for k in [k for k, v in _jobs.items() if v.get("ts", 0) < cutoff and v.get("status") != "running"]:
        _jobs.pop(k, None)


@app.get("/healthz")
def health():
    return "ok"


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
