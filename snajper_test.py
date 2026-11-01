"""Test reguł snajpera na nagraniach: te same tokeny, setki wariantów progów wejścia i wyjścia.

Nagrania robi `snajper` (dane/snajper-nagrania/): odczyty z Jupitera co kilka sekund dla każdego nowego tokenu.
Tokeny dzielimy w czasie. Starsze 60% służą do strojenia, nowsze 40% są sprawdzianem. Warianty wybieramy po
wyniku na strojeniu i pokazujemy, jak wypadły na sprawdzianie. Liczy się tylko sprawdzian: najlepszy z setek
wariantów prawie zawsze wygląda dobrze na danych, na których go wybrano.

Symulacja jest taka sama jak snajper na sucho: sygnał na jednym odczycie, wejście po cenie z następnego,
wyjście tak samo, z prowizją i opłatami. Cechy liczą te same funkcje co bot.
"""
import gzip
import itertools
import json
import statistics
import sys
import time
from datetime import datetime

import agent
import snajper as S

OUT = agent.DATA / "snajper-test.json"
TRAIN_SHARE = 0.6
MIN_TRAIN_N = 20

ENTRY_GRID = {
    "traders": [5, 10, 20, 40],
    "net_usd": [0.0, 500.0, 2000.0, 5000.0],
    "ratio": [1.0, 1.5, 2.5],
    "dev_mints": [3, 20, 10 ** 9],
    "top_pct": [40.0, 100.0],
    "max_age": [30.0, 60.0, 120.0],
}
EXIT_GRID = {"tp": [50.0, 100.0, 200.0], "sl": [-20.0, -30.0, -45.0], "trail": [20.0, 35.0],
             "max_hold": [60.0, 180.0, 600.0]}
MIN_TRADERS = min(ENTRY_GRID["traders"])


class Replay:
    """Nagrany token: odczyty i cechy policzone tak samo jak na żywo."""

    def __init__(self, rec):
        self.mint, self.symbol, self.created = rec["mint"], rec["symbol"], rec["created"]
        self.snaps = rec["snaps"]
        first = self.snaps[0][1]
        self.feats = [S.features(s, first, rec["static"], rec["dev_sol"]) for s in self.snaps]
        self.max_traders = max(f["traders"] for f in self.feats)

    def first_signal(self, rule):
        """Indeks pierwszego odczytu, na którym reguła się spełnia, albo None."""
        for i, f in enumerate(self.feats):
            if rule(f):
                return i
        return None


def simulate(r, i, ex, sol_usd):
    """Pozycja z sygnału na odczycie i. Zwraca net % albo None, gdy po sygnale nie ma już odczytu."""
    if i + 1 >= len(r.snaps):
        return None
    t_in, entry = r.snaps[i + 1][0], r.snaps[i + 1][1]
    pos = {"entry": entry, "peak": entry, "left": 1.0, "realized": 0.0, "usd": S.USD, "exits": []}
    pending = None
    for s in r.snaps[i + 2:]:
        t, price = s[0], s[1]
        if pending:
            reason, frac = pending
            pending = None
            pos["realized"] += S.sell_value(frac, price, entry)
            pos["left"] = round(pos["left"] - frac, 6)
            pos["exits"].append({"reason": reason})
            if pos["left"] <= 1e-6:
                break
            continue
        pos["peak"] = max(pos["peak"], price)
        pending = S.exit_decision(pos, price, t - t_in, ex)
    if pos["left"] > 1e-6:
        # Nagranie się skończyło (martwy token bez traderów albo koniec okna nagrania). Bez transakcji cena na
        # krzywej stoi, a krzywa zawsze odkupuje, więc resztę sprzedajemy po ostatniej cenie.
        pos["realized"] += S.sell_value(pos["left"], r.snaps[-1][1], entry)
        pos["exits"].append({"reason": "koniec nagrania"})
    net, _ = S.finish(pos, sol_usd)
    return net


def summary(nets):
    if not nets:
        return {"n": 0}
    return {"n": len(nets), "mean_pct": round(statistics.mean(nets), 1), "median_pct": round(statistics.median(nets), 1),
            "win_pct": round(sum(n > 0 for n in nets) / len(nets) * 100),
            "usd": round(sum(nets) / 100 * S.USD, 2)}


def run_rule(replays, rule, ex, sol_usd):
    nets = []
    for r in replays:
        i = r.first_signal(rule)
        if i is not None:
            n = simulate(r, i, ex, sol_usd)
            if n is not None:
                nets.append(n)
    return summary(nets)


def load(days):
    files = sorted(S.RECORDINGS.glob("*.jsonl.gz"))
    if days:
        files = files[-int(days):]
    reps = []
    for path in files:
        try:
            with gzip.open(path, "rt") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue   # niedokończona linia po twardym zamknięciu
                    if len(rec.get("snaps") or []) >= 2:
                        reps.append(Replay(rec))
        except (OSError, EOFError):
            continue
    reps.sort(key=lambda r: r.created)
    return files, reps


