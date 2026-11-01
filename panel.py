"""Panel: lokalny interfejs w przeglądarce do agenta wielorybów, czuwania, flipów i snajpera.

Uruchom: python3 panel.py   (albo python3 agent.py panel) i otwórz adres, który się wypisze.
Serwer słucha tylko na 127.0.0.1, sprawdza nagłówek Host i wymaga klucza, który dostaje tylko jego własna
strona, więc inne strony otwarte w przeglądarce nie mogą wywołać jego poleceń. Panel nie zna klucza prywatnego: wywołuje te same polecenia co Terminal.
Prawdziwego snajpera (--naprawde) panel nie uruchamia: to zostaje w Twoim oknie Terminala.
"""
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
_venv = ROOT / ".venv"
if (_venv / "bin" / "python").exists() and Path(sys.prefix).resolve() != _venv.resolve():
    os.execv(_venv / "bin" / "python", [str(_venv / "bin" / "python"), *sys.argv])  # websockets i podpis są w .venv
sys.path.insert(0, str(ROOT))
import agent  # noqa: E402

UI = ROOT / "panel"
DATA = agent.DATA
PY = str(ROOT / ".venv" / "bin" / "python") if (ROOT / ".venv" / "bin" / "python").exists() else sys.executable
PORT = int(os.environ.get("PANEL_PORT", "8765"))
TOKEN = secrets.token_urlsafe(24)
MINT_CHARS = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")

# Procesy w tle, które panel umie włączyć i wyłączyć. Prawdziwego snajpera tu celowo nie ma.
SERVICES = {
    "czuwaj": {"args": ["czuwaj", "--co", "2"], "match": r"agent\.py czuwaj( |$)"},
    "snajper": {"args": ["snajper"], "match": r"agent\.py snajper( |$)"},
}
procs = {}
jobs = {}   # długie polecenia (raport, odkryj): {id: {cmd, started, done, out}}
_cache = {}


# --------------------------------------------------------------------------- pomocnicze

