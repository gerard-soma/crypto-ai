#!/usr/bin/env python3
"""Agent wielorybów: śledzi skuteczne portfele na Solanie i podpowiada tokeny do kupna w jup.ag.

Polecenia:
  python3 agent.py odkryj          # znajduje skuteczne portfele i zapisuje listę obserwowanych
  python3 agent.py raport          # sprawdza, co te portfele kupują, i daje ranking sygnałów
  python3 agent.py portfel ADRES   # pokazuje ostatnie transakcje jednego portfela

Tylko odczyt danych: agent nie zna Twojego klucza prywatnego i niczego nie kupuje.
"""
import argparse
import json
import math
import os
import re
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
DISCOVERY_TOKENS = cfg("DISCOVERY_TOKENS", 8)    # na ilu zwycięskich tokenach szukamy portfeli
DISCOVERY_DAYS = cfg("DISCOVERY_DAYS", 3.0)      # jak daleko wstecz patrzymy przy odkrywaniu
LOOKBACK_HOURS = cfg("LOOKBACK_HOURS", 24.0)     # okno raportu
MIN_SIGNAL_USD = cfg("MIN_SIGNAL_USD", 50.0)     # minimalny zakup netto, który liczymy jako sygnał
MAX_TRADES_PER_DAY = cfg("MAX_TRADES_PER_DAY", 60)  # częściej handlują boty i skalperzy, których nie da się skopiować

CREDITS_PER_CALL = 100  # Enhanced Transactions API Helius
helius_calls = 0
_lock = threading.Lock()
_last_call = [0.0]


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


def trades_per_day(txs):
    if len(txs) < 20:
        return 0.0
    stamps = [t.get("timestamp", 0) for t in txs]
    span_h = max((max(stamps) - min(stamps)) / 3600, 0.25)
    return len(txs) / span_h * 24


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

    ranked = sorted(found.items(), key=lambda kv: score(kv[1]), reverse=True)[: MAX_WALLETS * 2]

    # Odsiewamy boty i skalperów: portfele, które handlują częściej niż MAX_TRADES_PER_DAY.
    def bot_check(w):
        try:
            return w, trades_per_day(helius_txs(w, pages=1))
        except ApiError:
            return w, 0.0

    with ThreadPoolExecutor(max_workers=3) as pool:
        rates = dict(pool.map(bot_check, [w for w, _ in ranked]))

    old = load_watchlist()
    today = datetime.now(timezone.utc).date().isoformat()
    book = {w: {**e, "score": round(e["score"] * 0.8, 2)} for w, e in old.items()  # stare wyniki z czasem słabną
            if e.get("trades_per_day", 0) <= MAX_TRADES_PER_DAY}
    for w, wins in ranked:
        if rates.get(w, 0) > MAX_TRADES_PER_DAY:
            continue
        prev = book.get(w, {})
        book[w] = {"score": round(prev.get("score", 0) + score(wins), 2),
                   "wins": (wins + prev.get("wins", []))[:10],
                   "trades_per_day": round(rates.get(w, 0)),
                   "found": prev.get("found", today), "updated": today}
    book = dict(sorted(book.items(), key=lambda kv: kv[1]["score"], reverse=True)[:MAX_WALLETS])
    DATA.mkdir(exist_ok=True)
    WATCHLIST.write_text(json.dumps(book, indent=1, ensure_ascii=False))

    print(f"\nObserwuję {len(book)} portfeli (zapisane w {WATCHLIST.relative_to(ROOT)}). Najlepsze:")
    for w, e in list(book.items())[:10]:
        wins = ", ".join(f"{x['symbol']} +{x['roi']:.0%}" for x in e["wins"][:3])
        print(f"  {short(w)}  wynik {e['score']:>5}  {wins}")
    print(f"\nZużyte kredyty Helius: ~{helius_calls * CREDITS_PER_CALL:,} (darmowy plan: 1 000 000/mies.)")


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
    sol_usd = sol_price()
    since = time.time() - args.godziny * 3600
    wallets = list(book)
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
        if trades_per_day(txs) > MAX_TRADES_PER_DAY:
            bots.append(w)
            continue
        for tr in wallet_trades(txs, w, sol_usd):
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

    if args.json:
        print(json.dumps(report, indent=1, ensure_ascii=False))
        return
    print_report(report, path)


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
    print(f"\nZapisano: {path.relative_to(ROOT)} · kredyty Helius: ~{r['helius_credits']:,}")
    print("To nie jest porada inwestycyjna. Sygnał pokazuje, co kupili inni, a nie co zarobisz.")


# --------------------------------------------------------------------------- portfel

def cmd_wallet(args):
    sol_usd = sol_price()
    txs = helius_txs(args.adres, pages=2, gte=time.time() - args.godziny * 3600)
    trades = wallet_trades(txs, args.adres, sol_usd)
    info = token_info({t["mint"] for t in trades}) if trades else {}
    print(f"{args.adres}: {len(txs)} transakcji, {len(trades)} zamian w {args.godziny:g} h "
          f"(~{trades_per_day(txs):.0f}/dzień)")
    for t in trades:
        when = datetime.fromtimestamp(t["ts"]).strftime("%d.%m %H:%M")
        sym = info.get(t["mint"], {}).get("symbol", short(t["mint"]))
        print(f"  {when}  {'KUPNO ' if t['side'] == 'buy' else 'SPRZED'}  {sym:<12} {fmt_usd(t['usd']):>8}")


def main():
    ap = argparse.ArgumentParser(description="Agent wielorybów dla jup.ag (Solana)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("odkryj", help="znajdź skuteczne portfele")
    rp = sub.add_parser("raport", help="co kupują obserwowane portfele")
    rp.add_argument("--godziny", type=float, default=LOOKBACK_HOURS)
    rp.add_argument("--ile", type=int, default=10, help="ile sygnałów pokazać")
    rp.add_argument("--json", action="store_true", help="wypisz raport jako JSON")
    wp = sub.add_parser("portfel", help="ostatnie transakcje jednego portfela")
    wp.add_argument("adres")
    wp.add_argument("--godziny", type=float, default=72)

    sub.add_parser("nowy-portfel", help="utwórz osobny portfel do handlu")
    sub.add_parser("eksport", help="pokaż klucz prywatny (tylko we własnym Terminalu)")
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
               "kup": "cmd_buy", "sprzedaj": "cmd_sell"}
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
    {"odkryj": cmd_discover, "raport": cmd_report, "portfel": cmd_wallet}[args.cmd](args)


if __name__ == "__main__":
    main()
