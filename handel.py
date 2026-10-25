"""Handel z osobnego portfela agenta przez Jupiter: podgląd, limity, wykonanie dopiero po akceptacji.

Klucz prywatny leży w ~/.config/wieloryby/portfel.json (poza folderem projektu, uprawnienia 600).
Ten portfel służy tylko do handlu: trzymaj na nim tyle, ile możesz stracić.
"""
import base64
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import agent
from agent import SOL, cfg, fmt_usd, jup_get, http_json, risk_flags, short, sol_price, token_info

KEY_DIR = Path.home() / ".config" / "wieloryby"
KEY_FILE = KEY_DIR / "portfel.json"
TRADE_LOG = agent.DATA / "transakcje.json"

MAX_TRADE_USD = cfg("MAX_TRADE_USD", 10.0)        # największy pojedynczy zakup
MAX_DAILY_USD = cfg("MAX_DAILY_USD", 25.0)        # suma zakupów w ciągu 24 h
MAX_SLIPPAGE_BPS = cfg("MAX_SLIPPAGE_BPS", 300)   # 300 = 3%
MAX_BUY_IMPACT = cfg("MAX_BUY_IMPACT_PCT", 3.0)   # wpływ na cenę przy zakupie, w %
MAX_SELL_IMPACT = cfg("MAX_SELL_IMPACT_PCT", 15.0)  # przy wyjściu akceptujemy więcej
SOL_RESERVE = cfg("SOL_RESERVE", 0.02)            # zawsze zostaje na opłaty
TAKE_PROFIT = cfg("TAKE_PROFIT_PCT", 100.0)       # podpowiedź: sprzedaj połowę przy +100%
STOP_LOSS = cfg("STOP_LOSS_PCT", -40.0)           # podpowiedź: wyjdź przy −40%

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b):
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


# --------------------------------------------------------------------------- portfel

def load_key():
    if not KEY_FILE.exists():
        sys.exit("Agent nie ma jeszcze portfela. Utwórz go: python3 agent.py nowy-portfel")
    raw = bytes(json.loads(KEY_FILE.read_text()))
    priv = Ed25519PrivateKey.from_private_bytes(raw[:32])
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    if pub != raw[32:64]:
        sys.exit(f"Plik {KEY_FILE} jest uszkodzony (klucz publiczny się nie zgadza).")
    return priv, pub


def cmd_new_wallet(args):
    if KEY_FILE.exists():
        _, pub = load_key()
        sys.exit(f"Portfel agenta już istnieje: {b58encode(pub)}\nNie nadpisuję go, żeby nie stracić środków.")
    priv = Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives.serialization import NoEncryption, PrivateFormat
    seed = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(KEY_DIR, stat.S_IRWXU)
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(list(seed + pub), f)  # format Solana CLI: 64 bajty
    print(f"Utworzyłem portfel agenta:\n\n  {b58encode(pub)}\n")
    print(f"Klucz prywatny: {KEY_FILE} (tylko Ty masz do niego dostęp).")
    print("Przelej na ten adres SOL ze swojego portfela (np. 25 USD). Zrób kopię klucza:")
    print("uruchom `python3 agent.py eksport` w osobnym oknie Terminala i zaimportuj klucz do Phantoma.")


def cmd_export(args):
    if not sys.stdin.isatty():
        sys.exit("Eksport działa tylko w Twoim własnym oknie Terminala, żeby klucz nie trafił do żadnej rozmowy.")
    priv, pub = load_key()
    if input("Klucz prywatny pozwala zabrać wszystkie środki. Pokazać? Wpisz TAK: ").strip().upper() != "TAK":
        return
    print(f"\nAdres: {b58encode(pub)}\nKlucz prywatny (import do Phantoma / Solflare):\n{b58encode(bytes(json.loads(KEY_FILE.read_text())))}\n")


def holdings(address):
    h = jup_get(f"/ultra/v1/holdings/{address}")
    sol = int(h.get("amount") or 0) / 1e9
    tokens = {}
    for mint, accts in (h.get("tokens") or {}).items():
        amt = sum(int(a["amount"]) for a in accts)
        if amt and mint != SOL:
            tokens[mint] = {"raw": amt, "decimals": accts[0]["decimals"], "ui": amt / 10 ** accts[0]["decimals"]}
    return sol, tokens


# --------------------------------------------------------------------------- dziennik

