"""Czuwanie: co kilka minut pilnuje listy tokenów i testuje flipy na sucho, bez wydawania pieniędzy.

Ceny bierze z darmowego API Jupitera (0 kredytów Helius). Gdy token spełni regułę wejścia albo wyjścia,
zapisuje symulowany flip w dane/flipy.json i wysyła powiadomienie na Macu. Niczego nie kupuje:
prawdziwa transakcja idzie zawsze przez `kup` / `sprzedaj` z podglądem i Twoją zgodą.
"""
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

import agent
from agent import ApiError, cfg, fmt_usd, jup_get, risk_flags, token_info

WATCH = agent.DATA / "czuwanie.json"   # obserwowane tokeny: {mint: {symbol, added}}
PRICES = agent.DATA / "ceny.json"      # historia cen: {mint: [[ts, cena], ...]}
FLIPS = agent.DATA / "flipy.json"      # symulowane flipy
ALERTS = agent.DATA / "alerty.json"    # kiedy ostatnio wysłano dane powiadomienie

FLIP_USD = cfg("FLIP_USD", 3.0)                  # symulowana stawka jednego flipa
DIP_PCT = cfg("FLIP_DIP_PCT", -15.0)             # dołek: spadek w ciągu 1 h
DIP_MAX_4H = cfg("FLIP_DIP_MAX_4H_PCT", -35.0)   # ale nie łapiemy noża: w 4 h nie więcej niż tyle
DIP_MAX_24H = cfg("FLIP_DIP_MAX_24H_PCT", -25.0) # ani w 24 h (z Jupitera, bo historia czuwania ma tylko 6 h)
BREAKOUT_PCT = cfg("FLIP_BREAKOUT_PCT", 10.0)    # wybicie: wzrost w ciągu 15 min
TP_PCT = cfg("FLIP_TP_PCT", 20.0)                # realizacja zysku
SL_PCT = cfg("FLIP_SL_PCT", -12.0)               # stop
MAX_HOLD_H = cfg("FLIP_MAX_H", 4.0)              # wyjście najpóźniej po tylu godzinach
COST_PCT = cfg("FLIP_COST_PCT", 3.0)             # koszty w obie strony: opłaty, poślizg, wpływ na cenę
MIN_LIQ = cfg("FLIP_MIN_LIQ", 50_000.0)          # poniżej tej płynności nie wchodzimy
COOLDOWN_H = cfg("FLIP_COOLDOWN_H", 1.0)         # przerwa po wyjściu, zanim ten sam token wejdzie znowu
HISTORY_H = 6                                    # ile historii cen trzymamy
# Automatyczna lista: najbardziej obracane tokeny z Jupitera, z płynnością i bez prawa dodruku czy zamrożenia.
AUTO = cfg("FLIP_AUTO", 1)                       # 0 wyłącza, wtedy lista jest tylko ręczna
AUTO_N = cfg("FLIP_AUTO_N", 25)                  # ile tokenów dobieramy
AUTO_EVERY_H = cfg("FLIP_AUTO_CO_H", 6.0)        # co ile godzin odświeżamy listę
AUTO_MIN_LIQ = cfg("FLIP_AUTO_MIN_LIQ", 100_000.0)
AUTO_MIN_AGE_H = cfg("FLIP_AUTO_MIN_WIEK_H", 24.0)   # świeże tokeny to nie flipy, tylko loteria
AUTO_MAX_MCAP = cfg("FLIP_AUTO_MAX_MCAP", 300_000_000.0)
AUTO_MIN_ORGANIC = cfg("FLIP_AUTO_MIN_ORGANIC", 30.0)  # niżej: obrót głównie od botów
# Podpowiedzi wyjścia dla prawdziwych pozycji agenta, te same progi co w handel.py.
TAKE_PROFIT = cfg("TAKE_PROFIT_PCT", 100.0)
STOP_LOSS = cfg("STOP_LOSS_PCT", -40.0)
ALERT_REPEAT_H = 6                               # to samo ostrzeżenie o pozycji najwyżej co tyle godzin

