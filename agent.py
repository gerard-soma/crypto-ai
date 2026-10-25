#!/usr/bin/env python3
"""Agent wielorybów: śledzi skuteczne portfele na Solanie i podpowiada tokeny do kupna w jup.ag.

Polecenia:
  python3 agent.py odkryj          # znajduje skuteczne portfele i zapisuje listę obserwowanych
  python3 agent.py raport          # sprawdza, co te portfele kupują, i daje ranking sygnałów
  python3 agent.py portfel ADRES   # pokazuje ostatnie transakcje jednego portfela
  python3 agent.py wyniki          # dziennik sygnałów: jak zachowały się po 24 h i 72 h

Tylko odczyt danych: agent nie zna Twojego klucza prywatnego i niczego nie kupuje.
"""
import argparse
import atexit
import json
import math
import os
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "dane"
WATCHLIST = DATA / "portfele.json"
REPORTS = DATA / "raporty"
JOURNAL = DATA / "dziennik.json"
USAGE = DATA / "helius.json"

SOL = "So11111111111111111111111111111111111111112"  # natywny SOL i WSOL liczymy razem
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE = {SOL, USDC, USDT}
# Płynne stakingi SOL i stablecoiny: to nie są „okazje”, tylko przechowywanie wartości.
SKIP_MINTS = QUOTE | {
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",
    "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v",
    "5oVNBeEEQvYi1cX3ir8Dx5n1P7pdxydbGF2X4TxVusJm",
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj",
}
SKIP_TAGS = {"stocks", "rwa", "stablecoin", "lst"}


def load_env():
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()


def cfg(name, default):
    raw = os.environ.get(name)
    return type(default)(raw) if raw not in (None, "") else default


HELIUS_KEY = os.environ.get("HELIUS_API_KEY", "")
HELIUS_BASE = cfg("HELIUS_BASE", "https://api-mainnet.helius-rpc.com")
HELIUS_INTERVAL = cfg("HELIUS_INTERVAL", 0.6)  # sekundy między zapytaniami; płatny plan zniesie 0.1
JUP_KEY = os.environ.get("JUP_API_KEY", "")
JUP_BASE = "https://api.jup.ag" if JUP_KEY else "https://lite-api.jup.ag"

MIN_BUY_USD = cfg("MIN_BUY_USD", 150.0)          # od jakiej kwoty zakupu portfel uznajemy za „duży”
MIN_WIN_ROI = cfg("MIN_WIN_ROI", 1.0)            # 1.0 = co najmniej podwoił pieniądze na tokenie
MAX_WALLETS = cfg("MAX_WALLETS", 30)             # ile portfeli obserwujemy
DISCOVERY_TOKENS = cfg("DISCOVERY_TOKENS", 15)   # na ilu zwycięskich tokenach szukamy portfeli
DISCOVERY_DAYS = cfg("DISCOVERY_DAYS", 3.0)      # jak daleko wstecz patrzymy przy odkrywaniu
LOOKBACK_HOURS = cfg("LOOKBACK_HOURS", 24.0)     # okno raportu
MIN_SIGNAL_USD = cfg("MIN_SIGNAL_USD", 50.0)     # minimalny zakup netto, który liczymy jako sygnał
MAX_TRADES_PER_DAY = cfg("MAX_TRADES_PER_DAY", 20)  # zamian dziennie; częściej handlują boty i skalperzy, których nie da się skopiować
# Profil portfela z jego własnej historii: jedno trafienie to może być fart, liczymy też przegrane.
PROFILE_PAGES = cfg("PROFILE_PAGES", 3)          # ile stron (po 100 transakcji) historii portfela czytamy
MIN_TOKENS = cfg("MIN_TOKENS", 3)                # na ilu tokenach musi mieć ocenialny wynik
MIN_WINS = cfg("MIN_WINS", 2)                    # na ilu różnych tokenach musi być na plusie
MIN_WIN_RATE = cfg("MIN_WIN_RATE", 0.3)          # odsetek tokenów na plusie
MIN_HOLD_HOURS = cfg("MIN_HOLD_HOURS", 1.0)      # mediana czasu od zakupu do sprzedaży; krócej = flipper

HELIUS_BUDGET = cfg("HELIUS_MONTHLY_BUDGET", 300_000)  # twardy limit kredytów na miesiąc (darmowy plan: 1 mln)
DISCOVERY_COOLDOWN_H = cfg("DISCOVERY_COOLDOWN_H", 20.0)  # odkrywanie najwyżej raz na tyle godzin
REPORT_CACHE_MIN = cfg("REPORT_CACHE_MIN", 60.0)  # raport młodszy niż tyle minut pokazujemy z pamięci

CREDITS_PER_CALL = 100  # Enhanced Transactions API Helius
helius_calls = 0
_lock = threading.Lock()
_last_call = [0.0]


# --------------------------------------------------------------------------- budżet Helius

def load_usage():
    return json.loads(USAGE.read_text()) if USAGE.exists() else {}


def month_key():
    return datetime.now().strftime("%Y-%m")


_used_before = [None]  # kredyty zużyte w tym miesiącu przed tym uruchomieniem


