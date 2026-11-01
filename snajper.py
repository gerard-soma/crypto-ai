"""Snajper: łapie nowe tokeny z pump.fun, ocenia, kto je kupuje, nagrywa je i sam z nich wychodzi.

Nowe tokeny przychodzą na żywo z darmowego strumienia PumpPortal. Co kilka sekund Jupiter (0 kredytów Helius)
podaje dla każdego cenę i dane o przepływie: ilu jest traderów i posiadaczy, ile było kupna i sprzedaży,
jaki udział mają najwięksi posiadacze i twórca, ile tokenów twórca wypuścił wcześniej.

Na sucho bot zapisuje, co by kupił i sprzedał. Wejście i wyjście wypełnia po cenie z następnego odczytu
po sygnale, tak jak prawdziwa transakcja, i odejmuje koszty. Każdy nowy token trafia do nagrań
(dane/snajper-nagrania/), żeby `snajper-test` sprawdził inne progi na tych samych danych.

Z flagą --naprawde handluje sam z osobnego portfela snajpera (~/.config/wieloryby/snajper.json), bez pytania
o każdą transakcję, ale w twardych limitach z .env. Po zamknięciu pozycji zamyka puste konto tokenu, żeby
wróciła kaucja. Plik dane/snajper.STOP wstrzymuje nowe zakupy (otwarte pozycje dalej są pilnowane).
"""
import asyncio
import gzip
import json
import signal
import statistics
import sys
import time
from datetime import datetime

import websockets

import agent
from agent import SOL, cfg, fmt_usd, jup_get
from czuwaj import notify

STREAM = "wss://pumpportal.fun/api/data"
RESULTS = agent.DATA / "snajper-wyniki.jsonl"   # zamknięte pozycje, jedna na linię
OPEN = agent.DATA / "snajper-otwarte.json"      # otwarte pozycje (prawdziwe wznawiamy po restarcie)
STOP_FILE = agent.DATA / "snajper.STOP"
RECORDINGS = agent.DATA / "snajper-nagrania"    # odczyty każdego nowego tokenu, plik .jsonl.gz na dzień

POLL_S = cfg("SNAJPER_POLL_S", 3.0)              # co ile sekund pytamy Jupitera
ENTRY_WINDOW_S = 180.0                           # tyle sekund od startu pilnujemy każdego tokenu
RECORD_S = cfg("SNAJPER_NAGRANIE_S", 900.0)      # żywe tokeny nagrywamy do tylu sekund od startu
KEEP_TRADERS = 5                                 # po oknie wejścia nagrywamy dalej tylko tokeny z tyloma traderami
NO_PRICE_S = 60.0                                # tyle bez ceny = traktujemy jako nie do sprzedania

# Rozpęd (stara reguła, zostaje do porównania): cena +50% od pierwszej w ciągu 3 min.
WINDOW_S = cfg("SNAJPER_OKNO_S", 180.0)
MOMENTUM_PCT = cfg("SNAJPER_ROZPED_PCT", 50.0)
# Przepływ: kto i ile kupuje w pierwszej minucie. Progi do strojenia przez `snajper-test`.
FLOW = {
    "min_age": cfg("SNAJPER_PRZEPLYW_OD_S", 10.0),           # nie wcześniej niż tyle sekund od startu
    "max_age": cfg("SNAJPER_PRZEPLYW_DO_S", 60.0),           # i nie później
    "traders": cfg("SNAJPER_PRZEPLYW_TRADEROW", 15),         # różnych traderów
    "ratio": cfg("SNAJPER_PRZEPLYW_KUPNO_SPRZEDAZ", 1.5),    # liczba kupna / liczba sprzedaży
    "net_usd": cfg("SNAJPER_PRZEPLYW_NETTO_USD", 500.0),     # wolumen kupna minus sprzedaży
    "dev_mints": cfg("SNAJPER_PRZEPLYW_MAX_TOKENOW_TWORCY", 20),  # seryjni twórcy zwykle zrzucają
    "top_pct": cfg("SNAJPER_PRZEPLYW_MAX_TOP_PCT", 100.0),   # udział największych posiadaczy (100 = bez filtra)
    "max_rise": cfg("SNAJPER_PRZEPLYW_MAX_WZROST_PCT", 300.0),  # nie gonimy, gdy cena już tyle urosła
}
MAX_DEV_SOL = cfg("SNAJPER_MAX_DEV_SOL", 5.0)    # twórca kupił na starcie więcej: pomijamy (zwykle zrzuca)

