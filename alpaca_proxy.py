#!/usr/bin/env python3
"""
alpaca_proxy.py  —  local proxy + static server for the Gauntlet dashboard
==========================================================================
FIXED VERSION — token injection for Fyers/Upstox from gauntlet_tokens.json
  ✓ /india/status  — dashboard auto-detects provider
  ✓ Token injection — reads gauntlet_tokens.json and injects real auth
  ✓ /fyers/order, /fyers/positions, /fyers/funds — algo trading endpoints
  ✓ /signal-log    — strategy bridge live signal display
  ✓ /td/ping, /td/run, /td/results — stub (no 404 errors)
  ✓ /anthropic/    — Ask-Claude panel passthrough
  ✓ /bn/           — Binance spot price proxy
  ✓ /bybit/        — Bybit spot price proxy
  ✓ /cg/           — CoinGecko price proxy
  ✓ CORS headers   — includes x-anthropic-key

Run:
    python alpaca_proxy.py
Then open:
    http://127.0.0.1:8787/gauntlet_alpaca_CONFIGURED.html
"""

import argparse, json, os, sys, urllib.parse, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from functools import partial

ALPACA    = "https://data.alpaca.markets"
FYERS     = "https://api-t1.fyers.in"
UPSTOX    = "https://api.upstox.com"
ANTHROPIC = "https://api.anthropic.com"
BINANCE   = "https://api.binance.com"
BYBIT     = "https://api.bybit.com"
COINGECKO = "https://api.coingecko.com"
ALTERN    = "https://api.alternative.me"   # crypto Fear & Greed (no key)

TOKENS_FILE = "gauntlet_tokens.json"

PASS_HEADERS = ("authorization", "apca-api-key-id", "apca-api-secret-key",
                "accept", "content-type")

YF = {"GOLD":"GC=F","SILVER":"SI=F","CRUDE":"CL=F","BRENT":"BZ=F","NATGAS":"NG=F",
      "COPPER":"HG=F","NIFTY":"^NSEI","BANKNIFTY":"^NSEBANK",
      "MCXGOLD":"GC=F","MCXSILVER":"SI=F","MCXNATGAS":"NG=F","MCXCRUDE":"CL=F"}
YF_TF = {"1m":("1m","5d"),"5m":("5m","1mo"),"15m":("15m","1mo"),
         "1h":("60m","3mo"),"1d":("1d","2y")}

_signal_log = []
_positions  = {}
_daily_pnl  = 0.0


def _load_tokens(directory):
    try:
        with open(os.path.join(directory, TOKENS_FILE)) as f:
            return json.load(f)
    except Exception:
        return {}


def _india_auth(directory, kind):
    store = _load_tokens(directory)
    if kind == "fyers":
        fy  = store.get("fyers", {})
        app = fy.get("app_id")       or os.environ.get("FYERS_APP_ID")
        tok = fy.get("access_token") or os.environ.get("FYERS_ACCESS_TOKEN")
        if app and tok:
            return app + ":" + tok
    if kind == "upstox":
        up  = store.get("upstox", {})
        tok = up.get("access_token") or os.environ.get("UPSTOX_ACCESS_TOKEN")
        if tok:
            return "Bearer " + tok
    return None


def _forward(handler, target_url, method="GET", body=None, override=None):
    req_headers = {}
    for k in PASS_HEADERS:
        v = handler.headers.get(k)
        if v:
            req_headers[k.title()] = v
    if handler.headers.get("apca-api-key-id"):
        req_headers["APCA-API-KEY-ID"] = handler.headers["apca-api-key-id"]
    if handler.headers.get("apca-api-secret-key"):
        req_headers["APCA-API-SECRET-KEY"] = handler.headers["apca-api-secret-key"]
    if override:
        req_headers.update(override)
    req_headers.setdefault("User-Agent", "GauntletProxy/1.0")
    try:
        req = urllib.request.Request(target_url, data=body, headers=req_headers, method=method)
        with urllib.request.urlopen(req, timeout=25) as r:
            data  = r.read()
            ctype = r.headers.get("Content-Type", "application/json")
            handler._send(r.status, data, ctype)
    except urllib.error.HTTPError as e:
        data = e.read() or json.dumps({"error": str(e)}).encode()
        handler._send(e.code, data, e.headers.get("Content-Type", "application/json"))
    except Exception as e:
        handler._send(502, json.dumps({"error": "proxy upstream failed",
                                       "detail": str(e), "target": target_url}).encode(),
                      "application/json")


