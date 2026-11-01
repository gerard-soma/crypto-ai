# Agent wielorybów (Solana / jup.ag)

Śledzi portfele, które ostatnio dobrze zarobiły na tokenach Solany, i pokazuje, co kupują teraz. Każdy sygnał ma ocenę ryzyka i gotowy link do swapu w jup.ag. Na Twoje polecenie może też kupować i sprzedawać z osobnego portfela agenta, ale zawsze po podglądzie i Twojej akceptacji (patrz „Handel z akceptacją”).

## Jak to działa

1. **Odkrywanie** (`odkryj`) bierze z Jupitera tokeny, które urosły o 50% i więcej w ciągu 24 h, mają płynność od $30k i kapitalizację od $100k. Na każdym z nich szuka portfeli, które kupiły za co najmniej $150 i podwoiły pieniądze. Pomija pierwszą minutę po starcie tokenu (to snajperzy), a także pule i routery. Jedno trafienie to może być fart, więc każdy kandydat i każdy już obserwowany portfel dostaje profil z własnej historii (ostatnie ok. 300 transakcji): na ilu tokenach jest na plusie, a na ilu na minusie, jaki ma łączny wynik, jak długo trzyma tokeny i ile robi zamian dziennie. Na listę trafiają tylko portfele z co najmniej 2 tokenami na plusie, skutecznością od 30%, łącznym wynikiem na plusie, medianą trzymania od 1 h i najwyżej 20 zamianami dziennie (progi w `.env.example`). Większość kandydatów odpada: to boty, skalperzy albo portfele, które ogólnie tracą.
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

Darmowy plan daje 1 mln kredytów miesięcznie. Odkrywanie zużywa ok. 30 tys., raport ok. 3–9 tys., a agent wypisuje zużycie po każdym uruchomieniu. Licznik miesięczny leży w `dane/helius.json`. Agent odmówi pracy po przekroczeniu `HELIUS_MONTHLY_BUDGET` (domyślnie 300 tys.), odkrywa najwyżej raz na 20 h (`odkryj --wymus` pomija limit), a raport młodszy niż godzina pokazuje z pamięci (`raport --swiezy` pobiera nowy).

## Użycie

```
python3 agent.py odkryj        # codziennie przez pierwsze dni, potem co kilka: buduje i weryfikuje listę portfeli
python3 agent.py raport        # kiedy chcesz: co kupują teraz
python3 agent.py raport --godziny 6
python3 agent.py portfel ADRES # podgląd jednego portfela
python3 agent.py wyniki        # dziennik sygnałów: jak zachowały się po 24 h i 72 h
python3 agent.py diagnoza      # szybki stan portfela agenta i prywatnego, bez kredytów Helius
```

### Dziennik sygnałów

Każdy raport dopisuje swoje sygnały do `dane/dziennik.json` razem z ceną z chwili sygnału (ten sam token najwyżej raz na dobę). Raport i `wyniki` uzupełniają cenę po 24 h i 72 h. `wyniki` pokazuje medianę zmiany ceny, odsetek sygnałów, które doszły do 2×, i odsetek, które spadły o 40% lub więcej, osobno dla sygnałów z ostrzeżeniami, bez ostrzeżeń i dla KONSENSUSU. Po 1–2 tygodniach widać, czy kopiowanie tych portfeli daje przewagę, zanim wyda się na to pieniądze. Cena jest z chwili sprawdzenia, więc uruchamiaj raport albo `wyniki` codziennie.

### Czuwanie i flipy na sucho

```
python3 agent.py obserwuj --dodaj MINT  # dodaj token do czuwania (bez prawa dodruku i zamrożenia)
python3 agent.py obserwuj               # lista ze zmianą ceny w 15 min / 1 h / 4 h
python3 agent.py czuwaj --co 2          # we własnym Terminalu: sprawdza ceny co 2 min, Ctrl+C kończy
python3 agent.py flipy                  # wyniki flipów na sucho
```

Czuwanie bierze ceny z darmowego API Jupitera (0 kredytów Helius). Testuje dwie strategie bez pieniędzy: **dołek** (spadek ≥ 15% w 1 h, ale nie więcej niż 35% w 4 h ani 25% w 24 h) i **wybicie** (wzrost ≥ 10% w 15 min). Wchodzi tylko przy płynności od $50k. Symulowany flip kończy się przy +20%, −12% albo po 4 h, a od wyniku odejmuje 3% kosztów (opłaty, poślizg, wpływ na cenę). Wejścia i wyjścia trafiają do `dane/flipy.json` i do powiadomień na Macu. Czuwanie pilnuje też prawdziwych pozycji agenta i daje znać przy celu albo stopie (najwyżej raz na 6 h). Niczego nie kupuje. Progi zmienisz w `.env` (`FLIP_*`). Reguły wejścia potrzebują historii cen, więc pierwsze sygnały pojawią się po ok. 15 min (wybicie) i 1 h (dołek) czuwania.