EXITS = {
    "tp": cfg("SNAJPER_TP_PCT", 100.0),          # przy tylu % sprzedajemy połowę
    "sl": cfg("SNAJPER_SL_PCT", -30.0),          # stop od ceny wejścia
    "trail": cfg("SNAJPER_TRAIL_PCT", 30.0),     # po realizacji połowy: wyjście, gdy cena spadnie tyle od szczytu
    "max_hold": cfg("SNAJPER_MAX_S", 600.0),     # wyjście najpóźniej po tylu sekundach
}
FEE_PCT = cfg("SNAJPER_FEE_PCT", 1.5)            # prowizje w każdą stronę (pump.fun + Jupiter)
TX_SOL = cfg("SNAJPER_TX_SOL", 0.00005)          # opłata sieci i pierwszeństwa za jedną transakcję
RENT_SOL = cfg("SNAJPER_RENT_SOL", 0.0015)       # kaucja za konto tokenu
CLOSE_ACCOUNTS = cfg("SNAJPER_ZAMYKAJ_KONTA", 1)  # 1: po wyjściu zamykamy puste konto i kaucja wraca

# Limity prawdziwego handlu. Twarde maksimum stawki jest w kodzie, żeby literówka w .env nie wydała więcej.
USD = cfg("SNAJPER_USD", 1.0)
HARD_MAX_USD = 5.0
LIVE_STRATEGY = cfg("SNAJPER_STRATEGIA", "przepływ")
MAX_OPEN = cfg("SNAJPER_MAX_OTWARTYCH", 2)
MAX_BUYS_DAY = cfg("SNAJPER_MAX_ZAKUPOW_DZIENNIE", 10)
MAX_LOSS_DAY = cfg("SNAJPER_MAX_STRATA_DZIENNIE_USD", 3.0)
MAX_SLIPPAGE_BPS = cfg("SNAJPER_MAX_SLIPPAGE_BPS", 2500)  # Jupiter daje świeżym tokenom zwykle 2000
MAX_IMPACT = cfg("SNAJPER_MAX_IMPACT_PCT", 25.0)  # pole wpływu jest dla świeżych tokenów zawyżone
SOL_RESERVE = cfg("SNAJPER_SOL_RESERVE", 0.01)

STRATEGIES = {
    "przepływ": (f"w {FLOW['min_age']:g}–{FLOW['max_age']:g} s od startu: ≥ {FLOW['traders']} traderów, "
                 f"kupno/sprzedaż ≥ {FLOW['ratio']:g}, netto ≥ ${FLOW['net_usd']:g}, twórca z ≤ {FLOW['dev_mints']} "
                 f"tokenami"),
    "rozpęd": f"cena +{MOMENTUM_PCT:g}% od pierwszej w {WINDOW_S:g} s (stara reguła, do porównania)",
}


def log(text):
    print(f"{datetime.now().strftime('%H:%M:%S')}  {text}", flush=True)


# --------------------------------------------------------------------------- odczyty i cechy
# Te funkcje liczą to samo na żywo i przy odtwarzaniu nagrań w `snajper-test`.

SNAP_FIELDS = ("s", "price", "holders", "traders", "buys", "sells", "buy_usd", "sell_usd", "top_pct", "dev_pct", "liq")


