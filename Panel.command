#!/bin/zsh
# Dwuklik w Finderze uruchamia panel agenta i otwiera go w przeglądarce. Zamknięcie okna Terminala kończy panel.
cd "$(dirname "$0")" && exec python3 panel.py
