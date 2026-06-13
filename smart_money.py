#!/usr/bin/env python3
"""Polymarket Smart Money Tracker.

Finds wallets that win more than 75% of their resolved bets and bet
multiple times per week. Zero dependencies — Python stdlib only.

Run the dashboard:   python3 smart_money.py            (http://localhost:8899)
Run a terminal scan: python3 smart_money.py --scan
"""

import argparse
import json
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA_API = "https://data-api.polymarket.com"
PORT = 8899

# Scan defaults — adjustable in the UI
DEFAULTS = {
    "pool": 150,           # candidate wallets pulled from the leaderboard
    "min_win_rate": 75.0,  # percent of resolved bets with realizedPnl > 0
    "min_bets_week": 2.0,  # distinct markets traded per week, recent 4 weeks
    "min_resolved": 10,    # resolved bets required (filters 3-for-3 flukes)
    "max_positions": 300,  # most recent resolved positions sampled per wallet
}
FREQ_WEEKS = 4  # window for the bets-per-week measurement


def _ssl_context():
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
        return ctx
    except ImportError:
        pass
    # Some Python installs (notably python.org builds on macOS) ship without
    # usable CA certs. Probe once; fall back to unverified for this
    # read-only public API rather than failing every request.
    try:
        req = urllib.request.Request(DATA_API + "/v1/leaderboard?limit=1",
                                     headers={"User-Agent": "Mozilla/5.0"})
        urllib.request.urlopen(req, timeout=10, context=ctx).read()
        return ctx
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
            print("warning: no usable CA certificates found; SSL verification "
                  "disabled (pip3 install certifi to fix)", file=sys.stderr)
            return ssl._create_unverified_context()
        return ctx


SSL_CTX = _ssl_context()


def get_json(path, params=None, retries=2):
    url = DATA_API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt == retries:
                return None
            time.sleep(1 + attempt)


def leaderboard_candidates(pool):
    """Unique wallets from the 7d/30d/all leaderboards, best PnL first."""
    seen = {}
    for window in ("7d", "30d", "all"):
        offset = 0
        while offset < pool:
            page = get_json("/v1/leaderboard",
                            {"window": window, "limit": 50, "offset": offset})
            if not page:
                break
            for u in page:
                w = u.get("proxyWallet")
                if w and w not in seen:
                    seen[w] = {
                        "wallet": w,
                        "username": u.get("userName") or w[:10] + "...",
                        "leaderboard_pnl": u.get("pnl", 0),
                        "volume": u.get("vol", 0),
                    }
            offset += 50
            if len(page) < 50:
                break
    ranked = sorted(seen.values(), key=lambda u: u["leaderboard_pnl"], reverse=True)
    return ranked[:pool]


def closed_positions(wallet, max_positions):
    """Most recent resolved positions, newest first.

    The API defaults to sorting by realizedPnl descending — without an
    explicit TIMESTAMP sort you get a wallet's biggest *wins* first, which
    inflates every win rate toward 100%. Sort by time so we sample the
    actual recent record.
    """
    out = []
    offset = 0
    while offset < max_positions:
        page = get_json("/closed-positions",
                        {"user": wallet, "limit": 50, "offset": offset,
                         "sortBy": "TIMESTAMP", "sortDirection": "DESC"})
        if not page:
            break
        out.extend(page)
        offset += 50
        if len(page) < 50:
            break
    return out


def recent_trade_frequency(wallet, weeks=FREQ_WEEKS):
    """(trades, distinct markets) over the last `weeks` weeks."""
    cutoff = time.time() - weeks * 7 * 86400
    trades = 0
    markets = set()
    offset = 0
    while offset < 1000:
        page = get_json("/activity",
                        {"user": wallet, "type": "TRADE", "limit": 500, "offset": offset})
        if not page:
            break
        for t in page:
            if t.get("timestamp", 0) >= cutoff:
                trades += 1
                if t.get("conditionId"):
                    markets.add(t["conditionId"])
        offset += 500
        if len(page) < 500 or page[-1].get("timestamp", 0) < cutoff:
            break
    return trades, len(markets)