def snapshot(info, age):
    """Jeden odczyt tokenu z Jupitera jako lista w kolejności SNAP_FIELDS. None, gdy brak ceny."""
    if not info.get("usdPrice"):
        return None
    s5 = info.get("stats5m") or {}   # dla tokenu młodszego niż 5 min to suma od startu
    audit = info.get("audit") or {}
    return [round(age, 1), float(info["usdPrice"]), info.get("holderCount") or 0, s5.get("numTraders") or 0,
            s5.get("numBuys") or 0, s5.get("numSells") or 0, round(s5.get("buyVolume") or 0, 1),
            round(s5.get("sellVolume") or 0, 1), audit.get("topHoldersPercentage"), audit.get("devBalancePercentage"),
            round(info.get("liquidity") or 0)]


def static_info(info):
    audit = info.get("audit") or {}
    return {"dev_mints": audit.get("devMints") or 0, "dev_migrations": audit.get("devMigrations") or 0,
            "socials": sum(bool(info.get(k)) for k in ("twitter", "website", "telegram")),
            "graduated": bool(info.get("graduatedAt"))}


def features(snap, first, static, dev_sol):
    s = dict(zip(SNAP_FIELDS, snap))
    return {"age": s["s"], "traders": s["traders"], "holders": s["holders"],
            "ratio": s["buys"] / max(1, s["sells"]), "net_usd": s["buy_usd"] - s["sell_usd"],
            "top_pct": s["top_pct"] if s["top_pct"] is not None else 100.0,
            "rise": (s["price"] / first - 1) * 100 if first else 0.0,
            "dev_sol": dev_sol, "dev_mints": static.get("dev_mints", 0), "socials": static.get("socials", 0)}


def flow_signal(f, p=FLOW):
    return (p["min_age"] <= f["age"] <= p["max_age"] and f["traders"] >= p["traders"] and f["ratio"] >= p["ratio"]
            and f["net_usd"] >= p["net_usd"] and f["dev_mints"] <= p["dev_mints"] and f["top_pct"] <= p["top_pct"]
            and f["rise"] <= p["max_rise"] and f["dev_sol"] <= MAX_DEV_SOL)


def momentum_signal(f):
    return f["age"] <= WINDOW_S and f["rise"] >= MOMENTUM_PCT and f["dev_sol"] <= MAX_DEV_SOL


SIGNALS = {"przepływ": flow_signal, "rozpęd": momentum_signal}


def exit_decision(pos, price, age, p=EXITS):
    """(powód, część pozycji do sprzedania) albo None. `age` to sekundy od wejścia."""
    gain = (price / pos["entry"] - 1) * 100
    if pos["left"] == 1.0 and gain >= p["tp"]:
        return "cel", 0.5
    if gain <= p["sl"]:
        return "stop", pos["left"]
    if pos["left"] < 1.0 and price <= pos["peak"] * (1 - p["trail"] / 100):
        return "ruchomy stop", pos["left"]
    if age >= p["max_hold"]:
        return "czas", pos["left"]
    return None


def fixed_cost_usd(n_tx, sol_usd):
    """Opłaty sieci za wszystkie transakcje i kaucja, jeśli konto tokenu zostaje otwarte."""
    if CLOSE_ACCOUNTS:
        return (n_tx + 1) * TX_SOL * sol_usd   # +1: transakcja zamykająca konto
    return (n_tx * TX_SOL + RENT_SOL) * sol_usd


def sell_value(fraction, price, entry):
    """Ile wraca ze sprzedaży części pozycji, jako ułamek stawki, po prowizji w obie strony."""
    return fraction * price / entry * (1 - FEE_PCT / 100) ** 2


def finish(pos, sol_usd):
    """Wynik zamkniętej pozycji: (net %, USD)."""
    usd_pnl = pos["realized"] * pos["usd"] - pos["usd"] - fixed_cost_usd(1 + len(pos["exits"]), sol_usd)
    return round(usd_pnl / pos["usd"] * 100, 1), round(usd_pnl, 3)


# --------------------------------------------------------------------------- bot

