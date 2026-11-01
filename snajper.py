"""Snajper: łapie nowe tokeny z pump.fun w pierwszych minutach i sam z nich wychodzi.

Nowe tokeny przychodzą na żywo z darmowego strumienia PumpPortal, a ceny co kilka sekund z Jupitera
(0 kredytów Helius). Domyślnie wszystko dzieje się na sucho: bot zapisuje, co by kupił i sprzedał, po cenie
z chwili, w której naprawdę zdążyłby to zrobić, i odejmuje koszty.

Z flagą --naprawde handluje sam z osobnego portfela snajpera (~/.config/wieloryby/snajper.json), bez pytania
o każdą transakcję, ale w twardych limitach z .env: stawka, liczba otwartych pozycji, zakupy i strata na dobę.
Plik dane/snajper.STOP wstrzymuje nowe zakupy (otwarte pozycje dalej są pilnowane i sprzedawane).
"""
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime, timezone

import websockets

import agent
from agent import SOL, cfg, fmt_usd, jup_get
from czuwaj import notify

STREAM = "wss://pumpportal.fun/api/data"
RESULTS = agent.DATA / "snajper-wyniki.jsonl"   # zamknięte pozycje, jedna na linię
OPEN = agent.DATA / "snajper-otwarte.json"      # otwarte pozycje (prawdziwe wznawiamy po restarcie)
STOP_FILE = agent.DATA / "snajper.STOP"

POLL_S = cfg("SNAJPER_POLL_S", 3.0)              # co ile sekund pytamy Jupitera o ceny
WINDOW_S = cfg("SNAJPER_OKNO_S", 180.0)          # tyle sekund od startu tokenu czekamy na rozpęd
MOMENTUM_PCT = cfg("SNAJPER_ROZPED_PCT", 50.0)   # rozpęd: wzrost od pierwszej ceny, po którym wchodzimy
MAX_DEV_SOL = cfg("SNAJPER_MAX_DEV_SOL", 5.0)    # twórca kupił na starcie więcej: pomijamy (zwykle zrzuca)
TP_PCT = cfg("SNAJPER_TP_PCT", 100.0)            # przy tylu % sprzedajemy połowę
SL_PCT = cfg("SNAJPER_SL_PCT", -30.0)            # stop od ceny wejścia
TRAIL_PCT = cfg("SNAJPER_TRAIL_PCT", 30.0)       # po realizacji połowy: wyjście, gdy cena spadnie tyle od szczytu
MAX_HOLD_S = cfg("SNAJPER_MAX_S", 600.0)         # wyjście najpóźniej po tylu sekundach
NO_PRICE_S = 60.0                                # tyle bez ceny = traktujemy jako nie do sprzedania
FEE_PCT = cfg("SNAJPER_FEE_PCT", 1.5)            # prowizje w każdą stronę (pump.fun + Jupiter)
TX_SOL = cfg("SNAJPER_TX_SOL", 0.00002)          # opłata sieci i pierwszeństwa za jedną transakcję
RENT_SOL = cfg("SNAJPER_RENT_SOL", 0.0015)       # kaucja za konto tokenu; wraca dopiero po zamknięciu konta

# Limity prawdziwego handlu. Twarde maksimum stawki jest w kodzie, żeby literówka w .env nie wydała więcej.
USD = cfg("SNAJPER_USD", 1.0)
HARD_MAX_USD = 5.0
LIVE_STRATEGY = cfg("SNAJPER_STRATEGIA", "rozpęd")
MAX_OPEN = cfg("SNAJPER_MAX_OTWARTYCH", 2)
MAX_BUYS_DAY = cfg("SNAJPER_MAX_ZAKUPOW_DZIENNIE", 10)
MAX_LOSS_DAY = cfg("SNAJPER_MAX_STRATA_DZIENNIE_USD", 3.0)
MAX_SLIPPAGE_BPS = cfg("SNAJPER_MAX_SLIPPAGE_BPS", 2500)  # Jupiter daje świeżym tokenom zwykle 2000
MAX_IMPACT = cfg("SNAJPER_MAX_IMPACT_PCT", 25.0)  # pole wpływu jest dla świeżych tokenów zawyżone
SOL_RESERVE = cfg("SNAJPER_SOL_RESERVE", 0.01)