def analyze_wallet(candidate, max_positions):
    wallet = candidate["wallet"]
    resolved = closed_positions(wallet, max_positions)
    if not resolved:
        return None
    # A bet won if the outcome it held resolved YES. For resolved positions
    # curPrice is binary (1 = won, 0 = lost), so it's a cleaner signal than
    # the sign of realizedPnl — a hedged position can win yet net $0 PnL.
    def won(p):
        return p.get("curPrice", 0) >= 0.5
    wins = sum(1 for p in resolved if won(p))
    realized_pnl = sum(p.get("realizedPnl", 0) for p in resolved)
    trades, markets = recent_trade_frequency(wallet)
    return {
        **candidate,
        "resolved": len(resolved),
        "wins": wins,
        "win_rate": round(wins / len(resolved) * 100, 1),
        "realized_pnl": round(realized_pnl, 2),
        "trades_4w": trades,
        "markets_4w": markets,
        "bets_per_week": round(markets / FREQ_WEEKS, 1),
        "recent": [
            {
                "title": p.get("title", "?"),
                "outcome": p.get("outcome", "?"),
                "avgPrice": p.get("avgPrice", 0),
                "realizedPnl": round(p.get("realizedPnl", 0), 2),
                "won": won(p),
                "timestamp": p.get("timestamp", 0),
            }
            for p in resolved[:15]
        ],
    }


# ── scan state shared with the web UI ──────────────────────────────────────

scan_lock = threading.Lock()
scan_state = {"state": "idle", "done": 0, "total": 0, "results": [], "params": {}}


def run_scan(params):
    with scan_lock:
        scan_state.update(state="scanning", done=0, total=0, results=[], params=params)
    candidates = leaderboard_candidates(params["pool"])
    with scan_lock:
        scan_state["total"] = len(candidates)
    results = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(analyze_wallet, c, params["max_positions"]): c
                   for c in candidates}
        for f in as_completed(futures):
            try:
                r = f.result()
            except Exception:
                r = None
            with scan_lock:
                scan_state["done"] += 1
                if r:
                    results.append(r)
                    scan_state["results"] = sorted(
                        results, key=lambda x: (x["win_rate"], x["realized_pnl"]),
                        reverse=True)
    with scan_lock:
        scan_state["state"] = "done"


def filter_results(results, params):
    return [r for r in results
            if r["win_rate"] >= params["min_win_rate"]
            and r["bets_per_week"] >= params["min_bets_week"]
            and r["resolved"] >= params["min_resolved"]]


# ── web UI ──────────────────────────────────────────────────────────────────

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Polymarket Smart Money</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#e6edf3;--dim:#8b949e;
      --green:#3fb950;--red:#f85149;--accent:#58a6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
     font:14px/1.5 -apple-system,"Segoe UI",sans-serif}
header{padding:18px 24px;border-bottom:1px solid var(--border);
       display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
h1{font-size:18px;margin:0}
header span{color:var(--dim);font-size:13px}
#controls{display:flex;gap:14px;align-items:end;padding:16px 24px;flex-wrap:wrap}
#controls label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--dim)}
#controls input{background:var(--panel);border:1px solid var(--border);color:var(--text);
                border-radius:6px;padding:7px 10px;width:90px;font-size:14px}