class Sniper:
    def __init__(self, live):
        self.live = live
        self.tokens = {}       # mint -> {symbol, created, dev_sol, first, snaps, static, entered, last_seen}
        self.positions = []
        self.sol_usd = agent.sol_price()
        self.sol_ts = time.time()
        self.lock = asyncio.Lock()   # prawdziwe transakcje po kolei, nigdy dwie naraz
        self.priv = self.pub = self.wallet = None
        self.counts = {"tokens": 0, "polls": 0}
        saved = json.loads(OPEN.read_text()) if OPEN.exists() else []
        self.positions = [p for p in saved if p.get("live")]  # pozycje na sucho po restarcie nic nie znaczą
        for p in self.positions:
            self.tokens.setdefault(p["mint"], self.new_token(p["symbol"], p["entry_ts"] - p["age_s"], 0.0,
                                                             set(STRATEGIES)))
        if live:
            import handel
            self.handel = handel
            self.priv, self.pub = handel.load_key(handel.SNIPER_KEY_FILE)
            self.wallet = handel.b58encode(self.pub)

    @staticmethod
    def new_token(symbol, created, dev_sol, entered=None):
        return {"symbol": symbol, "created": created, "dev_sol": dev_sol, "first": None, "snaps": [], "static": {},
                "entered": entered or set(), "last_seen": time.time()}

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

    # ------------------------------------------------------------------ wejście

    async def enter(self, mint, strategy, price, now, f):
        t = self.tokens[mint]
        pos = {"mint": mint, "symbol": t["symbol"], "strategy": strategy, "entry_ts": now, "entry": price,
               "peak": price, "left": 1.0, "realized": 0.0, "usd": USD, "dev_sol": t["dev_sol"],
               "age_s": round(now - t["created"]), "first": t["first"], "live": False,
               "features": {k: round(v, 2) if isinstance(v, float) else v for k, v in f.items()}}
        if self.live and strategy == LIVE_STRATEGY:
            async with self.lock:
                why = self.live_blocker()
                if why:
                    log(f"  pomijam prawdziwy zakup {t['symbol']}: {why}")
                else:
                    sol, _ = await asyncio.to_thread(self.handel.holdings, self.wallet)
                    lamports = int(USD / self.sol_usd * 1e9)
                    if sol - lamports / 1e9 < SOL_RESERVE:
                        log(f"  pomijam prawdziwy zakup {t['symbol']}: za mało SOL ({sol:.4f})")
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
        log(f"{'NAPRAWDĘ ' if pos['live'] else ''}wejście {t['symbol']} ({strategy}) po {pos['age_s']} s od startu, "
            f"{f['rise']:+.0f}% od pierwszej ceny · {f['traders']} traderów, netto {fmt_usd(f['net_usd'])}, "
            f"kupno/sprzedaż {f['ratio']:.1f}, twórca {f['dev_mints']} tokenów")

    # ------------------------------------------------------------------ wyjście

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
        """Reguły wyjścia. Na sucho sygnał wypełniamy przy następnym odczycie ceny, tak jak prawdziwą
        sprzedaż, która trwa kilka sekund. Prawdziwą pozycję sprzedajemy od razu."""
        t = self.tokens[pos["mint"]]
        if pos.get("pending"):
            reason, fraction = pos.pop("pending")
            if not price and now - t["last_seen"] < NO_PRICE_S:
                pos["pending"] = [reason, fraction]   # czekamy na cenę
                return
            await self.fill(pos, reason, fraction, price, now)
            return
        if not price:
            if now - t["last_seen"] >= NO_PRICE_S:
                await self.fill(pos, "brak ceny", pos["left"], None, now)
            return
        pos["peak"] = max(pos["peak"], price)
        decision = exit_decision(pos, price, now - pos["entry_ts"])
        if not decision:
            return
        if pos["live"]:
            await self.fill(pos, *decision, price, now)
        else:
            pos["pending"] = list(decision)

    async def fill(self, pos, reason, fraction, price, now):
        # Na sucho bez ceny zakładamy, że nic nie odzyskamy.
        value = sell_value(fraction, price, pos["entry"]) if price else 0.0
        if pos["live"]:
            got = await self.sell_part(pos, fraction)
            if got is None:
                return
            value = got / pos["sol_in"]
        pos["realized"] += value
        pos["left"] = round(pos["left"] - fraction, 6)
        gain = (price / pos["entry"] - 1) * 100 if price else None
        pos.setdefault("exits", []).append({"reason": reason, "s": round(now - pos["entry_ts"]),
                                            "gain_pct": gain is not None and round(gain, 1)})
        if pos["left"] > 1e-6:
            if pos["live"]:
                notify(f"Snajper: połowa {pos['symbol']}", f"{gain:+.0f}%")
            self.save_open()
            return
        await self.close(pos, now)

    async def close(self, pos, now):
        net, usd_pnl = finish(pos, self.sol_usd)
        r = {**{k: v for k, v in pos.items() if k != "raw"}, "exit_ts": now, "hold_s": round(now - pos["entry_ts"]),
             "net_pct": net, "usd_pnl": usd_pnl, "max_pct": round((pos["peak"] / pos["entry"] - 1) * 100, 1)}
        self.positions.remove(pos)
        agent.DATA.mkdir(exist_ok=True)
        with RESULTS.open("a") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.save_open()
        reasons = " → ".join(e["reason"] for e in pos["exits"])
        log(f"{'NAPRAWDĘ ' if pos['live'] else ''}wyjście {pos['symbol']} ({pos['strategy']}): {reasons}, "
            f"{net:+.0f}% po kosztach, szczyt {r['max_pct']:+.0f}%, {r['hold_s']} s")
        if pos["live"]:
            notify(f"Snajper sprzedał {pos['symbol']}", f"{usd_pnl:+.2f} USD ({net:+.0f}%)")
            if CLOSE_ACCOUNTS:
                asyncio.get_running_loop().create_task(self.close_accounts())

    async def close_accounts(self):
        await asyncio.sleep(20)   # saldo po sprzedaży musi się najpierw pojawić w Jupiterze
        try:
            async with self.lock:
                n, sig = await asyncio.to_thread(self.handel.close_empty_accounts, self.priv, self.pub)
            if n:
                log(f"  zamknięte puste konta tokenów: {n}, kaucja wraca ({sig[:12]}…)")
        except Exception as e:   # to tylko zwrot kaucji, nie może zatrzymać bota
            log(f"  ! nie udało się zamknąć kont tokenów: {str(e)[:120]}")

    def save_open(self):
        agent.DATA.mkdir(exist_ok=True)
        OPEN.write_text(json.dumps(self.positions, indent=1, ensure_ascii=False))

    # ------------------------------------------------------------------ nagrania

    def flush(self, mint, t):
        if not t["snaps"]:
            return
        RECORDINGS.mkdir(parents=True, exist_ok=True)
        path = RECORDINGS / f"{datetime.fromtimestamp(t['created']).strftime('%Y-%m-%d')}.jsonl.gz"
        rec = {"mint": mint, "symbol": t["symbol"], "created": round(t["created"], 1), "dev_sol": t["dev_sol"],
               "static": t["static"], "snaps": t["snaps"]}
        with gzip.open(path, "at") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")

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
                        self.counts["tokens"] += 1
                        self.tokens.setdefault(m["mint"], self.new_token((m.get("symbol") or "?")[:12], time.time(),
                                                                         round(float(m.get("solAmount") or 0), 3)))
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as e:
                log(f"! strumień zerwany ({e}), łączę ponownie za 5 s")
                await asyncio.sleep(5)

    def tracked(self, now):
        """Tokeny do odpytania. Stare albo martwe kończą nagranie i wypadają z listy."""
        held = {p["mint"] for p in self.positions}
        out = []
        for mint, t in list(self.tokens.items()):
            age = now - t["created"]
            alive = max((s[3] for s in t["snaps"]), default=0) >= KEEP_TRADERS
            if mint in held or age <= ENTRY_WINDOW_S or (alive and age <= RECORD_S):
                out.append(mint)
            else:
                self.flush(mint, t)
                del self.tokens[mint]
        return out

    async def poll(self):
        while True:
            await asyncio.sleep(POLL_S)
            now = time.time()
            if now - self.sol_ts > 60:
                try:
                    self.sol_usd = await asyncio.to_thread(agent.sol_price)
                except agent.ApiError:
                    pass
                self.sol_ts = now
            mints = self.tracked(now)
            infos = {}
            try:
                for i in range(0, len(mints), 50):
                    r = await asyncio.to_thread(jup_get, f"/tokens/v2/search?query={','.join(mints[i:i + 50])}")
                    infos.update({x["id"]: x for x in r or []})
            except agent.ApiError as e:
                log(f"! Jupiter: {e}")
                continue
            now = time.time()
            self.counts["polls"] += 1
            quotes = {}
            for mint, info in infos.items():
                t = self.tokens.get(mint)
                if not t:
                    continue
                snap = snapshot(info, now - t["created"])
                if not snap:
                    continue
                t["static"] = static_info(info)
                t["snaps"].append(snap)
                t["last_seen"] = now
                if t["first"] is None:
                    t["first"] = snap[1]
                quotes[mint] = snap[1]
            # Wyjścia najpierw, potem wejścia (żeby nie przekroczyć limitu otwartych).
            for pos in list(self.positions):
                await self.manage(pos, quotes.get(pos["mint"]), now)
            for mint, price in quotes.items():
                t = self.tokens[mint]
                # Sygnał z poprzedniego odczytu: na sucho wchodzimy po dzisiejszej cenie, jak prawdziwy bot.
                for strategy, f in t.pop("pending", []):
                    await self.enter(mint, strategy, price, now, f)
                f = features(t["snaps"][-1], t["first"], t["static"], t["dev_sol"])
                for strategy, rule in SIGNALS.items():
                    if strategy in t["entered"] or not rule(f):
                        continue
                    t["entered"].add(strategy)
                    if self.live and strategy == LIVE_STRATEGY:
                        await self.enter(mint, strategy, price, now, f)   # prawdziwy zakup od razu
                    else:
                        t.setdefault("pending", []).append((strategy, f))

    async def status(self):
        while True:
            await asyncio.sleep(300)
            rows = [json.loads(l) for l in RESULTS.read_text().splitlines()] if RESULTS.exists() else []
            rows = [r for r in rows if r["exit_ts"] >= time.time() - 3600]
            parts = []
            for s in STRATEGIES:
                rs = [r for r in rows if r["strategy"] == s]
                if rs:
                    parts.append(f"{s}: {len(rs)} · mediana {statistics.median(r['net_pct'] for r in rs):+.0f}% · "
                                 f"{sum(r['usd_pnl'] for r in rs):+.2f} USD")
            c = self.counts
            log(f"ostatnia godzina · {' | '.join(parts) or 'brak zamkniętych'} · otwarte {len(self.positions)} · "
                f"5 min: {c['tokens']} nowych tokenów, {c['polls']} odczytów, pilnuję {len(self.tokens)}"
                + (" · STOP: nowe zakupy wstrzymane" if STOP_FILE.exists() else ""))
            self.counts = {"tokens": 0, "polls": 0}

    async def run(self):
        await asyncio.gather(self.listen(), self.poll(), self.status())