STRATEGIES = {"start": "kupuje pierwszą cenę każdego tokenu (punkt odniesienia)",
              "rozpęd": f"kupuje, gdy w {WINDOW_S:g} s od startu cena wzrośnie o {MOMENTUM_PCT:g}%"}


def log(text):
    print(f"{datetime.now().strftime('%H:%M:%S')}  {text}", flush=True)


class Sniper:
    def __init__(self, live):
        self.live = live
        self.tokens = {}       # mint -> {symbol, created, first, dev_sol, last_price, last_seen}
        self.positions = []    # otwarte pozycje (na sucho i prawdziwe)
        self.sol_usd = agent.sol_price()
        self.sol_ts = time.time()
        self.lock = asyncio.Lock()   # prawdziwe transakcje po kolei, nigdy dwie naraz
        self.priv = self.pub = self.wallet = None
        saved = json.loads(OPEN.read_text()) if OPEN.exists() else []
        self.positions = [p for p in saved if p.get("live")]  # pozycje na sucho po restarcie nic nie znaczą
        for p in self.positions:
            self.tokens.setdefault(p["mint"], {"symbol": p["symbol"], "created": p["entry_ts"], "first": p["entry"],
                                               "dev_sol": 0, "last_price": p["entry"], "last_seen": time.time()})
        if live:
            import handel
            self.handel = handel
            self.priv, self.pub = handel.load_key(handel.SNIPER_KEY_FILE)
            self.wallet = handel.b58encode(self.pub)

    # ------------------------------------------------------------------ limity prawdziwego handlu

    def closed_since(self, seconds):
        if not RESULTS.exists():
            return []
        since = time.time() - seconds
        out = []
        for line in RESULTS.read_text().splitlines():
            r = json.loads(line)
            if r.get("live") and r["exit_ts"] >= since:
                out.append(r)
        return out

    def live_blocker(self):
        """Powód, dla którego nie wolno teraz kupić naprawdę, albo None."""
        if STOP_FILE.exists():
            return "jest plik dane/snajper.STOP"
        opened = [p for p in self.positions if p.get("live")]
        if len(opened) >= MAX_OPEN:
            return f"otwartych pozycji {len(opened)}/{MAX_OPEN}"
        day = self.closed_since(86400)
        buys = len(day) + len(opened)
        if buys >= MAX_BUYS_DAY:
            return f"zakupów w 24 h {buys}/{MAX_BUYS_DAY}"
        # Otwarte pozycje liczymy tak, jakby przepadły w całości: limit straty ma działać z zapasem.
        at_risk = -sum(r["usd_pnl"] for r in day) + sum(p["usd"] for p in opened)
        if at_risk + USD > MAX_LOSS_DAY:
            return f"limit straty na dobę: zagrożone {at_risk:.2f} z {MAX_LOSS_DAY:g} USD"
        return None

    def swap(self, in_mint, out_mint, amount_raw, side):
        """Prawdziwa zamiana przez Jupitera. Zwraca (wynik, None) albo (None, powód)."""
        h = self.handel
        try:
            order = h.get_order(in_mint, out_mint, amount_raw, self.wallet)
            if int(order.get("slippageBps") or 0) > MAX_SLIPPAGE_BPS:
                return None, f"poślizg {order['slippageBps']} bps > {MAX_SLIPPAGE_BPS}"
            if side == "buy" and h.impact_pct(order) > MAX_IMPACT:
                return None, f"wpływ na cenę {h.impact_pct(order):.1f}%"
            res = h.execute(order, self.priv, self.pub)
        except SystemExit as e:   # get_order / sign_tx kończą program przy błędzie; tu tylko odpuszczamy
            return None, str(e)
        except agent.ApiError as e:
            return None, f"Jupiter: {e}"
        if res.get("status") != "Success":
            return None, f"{res.get('error') or res}"[:200]
        return res, None

    # ------------------------------------------------------------------ wejście i wyjście

    async def enter(self, mint, strategy, price, now):
        t = self.tokens[mint]
        pos = {"mint": mint, "symbol": t["symbol"], "strategy": strategy, "entry_ts": now, "entry": price,
               "peak": price, "left": 1.0, "realized": 0.0, "usd": USD, "dev_sol": t["dev_sol"],
               "age_s": round(now - t["created"]), "first": t["first"], "live": False}
        live = self.live and strategy == LIVE_STRATEGY
        if live:
            async with self.lock:
                why = self.live_blocker()
                if why:
                    log(f"  pomijam prawdziwy zakup {t['symbol']}: {why}")
                    live = False
                else:
                    sol, _ = await asyncio.to_thread(self.handel.holdings, self.wallet)
                    lamports = int(USD / self.sol_usd * 1e9)
                    if sol - lamports / 1e9 < SOL_RESERVE:
                        log(f"  pomijam prawdziwy zakup {t['symbol']}: za mało SOL ({sol:.4f})")
                        live = False
                    else:
                        res, err = await asyncio.to_thread(self.swap, SOL, mint, lamports, "buy")
                        if err:
                            log(f"  ✗ zakup {t['symbol']} nie przeszedł: {err}")
                            return
                        sol_in = int(res.get("totalInputAmount") or lamports) / 1e9
                        pos.update(live=True, raw=int(res["totalOutputAmount"]), sol_in=sol_in,
                                   usd=round(sol_in * self.sol_usd, 2), buy_sig=res.get("signature"))
                        notify(f"Snajper kupił {t['symbol']}", f"{fmt_usd(pos['usd'])} · {strategy}")
        self.positions.append(pos)
        self.save_open()
        tag = "NAPRAWDĘ " if pos["live"] else ""
        if pos["live"] or strategy != "start":
            log(f"{tag}wejście {t['symbol']} ({strategy}) po {pos['age_s']} s od startu, "
                f"{(price / t['first'] - 1) * 100:+.0f}% od pierwszej ceny")

    async def sell_part(self, pos, fraction):
        """Sprzedaje część pozycji naprawdę. Zwraca SOL z tej sprzedaży albo None przy błędzie."""
        raw = pos["raw"] if fraction >= pos["left"] - 1e-9 else int(pos["raw"] * fraction / pos["left"])
        async with self.lock:
            res, err = await asyncio.to_thread(self.swap, pos["mint"], SOL, raw, "sell")
        if err:
            pos["sell_errors"] = pos.get("sell_errors", 0) + 1
            log(f"  ✗ sprzedaż {pos['symbol']} nie przeszła ({pos['sell_errors']}×): {err}")
            if pos["sell_errors"] == 3:
                notify(f"Snajper: nie mogę sprzedać {pos['symbol']}", "próbuję dalej, sprawdź portfel snajpera")
            return None
        pos["raw"] -= raw
        pos["sell_errors"] = 0
        return int(res.get("totalOutputAmount") or 0) / 1e9

    async def manage(self, pos, price, now):
        """Reguły wyjścia: połowa przy celu, stop, ruchomy stop po celu, limit czasu, brak ceny.

        Na sucho sygnał wyjścia wypełniamy dopiero przy następnym odczycie ceny, tak jak prawdziwa
        sprzedaż, która trwa kilka sekund. Prawdziwą pozycję sprzedajemy od razu."""
        t = self.tokens[pos["mint"]]
        gain = (price / pos["entry"] - 1) * 100 if price else None
        if pos.get("pending"):
            reason, fraction = pos.pop("pending")
            if not price and now - t["last_seen"] < NO_PRICE_S:
                pos["pending"] = [reason, fraction]   # czekamy na cenę
                return
            await self.fill(pos, reason, fraction, price, gain, now)
            return
        if price:
            pos["peak"] = max(pos["peak"], price)
        reason, fraction = None, 0.0
        if not price and now - t["last_seen"] >= NO_PRICE_S:
            reason, fraction = "brak ceny", pos["left"]
        elif price and pos["left"] == 1.0 and gain >= TP_PCT:
            reason, fraction = "cel", 0.5
        elif price and gain <= SL_PCT:
            reason, fraction = "stop", pos["left"]
        elif price and pos["left"] < 1.0 and price <= pos["peak"] * (1 - TRAIL_PCT / 100):
            reason, fraction = "ruchomy stop", pos["left"]
        elif now - pos["entry_ts"] >= MAX_HOLD_S:
            reason, fraction = "czas", pos["left"]
        if not reason:
            return
        if not pos["live"] and reason != "brak ceny":
            pos["pending"] = [reason, fraction]
            return
        await self.fill(pos, reason, fraction, price, gain, now)

    async def fill(self, pos, reason, fraction, price, gain, now):
        # Na sucho bez ceny zakładamy, że nic nie odzyskamy.
        value = fraction * (price / pos["entry"] if price else 0.0) * (1 - FEE_PCT / 100) ** 2
        if pos["live"]:
            got = await self.sell_part(pos, fraction)
            if got is None:
                return
            value = got / pos["sol_in"]
        pos["realized"] += value
        pos["left"] = round(pos["left"] - fraction, 6)
        pos.setdefault("exits", []).append({"reason": reason, "s": round(now - pos["entry_ts"]),
                                            "gain_pct": gain is not None and round(gain, 1)})
        if pos["left"] > 1e-6:
            if pos["live"]:
                notify(f"Snajper: połowa {pos['symbol']}", f"{gain:+.0f}%")
            self.save_open()
            return
        await self.close(pos, now)

    async def close(self, pos, now):
        txs = 1 + len(pos["exits"])
        fixed = (txs * TX_SOL + RENT_SOL) * self.sol_usd
        gross_usd = pos["realized"] * pos["usd"]
        usd_pnl = gross_usd - pos["usd"] - fixed  # opłaty sieci płaci portfel osobno, więc odejmujemy je zawsze
        r = {**{k: v for k, v in pos.items() if k not in ("raw",)}, "exit_ts": now,
             "hold_s": round(now - pos["entry_ts"]), "net_pct": round(usd_pnl / pos["usd"] * 100, 1),
             "usd_pnl": round(usd_pnl, 3), "max_pct": round((pos["peak"] / pos["entry"] - 1) * 100, 1)}
        self.positions.remove(pos)
        agent.DATA.mkdir(exist_ok=True)
        with RESULTS.open("a") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.save_open()
        if pos["live"] or pos["strategy"] != "start":
            reasons = " → ".join(e["reason"] for e in pos["exits"])
            log(f"{'NAPRAWDĘ ' if pos['live'] else ''}wyjście {pos['symbol']} ({pos['strategy']}): {reasons}, "
                f"{r['net_pct']:+.0f}% po kosztach, szczyt {r['max_pct']:+.0f}%, {r['hold_s']} s")
        if pos["live"]:
            notify(f"Snajper sprzedał {pos['symbol']}", f"{r['usd_pnl']:+.2f} USD ({r['net_pct']:+.0f}%)")

    def save_open(self):
        agent.DATA.mkdir(exist_ok=True)
        OPEN.write_text(json.dumps(self.positions, indent=1, ensure_ascii=False))

    # ------------------------------------------------------------------ pętle

    async def listen(self):
        """Nowe tokeny ze strumienia. Po zerwaniu łączymy się ponownie (jedno połączenie, jak prosi PumpPortal)."""
        while True:
            try:
                async with websockets.connect(STREAM, open_timeout=15, ping_interval=20) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    log("Połączono ze strumieniem nowych tokenów.")
                    async for raw in ws:
                        m = json.loads(raw)
                        if m.get("txType") != "create" or not m.get("mint"):
                            continue
                        dev = float(m.get("solAmount") or 0)
                        if dev > MAX_DEV_SOL:
                            continue
                        self.tokens.setdefault(m["mint"], {"symbol": (m.get("symbol") or "?")[:12],
                                                           "created": time.time(), "first": None, "dev_sol": dev,
                                                           "last_price": None, "last_seen": time.time(),
                                                           "entered": set()})
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as e:
                log(f"! strumień zerwany ({e}), łączę ponownie za 5 s")
                await asyncio.sleep(5)

    def tracked(self, now):
        held = {p["mint"] for p in self.positions}
        for mint, t in list(self.tokens.items()):
            if mint not in held and not t.get("pending") and now - t["created"] > WINDOW_S:
                del self.tokens[mint]
        return list(self.tokens)

    async def poll(self):
        while True:
            await asyncio.sleep(POLL_S)
            now = time.time()
            if now - self.sol_ts > 60:
                try:
                    self.sol_usd, self.sol_ts = await asyncio.to_thread(agent.sol_price), now
                except agent.ApiError:
                    pass
            mints = self.tracked(now)
            quotes = {}
            try:
                for i in range(0, len(mints), 50):
                    r = await asyncio.to_thread(jup_get, f"/price/v3?ids={','.join(mints[i:i + 50])}")
                    quotes.update({m: float(p["usdPrice"]) for m, p in (r or {}).items()
                                   if p and p.get("usdPrice")})
            except agent.ApiError as e:
                log(f"! Jupiter: {e}")
                continue
            for mint, price in quotes.items():
                t = self.tokens.get(mint)
                if t:
                    t["last_price"], t["last_seen"] = price, now
            # Wyjścia najpierw, potem wejścia (żeby nie przekroczyć limitu otwartych).
            for pos in list(self.positions):
                await self.manage(pos, quotes.get(pos["mint"]), now)
            for mint, price in quotes.items():
                t = self.tokens.get(mint)
                if not t or "entered" not in t:
                    continue
                # Sygnał z poprzedniego odczytu: na sucho wchodzimy po dzisiejszej cenie, jak prawdziwy bot.
                for strategy in t.pop("pending", []):
                    await self.enter(mint, strategy, price, now)
                signals = []
                if t["first"] is None:
                    t["first"] = price
                    signals.append("start")
                elif (now - t["created"] <= WINDOW_S and price >= t["first"] * (1 + MOMENTUM_PCT / 100)):
                    signals.append("rozpęd")
                for strategy in signals:
                    if strategy in t["entered"]:
                        continue
                    t["entered"].add(strategy)
                    if self.live and strategy == LIVE_STRATEGY:
                        await self.enter(mint, strategy, price, now)   # prawdziwy zakup od razu
                    else:
                        t.setdefault("pending", []).append(strategy)

    async def status(self):
        while True:
            await asyncio.sleep(300)
            day = [json.loads(l) for l in RESULTS.read_text().splitlines()] if RESULTS.exists() else []
            day = [r for r in day if r["exit_ts"] >= time.time() - 3600]
            parts = []
            for s in STRATEGIES:
                rs = [r for r in day if r["strategy"] == s]
                if rs:
                    parts.append(f"{s}: {len(rs)} · mediana {statistics.median(r['net_pct'] for r in rs):+.0f}% · "
                                 f"{sum(r['usd_pnl'] for r in rs):+.2f} USD")
            opened = len(self.positions)
            log(f"ostatnia godzina · {' | '.join(parts) or 'brak zamkniętych'} · otwarte {opened}"
                + (" · STOP: nowe zakupy wstrzymane" if STOP_FILE.exists() else ""))

    async def run(self):
        await asyncio.gather(self.listen(), self.poll(), self.status())