STRATEGIES = {"dołek": f"spadek ≥ {-DIP_PCT:g}% w 1 h, w 24 h nie więcej niż {-DIP_MAX_24H:g}%", "wybicie": f"wzrost ≥ {BREAKOUT_PCT:g}% w 15 min"}


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save(path, data):
    agent.DATA.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False))


def notify(title, text):
    """Powiadomienie macOS. Bez osascript (inny system) tylko wypisujemy."""
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(["osascript", "-e", f'display notification "{esc(text)}" with title "{esc(title)}" '
                        f'sound name "Glass"'], check=False, timeout=10, capture_output=True)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


# --------------------------------------------------------------------------- ceny

def fetch_prices(mints):
    """{mint: (cena, płynność)} z Jupitera, po 50 naraz."""
    out, mints = {}, list(mints)
    for i in range(0, len(mints), 50):
        for m, p in (jup_get(f"/price/v3?ids={','.join(mints[i:i + 50])}") or {}).items():
            if p and p.get("usdPrice") is not None:
                out[m] = (float(p["usdPrice"]), float(p.get("liquidity") or 0))
    return out


def change(history, now, price, minutes):
    """Zmiana ceny w % względem próbki sprzed `minutes` albo None, jeśli historia jest za krótka."""
    target, tol = now - minutes * 60, max(180, minutes * 60 * 0.25)
    best = min(history, key=lambda s: abs(s[0] - target), default=None)
    if not best or abs(best[0] - target) > tol or best[1] <= 0:
        return None
    return (price / best[1] - 1) * 100


def held_positions():
    """Prawdziwe pozycje agenta z dziennika transakcji: {mint: (symbol, średnie wejście)}."""
    per = {}
    for e in load(agent.DATA / "transakcje.json", []):
        p = per.setdefault(e["mint"], {"symbol": e.get("symbol"), "bought": 0, "sold": 0, "usd": 0})
        if e["side"] == "buy":
            p["bought"] += e["tokens"]
            p["usd"] += e["usd"]
        else:
            p["sold"] += e["tokens"]
    return {m: (p["symbol"], p["usd"] / p["bought"]) for m, p in per.items()
            if p["bought"] and p["sold"] < p["bought"] * 0.99}


# --------------------------------------------------------------------------- lista automatyczna

def auto_pick():
    """Tokeny do flipów: obrót organiczny względem kapitalizacji, czyli to, co naprawdę się rusza."""
    seen = {}
    for path in ("toptraded/24h", "toptrending/24h", "toporganicscore/24h"):
        try:
            for t in jup_get(f"/tokens/v2/{path}?limit=100") or []:
                seen.setdefault(t["id"], t)
        except ApiError as e:
            print(f"  ! Jupiter {path}: {e}", file=sys.stderr)
    now, picks = time.time(), []
    for t in seen.values():
        if t["id"] in agent.SKIP_MINTS or agent.SKIP_TAGS & set(t.get("tags") or []):
            continue
        if (t.get("liquidity") or 0) < AUTO_MIN_LIQ or not 0 < (t.get("mcap") or 0) <= AUTO_MAX_MCAP:
            continue
        if (t.get("organicScore") or 0) < AUTO_MIN_ORGANIC:
            continue
        born = agent.parse_ts((t.get("firstPool") or {}).get("createdAt"))
        if not born or now - born < AUTO_MIN_AGE_H * 3600:
            continue
        flags, _ = risk_flags(t, None)
        if any("dodrukować" in f or "zamrozić" in f for f in flags):
            continue
        s = t.get("stats24h") or {}
        organic = (s.get("buyOrganicVolume") or 0) + (s.get("sellOrganicVolume") or 0)
        picks.append((organic / t["mcap"], t))
    picks.sort(key=lambda x: x[0], reverse=True)
    return [t for _, t in picks[:AUTO_N]]