def load_log():
    return json.loads(TRADE_LOG.read_text()) if TRADE_LOG.exists() else []


def append_log(entry):
    log = load_log() + [entry]
    agent.DATA.mkdir(exist_ok=True)
    TRADE_LOG.write_text(json.dumps(log, indent=1, ensure_ascii=False))


def bought_last_24h():
    since = time.time() - 86400
    return sum(e["usd"] for e in load_log() if e["side"] == "buy" and e["ts"] >= since)


# --------------------------------------------------------------------------- Jupiter swap

def swap_base():
    return "https://api.jup.ag/swap/v2" if agent.JUP_KEY else "https://lite-api.jup.ag/ultra/v1"


def jup_headers():
    return {"x-api-key": agent.JUP_KEY} if agent.JUP_KEY else {}


def get_order(input_mint, output_mint, amount_raw, taker):
    q = urllib.parse.urlencode({"inputMint": input_mint, "outputMint": output_mint,
                                "amount": int(amount_raw), "taker": taker})
    o = http_json(f"{swap_base()}/order?{q}", jup_headers())
    if not o.get("transaction"):
        sys.exit(f"Jupiter nie zbudował transakcji: {o.get('errorMessage') or o.get('error') or 'brak trasy'} "
                 f"(kod {o.get('errorCode')}).")
    return o


def shortvec(buf, off):
    val = shift = 0
    while True:
        b = buf[off]
        off += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, off
        shift += 7


def sign_tx(tx_b64, priv, pub):
    """Podpisuje transakcję z Jupitera w miejscu, które należy do naszego klucza."""
    raw = base64.b64decode(tx_b64)
    nsig, off = shortvec(raw, 0)
    msg = raw[off + 64 * nsig:]
    p = 1 if msg[0] & 0x80 else 0  # transakcja wersjonowana ma bajt prefiksu
    required = msg[p]
    nkeys, p = shortvec(msg, p + 3)
    signers = [msg[p + 32 * i: p + 32 * (i + 1)] for i in range(min(required, nkeys))]
    if pub not in signers:
        sys.exit("Transakcja z Jupitera nie jest dla portfela agenta. Przerywam bez podpisu.")
    i = signers.index(pub)
    start = off + 64 * i
    return base64.b64encode(raw[:start] + priv.sign(msg) + raw[start + 64:]).decode()


def execute(order, priv, pub):
    body = json.dumps({"signedTransaction": sign_tx(order["transaction"], priv, pub),
                       "requestId": order["requestId"]}).encode()
    req = urllib.request.Request(f"{swap_base()}/execute", data=body, method="POST",
                                 headers={"Content-Type": "application/json", **jup_headers()})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"status": "Failed", "error": e.read().decode(errors="replace")[:300]}


def impact_pct(order):
    if order.get("priceImpact") is not None:
        return abs(float(order["priceImpact"]))
    return abs(float(order.get("priceImpactPct") or 0)) * 100


# --------------------------------------------------------------------------- akceptacja

def confirmed(args, what):
    if args.potwierdzam:
        return True
    if sys.stdin.isatty():
        return input(f"\n{what} Wpisz TAK, aby wykonać: ").strip().upper() == "TAK"
    print("\nTo tylko podgląd. Żeby wykonać, uruchom to samo polecenie z --potwierdzam "
          "(Claude Code zapyta Cię wtedy o zgodę).")
    return False


def print_preview(title, rows):
    print(f"\n{title}")
    for k, v in rows:
        print(f"  {k:<22} {v}")


# --------------------------------------------------------------------------- kup / sprzedaj