def cmd_test(args):
    files, reps = load(args.dni)
    if not reps:
        sys.exit("Brak nagrań. Uruchom snajpera (python3 agent.py snajper) i daj mu pozbierać dane.")
    try:
        sol_usd = agent.sol_price()
    except agent.ApiError:
        sol_usd = 150.0
    cut = reps[int(len(reps) * TRAIN_SHARE)].created
    train_all = [r for r in reps if r.created < cut]
    test_all = [r for r in reps if r.created >= cut]
    train = [r for r in train_all if r.max_traders >= MIN_TRADERS]
    test = [r for r in test_all if r.max_traders >= MIN_TRADERS]
    span_h = (reps[-1].created - reps[0].created) / 3600
    print(f"TEST SNAJPERA · {len(files)} plików nagrań · {len(reps)} tokenów z ceną · {span_h:.1f} h · "
          f"stawka {agent.fmt_usd(S.USD)} · odczyty co ok. {S.POLL_S:g} s")
    print(f"  strojenie: tokeny sprzed {datetime.fromtimestamp(cut).strftime('%d.%m %H:%M')}, sprawdzian: późniejsze\n")
    if len(reps) < 2000:
        print(f"  ⚠ Tylko {len(reps)} tokenów. Wiarygodne wnioski dopiero przy kilku tysiącach (ok. doba nagrań).\n")
    started = time.time()

    ref = {}
    for name, (tr, te, rule) in {
        "start (każdy token)": (train_all, test_all, lambda f: True),
        "rozpęd (obecny)": (train_all, test_all, S.momentum_signal),
        "przepływ (obecny)": (train, test, S.flow_signal),
    }.items():
        ref[name] = [run_rule(tr, rule, S.EXITS, sol_usd), run_rule(te, rule, S.EXITS, sol_usd)]

    keys = list(ENTRY_GRID)
    stage1 = []
    for combo in itertools.product(*ENTRY_GRID.values()):
        p = dict(S.FLOW, **dict(zip(keys, combo)))
        tr = run_rule(train, lambda f, p=p: S.flow_signal(f, p), S.EXITS, sol_usd)
        if tr["n"] >= MIN_TRAIN_N:
            stage1.append((tr["mean_pct"], p))
    stage1.sort(key=lambda x: x[0], reverse=True)

    stage2 = []
    exit_combos = [dict(zip(EXIT_GRID, c)) for c in itertools.product(*EXIT_GRID.values())]
    for _, p in stage1[:5]:
        rule = lambda f, p=p: S.flow_signal(f, p)
        for ex in exit_combos:
            tr = run_rule(train, rule, ex, sol_usd)
            if tr["n"] >= MIN_TRAIN_N:
                stage2.append((tr["mean_pct"], p, ex, tr))
    stage2.sort(key=lambda x: x[0], reverse=True)
    best = []
    for _, p, ex, tr in stage2[:10]:
        te = run_rule(test, lambda f, p=p: S.flow_signal(f, p), ex, sol_usd)
        best.append({"entry": {k: p[k] for k in keys}, "exits": ex, "train": tr, "test": te})

    def fmt(s):
        if not s["n"]:
            return "    0 pozycji"
        return (f"{s['n']:>5} poz. · średnio {s['mean_pct']:+6.1f}% · mediana {s['median_pct']:+6.1f}% · "
                f"na plusie {s['win_pct']:>3}%")

    print("PUNKTY ODNIESIENIA (obecne wyjścia)")
    for name, (tr, te) in ref.items():
        print(f"  {name}\n      strojenie   {fmt(tr)}\n      sprawdzian  {fmt(te)}")
    print(f"\nNAJLEPSZE WARIANTY PRZEPŁYWU (spośród {len(stage1)} wariantów wejścia z ≥ {MIN_TRAIN_N} pozycjami "
          f"na strojeniu; dla 5 najlepszych {len(exit_combos)} wariantów wyjścia)")
    if not best:
        print(f"  Żaden wariant nie miał na strojeniu ≥ {MIN_TRAIN_N} pozycji. Potrzeba więcej nagrań.")
    for b in best:
        e, x = b["entry"], b["exits"]
        dev = "dowolny twórca" if e["dev_mints"] >= 10 ** 9 else f"twórca ≤ {e['dev_mints']} tok."
        print(f"  ≥ {e['traders']} traderów, netto ≥ ${e['net_usd']:g}, k/s ≥ {e['ratio']:g}, {dev}, "
              f"top ≤ {e['top_pct']:g}%, do {e['max_age']:g} s | cel +{x['tp']:g}%, stop {x['sl']:g}%, "
              f"ruchomy {x['trail']:g}%, max {x['max_hold']:g} s\n"
              f"      strojenie   {fmt(b['train'])}\n      sprawdzian  {fmt(b['test'])}")
    good = [b for b in best if b["test"]["n"] >= 30 and b["test"]["mean_pct"] > 0]
    print()
    if good:
        print(f"Na sprawdzianie {len(good)} z {len(best)} najlepszych wariantów jest na plusie przy ≥ 30 pozycjach. "
              "To dopiero sygnał: potwierdź go na nagraniach z kolejnych dni, zanim ustawisz progi w .env.")
    else:
        print("Żaden z najlepszych wariantów nie jest na plusie na sprawdzianie przy ≥ 30 pozycjach. "
              "Przewagi na razie nie widać: zbieraj nagrania dalej albo zmień podejście.")
    print(f"(obliczenia {time.time() - started:.0f} s)")
    agent.DATA.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"created": time.time(), "tokens": len(reps), "span_h": round(span_h, 1), "cut": cut,
                               "reference": ref, "best": best}, indent=1, ensure_ascii=False))