def used_before():
    if _used_before[0] is None:
        _used_before[0] = load_usage().get(month_key(), 0)
    return _used_before[0]


def save_usage(**extra):
    if not helius_calls and not extra:
        return
    usage = load_usage()
    usage[month_key()] = used_before() + helius_calls * CREDITS_PER_CALL
    usage.update(extra)
    DATA.mkdir(exist_ok=True)
    USAGE.write_text(json.dumps(usage, indent=1))


atexit.register(save_usage)


def require_budget(estimate, what):
    left = HELIUS_BUDGET - used_before()
    if estimate > left:
        sys.exit(f"{what} zużyłoby ok. {estimate:,} kredytów Helius, a w tym miesiącu zostało {max(left, 0):,} "
                 f"z budżetu {HELIUS_BUDGET:,} (HELIUS_MONTHLY_BUDGET w .env).")


def usage_line():
    session = helius_calls * CREDITS_PER_CALL
    return (f"kredyty Helius: ~{session:,} teraz · ~{used_before() + session:,} w tym miesiącu "
            f"z budżetu {HELIUS_BUDGET:,}")


# --------------------------------------------------------------------------- HTTP

def http_json(url, headers=None, tries=6):
    req = urllib.request.Request(url, headers={"User-Agent": "agent-wielorybow/1.0", **(headers or {})})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2 ** attempt + 1)  # 2, 3, 5, 9, 17 s: darmowy plan Helius ma niski limit
                continue
            raise ApiError(e.code, body, url) from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < tries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise ApiError(0, str(e), url) from None


class ApiError(Exception):
    def __init__(self, code, body, url):
        self.code, self.body = code, body
        safe_url = re.sub(r"api-key=[^&]+", "api-key=***", url)
        super().__init__(f"HTTP {code} dla {safe_url}: {body[:300]}")


def helius_page(address, params):
    """Jedna strona sparsowanych transakcji z Helius. Na darmowym planie ok. 2 zapytania/s."""
    global helius_calls
    if not HELIUS_KEY:
        sys.exit("Brak HELIUS_API_KEY. Skopiuj .env.example do .env i wklej klucz (instrukcja w README.md).")
    with _lock:
        if used_before() + (helius_calls + 1) * CREDITS_PER_CALL > HELIUS_BUDGET:
            raise ApiError(0, "miesięczny budżet Helius wyczerpany (HELIUS_MONTHLY_BUDGET)", "helius")
        wait = _last_call[0] + HELIUS_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        helius_calls += 1
    q = urllib.parse.urlencode({"api-key": HELIUS_KEY, "limit": 100, **params})
    return http_json(f"{HELIUS_BASE}/v0/addresses/{address}/transactions?{q}")


SIG_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{80,90}")


def helius_txs(address, pages=1, gte=None, lte=None, asc=False):
    """Transakcje adresu w oknie czasu, stronicowane. Zwraca listę od najnowszej (lub najstarszej przy asc)."""
    out = []
    cursor_key = "after-signature" if asc else "before-signature"
    params = {"sort-order": "asc" if asc else "desc"}
    if gte:
        params["gte-time"] = int(gte)
    if lte:
        params["lte-time"] = int(lte)
    for _ in range(pages):
        try:
            batch = helius_page(address, params)
        except ApiError as e:
            # Helius czasem prosi o kontynuację od wskazanej sygnatury, zamiast zwrócić pustą stronę.
            m = SIG_RE.search(e.body) if e.code == 404 else None
            if m:
                params[cursor_key] = m.group(0)
                continue
            if e.code in (401, 403):
                sys.exit(f"Helius odrzucił klucz API ({e.code}). Sprawdź HELIUS_API_KEY w .env.")
            raise
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        params[cursor_key] = batch[-1]["signature"]
    return out


# --------------------------------------------------------------------------- Jupiter

def jup_get(path):
    return http_json(f"{JUP_BASE}{path}", {"x-api-key": JUP_KEY} if JUP_KEY else None)


def sol_price():
    return float(jup_get(f"/price/v3?ids={SOL}")[SOL]["usdPrice"])


def token_prices(mints):
    """Ceny USD z Jupitera, po 50 naraz. Tokenów bez ceny (martwych) nie ma w wyniku."""
    prices, mints = {}, list(mints)
    for i in range(0, len(mints), 50):
        for m, p in (jup_get(f"/price/v3?ids={','.join(mints[i:i + 50])}") or {}).items():
            if p and p.get("usdPrice") is not None:
                prices[m] = float(p["usdPrice"])
    return prices


def token_info(mints):
    """Dane rynkowe i audyt tokenów z Jupitera, po 50 naraz."""
    info, mints = {}, list(mints)
    for i in range(0, len(mints), 50):
        chunk = ",".join(mints[i:i + 50])
        for t in jup_get(f"/tokens/v2/search?query={chunk}"):
            info[t["id"]] = t
    return info


def parse_ts(iso):
    if not iso:
        return None
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


# --------------------------------------------------------------------------- transakcje

