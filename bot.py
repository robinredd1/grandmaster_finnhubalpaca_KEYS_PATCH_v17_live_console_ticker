import os, sys, time, math, asyncio, itertools, collections
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Tuple
import httpx
from config import *
HEADERS_ALPACA = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_API_SECRET}
FINNHUB_BASE = "https://finnhub.io/api/v1"
BOLD="\033[1m"; GREEN="\033[92m"; RED="\033[91m"; RESET="\033[0m"
def bold(s: str) -> str: return f"{BOLD}{s}{RESET}" if BOLD_MONEY_BALANCES else s
def color_day(val: float) -> str:
    try: v=float(val)
    except: return str(val)
    txt=f"{v:+.2f}"
    if not COLOR_DAY_PNL: return bold(txt)
    if v>0: return bold(f"{GREEN}{txt}{RESET}")
    if v<0: return bold(f"{RED}{txt}{RESET}")
    return bold(txt)
def fmt_money(x, make_bold=False):
    try: s=f"${float(x):,.2f}"
    except: s=str(x)
    return bold(s) if make_bold else s
def now_utc(): return datetime.now(timezone.utc)
class RateLimiter:
    def __init__(self, max_calls:int, period:float):
        self.max_calls=max_calls; self.period=period
        self.calls=collections.deque(); self.lock=asyncio.Lock()
    async def acquire(self):
        while True:
            async with self.lock:
                now=time.monotonic()
                while self.calls and now-self.calls[0]>self.period:
                    self.calls.popleft()
                if len(self.calls)<self.max_calls:
                    self.calls.append(now); return
                wait=self.period-(now-self.calls[0])+0.01
            await asyncio.sleep(max(wait,0.05))
async def fetch_symbols_finnhub(client: httpx.AsyncClient):
    url=f"{FINNHUB_BASE}/stock/symbol"; params={"exchange":"US","token":FINNHUB_API_KEY}
    r=await client.get(url, params=params, timeout=60.0); r.raise_for_status()
    data=r.json(); syms=[]
    for d in data:
        s=d.get("symbol")
        if s and s.isupper() and s.isascii(): syms.append(s)
    return sorted(set(syms))
def alpaca_get(s, path, params=None, timeout=60.0):
    r=s.get(ALPACA_BROKER_BASE+path, headers=HEADERS_ALPACA, timeout=timeout, params=params)
    r.raise_for_status(); return r.json()
def alpaca_post(s, path, payload, timeout=40.0):
    r=s.post(ALPACA_BROKER_BASE+path, headers=HEADERS_ALPACA, timeout=timeout, json=payload)
    if r.status_code>=400:
        try: body=r.json()
        except Exception: body={"text": r.text[:400]}
        raise RuntimeError(f"HTTP {r.status_code} {body}")
    return r.json()
def alpaca_delete(s, path, timeout=40.0):
    r=s.delete(ALPACA_BROKER_BASE+path, headers=HEADERS_ALPACA, timeout=timeout)
    if r.status_code>=400:
        try: body=r.json()
        except Exception: body={"text": r.text[:400]}
        raise RuntimeError(f"HTTP {r.status_code} {body}")
    return r.json() if r.text else {}
def alpaca_tradable_set_single(s)->set:
    print("[UNIVERSE] Calling Alpaca /v2/assets (single request)...", flush=True)
    arr=alpaca_get(s,"/v2/assets",params={"status":"active","asset_class":"us_equity"})
    print(f"[UNIVERSE] Assets returned: {len(arr)}", flush=True)
    out=set()
    for a in arr:
        exch=(a.get("exchange") or "").upper(); sym=a.get("symbol") or ""
        if a.get("tradable") and exch in ("NYSE","NASDAQ","AMEX","ARCA") and not sym.endswith(".W"):
            out.add(sym)
    return out
async def account_ticker(alp: httpx.Client):
    last=None
    while True:
        try:
            acct=alpaca_get(alp,"/v2/account")
            equity=float(acct.get("equity") or 0); last_eq=float(acct.get("last_equity") or 0)
            cash=float(acct.get("cash") or 0); bp=float(acct.get("buying_power") or 0)
            day_pnl=equity-last_eq
            snap=(equity,cash,bp,day_pnl)
            dot="•" if snap!=last else "-"
            print(f"[TICK {datetime.now().strftime('%H:%M:%S')}] "
                  f"{bold('EQUITY=')}{fmt_money(equity, True)}  "
                  f"{bold('CASH=')}{fmt_money(cash, True)}  "
                  f"{bold('BP=')}{fmt_money(bp, True)}  "
                  f"{bold('DAYPNL=')}{color_day(day_pnl)}  {dot}")
            last=snap
        except Exception as e:
            print(f"[WARN] tick account: {e}")
        await asyncio.sleep(max(ACCOUNT_TICK_SECONDS,0.5))