### Snajper nowych tokenów (pump.fun)

```
python3 agent.py snajper                 # na sucho: łapie nowe tokeny, niczego nie kupuje
python3 agent.py snajper-wyniki          # wyniki; --godziny 24 tylko ostatnia doba
python3 agent.py nowy-portfel --snajper  # osobny portfel do prawdziwego handlu
python3 agent.py snajper --naprawde      # we własnym Terminalu: handluje sam, w limitach z .env
touch dane/snajper.STOP                  # wstrzymuje nowe zakupy (rm, żeby wznowić)
```

Nowe tokeny przychodzą na żywo z darmowego strumienia PumpPortal, a ceny co 3 s z Jupitera (0 kredytów Helius). Strategie: **start** kupuje pierwszą cenę każdego tokenu (punkt odniesienia), **rozpęd** kupuje, gdy w 3 min od startu cena wzrośnie o 50%. Tokeny, na które twórca wydał na starcie ponad 5 SOL, są pomijane. Wyjście: połowa przy +100%, stop −30%, po realizacji połowy ruchomy stop 30% od szczytu, najwyżej 10 min. Na sucho wejście i wyjście liczymy po cenie z następnego odczytu po sygnale, czyli ok. 3 s później, jak w prawdziwej transakcji. Koszty: 1,5% prowizji w każdą stronę, opłata sieci i 0,0015 SOL kaucji za konto tokenu. Kaucja wraca dopiero po zamknięciu konta, a przy stawce $1 to ok. 14%.

Z `--naprawde` bot handluje sam z portfela snajpera (`~/.config/wieloryby/snajper.json`), bez pytania o każdą transakcję. Naprawdę handluje tylko jedna strategia (`SNAJPER_STRATEGIA`, domyślnie rozpęd), reszta dalej na sucho. Twarde limity: stawka `SNAJPER_USD` (domyślnie $1, w kodzie najwyżej $5), najwyżej 2 otwarte pozycje, 10 zakupów i $3 straty na dobę (otwarte pozycje liczą się jak stracone). Start wymaga wpisania TAK we własnym oknie Terminala. Po Ctrl+C otwarte pozycje zostają w `dane/snajper-otwarte.json`, a następne uruchomienie je domyka.

Boty, które kupują w tym samym bloku co start tokenu, zawsze będą szybsze. Snajper łapie dopiero to, co dzieje się po kilku sekundach. Najpierw zbierz kilkaset pozycji na sucho i zobacz medianę.

## Panel w przeglądarce

```
python3 panel.py        # albo dwuklik w Panel.command w Finderze
```

Panel otwiera się pod `http://127.0.0.1:8765/` (inny port: `PANEL_PORT=8800 python3 panel.py`). Pokazuje portfele, czuwanie z flipami, snajpera, raport wielorybów i dziennik sygnałów. Pozwala też:
- włączać i wyłączać czuwanie i snajpera na sucho,
- dodawać i usuwać obserwowane tokeny,
- wstrzymywać zakupy snajpera,
- kupować i sprzedawać z portfela agenta: najpierw podgląd, potem osobne „Wykonaj” dla każdej transakcji.

Nowy raport i odkrywanie zużywają kredyty Helius, więc panel pyta przed ich uruchomieniem. Prawdziwego snajpera (`--naprawde`) panel nie uruchamia ani nie zatrzymuje, to zostaje w Terminalu.

Panel słucha tylko na tym Macu, sprawdza nagłówek Host i wymaga klucza, który dostaje tylko jego własna strona. Inne strony otwarte w przeglądarce nie mogą więc wywołać jego poleceń. Zamknięcie panelu (Ctrl+C) zatrzymuje też to, co uruchomił.

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

Wymagania: Python 3.10+. Raporty działają bez instalowania czegokolwiek. Handel potrzebuje biblioteki `cryptography` do podpisu, którą już zainstalowałem w `.venv`. Snajper potrzebuje jeszcze `websockets`. Na nowym komputerze: `python3 -m venv .venv && .venv/bin/pip install cryptography websockets`.

## Czego się spodziewać

- Wieloryby kupują przed Tobą. Kiedy zobaczysz sygnał, cena mogła już urosnąć. Dlatego raport pokazuje stosunek obecnej ceny do ich wejścia.
- Wartości w USD to przybliżenie, liczone po dzisiejszym kursie SOL.
- Niektóre portfele z wysokim zwrotem to insiderzy albo twórcy tokenów. Filtr ich nie wyłapie, więc dwa niezależne portfele (konsensus) są dużo mocniejszym sygnałem niż jeden.
- To narzędzie do research'u, a nie gwarancja zysku. Wkładaj tylko tyle, ile możesz stracić.