def refresh_auto(watch, flips):
    """Podmienia tokeny dobrane automatycznie. Ręcznie dodane i te z otwartym flipem zostają."""
    picks = auto_pick()
    if not picks:
        return [], []   # Jupiter nie odpowiedział: zostawiamy starą listę
    busy = {f["mint"] for f in flips if not f.get("exit_ts")}
    new = {t["id"]: t for t in picks}
    removed = [m for m, v in watch.items() if v.get("auto") and m not in new and m not in busy]
    for m in removed:
        watch.pop(m)
    added = [m for m in new if m not in watch]
    today = datetime.now(timezone.utc).date().isoformat()
    for m in added:
        watch[m] = {"symbol": new[m]["symbol"], "added": today, "auto": True}
    return added, removed


# --------------------------------------------------------------------------- jeden przebieg

def tick(quiet=False):
    watch = load(WATCH, {})
    alerts = load(ALERTS, {})
    auto_note = ""
    if AUTO and time.time() - alerts.get("_auto_ts", 0) >= AUTO_EVERY_H * 3600:
        added, removed = refresh_auto(watch, load(FLIPS, []))
        alerts["_auto_ts"] = time.time()
        save(WATCH, watch)
        save(ALERTS, alerts)
        auto_note = f" · lista automatyczna odświeżona: +{len(added)} −{len(removed)}"
    if not watch:
        sys.exit("Lista obserwowanych tokenów jest pusta. Dodaj token: python3 agent.py obserwuj --dodaj MINT")
    held = held_positions()
    now = time.time()
    quotes = fetch_prices(set(watch) | set(held))
    history = load(PRICES, {})
    flips = load(FLIPS, [])
    events = []
    ch24_cache = {}

    def ch24(mint):
        """Zmiana ceny w 24 h z Jupitera, pobierana tylko gdy dołek się zapali. None, gdy brak danych."""
        if mint not in ch24_cache:
            try:
                ch24_cache[mint] = ((token_info([mint]).get(mint) or {}).get("stats24h") or {}).get("priceChange")
            except ApiError:
                ch24_cache[mint] = None
        return ch24_cache[mint]

    def event(title, text):
        events.append(f"{title}: {text}")
        if not quiet:
            notify(title, text)

    # Wyjścia z otwartych flipów. Brak ceny oznacza, że tokenu nie da się już sprzedać.
    for f in flips:
        if f.get("exit_ts"):
            continue
        price = quotes.get(f["mint"], (0.0, 0))[0]
        gross = (price / f["entry"] - 1) * 100
        age_h = (now - f["entry_ts"]) / 3600
        reason = ("brak ceny" if not price else "cel" if gross >= TP_PCT else "stop" if gross <= SL_PCT
                  else "czas" if age_h >= MAX_HOLD_H else None)
        if reason:
            net = gross - COST_PCT
            f.update(exit_ts=now, exit=price, reason=reason, gross_pct=round(gross, 1), net_pct=round(net, 1))
            event(f"Flip na sucho: wyjście {f['symbol']}",
                  f"{reason}, {net:+.0f}% po kosztach ({f['strategy']}, {age_h:.1f} h)")

    # Wejścia: najwyżej jeden otwarty flip na token i strategię, z przerwą po wyjściu.
    for mint, meta in watch.items():
        if mint not in quotes:
            continue
        price, liq = quotes[mint]
        hist = history.get(mint, [])
        ch15, ch60, ch240 = (change(hist, now, price, m) for m in (15, 60, 240))
        triggers = {"dołek": ch60 is not None and ch60 <= DIP_PCT and (ch240 is None or ch240 > DIP_MAX_4H),
                    "wybicie": ch15 is not None and ch15 >= BREAKOUT_PCT}
        for strategy, hit in triggers.items():
            if not hit or liq < MIN_LIQ:
                continue
            mine = [f for f in flips if f["mint"] == mint and f["strategy"] == strategy]
            if any(not f.get("exit_ts") for f in mine) or any(now - f["exit_ts"] < COOLDOWN_H * 3600 for f in mine):
                continue
            ch1d = ch24(mint) if strategy == "dołek" else None
            if strategy == "dołek" and (ch1d is None or ch1d <= DIP_MAX_24H):
                continue  # długi trend spadkowy albo brak danych: to nie dołek, tylko nóż
            flips.append({"mint": mint, "symbol": meta["symbol"], "strategy": strategy, "entry_ts": now,
                          "date": datetime.now(timezone.utc).isoformat(), "entry": price, "liquidity": round(liq),
                          "ch15": ch15 and round(ch15, 1), "ch60": ch60 and round(ch60, 1),
                          "ch240": ch240 and round(ch240, 1), "ch24h": ch1d and round(ch1d, 1)})
            move = ch60 if strategy == "dołek" else ch15
            event(f"Flip na sucho: wejście {meta['symbol']}",
                  f"{strategy} {move:+.0f}% · płynność {fmt_usd(liq)} · cel +{TP_PCT:g}%, stop {SL_PCT:g}%")

    # Prawdziwe pozycje agenta: ostrzeż o celu albo stopie, ale nie częściej niż co ALERT_REPEAT_H.
    for mint, (symbol, entry) in held.items():
        if mint not in quotes:
            continue
        pnl = (quotes[mint][0] / entry - 1) * 100
        kind = "cel" if pnl >= TAKE_PROFIT else "stop" if pnl <= STOP_LOSS else None
        key = f"{mint}:{kind}"
        if kind and now - alerts.get(key, 0) >= ALERT_REPEAT_H * 3600:
            alerts[key] = now
            what = "sprzedaż połowy" if kind == "cel" else "sprzedaż całości"
            event(f"Pozycja agenta: {symbol} {pnl:+.0f}%", f"{kind} → rozważ {what}, otwórz /wieloryby")

    for mint, (price, _) in quotes.items():
        history[mint] = [s for s in history.get(mint, []) if now - s[0] <= HISTORY_H * 3600] + [[now, price]]
    history = {m: h for m, h in history.items() if m in watch or m in held}
    save(PRICES, history)
    save(FLIPS, flips)
    save(ALERTS, alerts)

    open_n = sum(not f.get("exit_ts") for f in flips)
    stamp = datetime.now().strftime("%H:%M")
    print(f"{stamp}  {len(quotes)} tokenów · otwarte flipy na sucho: {open_n}{auto_note}" + "".join(f"\n  • {e}" for e in events),
          flush=True)