def cmd_buy(args):
    priv, pub = load_key()
    wallet = b58encode(pub)
    if args.usd <= 0 or args.usd > MAX_TRADE_USD:
        sys.exit(f"Kwota musi być między 0 a {MAX_TRADE_USD:g} USD (limit MAX_TRADE_USD w .env).")
    spent = bought_last_24h()
    if spent + args.usd > MAX_DAILY_USD:
        sys.exit(f"Limit dzienny: kupiono już za {spent:.2f} USD z {MAX_DAILY_USD:g} USD w ostatnich 24 h.")

    t = token_info([args.mint]).get(args.mint)
    if not t:
        sys.exit("Jupiter nie zna tego tokenu. Sprawdź adres.")
    flags, _ = risk_flags(t, None)
    hard = [f for f in flags if "dodrukować" in f or "zamrozić" in f]
    if hard:
        sys.exit(f"Odmawiam zakupu {t['symbol']}: {', '.join(hard)}. To typowy znak oszustwa (honeypot).")

    sol_usd = sol_price()
    sol, _ = holdings(wallet)
    lamports = int(args.usd / sol_usd * 1e9)
    if sol - lamports / 1e9 < SOL_RESERVE:
        sys.exit(f"Za mało SOL: masz {sol:.4f}, zakup {lamports / 1e9:.4f}, rezerwa na opłaty {SOL_RESERVE}.")

    order = get_order(SOL, args.mint, lamports, wallet)
    check_order(order, MAX_BUY_IMPACT)
    out_ui = int(order["outAmount"]) / 10 ** t["decimals"]
    rent = int(order.get("rentFeeLamports") or 0) / 1e9
    print_preview(f"ZAKUP {t['symbol']} ({t.get('name', '')}) z portfela {short(wallet)}", [
        ("Płacisz", f"{lamports / 1e9:.4f} SOL (~{fmt_usd(args.usd)})"),
        ("Dostaniesz ok.", f"{out_ui:,.2f} {t['symbol']}"),
        ("Wpływ na cenę", f"{impact_pct(order):.2f}%"),
        ("Poślizg max", f"{order.get('slippageBps', '?')} bps"),
        ("Opłata Jupitera", f"{order.get('feeBps', '?')} bps"),
        ("Konto tokenu (rent)", f"{rent:.4f} SOL" if rent else "już istnieje"),
        ("Kapitalizacja / płynn.", f"{fmt_usd(t.get('mcap'))} / {fmt_usd(t.get('liquidity'))}"),
        ("Ostrzeżenia", "; ".join(flags) or "brak"),
        ("Limit dzienny", f"{spent + args.usd:.2f} / {MAX_DAILY_USD:g} USD po tej transakcji"),
    ])
    if not confirmed(args, f"Kupić {t['symbol']} za ~{fmt_usd(args.usd)}?"):
        return
    run_swap(SOL, args.mint, lamports, order, priv, pub, "buy", t, sol_usd)


def cmd_sell(args):
    priv, pub = load_key()
    wallet = b58encode(pub)
    if not 0 < args.procent <= 100:
        sys.exit("--procent musi być od 1 do 100.")
    _, tokens = holdings(wallet)
    pos = tokens.get(args.mint)
    if not pos:
        sys.exit("Portfel agenta nie ma tego tokenu.")
    t = token_info([args.mint]).get(args.mint, {"symbol": short(args.mint), "decimals": pos["decimals"]})
    raw = pos["raw"] if args.procent == 100 else int(pos["raw"] * args.procent / 100)
    sol_usd = sol_price()
    order = get_order(args.mint, SOL, raw, wallet)
    check_order(order, MAX_SELL_IMPACT)
    out_sol = int(order["outAmount"]) / 1e9
    print_preview(f"SPRZEDAŻ {args.procent:g}% {t['symbol']} z portfela {short(wallet)}", [
        ("Sprzedajesz", f"{raw / 10 ** pos['decimals']:,.2f} {t['symbol']}"),
        ("Dostaniesz ok.", f"{out_sol:.4f} SOL (~{fmt_usd(out_sol * sol_usd)})"),
        ("Wpływ na cenę", f"{impact_pct(order):.2f}%"),
        ("Poślizg max", f"{order.get('slippageBps', '?')} bps"),
    ])
    if not confirmed(args, f"Sprzedać {args.procent:g}% {t['symbol']}?"):
        return
    run_swap(args.mint, SOL, raw, order, priv, pub, "sell", t, sol_usd)


def check_order(order, max_impact):
    if int(order.get("slippageBps") or 0) > MAX_SLIPPAGE_BPS:
        sys.exit(f"Poślizg {order['slippageBps']} bps przekracza limit {MAX_SLIPPAGE_BPS}. Płynność jest za mała.")
    if impact_pct(order) > max_impact:
        sys.exit(f"Wpływ na cenę {impact_pct(order):.2f}% przekracza limit {max_impact:g}%. Odpuszczam.")