def owner_deltas(tx, owner):
    """Zmiana sald właściciela w transakcji: {mint: ilość}. SOL i WSOL sumujemy pod SOL."""
    d = {}
    fee = tx.get("fee", 0) if tx.get("feePayer") == owner else 0
    for acc in tx.get("accountData") or []:
        if acc.get("account") == owner:
            d[SOL] = d.get(SOL, 0) + (acc.get("nativeBalanceChange", 0) + fee) / 1e9
        for ch in acc.get("tokenBalanceChanges") or []:
            if ch.get("userAccount") == owner:
                raw = ch["rawTokenAmount"]
                d[ch["mint"]] = d.get(ch["mint"], 0) + int(raw["tokenAmount"]) / 10 ** int(raw["decimals"])
    return d


def classify(deltas, sol_usd):
    """Zamienia zmiany sald na transakcję: ('buy'|'sell', mint, ilość tokenu, wartość USD) albo None."""
    quote_usd = deltas.get(SOL, 0) * sol_usd + deltas.get(USDC, 0) + deltas.get(USDT, 0)
    others = {m: a for m, a in deltas.items() if m not in QUOTE and a != 0}
    if len(others) != 1:
        return None  # transfer, zamiana token na token albo coś złożonego: pomijamy
    mint, amt = next(iter(others.items()))
    if amt > 0 and quote_usd < -1:
        return "buy", mint, amt, -quote_usd
    if amt < 0 and quote_usd > 1:
        return "sell", mint, -amt, quote_usd
    return None


def wallet_trades(txs, wallet, sol_usd):
    trades = []
    for tx in txs:
        if tx.get("transactionError"):
            continue
        c = classify(owner_deltas(tx, wallet), sol_usd)
        if c:
            side, mint, amt, usd = c
            trades.append({"ts": tx.get("timestamp", 0), "side": side, "mint": mint, "amount": amt,
                           "usd": usd, "sig": tx.get("signature")})
    return trades


def trades_per_day(trades, days=None):
    """Zamiany (nie wszystkie transakcje: spam i przelewy zawyżają wynik) na dobę. Bez okna liczymy rozpiętość historii."""
    if days is None:
        if len(trades) < 10:
            return 0.0
        stamps = [t["ts"] for t in trades]
        days = max((max(stamps) - min(stamps)) / 86400, 1 / 24)
    return len(trades) / days


def wallet_profile(trades, prices):
    """Wynik portfela na każdym tokenie z jego historii: wygrane, przegrane i jak długo trzyma."""
    per = {}
    for tr in sorted(trades, key=lambda t: t["ts"]):
        if tr["mint"] in SKIP_MINTS:
            continue
        p = per.setdefault(tr["mint"], {"buy_usd": 0, "buy_amt": 0, "sell_usd": 0, "sell_amt": 0,
                                        "last_buy": None, "holds": []})
        p[f"{tr['side']}_usd"] += tr["usd"]
        p[f"{tr['side']}_amt"] += tr["amount"]
        if tr["side"] == "buy":
            p["last_buy"] = tr["ts"]
        elif p["last_buy"] is not None:
            p["holds"].append(tr["ts"] - p["last_buy"])
    rois, holds, pnl = [], [], 0.0
    for mint, p in per.items():
        if p["buy_usd"] < 20:
            continue  # kupione przed oknem historii albo grosze: nie da się ocenić
        sell_usd = p["sell_usd"]
        if p["sell_amt"] > p["buy_amt"] * 1.05:
            sell_usd *= p["buy_amt"] / p["sell_amt"]  # część sprzedanych tokenów kupił przed oknem
        value = sell_usd + max(p["buy_amt"] - p["sell_amt"], 0) * prices.get(mint, 0)
        rois.append(value / p["buy_usd"] - 1)
        pnl += value - p["buy_usd"]
        holds += p["holds"]
    wins = sum(r > 0 for r in rois)
    return {"tokens": len(rois), "won": wins, "win_rate": round(wins / len(rois), 2) if rois else 0,
            "pnl_usd": round(pnl), "hold_h": round(statistics.median(holds) / 3600, 1) if holds else None}


def profile_problem(prof):
    """Powód odrzucenia portfela albo None, jeśli przechodzi."""
    if prof["trades_per_day"] > MAX_TRADES_PER_DAY:
        return "bot lub skalper"
    if prof["hold_h"] is not None and prof["hold_h"] < MIN_HOLD_HOURS:
        return "flipper (krótko trzyma)"
    if prof["tokens"] < MIN_TOKENS:
        return "za mało historii"
    if prof["won"] < MIN_WINS or prof["win_rate"] < MIN_WIN_RATE or prof["pnl_usd"] <= 0:
        return "słaba skuteczność"
    return None


# --------------------------------------------------------------------------- odkrywanie portfeli