# --------------------------------------------------------------------------- polecenia

def cmd_run(args):
    if USD <= 0 or USD > HARD_MAX_USD:
        sys.exit(f"SNAJPER_USD musi być między 0 a {HARD_MAX_USD:g} USD.")
    if LIVE_STRATEGY not in STRATEGIES:
        sys.exit(f"SNAJPER_STRATEGIA musi być jedną z: {', '.join(STRATEGIES)}.")
    s = Sniper(args.naprawde)
    print(f"SNAJPER · {'NAPRAWDĘ z portfela ' + s.wallet if args.naprawde else 'na sucho, niczego nie kupuję'}")
    print(f"  stawka {fmt_usd(USD)} · cel +{EXITS['tp']:g}% (połowa), stop {EXITS['sl']:g}%, ruchomy stop "
          f"{EXITS['trail']:g}% od szczytu, najwyżej {EXITS['max_hold']:g} s · odczyty co {POLL_S:g} s")
    print(f"  nagrania: {RECORDINGS.relative_to(agent.ROOT)}/ · kaucja "
          + ("wraca (zamykamy konto)" if CLOSE_ACCOUNTS else "zostaje w portfelu"))
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

    def stop(*_):
        raise KeyboardInterrupt
    # Proces uruchomiony w tle może mieć wyłączone Ctrl+C. Przywracamy je i tak samo traktujemy zwykłe zatrzymanie,
    # żeby zawsze zapisać nagrania i otwarte pozycje.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, stop)
    try:
        asyncio.run(s.run())
    except KeyboardInterrupt:
        for mint, t in list(s.tokens.items()):
            s.flush(mint, t)   # nagrania niedokończonych tokenów też się przydadzą
        live = [p for p in s.positions if p.get("live")]
        if live:
            print(f"\nUwaga: {len(live)} prawdziwe pozycje zostały otwarte: "
                  f"{', '.join(p['symbol'] for p in live)}. Uruchom snajpera ponownie, żeby je domknął.")