def cmd_watch(args):
    if not args.co:
        tick(args.cicho)
        return
    print(f"Czuwam co {args.co:g} min (Ctrl+C kończy). Flipy tylko na sucho, niczego nie kupuję.", flush=True)
    while True:
        try:
            tick(args.cicho)
        except ApiError as e:
            print(f"  ! Jupiter: {e}. Spróbuję w następnym przebiegu.", file=sys.stderr, flush=True)
        except KeyboardInterrupt:
            return
        try:
            time.sleep(args.co * 60)
        except KeyboardInterrupt:
            return


# --------------------------------------------------------------------------- lista tokenów

def cmd_list(args):
    watch = load(WATCH, {})
    if getattr(args, "auto", False):
        added, removed = refresh_auto(watch, load(FLIPS, []))
        alerts = load(ALERTS, {})
        alerts["_auto_ts"] = time.time()
        save(WATCH, watch)
        save(ALERTS, alerts)
        sym = lambda ms: ", ".join(watch.get(m, {}).get("symbol", m[:6]) for m in ms) or "—"
        print(f"Lista automatyczna odświeżona. Dodane: {sym(added)} · usunięte: {len(removed)}\n")
    if args.dodaj:
        t = token_info([args.dodaj]).get(args.dodaj)
        if not t:
            sys.exit("Jupiter nie zna tego tokenu. Sprawdź adres.")
        flags, _ = risk_flags(t, None)
        hard = [f for f in flags if "dodrukować" in f or "zamrozić" in f]
        if hard:
            sys.exit(f"Nie dodaję {t['symbol']}: {', '.join(hard)}.")
        watch[args.dodaj] = {"symbol": t["symbol"], "added": datetime.now(timezone.utc).date().isoformat()}
        save(WATCH, watch)
        print(f"Dodano {t['symbol']} · płynność {fmt_usd(t.get('liquidity'))}"
              + (f" · ⚠ poniżej progu wejścia {fmt_usd(MIN_LIQ)}" if (t.get("liquidity") or 0) < MIN_LIQ else ""))
        return
    if args.usun:
        key = next((m for m, v in watch.items() if args.usun in (m, v["symbol"])), None)
        if not key:
            sys.exit("Nie ma takiego tokenu na liście.")
        print(f"Usunięto {watch.pop(key)['symbol']}")
        save(WATCH, watch)
        return
    if not watch:
        print("Lista jest pusta. Dodaj token: python3 agent.py obserwuj --dodaj MINT")
        return
    quotes = fetch_prices(watch)
    history, now = load(PRICES, {}), time.time()
    print(f"OBSERWOWANE TOKENY · zmiana w 15 min / 1 h / 4 h (z historii czuwania)\n")
    for mint, meta in watch.items():
        price, liq = quotes.get(mint, (0.0, 0))
        hist = history.get(mint, [])
        chs = [change(hist, now, price, m) for m in (15, 60, 240)] if price else [None] * 3
        cols = " / ".join(f"{c:+.0f}%" if c is not None else "?" for c in chs)
        low = "  ⚠ płynność poniżej progu" if liq < MIN_LIQ else ""
        tag = "  (auto)" if meta.get("auto") else ""
        print(f"  {meta['symbol']:<10} płynność {fmt_usd(liq):>6}  {cols}{low}{tag}")
    if not history:
        print("\nHistoria cen jeszcze się nie zebrała: uruchom czuwanie (agent.py czuwaj --co 2).")