def run_swap(in_mint, out_mint, amount, preview, priv, pub, side, t, sol_usd):
    wallet = b58encode(pub)
    # Po akceptacji bierzemy świeże zlecenie: stare mogło wygasnąć, a cena się zmienić.
    order = get_order(in_mint, out_mint, amount, wallet)
    check_order(order, MAX_BUY_IMPACT if side == "buy" else MAX_SELL_IMPACT)
    if int(order["outAmount"]) < int(preview["outAmount"]) * 0.97:
        sys.exit("Cena zmieniła się o ponad 3% na Twoją niekorzyść od podglądu. Przerywam, sprawdź ponownie.")
    res = execute(order, priv, pub)
    if res.get("status") != "Success":
        sys.exit(f"Transakcja nie przeszła: {res.get('error') or res} (kod {res.get('code')}). Środki zostały w portfelu.")
    sig = res.get("signature")
    dec = t.get("decimals", 0)
    if side == "buy":
        sol_amt = int(res.get("totalInputAmount") or amount) / 1e9
        tok_amt = int(res.get("totalOutputAmount") or order["outAmount"]) / 10 ** dec
    else:
        tok_amt = int(res.get("totalInputAmount") or amount) / 10 ** dec
        sol_amt = int(res.get("totalOutputAmount") or order["outAmount"]) / 1e9
    usd = sol_amt * sol_usd
    append_log({"ts": int(time.time()), "date": datetime.now(timezone.utc).isoformat(), "side": side,
                "mint": out_mint if side == "buy" else in_mint, "symbol": t.get("symbol"),
                "tokens": tok_amt, "sol": sol_amt, "usd": round(usd, 2),
                "price": usd / tok_amt if tok_amt else None, "signature": sig})
    verb = "Kupiono" if side == "buy" else "Sprzedano"
    print(f"\n✔ {verb} {tok_amt:,.2f} {t.get('symbol')} za {sol_amt:.4f} SOL (~{fmt_usd(usd)})")
    print(f"  https://solscan.io/tx/{sig}")


# --------------------------------------------------------------------------- pozycje

def agent_positions():
    _, pub = load_key()
    wallet = b58encode(pub)
    sol, tokens = holdings(wallet)
    sol_usd = sol_price()
    info = token_info(tokens) if tokens else {}
    log = load_log()
    latest = agent.latest_report()
    exits = {e["mint"] for e in latest[1].get("exits", [])} if latest else set()

    total = sol * sol_usd
    rows = []
    for mint, pos in tokens.items():
        t = info.get(mint, {})
        price = t.get("usdPrice") or 0
        value = pos["ui"] * price
        total += value
        buys = [e for e in log if e["mint"] == mint and e["side"] == "buy"]
        bought = sum(e["tokens"] for e in buys)
        entry = sum(e["usd"] for e in buys) / bought if bought else None
        pnl = (price / entry - 1) * 100 if entry and price else None
        hint = ""
        if mint in exits:
            hint = "wieloryby wychodzą → rozważ sprzedaż"
        elif pnl is not None and pnl >= TAKE_PROFIT:
            hint = "cel osiągnięty → rozważ sprzedaż połowy (--procent 50)"
        elif pnl is not None and pnl <= STOP_LOSS:
            hint = "stop loss → rozważ sprzedaż całości"
        rows.append({"mint": mint, "symbol": t.get("symbol", short(mint)), "amount": pos["ui"], "value_usd": value,
                     "entry": entry, "price": price, "pnl_pct": pnl, "hint": hint})
    return {"wallet": wallet, "sol": sol, "sol_usd": sol_usd, "total_usd": total, "positions": rows}


def print_positions(d):
    print(f"Portfel agenta {d['wallet']}\n  SOL {d['sol']:.4f} (~{fmt_usd(d['sol'] * d['sol_usd'])})")
    for r in d["positions"]:
        pnl_s = f"{r['pnl_pct']:+.0f}%" if r["pnl_pct"] is not None else "wejście nieznane"
        print(f"  {r['symbol']:<10} {fmt_usd(r['value_usd']):>7}  {pnl_s:<16} {r['hint']}")
    print(f"Razem ~{fmt_usd(d['total_usd'])} · zakupy w 24 h: {bought_last_24h():.2f} / {MAX_DAILY_USD:g} USD")


def cmd_positions(args):
    d = agent_positions()
    print_positions(d)
    if args.json:
        print(json.dumps(d, indent=1, ensure_ascii=False))


# --------------------------------------------------------------------------- diagnoza