def run(args, timeout=180):
    """Polecenie agenta jak w Terminalu (bez TTY, więc kup/sprzedaj bez --potwierdzam to tylko podgląd)."""
    r = subprocess.run([PY, str(ROOT / "agent.py"), *args], cwd=ROOT, capture_output=True, text=True,
                       timeout=timeout, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    return r.returncode, (r.stdout + r.stderr).strip()


def run_json(args, timeout=180):
    code, out = run(args, timeout)
    start = out.find("\n{")
    start = 0 if out.startswith("{") else (start + 1 if start >= 0 else -1)
    if code != 0 or start < 0:
        raise RuntimeError(out[-600:] or f"kod {code}")
    return json.loads(out[start:])


def cached(key, seconds, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    value = fn()
    _cache[key] = (time.time(), value)
    return value


def valid_mint(m):
    return isinstance(m, str) and 32 <= len(m) <= 44 and set(m) <= MINT_CHARS


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def external_pids(match):
    """PID-y procesów uruchomionych poza panelem, np. w Terminalu."""
    try:
        out = subprocess.run(["pgrep", "-f", match], capture_output=True, text=True).stdout.split()
    except FileNotFoundError:
        return []
    own = {p.pid for p in procs.values() if p.poll() is None}
    return [int(p) for p in out if int(p) not in own and int(p) != os.getpid()]


def service_state(name):
    p = procs.get(name)
    mine = p is not None and p.poll() is None
    ext = external_pids(SERVICES[name]["match"])
    live = [pid for pid in ext if "--naprawde" in pid_cmd(pid)]
    ext = [pid for pid in ext if pid not in live]
    return {"running": mine or bool(ext), "panel": mine, "external": ext, "live": bool(live),
            "log": tail(DATA / f"panel-{name}.log", 40)}


def pid_cmd(pid):
    try:
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return ""


def tail(path, n):
    try:
        return path.read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []


def start_service(name):
    if service_state(name)["running"]:
        return {"ok": True, "msg": "Już działa."}
    DATA.mkdir(exist_ok=True)
    log = open(DATA / f"panel-{name}.log", "a")
    log.write(f"\n--- start z panelu {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    log.flush()
    procs[name] = subprocess.Popen([PY, str(ROOT / "agent.py"), *SERVICES[name]["args"]], cwd=ROOT, stdout=log,
                                   stderr=subprocess.STDOUT, env={**os.environ, "PYTHONUNBUFFERED": "1"},
                                   start_new_session=True)
    return {"ok": True, "msg": "Uruchomione."}


def stop_service(name):
    p = procs.get(name)
    stopped = 0
    if p and p.poll() is None:
        p.send_signal(signal.SIGINT)   # jak Ctrl+C: snajper zapisze otwarte pozycje
        stopped += 1
    for pid in external_pids(SERVICES[name]["match"]):
        if "--naprawde" in pid_cmd(pid):
            continue   # prawdziwego snajpera zatrzymujesz w jego Terminalu albo plikiem STOP
        os.kill(pid, signal.SIGINT)
        stopped += 1
    return {"ok": True, "msg": f"Zatrzymano: {stopped}." if stopped else "Nic nie działało."}


def start_job(kind, args, timeout=900):
    jid = secrets.token_hex(4)
    jobs[jid] = {"id": jid, "kind": kind, "started": time.time(), "done": False, "out": "", "code": None}

    def work():
        try:
            code, out = run(args, timeout)
        except subprocess.TimeoutExpired:
            code, out = -1, "Przekroczony czas."
        jobs[jid].update(done=True, out=out, code=code)
        _cache.clear()

    threading.Thread(target=work, daemon=True).start()
    return jobs[jid]


# --------------------------------------------------------------------------- dane dla widoków

def watchlist():
    import czuwaj
    watch = read_json(czuwaj.WATCH, {})
    quotes = czuwaj.fetch_prices(watch) if watch else {}
    history, now = read_json(czuwaj.PRICES, {}), time.time()
    rows = []
    for mint, meta in watch.items():
        price, liq = quotes.get(mint, (0.0, 0))
        hist = history.get(mint, [])
        chs = [czuwaj.change(hist, now, price, m) if price else None for m in (15, 60, 240)]
        rows.append({"mint": mint, "symbol": meta["symbol"], "price": price, "liquidity": liq,
                     "ch15": chs[0], "ch60": chs[1], "ch240": chs[2], "low_liq": liq < czuwaj.MIN_LIQ})
    return rows


def latest_report():
    files = sorted((DATA / "raporty").glob("raport-*.json"))
    return read_json(files[-1], None) if files else None


def sniper_view():
    import snajper
    return {"all": run_json(["snajper-wyniki", "--json"]), "day": run_json(["snajper-wyniki", "--godziny", "24", "--json"]),
            "open": read_json(snajper.OPEN, []), "stop": snajper.STOP_FILE.exists(),
            "recent": [json.loads(l) for l in tail(snajper.RESULTS, 30)][::-1],
            "wallet": (Path.home() / ".config" / "wieloryby" / "snajper.json").exists(),
            "limits": {"usd": snajper.USD, "max_open": snajper.MAX_OPEN, "max_buys": snajper.MAX_BUYS_DAY,
                       "max_loss": snajper.MAX_LOSS_DAY, "strategy": snajper.LIVE_STRATEGY}}


# --------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def allowed_host(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")   # chroni przed DNS rebinding

    def authorized(self):
        return secrets.compare_digest(self.headers.get("X-Panel-Token", ""), TOKEN)

    def do_GET(self):
        if not self.allowed_host():
            return self.send(403, {"error": "zły host"})
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            return self.static(url)
        if not self.authorized():
            return self.send(401, {"error": "brak klucza panelu"})
        self.api("GET", url.path[5:], parse_qs(url.query), {})

    def do_POST(self):
        if not self.allowed_host() or not self.authorized():
            return self.send(401, {"error": "brak klucza panelu"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send(415, {"error": "oczekuję JSON"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send(400, {"error": "zły JSON"})
        self.api("POST", urlparse(self.path).path[5:], {}, body)

    def static(self, url):
        name = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
        path = (UI / name).resolve()
        if UI.resolve() not in path.parents or not path.is_file():
            return self.send(404, {"error": "nie ma"})
        types = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}
        data = path.read_bytes()
        if name == "index.html":
            data = data.replace(b"__PANEL_TOKEN__", TOKEN.encode())
        self.send(200, data, types.get(path.suffix, "application/octet-stream"))

    def api(self, method, route, query, body):
        try:
            out = self.route(method, route, query, body)
        except subprocess.TimeoutExpired:
            return self.send(504, {"error": "Polecenie trwało za długo."})
        except Exception as e:  # pokazujemy błąd w panelu zamiast zrywać połączenie
            return self.send(500, {"error": str(e)[:600]})
        if out is None:
            return self.send(404, {"error": "nie ma takiej ścieżki"})
        self.send(200, out)

    def route(self, method, route, q, b):
        if method == "GET":
            if route == "stan":
                return cached("stan", 30, lambda: run_json(["diagnoza", "--json"]))
            if route == "uslugi":
                return {n: service_state(n) for n in SERVICES}
            if route == "flipy":
                return cached("flipy", 10, lambda: {**run_json(["flipy", "--json"]), "watch": watchlist()})
            if route == "wieloryby":
                return cached("wieloryby", 60, lambda: {"report": latest_report(), "journal": run_json(["wyniki", "--json"]),
                                                        "wallets": read_json(DATA / "portfele.json", None)})
            if route == "snajper":
                return cached("snajper", 5, sniper_view)
            if route == "zadania":
                return list(jobs.values())[-10:][::-1]
            return None

        _cache.clear()
        if route == "usluga":
            name, action = b.get("name"), b.get("action")
            if name not in SERVICES or action not in ("start", "stop"):
                raise ValueError("Nieznana usługa.")
            return start_service(name) if action == "start" else stop_service(name)
        if route == "obserwuj":
            if b.get("dodaj"):
                if not valid_mint(b["dodaj"]):
                    raise ValueError("To nie wygląda na adres tokenu Solany.")
                code, out = run(["obserwuj", "--dodaj", b["dodaj"]], 60)
            elif b.get("usun"):
                code, out = run(["obserwuj", "--usun", str(b["usun"])[:60]], 60)
            else:
                raise ValueError("Podaj token.")
            return {"ok": code == 0, "msg": out}
        if route == "snajper-stop":
            import snajper
            DATA.mkdir(exist_ok=True)
            if b.get("on"):
                snajper.STOP_FILE.write_text(time.strftime("%Y-%m-%d %H:%M:%S wstrzymane z panelu\n"))
            elif snajper.STOP_FILE.exists():
                snajper.STOP_FILE.unlink()
            return {"ok": True, "stop": snajper.STOP_FILE.exists()}
        if route == "zadanie":
            kind = b.get("kind")
            if kind == "raport":
                return start_job("raport", ["raport", "--json"])
            if kind == "odkryj":
                return start_job("odkryj", ["odkryj"])
            raise ValueError("Nieznane zadanie.")
        if route == "handel":
            side, mint = b.get("side"), b.get("mint")
            if side not in ("kup", "sprzedaj") or not valid_mint(mint):
                raise ValueError("Podaj stronę transakcji i poprawny adres tokenu.")
            args = [side, mint]
            if side == "kup":
                usd = float(b.get("usd") or 0)
                if not 0 < usd <= 50:
                    raise ValueError("Kwota musi być dodatnia.")
                args += ["--usd", f"{usd:g}"]
            else:
                pct = float(b.get("procent") or 100)
                if not 0 < pct <= 100:
                    raise ValueError("Procent od 1 do 100.")
                args += ["--procent", f"{pct:g}"]
            # Bez "confirm" tylko podgląd. Z "confirm" wykonanie: to Twoje kliknięcie w panelu, jedna transakcja.
            if b.get("confirm") is True:
                args.append("--potwierdzam")
            code, out = run(args, 120)
            return {"ok": code == 0, "executed": "--potwierdzam" in args and code == 0, "msg": out}
        return None


def main():
    if not (UI / "index.html").exists():
        sys.exit("Brakuje folderu panel/ z interfejsem.")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Panel działa: {url}  (tylko na tym Macu, Ctrl+C kończy)", flush=True)
    if "--bez-przegladarki" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for name, p in procs.items():
            if p.poll() is None:
                print(f"Zatrzymuję {name} uruchomione z panelu.")
                p.send_signal(signal.SIGINT)


if __name__ == "__main__":
    main()