def winning_tokens():
    """Tokeny, które mocno urosły i mają sensowną płynność: na nich szukamy portfeli, które weszły wcześnie."""
    seen = {}
    for path in ("toptrending/24h", "toporganicscore/24h", "toptraded/24h", "toptrending/6h"):
        try:
            for t in jup_get(f"/tokens/v2/{path}?limit=100"):
                seen.setdefault(t["id"], t)
        except ApiError as e:
            print(f"  ! Jupiter {path}: {e}", file=sys.stderr)
    picks = []
    for t in seen.values():
        ch = (t.get("stats24h") or {}).get("priceChange") or 0
        if t["id"] in SKIP_MINTS or SKIP_TAGS & set(t.get("tags") or []):
            continue
        if ch < 50 or (t.get("liquidity") or 0) < 30_000 or (t.get("mcap") or 0) < 100_000:
            continue
        picks.append((ch * (t.get("organicScore") or 1) / 100, t))
    picks.sort(key=lambda p: p[0], reverse=True)
    return [t for _, t in picks[:DISCOVERY_TOKENS]]


def early_traders(token, sol_usd):
    """Próbkuje transakcje tokenu w 4 momentach okna i liczy wynik każdego kupującego."""
    mint, price = token["id"], float(token.get("usdPrice") or 0)
    now = time.time()
    created = parse_ts((token.get("firstPool") or {}).get("createdAt")) or parse_ts(token.get("createdAt")) or 0
    start = max(created + 60, now - DISCOVERY_DAYS * 86400)  # pierwsza minuta to snajperzy i boty
    if price <= 0 or start >= now:
        return {}
    txs = []
    for k in range(4):
        txs += helius_txs(mint, pages=2, gte=start + (now - start) * k / 4, asc=True)

    # Konta, które pojawiają się w wielu transakcjach, to pule, routery i przekaźniki opłat.
    counts = {}
    for tx in txs:
        accts = {tx.get("feePayer")} | {c.get("userAccount") for a in tx.get("accountData") or []
                                        for c in a.get("tokenBalanceChanges") or [] if c.get("mint") == mint}
        for a in accts - {None}:
            counts[a] = counts.get(a, 0) + 1
    frequent = {a for a, n in counts.items() if n > max(10, 0.15 * len(txs))}

    stats = {}
    for tx in txs:
        if tx.get("transactionError"):
            continue
        cands = [tx.get("feePayer")] + [c.get("userAccount") for a in tx.get("accountData") or []
                                        for c in a.get("tokenBalanceChanges") or [] if c.get("mint") == mint]
        for w in dict.fromkeys(c for c in cands if c and c not in frequent):
            c = classify(owner_deltas(tx, w), sol_usd)
            if not c or c[1] != mint:
                continue
            s = stats.setdefault(w, {"buy_usd": 0, "buy_amt": 0, "sell_usd": 0, "sell_amt": 0, "n": 0})
            side, _, amt, usd = c
            s[f"{side}_usd"] += usd
            s[f"{side}_amt"] += amt
            s["n"] += 1
            break
    winners = {}
    for w, s in stats.items():
        if s["buy_usd"] < MIN_BUY_USD or s["buy_amt"] <= 0 or s["n"] > 30:
            continue
        held = max(s["buy_amt"] - s["sell_amt"], 0)
        pnl = s["sell_usd"] + held * price - s["buy_usd"]
        roi = pnl / s["buy_usd"]
        if roi >= MIN_WIN_ROI:
            winners[w] = {"symbol": token.get("symbol"), "mint": mint, "cost": round(s["buy_usd"]),
                          "roi": round(roi, 2), "entry": s["buy_usd"] / s["buy_amt"]}
    return winners


def safe_early_traders(token, sol_usd):
    try:
        return early_traders(token, sol_usd)
    except ApiError as e:
        print(f"  ! {token.get('symbol')}: pomijam ({e})", file=sys.stderr)
        return {}