DUST_USD = 0.5  # poniżej tej wartości token to pył: zostaje tylko kaucja ~0,002 SOL za konto


def private_wallet(sol_usd):
    """Prywatny portfel Gerarda: tylko odczyt po adresie publicznym z PRYWATNY_PORTFEL w .env."""
    address = os.environ.get("PRYWATNY_PORTFEL", "").strip()
    if not address:
        return None
    sol, tokens = holdings(address)
    info = token_info(tokens) if tokens else {}
    latest = agent.latest_report()
    exits = {e["mint"] for e in latest[1].get("exits", [])} if latest else set()
    rows, total = [], sol * sol_usd
    for mint, pos in tokens.items():
        t = info.get(mint, {})
        value = pos["ui"] * (t.get("usdPrice") or 0)
        total += value
        # Zweryfikowane tokeny opakowane (WBTC) i akcje (SPCXx) zawsze mają emitenta z prawem dodruku i zamrożenia.
        keep = ("płynność",) if t.get("isVerified") else ("dodrukować", "zamrozić", "płynność")
        flags = [f for f in risk_flags(t, None)[0] if any(k in f for k in keep)]
        if mint in exits:
            flags.append("obserwowane portfele wychodzą")
        rows.append({"mint": mint, "symbol": t.get("symbol", short(mint)), "value_usd": value,
                     "change_24h": (t.get("stats24h") or {}).get("priceChange"),
                     "dust": value < DUST_USD, "flags": flags})
    rows.sort(key=lambda r: r["value_usd"], reverse=True)
    return {"wallet": address, "sol": sol, "total_usd": total, "positions": rows}


def cmd_diagnosis(args):
    """Szybki stan bez kredytów Helius: oba portfele, wiek raportu, dziennik i budżet."""
    agent_d = agent_positions()
    priv = private_wallet(agent_d["sol_usd"])
    latest = agent.latest_report()
    report_age_h = (time.time() - agent.parse_ts(latest[1]["created"])) / 3600 if latest else None
    journal = agent.journal_update(agent.load_journal())
    stats = {k: agent.journal_stats(journal, k) for k in ("p24", "p72")}
    usage = agent.load_usage().get(agent.month_key(), 0)
    if args.json:
        print(json.dumps({"agent": agent_d, "private": priv, "report_age_h": report_age_h,
                          "journal_entries": len(journal), "journal_stats": stats,
                          "helius_month": usage, "helius_budget": agent.HELIUS_BUDGET}, indent=1, ensure_ascii=False))
        return
    print_positions(agent_d)
    print()
    if priv:
        print(f"Portfel prywatny {short(priv['wallet'])}  ~{fmt_usd(priv['total_usd'])} · SOL {priv['sol']:.4f}")
        dust = [r for r in priv["positions"] if r["dust"]]
        for r in priv["positions"]:
            if r["dust"]:
                continue
            ch = f"{r['change_24h']:+.1f}%" if r["change_24h"] is not None else "?"
            warn = f"  ⚠ {', '.join(r['flags'])}" if r["flags"] else ""
            print(f"  {r['symbol']:<10} {fmt_usd(r['value_usd']):>7}  24 h {ch:>7}{warn}")
        if dust:
            print(f"  pył (< ${DUST_USD:g}): {', '.join(r['symbol'] for r in dust)} · zamknięcie kont zwróci "
                  f"ok. {0.002 * len(dust):.3f} SOL")
        if priv["sol"] < 0.005:
            print("  ⚠ mało SOL na opłaty")
    else:
        print("Portfel prywatny: dodaj PRYWATNY_PORTFEL=<adres publiczny> do .env, żeby diagnoza go sprawdzała.")
    print()
    if report_age_h is not None:
        print(f"Ostatni raport wielorybów: {report_age_h:.0f} h temu ({len(latest[1]['signals'])} sygnałów)")
    st = stats["p24"]
    print(f"Dziennik sygnałów: {len(journal)} wpisów" + (f" · po 24 h mediana {st['median_pct']:+}%, "
          f"2×: {st['hit_2x_pct']}%, −40%: {st['stop_pct']}% ({st['n']} wyn.)" if st else " · wyniki jeszcze się zbierają"))
    print(f"Helius w tym miesiącu: ~{usage:,} z budżetu {agent.HELIUS_BUDGET:,} kredytów (diagnoza: 0)")