async def fetch_quote(client, limiter, sym):
    url=f"{FINNHUB_BASE}/quote"; params={"symbol":sym,"token":FINNHUB_API_KEY}
    await limiter.acquire()
    try:
        r=await client.get(url, params=params, timeout=12.0)
        if r.status_code==429: return sym,{}, "rate"
        if r.status_code!=200: return sym,{}, f"e{r.status_code}"
        q=r.json(); price=float(q.get("c") or 0)
        if price<=0: return sym,{}, "zero"
        return sym,q,""
    except Exception:
        return sym,{}, "err"
async def fetch_batch_quotes(symbols, concurrency, limiter):
    out={}; stats={"rate":0,"err":0,"zero":0,"other":0}
    sem=asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient() as client:
        async def worker(sym):
            async with sem:
                s,q,flag=await fetch_quote(client, limiter, sym)
                if q: out[s]=q
                else:
                    if flag in stats: stats[flag]+=1
                    else: stats["other"]+=1
        await asyncio.gather(*[worker(s) for s in symbols])
    return out, stats
def qty_from_dollars(price,dollars):
    if not price or price<=0: return "0"
    if ALLOW_FRACTIONAL: return f"{max(dollars/price,0.0):.4f}"
    return str(max(int(dollars//price),1))
def snap_to_tick(x): return round(x*100)/100.0 if x>=1.0 else round(x*10000)/10000.0
def limit_price_from_last(last):
    raw=last*(1.0+LIMIT_SLIPPAGE_BPS/10000.0); snapped=snap_to_tick(raw)
    if snapped<last:
        step=0.01 if last>=1.0 else 0.0001; snapped=snap_to_tick(last+step)
    return snapped
def rank_by_momentum(quotes):
    ranked=[]
    for s,q in quotes.items():
        price=float(q.get("c") or 0)
        if price<MIN_PRICE: continue
        day_pct=float(q.get("dp") or 0)
        ranked.append((s,day_pct,day_pct,price))
    ranked.sort(key=lambda x:x[1], reverse=True); return ranked
def qualifies(day_pct,mom): return (mom>=MIN_1MOMENTUM_PCT) and (day_pct>=MIN_DAY_PCT)
def startup_housekeeping(s):
    if CANCEL_ALL_OPEN_ORDERS_ON_START:
        try: alpaca_delete(s,"/v2/orders"); print("[STARTUP] Canceled all open orders.")
        except Exception as e: print(f"[WARN] cancel open orders: {e}")
    if FLATTEN_ON_START:
        try:
            positions=alpaca_get(s,"/v2/positions")
            for p in positions:
                sym=p.get("symbol"); qty=p.get("qty")
                if sym and qty:
                    try:
                        alpaca_post(s,"/v2/orders",{"symbol":sym,"qty":qty,"side":"sell","type":"market",
                                                    "time_in_force":"day","extended_hours":bool(USE_EXTENDED_HOURS_EXITS)})
                        print(f"[STARTUP] Flattened {sym} qty={qty}")
                    except Exception as e: print(f"[WARN] flatten {sym}: {e}")
        except Exception as e: print(f"[WARN] flatten positions: {e}")
def fetch_open_orders_map(s):
    try: oo=alpaca_get(s,"/v2/orders", params={"status":"open","limit":500})
    except Exception as e: print(f"[WARN] open orders: {e}"); return {}
    return {o.get("symbol"): True for o in oo if o.get("side")=="sell"}
def ensure_trailing_stops(s, positions):
    has_sell=fetch_open_orders_map(s)
    for p in positions:
        sym=p.get("symbol"); qty=p.get("qty")
        if sym and qty and not has_sell.get(sym):
            try:
                alpaca_post(s,"/v2/orders",{"symbol":sym,"qty":qty,"side":"sell","type":"trailing_stop",
                                            "time_in_force":"day","trail_percent":TRAIL_PERCENT,
                                            "extended_hours":bool(USE_EXTENDED_HOURS_EXITS)})
                print(f"[EXIT] Trailing stop for {sym} {TRAIL_PERCENT}%")
            except Exception as e:
                msg=str(e)
                if "insufficient qty" in msg or "held_for_orders" in msg:
                    print(f"[INFO] trailing stop exists for {sym} (skipping duplicate)")
                else:
                    print(f"[WARN] trailing stop {sym}: {e}")
async def main():
    print("=== Grandmaster Finnhub + Alpaca — PATCH v17 (live console ticker) ===")
    alp=httpx.Client()
    startup_housekeeping(alp)
    # start ticker in background
    asyncio.create_task(account_ticker(alp))
    # Universe
    print("[UNIVERSE] Fetching Finnhub symbols...")
    async with httpx.AsyncClient() as ac:
        data=await fetch_symbols_finnhub(ac)
    print(f"[UNIVERSE] Finnhub symbols: {len(data)}")
    print("[UNIVERSE] Fetching Alpaca tradable symbols...")
    tradable = set()
    try:
        tradable = set([a for a in alpaca_tradable_set_single(alp)])
    except Exception as e:
        print(f"[WARN] tradable set: {e}")
    syms = sorted(set(data).intersection(tradable)) if tradable else data
    print(f"[UNIVERSE] Intersection (scannable): {len(syms)}")
    # batching
    def batches(seq,n):
        it=iter(seq)
        while True:
            chunk=tuple(itertools.islice(it,n))
            if not chunk: return
            yield chunk
    batch_iter = itertools.cycle(list(batches(syms, SCAN_BATCH_SIZE)))
    limiter=RateLimiter(FINNHUB_MAX_CALLS_PER_MIN,60.0)
    loop_counter=0; opened_at={}; timed_out={}
    while True:
        loop_counter+=1
        # positions/orders heartbeat
        try: positions = alpaca_get(alp,"/v2/positions")
        except Exception as e: print(f"[WARN] positions: {e}"); positions=[]
        try: open_orders = alpaca_get(alp,"/v2/orders", params={"status":"open","limit":500})
        except Exception as e: print(f"[WARN] open orders: {e}"); open_orders=[]
        print(f"[HEARTBEAT] {datetime.now().strftime('%H:%M:%S')} | positions={len(positions)} open_orders={len(open_orders)}")
        # time exits
        now=now_utc()
        for p in positions:
            sym=p.get("symbol")
            if sym not in opened_at: opened_at[sym]=now
        active={p.get("symbol") for p in positions}
        for s in list(opened_at.keys()):
            if s not in active: opened_at.pop(s,None); timed_out.pop(s,None)
        if TIME_EXIT_MINUTES and TIME_EXIT_MINUTES>0:
            cutoff=now-timedelta(minutes=TIME_EXIT_MINUTES)
            for p in positions:
                sym=p.get("symbol"); qty=p.get("qty")
                if sym in opened_at and opened_at[sym]<=cutoff and not timed_out.get(sym):
                    try:
                        alpaca_post(alp,"/v2/orders",{"symbol":sym,"qty":qty,"side":"sell","type":"market",
                                                      "time_in_force":"day","extended_hours":bool(USE_EXTENDED_HOURS_EXITS)})
                        print(f"[TIME-EXIT] Market sell {sym} qty={qty}")
                    except Exception as e: print(f"[WARN] time-exit {sym}: {e}")
                    timed_out[sym]=True
        ensure_trailing_stops(alp, positions)
        # scan
        batch = next(batch_iter)
        print(f"[SCAN] {len(batch)} symbols @ {datetime.now().strftime('%H:%M:%S')}")
        quotes, qstats = await fetch_batch_quotes(list(batch), CONCURRENCY, limiter)
        if not quotes:
            print(f"[WARN] Empty batch (rate={qstats['rate']} err={qstats['err']} zero={qstats['zero']})")
            time.sleep(BASE_SCAN_DELAY); continue
        if sum(qstats.values())>0:
            print(f"[INFO] partial batch ok (rate={qstats['rate']} err={qstats['err']} zero={qstats['zero']})")
        # rank
        ranked = rank_by_momentum(quotes)
        ranked = [(s,m,dp,pr) for (s,m,dp,pr) in ranked if qualifies(dp, m)]
        if not ranked and FORCE_BUY_ON_FIRST_PASS and loop_counter<3:
            for s,q in list(quotes.items())[:TAKE_PER_SCAN]:
                p=float(q.get("c") or 0)
                if p>=MIN_PRICE: ranked.append((s,0.0,float(q.get("dp") or 0), p))
        if ranked:
            print("[TOP]", " | ".join([f"{s}: day {d:+.2f}% @ {p:.4f}" for s,m,d,p in ranked[:10]]))
        else:
            print("[TOP] No qualifiers.")
        # entries
        slots = max(MAX_OPEN_POSITIONS - len(positions), 0)
        to_take = min(slots, TAKE_PER_SCAN)
        if to_take>0 and ranked:
            picks = ranked[:to_take]
            for s,m,d,p in picks:
                qty = "0"
                try:
                    qty = str(max(int(DOLLARS_PER_TRADE//max(p,1e-6)),1)) if not ALLOW_FRACTIONAL else f"{DOLLARS_PER_TRADE/max(p,1e-6):.4f}"
                except Exception: pass
                lim = max(p, 0.0001)
                try:
                    alpaca_post(alp,"/v2/orders",{"symbol":s,"qty":qty,"side":"buy","type":"limit",
                                                  "time_in_force":"day","limit_price":lim,
                                                  "extended_hours":bool(USE_EXTENDED_HOURS_ENTRIES)})
                    print(f"[ENTRY] {s} qty={qty} lim={lim:.4f} | day {d:+.2f}%")
                except Exception as e:
                    print(f"[ERR] order {s}: {e}")
        time.sleep(BASE_SCAN_DELAY)
if __name__=="__main__":
    try:
        import asyncio; asyncio.run(main())
    except KeyboardInterrupt:
        print("Exiting...")