def cmd_discover(args):
    since_last = (time.time() - load_usage().get("last_discovery", 0)) / 3600
    if since_last < DISCOVERY_COOLDOWN_H and not args.wymus:
        sys.exit(f"Ostatnie odkrywanie było {since_last:.0f} h temu, a limit to raz na {DISCOVERY_COOLDOWN_H:g} h "
                 f"(ok. 30 tys. kredytów Helius). Lista portfeli jest aktualna. Wymuszenie: odkryj --wymus")
    require_budget(40_000, "Odkrywanie")
    sol_usd = sol_price()
    tokens = winning_tokens()
    if not tokens:
        print("Nie znalazłem dziś tokenów, które spełniają kryteria wzrostu i płynności. Spróbuj później.")
        return
    print(f"Szukam portfeli na {len(tokens)} tokenach, które urosły w 24 h:")
    for t in tokens:
        print(f"  • {t.get('symbol'):<10} +{(t.get('stats24h') or {}).get('priceChange', 0):.0f}%  "
              f"płynność ${t.get('liquidity', 0):,.0f}")

    found = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        for token, winners in zip(tokens, pool.map(lambda t: safe_early_traders(t, sol_usd), tokens)):
            print(f"  {token.get('symbol')}: {len(winners)} portfeli z wynikiem ≥ +{MIN_WIN_ROI:.0%}")
            for w, win in winners.items():
                found.setdefault(w, []).append(win)

    # Najpierw ci, którzy wygrali na kilku tokenach, potem najwyższy zwrot.
    def score(wins):
        return len(wins) * 10 + sum(math.log2(1 + min(x["roi"], 50)) for x in wins)

    ranked = dict(sorted(found.items(), key=lambda kv: score(kv[1]), reverse=True)[: MAX_WALLETS * 2])

    # Każdy kandydat i każdy już obserwowany portfel dostaje świeży profil z własnej historii.
    old = load_watchlist()
    candidates = list(dict.fromkeys([*ranked, *old]))
    print(f"\nSprawdzam historię {len(candidates)} portfeli (skuteczność, boty, czas trzymania)…")

    def fetch(w):
        try:
            return w, helius_txs(w, pages=PROFILE_PAGES)
        except ApiError as e:
            print(f"  ! {short(w)}: pomijam ({e})", file=sys.stderr)
            return w, []

    with ThreadPoolExecutor(max_workers=3) as pool:
        histories = dict(pool.map(fetch, candidates))
    trades = {w: wallet_trades(txs, w, sol_usd) for w, txs in histories.items()}
    prices = token_prices({t["mint"] for ts in trades.values() for t in ts} - SKIP_MINTS)

    today = datetime.now(timezone.utc).date().isoformat()
    book, rejected = {}, {}
    for w in candidates:
        if not histories[w]:
            continue
        prof = {**wallet_profile(trades[w], prices), "trades_per_day": round(trades_per_day(trades[w]))}
        problem = profile_problem(prof)
        if problem:
            rejected[problem] = rejected.get(problem, 0) + 1
            continue
        prev = old.get(w, {})
        base = prev.get("base", prev.get("score", 0)) * 0.8 + score(ranked.get(w, []))  # stare wyniki słabną
        book[w] = {"score": round(base * (0.5 + prof["win_rate"]), 2), "base": round(base, 2),
                   "wins": (ranked.get(w, []) + prev.get("wins", []))[:10], **prof,
                   "found": prev.get("found", today), "updated": today}
    book = dict(sorted(book.items(), key=lambda kv: kv[1]["score"], reverse=True)[:MAX_WALLETS])
    DATA.mkdir(exist_ok=True)
    WATCHLIST.write_text(json.dumps(book, indent=1, ensure_ascii=False))
    save_usage(last_discovery=time.time())

    if rejected:
        print("Odrzucone: " + ", ".join(f"{n} × {why}" for why, n in sorted(rejected.items(), key=lambda kv: -kv[1])))
    print(f"\nObserwuję {len(book)} portfeli (zapisane w {WATCHLIST.relative_to(ROOT)}). Najlepsze:")
    for w, e in list(book.items())[:10]:
        hold = f"{e['hold_h']:g} h" if e["hold_h"] is not None else "nie sprzedaje"
        print(f"  {short(w)}  wynik {e['score']:>5}  na plusie {e['won']}/{e['tokens']} tokenów "
              f"({e['win_rate']:.0%}) · wynik ${e['pnl_usd']:,} · trzyma {hold} · {e['trades_per_day']} zamian/dzień")
    print(f"\nZużyte {usage_line()}")


# --------------------------------------------------------------------------- raport

def load_watchlist():
    return json.loads(WATCHLIST.read_text()) if WATCHLIST.exists() else {}


def short(addr):
    return f"{addr[:4]}…{addr[-4:]}"


def jup_link(mint):
    return f"https://jup.ag/swap?sell={SOL}&buy={mint}"


def risk_flags(t, avg_entry):
    flags, penalty = [], 1.0
    audit = t.get("audit") or {}
    if t.get("mintAuthority") and not audit.get("mintAuthorityDisabled"):
        flags.append("twórca może dodrukować tokeny"); penalty *= 0.2
    if t.get("freezeAuthority") and not audit.get("freezeAuthorityDisabled"):
        flags.append("twórca może zamrozić tokeny"); penalty *= 0.2
    liq = t.get("liquidity") or 0
    if liq < 25_000:
        flags.append(f"niska płynność ${liq:,.0f}"); penalty *= 0.3
    top = audit.get("topHoldersPercentage") or 0
    if top > 40:
        flags.append(f"top 10 posiadaczy ma {top:.0f}%"); penalty *= 0.5
    if t.get("organicScoreLabel") == "low":
        flags.append("mało organicznego ruchu (dużo botów)"); penalty *= 0.7
    created = parse_ts(t.get("createdAt"))
    if created and time.time() - created < 3600:
        flags.append("token młodszy niż 1 h"); penalty *= 0.6
    price = t.get("usdPrice") or 0
    if avg_entry and price / avg_entry > 2:
        flags.append(f"cena już {price / avg_entry:.1f}× wyżej niż ich wejście"); penalty *= 0.5
    return flags, penalty


