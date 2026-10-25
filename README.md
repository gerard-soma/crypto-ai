# Agent wielorybów (Solana / jup.ag)

Śledzi portfele, które ostatnio dobrze zarobiły na tokenach Solany, i pokazuje, co kupują teraz. Każdy sygnał ma ocenę ryzyka i gotowy link do swapu w jup.ag. Na Twoje polecenie może też kupować i sprzedawać z osobnego portfela agenta, ale zawsze po podglądzie i Twojej akceptacji (patrz „Handel z akceptacją”).

## Jak to działa

1. **Odkrywanie** (`odkryj`) bierze z Jupitera tokeny, które urosły o 50% i więcej w ciągu 24 h, mają płynność od $30k i kapitalizację od $100k. Na każdym z nich szuka portfeli, które kupiły za co najmniej $150 i podwoiły pieniądze. Pomija pierwszą minutę po starcie tokenu (to snajperzy), a także pule, routery i boty z setkami transakcji dziennie. Portfele z wygranymi na kilku tokenach dostają wyższy wynik.
2. **Raport** (`raport`) sprawdza transakcje obserwowanych portfeli z ostatnich 24 h i grupuje zakupy według tokenu. Gdy ten sam token kupiło netto kilka portfeli, raport oznacza to jako „KONSENSUS”. Ocenę obniżają czerwone flagi: twórca może dodrukować albo zamrozić tokeny, płynność jest niska, posiadanie jest skupione w kilku portfelach, ruch jest sztuczny albo cena już uciekła ponad 2× od wejścia wielorybów.
3. **Ty decydujesz**: klikasz link, Jupiter otwiera się z ustawioną parą SOL → token, a transakcję potwierdzasz w swoim portfelu.

Źródła danych: [Helius](https://www.helius.dev) (transakcje portfeli, darmowy klucz) i API tokenów Jupitera (ceny, płynność, audyt, bez klucza).

## Klucz Helius (2 minuty, za darmo)

1. Wejdź na https://dashboard.helius.dev i zaloguj się (e-mail, Google albo GitHub).
2. Na stronie **API Keys** skopiuj klucz.
3. W tym folderze:
   ```
   cp .env.example .env
   ```
   Otwórz `.env` i wklej klucz po `HELIUS_API_KEY=`.

Darmowy plan daje 1 mln kredytów miesięcznie. Odkrywanie zużywa ok. 10–15 tys., raport ok. 3–9 tys., a agent wypisuje zużycie po każdym uruchomieniu.

## Użycie

```
python3 agent.py odkryj        # raz na kilka dni: buduje listę obserwowanych portfeli
python3 agent.py raport        # kiedy chcesz: co kupują teraz
python3 agent.py raport --godziny 6
python3 agent.py portfel ADRES # podgląd jednego portfela
```

## Handel z akceptacją

Agent handluje z **osobnego portfela**, a nie z Twojego głównego. Dzięki temu nawet błąd albo zły sygnał może kosztować najwyżej to, co na nim leży.

1. **Utwórz portfel agenta** we własnym Terminalu: `python3 agent.py nowy-portfel`. Klucz trafi do `~/.config/wieloryby/portfel.json`, poza folder projektu, z dostępem tylko dla Ciebie.
2. **Zrób kopię klucza**: `python3 agent.py eksport` (działa tylko we własnym oknie Terminala). Zaimportuj klucz do Phantoma albo Solflare. Wtedy zawsze możesz wypłacić środki ręcznie.
3. **Zasil portfel**: wyślij SOL za ok. 25 USD ze swojego portfela Jupiter/Phantom na adres agenta.

Każda transakcja ma dwa kroki:
```
python3 agent.py kup MINT --usd 5               # podgląd: kwota, ile dostaniesz, wpływ na cenę, ostrzeżenia
python3 agent.py kup MINT --usd 5 --potwierdzam # wykonanie
python3 agent.py sprzedaj MINT --procent 50
python3 agent.py pozycje                         # saldo, zysk/strata i podpowiedź, kiedy wyjść
```
We własnym Terminalu podgląd sam zapyta o „TAK”. W Claude Code każde polecenie `kup`/`sprzedaj` wymaga Twojej zgody w okienku uprawnień (`.claude/settings.json`). Claude nie może też czytać pliku z kluczem ani uruchomić `eksport`.

Twarde limity w kodzie (zmienisz je w `.env`):

| Limit | Domyślnie |
|---|---|
| Jeden zakup | 10 USD |
| Zakupy w ciągu 24 h | 25 USD |
| Poślizg | 3% |
| Wpływ na cenę przy zakupie / sprzedaży | 3% / 15% |
| Rezerwa SOL na opłaty | 0,02 SOL |

Agent odmówi zakupu tokenu, którego twórca może dodrukować albo zamrozić tokeny. Przed wykonaniem pobiera świeżą wycenę i przerywa, jeśli cena pogorszyła się o ponad 3% od podglądu. Każdą transakcję zapisuje w `dane/transakcje.json`.

Swap idzie przez Jupiter Ultra API (to samo co na jup.ag). Opłata Jupitera to ok. 0,1%. Przy pierwszym zakupie tokenu dochodzi ok. 0,006 SOL (~0,60 USD) na założenie konta tokenu, więc przy 25 USD lepiej 2–3 przemyślane zakupy niż dziesięć drobnych.

## Claude Code

W Claude Code wpisz `/wieloryby`. Claude uruchomi raport, sprawdzi portfele stojące za najlepszymi sygnałami i podpowie po polsku, czy warto wejść, za ile przy Twoim budżecie i kiedy wyjść.

Wymagania: Python 3.10+. Raporty działają bez instalowania czegokolwiek. Handel potrzebuje biblioteki `cryptography` do podpisu, którą już zainstalowałem w `.venv`. Na nowym komputerze: `python3 -m venv .venv && .venv/bin/pip install cryptography`.

## Czego się spodziewać

- Wieloryby kupują przed Tobą. Kiedy zobaczysz sygnał, cena mogła już urosnąć. Dlatego raport pokazuje stosunek obecnej ceny do ich wejścia.
- Wartości w USD to przybliżenie, liczone po dzisiejszym kursie SOL.
- Niektóre portfele z wysokim zwrotem to insiderzy albo twórcy tokenów. Filtr ich nie wyłapie, więc dwa niezależne portfele (konsensus) są dużo mocniejszym sygnałem niż jeden.
- To narzędzie do research'u, a nie gwarancja zysku. Wkładaj tylko tyle, ile możesz stracić.