def _yf_bars(symbol, tf):
    try:
        import yfinance as yf
    except ImportError:
        return None, "pip install yfinance"
    ticker   = YF.get(symbol, symbol + ".NS")
    interval, period = YF_TF.get(tf, ("1d", "1y"))
    try:
        df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
    except Exception as e:
        return None, f"yfinance error: {e}"
    if df is None or df.empty:
        return None, f"no data for {ticker} ({tf})"
    bars = []
    for ts, row in df.iterrows():
        try:
            bars.append({"t": int(ts.timestamp()), "o": float(row["Open"]),
                         "h": float(row["High"]),  "l": float(row["Low"]),
                         "c": float(row["Close"]),  "v": float(row.get("Volume", 0) or 0)})
        except Exception:
            continue
    return bars, None


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *a, directory=".", **kw):
        self.directory = directory
        super().__init__(*a, **kw)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization,Accept,Content-Type,"
                         "APCA-API-KEY-ID,APCA-API-SECRET-KEY,"
                         "x-anthropic-key,anthropic-version")

    def _send(self, status, body, ctype="application/json"):
        if isinstance(body, str): body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type",   ctype)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        try:    self.wfile.write(body)
        except BrokenPipeError: pass

    def _json(self, status, obj):
        self._send(status, json.dumps(obj).encode(), "application/json")

    def log_message(self, fmt, *args):
        sys.stderr.write("· " + (fmt % args) + "\n")

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b"{}"

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path.startswith("/anthropic/"):
            return self._anthropic("GET")

        # ── Crypto proxy routes ──────────────────────────────────────────────
        if path.startswith("/bn/"):
            # Strip /bn prefix, forward to Binance
            target = BINANCE + path[3:] + ("?" + urllib.parse.urlparse(self.path).query
                                           if urllib.parse.urlparse(self.path).query else "")
            return _forward(self, target)

        if path.startswith("/bybit/"):
            # Strip /bybit prefix, forward to Bybit
            target = BYBIT + path[6:] + ("?" + urllib.parse.urlparse(self.path).query
                                          if urllib.parse.urlparse(self.path).query else "")
            return _forward(self, target)

        if path.startswith("/cg/"):
            # Strip /cg prefix, forward to CoinGecko
            target = COINGECKO + path[3:] + ("?" + urllib.parse.urlparse(self.path).query
                                              if urllib.parse.urlparse(self.path).query else "")
            return _forward(self, target)
        # ─────────────────────────────────────────────────────────────────────

        if path.startswith(("/v2/", "/v1beta3/", "/v1beta1/", "/fyers/", "/upstox/")):
            return self._route()

        # ── News & sentiment ────────────────────────────────────────────────
        if path == "/fng":
            # crypto Fear & Greed index (no key). ?limit=N supported.
            q = urllib.parse.urlparse(self.path).query
            target = ALTERN + "/fng/" + ("?" + q if q else "?limit=1")
            return _forward(self, target)
        # ─────────────────────────────────────────────────────────────────────

        if path == "/india/status":
            fy = _india_auth(self.directory, "fyers")
            up = _india_auth(self.directory, "upstox")
            return self._json(200, {"fyers": bool(fy), "upstox": bool(up), "ready": bool(fy or up)})

        if path == "/india/bars":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            bars, err = _yf_bars(q.get("symbol", [""])[0], q.get("tf", ["1d"])[0])
            return self._json(200, {"bars": bars} if bars else {"bars": [], "hint": err})

        if path == "/india/quote":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            bars, err = _yf_bars(q.get("symbol", [""])[0], "1d")
            return self._json(200, {"price": bars[-1]["c"]} if bars else {"price": None, "hint": err})

        if path == "/signal-log":
            return self._json(200, {"signals": _signal_log, "positions": _positions, "daily_pnl": _daily_pnl})

        if path == "/td/ping":
            return self._json(200, {"traderdev": False, "error": "strategy_bridge not connected"})
        if path == "/td/results":
            return self._json(200, {"status": "idle", "results": []})

        return self._static(path)

    def do_POST(self):
        path = self.path.rstrip("/")

        if self.path.startswith("/anthropic/"):
            return self._anthropic("POST")

        if path == "/fyers/order":
            auth = _india_auth(self.directory, "fyers")
            if not auth:
                return self._json(400, {"error": "No Fyers token in gauntlet_tokens.json"})
            body = self._read_body()
            req  = urllib.request.Request(
                FYERS + "/api/v3/orders/sync", data=body,
                headers={"Authorization": auth, "Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    return self._json(r.status, json.loads(r.read()))
            except urllib.error.HTTPError as e:
                return self._json(e.code, json.loads(e.read() or b"{}"))
            except Exception as e:
                return self._json(500, {"error": str(e)})

        if path == "/signal-log":
            global _signal_log, _positions, _daily_pnl
            try:
                data = json.loads(self._read_body())
                if "signals"   in data: _signal_log = data["signals"]
                if "positions" in data: _positions  = data["positions"]
                if "daily_pnl" in data: _daily_pnl  = data["daily_pnl"]
                return self._json(200, {"ok": True})
            except Exception as e:
                return self._json(400, {"error": str(e)})

        if path == "/relay":
            try:
                payload = json.loads(self._read_body())
                url  = payload.get("url")
                body = payload.get("payload")

                def _fmt(p):
                    if isinstance(p, dict) and p.get("type") == "signal":
                        return (f"🚦 {str(p.get('side','SIGNAL')).upper()} · {p.get('symbol','?')} "
                                f"{p.get('timeframe','')}\n{p.get('strategyLabel') or p.get('strategy','')} "
                                f"@ {p.get('price','-')}")
                    if isinstance(p, dict):
                        return (f"🔔 {p.get('symbol','?')} @ {p.get('price','-')} "
                                f"{p.get('op','')} {p.get('message','')}").strip()
                    return str(p)

                # Shape the body for the destination so Telegram/Discord webhooks accept it.
                if url and "api.telegram.org" in url:
                    q = urllib.parse.urlparse(url).query
                    if "chat_id" not in q:
                        return self._json(200, {"ok": False,
                            "error": "telegram webhook URL needs ?chat_id=<id> — or better, leave the URL blank "
                                     "and set a telegram block in gauntlet_tokens.json (uses /telegram-alert)"})
                    data = json.dumps({"text": _fmt(body)}).encode()
                elif url and "discord.com/api/webhooks" in url:
                    data = json.dumps({"content": _fmt(body)}).encode()
                else:
                    data = json.dumps(body).encode() if body is not None else b"{}"

                req  = urllib.request.Request(url, data=data,
                        headers={"Content-Type": "application/json", "User-Agent": "GauntletProxy/1.0"},
                        method="POST")
                with urllib.request.urlopen(req, timeout=15) as r:
                    return self._json(200, {"ok": True, "status": r.status})
            except urllib.error.HTTPError as e:
                detail = ""
                try: detail = e.read().decode("utf-8", "replace")
                except Exception: pass
                return self._json(200, {"ok": False, "error": f"HTTP {e.code}: {detail}"})
            except Exception as e:
                return self._json(200, {"ok": False, "error": str(e)})

        if path == "/telegram-alert":
            try:
                payload = json.loads(self._read_body())
                t_type = payload.get("type", "alert")
                symbol = payload.get("symbol", "Unknown")
                price = payload.get("price", "-")
                
                if t_type == "signal":
                    strat = payload.get("strategy", "Strategy")
                    side = str(payload.get("side", "signal")).upper()
                    tf = payload.get("timeframe", "-")
                    msg = f"🚦 DASHBOARD STRATEGY SIGNAL ({side})\nSymbol: {symbol}\nTimeframe: {tf}\nStrategy: {strat}\nPrice: {price}"
                else:
                    msg = f"🔔 DASHBOARD PRICE ALERT\nSymbol: {symbol}\nPrice: {price}\nMessage: {payload.get('message', 'Price level triggered')}"
                
                token, cid = None, None
                tpath = os.path.join(self.directory, "gauntlet_tokens.json")
                if os.path.exists(tpath):
                    with open(tpath) as f:
                        c = json.load(f)
                        if "telegram" in c:
                            token = c["telegram"].get("bot_token")
                            cid = c["telegram"].get("chat_id")
                token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
                cid = cid or os.environ.get("TELEGRAM_CHAT_ID")

                # Don't silently swallow a missing config — tell the dashboard so it can toast it.
                if not (token and cid):
                    return self._json(200, {"ok": False, "sent": False,
                        "error": 'no telegram bot_token/chat_id — add a "telegram": {"bot_token":"...","chat_id":"..."} '
                                 'block to gauntlet_tokens.json (or set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars)'})
                try:
                    url = f"https://api.telegram.org/bot{token}/sendMessage"
                    tpayload = json.dumps({"chat_id": cid, "text": msg}).encode("utf-8")
                    req = urllib.request.Request(url, data=tpayload,
                            headers={"Content-Type": "application/json"}, method="POST")
                    with urllib.request.urlopen(req, timeout=8) as r:
                        r.read()
                    return self._json(200, {"ok": True, "sent": True})
                except urllib.error.HTTPError as e:
                    detail = ""
                    try: detail = e.read().decode("utf-8", "replace")
                    except Exception: pass
                    return self._json(200, {"ok": False, "sent": False,
                        "error": f"telegram HTTP {e.code}: {detail}"})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/td/run":
            return self._json(200, {"ok": True, "status": "strategy_bridge not connected"})

        self._route(method="POST")

    def _anthropic(self, method="POST"):
        key = os.environ.get("ANTHROPIC_API_KEY") or self.headers.get("x-anthropic-key")
        if not key:
            return self._json(400, {"error": "no_key", "message": "Set ANTHROPIC_API_KEY in gauntlet.env"})
        body   = self._read_body() if method == "POST" else None
        target = ANTHROPIC + self.path[len("/anthropic"):]
        hdrs   = {"x-api-key": key, "anthropic-version": "2023-06-01",
                  "content-type": "application/json", "User-Agent": "GauntletProxy/1.0"}
        try:
            req = urllib.request.Request(target, data=body, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=90) as r:
                self._send(r.status, r.read(), r.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as e:
            self._send(e.code, e.read() or b"{}", e.headers.get("Content-Type", "application/json"))
        except Exception as e:
            self._json(502, {"error": "anthropic upstream failed", "detail": str(e)})

    def _route(self, method="GET"):
        full = self.path
        body = self._read_body() if method == "POST" else None

        if full.startswith("/v2/") or full.startswith("/v1beta3/") or full.startswith("/v1beta1/"):
            return _forward(self, ALPACA + full, method, body)

        if full.startswith("/fyers/"):
            auth = _india_auth(self.directory, "fyers")
            ov   = {"Authorization": auth} if auth else None
            return _forward(self, FYERS + full[len("/fyers"):], method, body, override=ov)

        if full.startswith("/upstox/"):
            auth = _india_auth(self.directory, "upstox")
            ov   = {"Authorization": auth} if auth else None
            return _forward(self, UPSTOX + full[len("/upstox"):], method, body, override=ov)

        self._json(404, {"error": "no route", "path": full})

    def _static(self, path):
        if path in ("/", ""):
            for cand in ("gauntlet_alpaca_CONFIGURED.html", "gauntlet_alpaca.html"):
                if os.path.exists(os.path.join(self.directory, cand)):
                    path = "/" + cand; break
        rel = path.lstrip("/")
        fp  = os.path.normpath(os.path.join(self.directory, rel))
        if not fp.startswith(os.path.abspath(self.directory)):
            return self._json(403, {"error": "forbidden"})
        if os.path.isdir(fp):
            items = "".join(f'<li><a href="/{f}">{f}</a></li>' for f in sorted(os.listdir(fp)))
            return self._send(200, f"<h3>Gauntlet proxy</h3><ul>{items}</ul>", "text/html")
        if not os.path.exists(fp):
            return self._json(404, {"error": "not found", "path": path})
        ctype = ("text/html"              if fp.endswith(".html") else
                 "application/javascript" if fp.endswith(".js")   else
                 "text/css"               if fp.endswith(".css")  else
                 "application/octet-stream")
        with open(fp, "rb") as f:
            self._send(200, f.read(), ctype)


def load_dotenv(directory):
    for name in (".env", "gauntlet.env"):
        fp = os.path.join(directory, name)
        if not os.path.exists(fp): continue
        loaded = []
        for line in open(fp, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
                loaded.append(k)
        if loaded:
            print(f"  loaded {len(loaded)} key(s) from {name}: {', '.join(loaded)}")
        return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--dir",  default=".", help="folder containing the dashboard HTML")
    args = ap.parse_args()
    directory = os.path.abspath(args.dir)

    load_dotenv(directory)

    store  = _load_tokens(directory)
    fy_ok  = bool(store.get("fyers",  {}).get("access_token") or os.environ.get("FYERS_ACCESS_TOKEN"))
    up_ok  = bool(store.get("upstox", {}).get("access_token") or os.environ.get("UPSTOX_ACCESS_TOKEN"))

    srv  = ThreadingHTTPServer((args.host, args.port), partial(Handler, directory=directory))
    page = ("gauntlet_alpaca_CONFIGURED.html"
            if os.path.exists(os.path.join(directory, "gauntlet_alpaca_CONFIGURED.html"))
            else "gauntlet_alpaca.html")

    print(f"\nGauntlet proxy  —  serving: {directory}")
    print(f"  Dashboard  ->  http://{args.host}:{args.port}/{page}")
    print(f"  Routes     ->  /v2,/v1beta3 (Alpaca) · /fyers · /upstox · /india · /relay · /anthropic · /signal-log")
    print(f"               · /bn (Binance) · /bybit · /cg (CoinGecko) · /fng (Fear&Greed) · /v1beta1 (Alpaca news)")
    print(f"  Tokens     ->  Fyers={'OK' if fy_ok else 'MISSING'} · Upstox={'OK' if up_ok else 'MISSING'}")
    if not fy_ok and not up_ok:
        print(f"  WARNING: No India tokens found. Charts will use yfinance (delayed).")
    print("  Ctrl-C to stop.\n")

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()