def cmd_report(args):
    book = load_watchlist()
    if not book:
        sys.exit("Lista obserwowanych portfeli jest pusta. Najpierw uruchom: python3 agent.py odkryj")
    cached = latest_report()
    if cached and not args.swiezy and cached[1]["hours"] == args.godziny:
        age_min = (time.time() - parse_ts(cached[1]["created"])) / 60
        if age_min < REPORT_CACHE_MIN:
            print(f"Raport z pamięci sprzed {age_min:.0f} min (0 kredytów Helius). Świeży: raport --swiezy",
                  file=sys.stderr)
            if args.json:
                print(json.dumps(cached[1], indent=1, ensure_ascii=False))
            else:
                print_report(cached[1], cached[0])
            return
    wallets = list(book)
    require_budget(len(wallets) * 3 * CREDITS_PER_CALL, "Raport")
    sol_usd = sol_price()
    since = time.time() - args.godziny * 3600
    print(f"Sprawdzam {len(wallets)} portfeli z ostatnich {args.godziny:g} h…", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=3) as pool:
        def fetch(w):
            try:
                return w, helius_txs(w, pages=3, gte=since)
            except ApiError as e:
                print(f"  ! {short(w)}: pomijam ({e})", file=sys.stderr)
                return w, []

        results = list(pool.map(fetch, wallets))

    per_token, bots = {}, []
    for w, txs in results:
        trades = wallet_trades(txs, w, sol_usd)
        if trades_per_day(trades, args.godziny / 24) > MAX_TRADES_PER_DAY:
            bots.append(w)
            continue
        for tr in trades:
            if tr["mint"] in SKIP_MINTS:
                continue
            p = per_token.setdefault(tr["mint"], {}).setdefault(
                w, {"buy_usd": 0, "buy_amt": 0, "sell_usd": 0, "first": tr["ts"], "last": tr["ts"]})
            p[f"{tr['side']}_usd"] += tr["usd"]
            if tr["side"] == "buy":
                p["buy_amt"] += tr["amount"]
            p["first"], p["last"] = min(p["first"], tr["ts"]), max(p["last"], tr["ts"])

    info = token_info(per_token) if per_token else {}
    signals, exits = [], []
    for mint, by_wallet in per_token.items():
        t = info.get(mint, {})
        if SKIP_TAGS & set(t.get("tags") or []):
            continue
        buyers = {w: p for w, p in by_wallet.items() if p["buy_usd"] - p["sell_usd"] >= MIN_SIGNAL_USD}
        sellers = {w: p for w, p in by_wallet.items() if p["sell_usd"] - p["buy_usd"] >= MIN_SIGNAL_USD}
        if len(sellers) >= 2:
            exits.append({"mint": mint, "symbol": t.get("symbol", short(mint)), "wallets": len(sellers),
                          "usd": round(sum(p["sell_usd"] - p["buy_usd"] for p in sellers.values()))})
        if not buyers:
            continue
        net = sum(p["buy_usd"] - p["sell_usd"] for p in buyers.values())
        bought_amt = sum(p["buy_amt"] for p in buyers.values())
        avg_entry = sum(p["buy_usd"] for p in buyers.values()) / bought_amt if bought_amt else None
        flags, penalty = risk_flags(t, avg_entry)
        weight = sum(book[w]["score"] for w in buyers)
        score = weight * (1 + math.log10(1 + net / 100)) * (1.5 if len(buyers) >= 2 else 1) * penalty
        signals.append({
            "mint": mint, "symbol": t.get("symbol", short(mint)), "name": t.get("name", ""),
            "score": round(score, 1), "wallets": len(buyers), "net_usd": round(net),
            "avg_entry": avg_entry, "price": t.get("usdPrice"), "mcap": t.get("mcap"),
            "liquidity": t.get("liquidity"), "change_24h": (t.get("stats24h") or {}).get("priceChange"),
            "verified": t.get("isVerified", False), "organic": t.get("organicScoreLabel"),
            "last_buy": datetime.fromtimestamp(max(p["last"] for p in buyers.values()), timezone.utc).isoformat(),
            "buyers": [{"wallet": w, "net_usd": round(p["buy_usd"] - p["sell_usd"]),
                        "wallet_score": book[w]["score"]} for w, p in buyers.items()],
            "flags": flags, "jup": jup_link(mint),
        })
    signals.sort(key=lambda s: s["score"], reverse=True)
    exits.sort(key=lambda e: e["usd"], reverse=True)

    report = {"created": datetime.now(timezone.utc).isoformat(), "hours": args.godziny,
              "sol_usd": sol_usd, "wallets_checked": len(wallets), "bots_skipped": bots,
              "signals": signals[:args.ile], "exits": exits[:10],
              "helius_credits": helius_calls * CREDITS_PER_CALL}
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"raport-{datetime.now():%Y%m%d-%H%M}.json"
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    journal_update(journal_add(load_journal(), report))

    if args.json:
        print(json.dumps(report, indent=1, ensure_ascii=False))
        return
    print_report(report, path)


def latest_report():
    """(ścieżka, raport) najnowszego zapisanego raportu albo None."""
    reports = sorted(REPORTS.glob("raport-*.json")) if REPORTS.exists() else []
    return (reports[-1], json.loads(reports[-1].read_text())) if reports else None


def fmt_usd(x):
    if x is None:
        return "?"
    if x >= 1e6:
        return f"${x / 1e6:.1f}M"
    if x >= 1e3:
        return f"${x / 1e3:.0f}k"
    return f"${x:,.0f}"


