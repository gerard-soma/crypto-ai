"""Strażnik czatu w panelu: hook PreToolUse dla Claude Code.

Claude Code sam przepuszcza część poleceń tylko do odczytu (np. ls, cat), więc lista dozwolonych narzędzi nie wystarcza.
Ten skrypt dostaje każde wywołanie narzędzia przed wykonaniem i przepuszcza tylko:
- Bash: dokładnie polecenia agenta do odczytu z listy poniżej, bez łączenia poleceń i przekierowań,
- Read / Grep / Glob: pliki w folderze projektu, bez .env, dane/ z kluczami nie istnieją, ~/.config jest poza projektem,
- Skill: tylko skill wieloryby.
Kod wyjścia 2 blokuje narzędzie, a tekst z stderr trafia do asystenta jako powód.
"""
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AGENT = r"(?:python3|\.venv/bin/python) agent\.py"
ALLOWED_BASH = [
    rf"{AGENT} diagnoza( --json)?",
    rf"{AGENT} pozycje( --json)?",
    rf"{AGENT} wyniki( --json)?",
    rf"{AGENT} flipy( --json)?",
    rf"{AGENT} obserwuj",
    rf"{AGENT} snajper-wyniki( --godziny \d+(\.\d+)?)?( --json)?",
    rf"{AGENT} raport( --godziny \d+(\.\d+)?)?( --ile \d+)?( --json)?",
    rf"{AGENT} portfel [1-9A-HJ-NP-Za-km-z]{{32,44}}( --godziny \d+(\.\d+)?)?",
]
FORBIDDEN_NAMES = {".env"}


def deny(reason):
    print(f"Zablokowane przez panel (tylko odczyt): {reason}", file=sys.stderr)
    sys.exit(2)


def check_path(raw):
    if not raw:
        return
    p = Path(os.path.expanduser(raw))
    p = (ROOT / p if not p.is_absolute() else p).resolve()
    if p != ROOT and ROOT not in p.parents:
        deny("tylko pliki z folderu projektu.")
    if p.name in FORBIDDEN_NAMES or ".git" in p.parts or ".venv" in p.parts:
        deny(f"{p.name} jest niedostępny.")


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        deny("nieczytelne wywołanie.")
    tool, inp = event.get("tool_name"), event.get("tool_input") or {}
    if tool == "Bash":
        cmd = " ".join(str(inp.get("command", "")).split())
        if not any(re.fullmatch(rx, cmd) for rx in ALLOWED_BASH):
            deny("w panelu wolno uruchamiać tylko polecenia agenta do odczytu "
                 "(diagnoza, pozycje, wyniki, flipy, obserwuj, snajper-wyniki, raport, portfel).")
    elif tool in ("Read", "Grep", "Glob"):
        check_path(inp.get("file_path") or inp.get("path") or "")
        if ".env" in str(inp.get("glob", "")) or ".env" in str(inp.get("pattern", "")) and tool == "Glob":
            deny(".env jest niedostępny.")
        if tool == "Glob" and ".." in str(inp.get("pattern", "")):
            deny("tylko pliki z folderu projektu.")
    elif tool == "Skill":
        if str(inp.get("skill", "")).split(":")[-1] != "wieloryby":
            deny("w panelu dostępny jest tylko skill wieloryby.")
    else:
        deny(f"narzędzie {tool} jest w panelu wyłączone.")
    sys.exit(0)


if __name__ == "__main__":
    main()