def cmd_run(args):
    if USD <= 0 or USD > HARD_MAX_USD:
        sys.exit(f"SNAJPER_USD musi być między 0 a {HARD_MAX_USD:g} USD.")
    s = Sniper(args.naprawde)
    print(f"SNAJPER · {'NAPRAWDĘ z portfela ' + s.wallet if args.naprawde else 'na sucho, niczego nie kupuję'}")
    print(f"  stawka {fmt_usd(USD)} · cel +{TP_PCT:g}% (połowa), stop {SL_PCT:g}%, ruchomy stop {TRAIL_PCT:g}% "
          f"od szczytu, najwyżej {MAX_HOLD_S:g} s · ceny co {POLL_S:g} s")
    for k, v in STRATEGIES.items():
        print(f"  {k}: {v}")
    if args.naprawde:
        if not sys.stdin.isatty():
            sys.exit("Prawdziwy handel uruchamiasz tylko we własnym oknie Terminala.")
        sol, _ = s.handel.holdings(s.wallet)
        print(f"\n  Naprawdę handluje tylko strategia „{LIVE_STRATEGY}”, reszta dalej na sucho.")
        print(f"  Saldo {sol:.4f} SOL (~{fmt_usd(sol * s.sol_usd)}) · najwyżej {MAX_OPEN} otwarte · "
              f"{MAX_BUYS_DAY} zakupów i {fmt_usd(MAX_LOSS_DAY)} straty na dobę · rezerwa {SOL_RESERVE} SOL")
        print(f"  Wstrzymanie zakupów: touch {STOP_FILE}")
        if input("\nBot będzie kupował i sprzedawał bez pytania o każdą transakcję. Wpisz TAK: ").strip().upper() != "TAK":
            return
    if s.positions:
        print(f"  Wznawiam {len(s.positions)} otwarte prawdziwe pozycje.")
    print("Ctrl+C kończy.\n", flush=True)
    try:
        asyncio.run(s.run())
    except KeyboardInterrupt:
        live = [p for p in s.positions if p.get("live")]
        if live:
            print(f"\nUwaga: {len(live)} prawdziwe pozycje zostały otwarte: "
                  f"{', '.join(p['symbol'] for p in live)}. Uruchom snajpera ponownie, żeby je domknął.")