button{background:var(--accent);color:#fff;border:0;border-radius:6px;
       padding:9px 22px;font-size:14px;font-weight:600;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
#progress{padding:0 24px 8px;color:var(--dim);font-size:13px}
#bar{height:4px;background:var(--panel);border-radius:2px;margin-top:6px;overflow:hidden}
#bar div{height:100%;width:0;background:var(--accent);transition:width .4s}
main{display:flex;gap:0;min-height:60vh}
#results{flex:1;padding:8px 24px 40px;overflow-x:auto}
table{border-collapse:collapse;width:100%}
th,td{padding:9px 12px;text-align:right;border-bottom:1px solid var(--border);
      white-space:nowrap}
th{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.4px}
th:nth-child(-n+2),td:nth-child(-n+2){text-align:left}
tbody tr{cursor:pointer}
tbody tr:hover{background:var(--panel)}
tr.sel{background:var(--panel)}
.green{color:var(--green)}.red{color:var(--red)}
.addr{color:var(--dim);font-size:12px}
#detail{width:380px;border-left:1px solid var(--border);padding:16px 20px 40px;display:none}
#detail h2{font-size:15px;margin:4px 0 2px}
#detail .addr{word-break:break-all}
#detail a{color:var(--accent);font-size:13px}
.bet{padding:8px 0;border-bottom:1px solid var(--border);font-size:13px}
.bet .t{color:var(--text)}.bet .m{color:var(--dim);font-size:12px}
#empty{color:var(--dim);padding:40px 0;text-align:center}
</style></head><body>
<header><h1>Polymarket Smart Money</h1>
<span>wallets winning &gt;75% of resolved bets, betting multiple times per week</span></header>
<div id="controls">
  <label>Candidate pool<input id="pool" type="number" value="150" min="10" max="600"></label>
  <label>Min win rate %<input id="minwr" type="number" value="75" min="0" max="100"></label>
  <label>Min bets / week<input id="minbw" type="number" value="2" min="0" step="0.5"></label>
  <label>Min resolved bets<input id="minres" type="number" value="10" min="1"></label>
  <button id="go" onclick="startScan()">Scan</button>
</div>
<div id="progress"></div>
<main>
<div id="results"><div id="empty">Hit <b>Scan</b> to pull the leaderboards and
analyze each wallet's resolved bets. A full scan takes a minute or two.</div></div>
<div id="detail"></div>
</main>
<script>
let poll=null, all=[], selected=null;
const $=id=>document.getElementById(id);
const fmt=n=>'$'+Math.abs(n).toLocaleString(undefined,{maximumFractionDigits:0});
const pnl=n=>`<span class="${n>=0?'green':'red'}">${n>=0?'+':'-'}${fmt(n)}</span>`;

function params(){return{pool:+$('pool').value,min_win_rate:+$('minwr').value,
  min_bets_week:+$('minbw').value,min_resolved:+$('minres').value};}

async function startScan(){
  $('go').disabled=true;$('detail').style.display='none';selected=null;
  await fetch('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(params())});
  poll=setInterval(update,1500);update();
}
async function update(){
  const s=await(await fetch('/api/status?'+new URLSearchParams(params()))).json();
  const p=$('progress');
  if(s.state==='scanning'){
    p.innerHTML=`Scanning ${s.done}/${s.total} wallets — ${s.filtered.length} match so far
      <div id="bar"><div style="width:${s.total?100*s.done/s.total:0}%"></div></div>`;
  }else if(s.state==='done'){
    clearInterval(poll);$('go').disabled=false;
    p.textContent=`Done — ${s.filtered.length} of ${s.analyzed} analyzed wallets match your filters.`;
  }
  all=s.filtered;render();
}
function render(){
  if(!all.length){$('results').innerHTML='<div id="empty">No matches yet.</div>';return;}
  let h=`<table><thead><tr><th>#</th><th>Trader</th><th>Win rate</th><th>Record</th>
    <th>Bets/wk</th><th>Trades 4w</th><th>Realized PnL</th></tr></thead><tbody>`;
  all.forEach((r,i)=>{h+=`<tr class="${selected===r.wallet?'sel':''}" onclick="show(${i})">
    <td>${i+1}</td>
    <td><b>${r.username}</b><br><span class="addr">${r.wallet.slice(0,10)}…</span></td>
    <td class="green"><b>${r.win_rate}%</b></td>
    <td>${r.wins}–${r.resolved-r.wins}</td>
    <td>${r.bets_per_week}</td><td>${r.trades_4w}</td>
    <td>${pnl(r.realized_pnl)}</td></tr>`;});
  $('results').innerHTML=h+'</tbody></table>';
}
function show(i){
  const r=all[i];selected=r.wallet;render();
  const d=$('detail');d.style.display='block';
  let h=`<h2>${r.username}</h2><div class="addr">${r.wallet}</div>
    <a href="https://polymarket.com/profile/${r.wallet}" target="_blank">view on Polymarket ↗</a>
    <p>${r.win_rate}% win rate over ${r.resolved} resolved bets ·
    ${r.bets_per_week} bets/wk · realized ${pnl(r.realized_pnl)}</p>
    <h3 style="font-size:13px;color:var(--dim)">Recent resolved bets</h3>`;
  r.recent.forEach(b=>{
    const date=new Date(b.timestamp*1000).toLocaleDateString();
    h+=`<div class="bet"><div class="t">${b.won?'✅':'❌'} ${b.title}</div>
      <div class="m">${b.outcome} @ ${(b.avgPrice*100).toFixed(0)}¢ · ${date} ·
      ${pnl(b.realizedPnl)}</div></div>`;});
  d.innerHTML=h;
}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, ctype="application/json", code=200):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            return self._send(PAGE, "text/html")
        if url.path == "/api/status":
            q = urllib.parse.parse_qs(url.query)
            params = {
                "min_win_rate": float(q.get("min_win_rate", [DEFAULTS["min_win_rate"]])[0]),
                "min_bets_week": float(q.get("min_bets_week", [DEFAULTS["min_bets_week"]])[0]),
                "min_resolved": int(q.get("min_resolved", [DEFAULTS["min_resolved"]])[0]),
            }
            with scan_lock:
                snapshot = dict(scan_state)
                results = list(scan_state["results"])
            return self._send({
                "state": snapshot["state"],
                "done": snapshot["done"],
                "total": snapshot["total"],
                "analyzed": len(results),
                "filtered": filter_results(results, params),
            })
        self._send({"error": "not found"}, code=404)

    def do_POST(self):
        if self.path == "/api/scan":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            params = {**DEFAULTS, **{k: body[k] for k in body if k in DEFAULTS}}
            params["pool"] = int(params["pool"])
            with scan_lock:
                busy = scan_state["state"] == "scanning"
            if not busy:
                threading.Thread(target=run_scan, args=(params,), daemon=True).start()
            return self._send({"started": not busy})
        self._send({"error": "not found"}, code=404)

    def log_message(self, *args):
        pass


def terminal_scan(args):
    params = {**DEFAULTS, "pool": args.pool}
    print(f"Pulling leaderboards (pool={args.pool})...")
    run_scan(params)
    matches = filter_results(scan_state["results"], params)
    print(f"\n{'─' * 78}")
    print(f"  Smart money: win rate ≥ {params['min_win_rate']}%, "
          f"≥ {params['min_bets_week']} bets/wk, ≥ {params['min_resolved']} resolved")
    print(f"{'─' * 78}")
    print(f"{'Trader':<22} {'Win%':>6} {'Record':>9} {'Bets/wk':>8} {'Realized PnL':>15}")
    for r in matches:
        rec = f"{r['wins']}-{r['resolved'] - r['wins']}"
        print(f"{r['username']:<22} {r['win_rate']:>5.1f}% {rec:>9} "
              f"{r['bets_per_week']:>8.1f} ${r['realized_pnl']:>14,.2f}")
    print(f"{'─' * 78}")
    print(f"{len(matches)} of {len(scan_state['results'])} analyzed wallets match.\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scan", action="store_true", help="run once in the terminal")
    ap.add_argument("--pool", type=int, default=DEFAULTS["pool"],
                    help="candidate wallets to analyze (default 150)")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    if args.scan:
        return terminal_scan(args)
    print(f"Polymarket Smart Money dashboard → http://localhost:{args.port}")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