def print_report(r, path):
    print(f"\nRAPORT WIELORYBÓW · ostatnie {r['hours']:g} h · SOL ${r['sol_usd']:.2f} · "
          f"{r['wallets_checked']} portfeli")
    if not r["signals"]:
        print("\nObserwowane portfele nic istotnego nie kupiły w tym oknie. Brak sygnałów to też informacja.")
    for i, s in enumerate(r["signals"], 1):
        verdict = "KONSENSUS" if s["wallets"] >= 2 else "pojedynczy"
        ch = f"{s['change_24h']:+.0f}%" if s["change_24h"] is not None else "?"
        print(f"\n{i}. {s['symbol']}  ({s['name']})  wynik {s['score']}  [{verdict}]")
        print(f"   {s['wallets']} portf. kupiło netto {fmt_usd(s['net_usd'])} · kapitalizacja {fmt_usd(s['mcap'])} · "
              f"płynność {fmt_usd(s['liquidity'])} · 24 h {ch}")
        if s["avg_entry"] and s["price"]:
            print(f"   ich średnie wejście vs cena teraz: {s['price'] / s['avg_entry']:.2f}×")
        for f in s["flags"]:
            print(f"   ⚠ {f}")
        print(f"   {s['jup']}")
    if r["exits"]:
        print("\nWYCHODZĄ (≥ 2 portfele sprzedają netto):")
        for e in r["exits"]:
            print(f"  • {e['symbol']}: {e['wallets']} portf., {fmt_usd(e['usd'])}")
    if r["bots_skipped"]:
        print(f"\nPominięte jako boty: {', '.join(short(w) for w in r['bots_skipped'])}")
    print(f"\nZapisano: {path.relative_to(ROOT)} · {usage_line()}")
    print("To nie jest porada inwestycyjna. Sygnał pokazuje, co kupili inni, a nie co zarobisz.")


# --------------------------------------------------------------------------- dziennik sygnałów

def journal_add(journal, report):
    """Dopisuje sygnały z raportu. Ten sam token najwyżej raz na 24 h, żeby częste raporty nie dublowały wpisów."""
    ts = parse_ts(report["created"])
    for s in report["signals"]:
        if not s.get("price") or any(e["mint"] == s["mint"] and abs(ts - e["ts"]) < 86400 for e in journal):
            continue
        journal.append({"ts": ts, "date": report["created"], "mint": s["mint"], "symbol": s["symbol"],
                        "price": s["price"], "score": s["score"], "wallets": s["wallets"],
                        "net_usd": s["net_usd"], "liquidity": s["liquidity"], "flags": s["flags"],
                        "p24": None, "p72": None})
    return journal


def load_journal():
    if JOURNAL.exists():
        return json.loads(JOURNAL.read_text())
    # Pierwsze uruchomienie: zasilamy dziennik sygnałami z zapisanych już raportów.
    journal = []
    for path in sorted(REPORTS.glob("raport-*.json")):
        journal_add(journal, json.loads(path.read_text()))
    return journal


def journal_update(journal):
    """Uzupełnia ceny po 24 h i 72 h. Cena jest z chwili sprawdzenia, więc po przerwie może być późniejsza."""
    now = time.time()
    todo = [e for e in journal if (e["p24"] is None and now - e["ts"] >= 86400)
            or (e["p72"] is None and now - e["ts"] >= 3 * 86400)]
    if todo:
        prices = token_prices({e["mint"] for e in todo})
        for e in todo:
            price = prices.get(e["mint"], 0.0)  # brak ceny = token martwy
            for key, age in (("p24", 86400), ("p72", 3 * 86400)):
                if e[key] is None and now - e["ts"] >= age:
                    e[key] = price
                    e[f"{key}_checked"] = round((now - e["ts"]) / 3600)
    DATA.mkdir(exist_ok=True)
    JOURNAL.write_text(json.dumps(journal, indent=1, ensure_ascii=False))
    return journal


def journal_stats(entries, key):
    changes = [e[key] / e["price"] - 1 for e in entries if e[key] is not None]
    if not changes:
        return None
    return {"n": len(changes), "median_pct": round(statistics.median(changes) * 100),
            "hit_2x_pct": round(sum(c >= 1 for c in changes) / len(changes) * 100),
            "stop_pct": round(sum(c <= -0.4 for c in changes) / len(changes) * 100)}