def cmd_results(args):
    rows = [json.loads(l) for l in RESULTS.read_text().splitlines()] if RESULTS.exists() else []
    if args.godziny:
        rows = [r for r in rows if r["exit_ts"] >= time.time() - args.godziny * 3600]
    groups = {s: [r for r in rows if r["strategy"] == s and not r.get("live")] for s in STRATEGIES}
    groups["naprawdę"] = [r for r in rows if r.get("live")]
    stats = {}
    for name, rs in groups.items():
        if not rs:
            continue
        nets = [r["net_pct"] for r in rs]
        stats[name] = {"n": len(rs), "win_pct": round(sum(n > 0 for n in nets) / len(nets) * 100),
                       "median_pct": round(statistics.median(nets), 1), "mean_pct": round(statistics.mean(nets), 1),
                       "usd": round(sum(r["usd_pnl"] for r in rs), 2),
                       "hit_tp_pct": round(sum(any(e["reason"] == "cel" for e in r["exits"]) for r in rs) / len(rs) * 100),
                       "dead_pct": round(sum(n <= -80 for n in nets) / len(nets) * 100),
                       "best_pct": max(nets)}
    if args.json:
        print(json.dumps(stats, indent=1, ensure_ascii=False))
        return
    span = f"ostatnie {args.godziny:g} h" if args.godziny else "całość"
    print(f"SNAJPER · {span} · stawka {fmt_usd(USD)} · koszty {FEE_PCT:g}% w każdą stronę, {TX_SOL:.5f} SOL za "
          f"transakcję i {RENT_SOL:g} SOL kaucji za konto tokenu\n")
    if not stats:
        print("  Jeszcze brak zamkniętych pozycji. Uruchom: python3 agent.py snajper")
        return
    for name, st in stats.items():
        label = f"{name} ({STRATEGIES[name]})" if name in STRATEGIES else name
        print(f"  {label}\n    {st['n']} pozycji · na plusie {st['win_pct']}% · mediana {st['median_pct']:+}% · "
              f"średnio {st['mean_pct']:+}% · razem {st['usd']:+.2f} USD\n"
              f"    doszło do +{TP_PCT:g}%: {st['hit_tp_pct']}% · straciło ≥ 80%: {st['dead_pct']}% · "
              f"najlepsza {st['best_pct']:+.0f}%")
    print("\nNa sucho cena wejścia i wyjścia to pierwsza cena z Jupitera po sygnale, czyli mniej więcej to, "
          "na co zdążyłby prawdziwy bot. Boty w tym samym bloku co start tokenu i tak są szybsze.")