def stats_of(rows):
    nets = [r["net_pct"] for r in rows]
    if not nets:
        return None
    return {"n": len(nets), "win_pct": round(sum(n > 0 for n in nets) / len(nets) * 100),
            "median_pct": round(statistics.median(nets), 1), "mean_pct": round(statistics.mean(nets), 1),
            "usd": round(sum(r["usd_pnl"] for r in rows), 2),
            "hit_tp_pct": round(sum(any(e["reason"] == "cel" for e in r["exits"]) for r in rows) / len(rows) * 100),
            "dead_pct": round(sum(n <= -80 for n in nets) / len(nets) * 100),
            "best_pct": max(nets)}


def cmd_results(args):
    rows = [json.loads(l) for l in RESULTS.read_text().splitlines()] if RESULTS.exists() else []
    if args.godziny:
        rows = [r for r in rows if r["exit_ts"] >= time.time() - args.godziny * 3600]
    groups = {s: [r for r in rows if r["strategy"] == s and not r.get("live")] for s in STRATEGIES}
    groups["naprawdę"] = [r for r in rows if r.get("live")]
    stats = {k: v for k, v in ((k, stats_of(rs)) for k, rs in groups.items()) if v}
    if args.json:
        print(json.dumps(stats, indent=1, ensure_ascii=False))
        return
    span = f"ostatnie {args.godziny:g} h" if args.godziny else "całość"
    rent = "kaucja wraca (zamykamy konto)" if CLOSE_ACCOUNTS else f"{RENT_SOL:g} SOL kaucji za konto tokenu"
    print(f"SNAJPER · {span} · stawka {fmt_usd(USD)} · koszty {FEE_PCT:g}% w każdą stronę, {TX_SOL:.5f} SOL za "
          f"transakcję, {rent}\n")
    if not stats:
        print("  Jeszcze brak zamkniętych pozycji. Uruchom: python3 agent.py snajper")
    for name, st in stats.items():
        label = f"{name} ({STRATEGIES[name]})" if name in STRATEGIES else name
        print(f"  {label}\n    {st['n']} pozycji · na plusie {st['win_pct']}% · mediana {st['median_pct']:+}% · "
              f"średnio {st['mean_pct']:+}% · razem {st['usd']:+.2f} USD\n"
              f"    doszło do +{EXITS['tp']:g}%: {st['hit_tp_pct']}% · straciło ≥ 80%: {st['dead_pct']}% · "
              f"najlepsza {st['best_pct']:+.0f}%")
    if stats:
        print("\nNa sucho cena wejścia i wyjścia to następny odczyt z Jupitera po sygnale, czyli mniej więcej to, "
              "na co zdążyłby prawdziwy bot. Boty w tym samym bloku co start tokenu i tak są szybsze.")
    old = agent.DATA / "snajper-wyniki-v1.jsonl"
    if old.exists():
        print(f"Wyniki starej wersji (bez filtrów przepływu, kaucja bez zwrotu) są w {old.relative_to(agent.ROOT)}.")