# --------------------------------------------------------------------------- wyniki flipów

def flip_stats(closed):
    nets = [f["net_pct"] for f in closed]
    if not nets:
        return None
    return {"n": len(nets), "win_pct": round(sum(n > 0 for n in nets) / len(nets) * 100),
            "median_pct": round(statistics.median(nets), 1), "mean_pct": round(statistics.mean(nets), 1),
            "usd": round(sum(nets) / 100 * FLIP_USD, 2),
            "reasons": {r: sum(f["reason"] == r for f in closed) for r in dict.fromkeys(f["reason"] for f in closed)}}


def cmd_flips(args):
    flips = load(FLIPS, [])
    closed = [f for f in flips if f.get("exit_ts")]
    stats = {s: flip_stats([f for f in closed if f["strategy"] == s]) for s in STRATEGIES}
    stats["razem"] = flip_stats(closed)
    if args.json:
        print(json.dumps({"stats": stats, "open": [f for f in flips if not f.get("exit_ts")]}, indent=1,
                         ensure_ascii=False))
        return
    print(f"FLIPY NA SUCHO · stawka {fmt_usd(FLIP_USD)} · koszty {COST_PCT:g}% w obie strony · "
          f"cel +{TP_PCT:g}%, stop {SL_PCT:g}%, najwyżej {MAX_HOLD_H:g} h\n")
    for name, st in stats.items():
        label = f"{name} ({STRATEGIES[name]})" if name in STRATEGIES else name
        if not st:
            print(f"  {label}: jeszcze brak zamkniętych flipów")
            continue
        reasons = ", ".join(f"{n} × {r}" for r, n in st["reasons"].items())
        print(f"  {label}: {st['n']} flipów · na plusie {st['win_pct']}% · mediana {st['median_pct']:+}% · "
              f"średnio {st['mean_pct']:+}% · razem {st['usd']:+.2f} USD · {reasons}")
    opened = [f for f in flips if not f.get("exit_ts")]
    if opened:
        print("\nOtwarte:")
        quotes = fetch_prices({f["mint"] for f in opened})
        for f in opened:
            price = quotes.get(f["mint"], (0.0, 0))[0]
            age = (time.time() - f["entry_ts"]) / 3600
            print(f"  {f['symbol']:<10} {f['strategy']:<8} {(price / f['entry'] - 1) * 100:+.0f}% przed kosztami · "
                  f"od {age:.1f} h")
    n = stats["razem"]["n"] if stats["razem"] else 0
    if n < 20:
        print(f"\nZamkniętych flipów: {n}. Wnioski dopiero przy ok. 20–30, wcześniej to głównie przypadek.")
    print("Wynik jest przybliżony: cenę sprawdzamy co kilka minut, więc prawdziwe wyjście mogło być lepsze albo gorsze.")