def cmd_results(args):
    journal = journal_update(load_journal())
    groups = {"wszystkie": journal,
              "bez ostrzeżeń": [e for e in journal if not e["flags"]],
              "z ostrzeżeniami": [e for e in journal if e["flags"]],
              "KONSENSUS": [e for e in journal if e["wallets"] >= 2]}
    stats = {g: {k: journal_stats(es, k) for k in ("p24", "p72")} for g, es in groups.items()}
    if args.json:
        print(json.dumps({"entries": len(journal), "stats": stats}, indent=1, ensure_ascii=False))
        return
    waiting = sum(e["p24"] is None for e in journal)
    print(f"DZIENNIK SYGNAŁÓW · {len(journal)} wpisów · {waiting} czeka na pierwsze 24 h")
    print("Zmiana ceny od chwili sygnału (mediana) · ile doszło do 2× · ile spadło o 40% lub więcej\n")
    for g, by_key in stats.items():
        for key, label in (("p24", "24 h"), ("p72", "72 h")):
            st = by_key[key]
            if st:
                print(f"  {g:<16} po {label}: {st['n']:>3} sygn. · mediana {st['median_pct']:+}% · "
                      f"2×: {st['hit_2x_pct']}% · −40%: {st['stop_pct']}%")
    if not any(st for by_key in stats.values() for st in by_key.values()):
        print("  Jeszcze za wcześnie: pierwsze wyniki pojawią się 24 h po pierwszym sygnale.")
    print("\nCena jest z chwili sprawdzenia: uruchamiaj raport albo wyniki codziennie, żeby była bliska 24 h i 72 h.")


# --------------------------------------------------------------------------- portfel

def cmd_wallet(args):
    sol_usd = sol_price()
    txs = helius_txs(args.adres, pages=2, gte=time.time() - args.godziny * 3600)
    trades = wallet_trades(txs, args.adres, sol_usd)
    info = token_info({t["mint"] for t in trades}) if trades else {}
    print(f"{args.adres}: {len(txs)} transakcji, {len(trades)} zamian w {args.godziny:g} h "
          f"(~{trades_per_day(trades, args.godziny / 24):.0f} zamian/dzień)")
    for t in trades:
        when = datetime.fromtimestamp(t["ts"]).strftime("%d.%m %H:%M")
        sym = info.get(t["mint"], {}).get("symbol", short(t["mint"]))
        print(f"  {when}  {'KUPNO ' if t['side'] == 'buy' else 'SPRZED'}  {sym:<12} {fmt_usd(t['usd']):>8}")


def main():
    ap = argparse.ArgumentParser(description="Agent wielorybów dla jup.ag (Solana)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    op = sub.add_parser("odkryj", help="znajdź skuteczne portfele")
    op.add_argument("--wymus", action="store_true", help="pomiń limit raz na DISCOVERY_COOLDOWN_H godzin")
    rp = sub.add_parser("raport", help="co kupują obserwowane portfele")
    rp.add_argument("--godziny", type=float, default=LOOKBACK_HOURS)
    rp.add_argument("--ile", type=int, default=10, help="ile sygnałów pokazać")
    rp.add_argument("--json", action="store_true", help="wypisz raport jako JSON")
    rp.add_argument("--swiezy", action="store_true", help="nie używaj raportu z pamięci")
    wp = sub.add_parser("portfel", help="ostatnie transakcje jednego portfela")
    wp.add_argument("adres")
    wp.add_argument("--godziny", type=float, default=72)
    wy = sub.add_parser("wyniki", help="dziennik sygnałów: jak zachowały się po 24 h i 72 h")
    wy.add_argument("--json", action="store_true")

    sub.add_parser("nowy-portfel", help="utwórz osobny portfel do handlu")
    sub.add_parser("eksport", help="pokaż klucz prywatny (tylko we własnym Terminalu)")
    dp = sub.add_parser("diagnoza", help="szybki stan portfeli bez kredytów Helius")
    dp.add_argument("--json", action="store_true")
    pp = sub.add_parser("pozycje", help="saldo portfela agenta i podpowiedzi wyjścia")
    pp.add_argument("--json", action="store_true")
    bp = sub.add_parser("kup", help="kup token za SOL (podgląd, potem akceptacja)")
    bp.add_argument("mint")
    bp.add_argument("--usd", type=float, required=True, help="kwota w USD")
    bp.add_argument("--potwierdzam", action="store_true", help="wykonaj bez pytania (po Twojej zgodzie)")
    sp = sub.add_parser("sprzedaj", help="sprzedaj token za SOL (podgląd, potem akceptacja)")
    sp.add_argument("mint")
    sp.add_argument("--procent", type=float, default=100)
    sp.add_argument("--potwierdzam", action="store_true", help="wykonaj bez pytania (po Twojej zgodzie)")
    args = ap.parse_args()

    trading = {"nowy-portfel": "cmd_new_wallet", "eksport": "cmd_export", "pozycje": "cmd_positions",
               "kup": "cmd_buy", "sprzedaj": "cmd_sell", "diagnoza": "cmd_diagnosis"}
    if args.cmd in trading:
        try:
            import handel
        except ImportError:
            venv = ROOT / ".venv"
            venv_py = venv / "bin" / "python"
            if venv_py.exists() and Path(sys.prefix).resolve() != venv.resolve():
                os.execv(venv_py, [str(venv_py), *sys.argv])  # biblioteka do podpisu jest w .venv
            sys.exit("Brakuje biblioteki do podpisu. Zainstaluj: python3 -m venv .venv && .venv/bin/pip install cryptography")
        getattr(handel, trading[args.cmd])(args)
        return
    {"odkryj": cmd_discover, "raport": cmd_report, "portfel": cmd_wallet, "wyniki": cmd_results}[args.cmd](args)


if __name__ == "__main__":
    main()
