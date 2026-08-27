# Ruční obchodování opcí přes TWS

Webová aplikace pro **ruční** nákup a prodej amerických akciových opcí přes
Interactive Brokers TWS API. Obchodník zadá ticker, množství a směr; program
sám vybere expiraci i strike podle aktuální ceny podkladu a nabídne tlačítka
pro nákup a prodej za konkrétní cenu z kotace.

Aplikace **nezadává žádné automatické cíle ani stop-lossy**. Každý příkaz
vzniká výhradně stiskem tlačítka.

---

## Obsah

- [Co aplikace dělá](#co-aplikace-dělá)
- [Instalace a spuštění](#instalace-a-spuštění)
- [Nastavení TWS](#nastavení-tws)
- [Ovládání](#ovládání)
  - [Zadání a náhled kontraktu](#zadání-a-náhled-kontraktu)
  - [Nákup](#nákup)
  - [Prodej a runner](#prodej-a-runner)
  - [Přecenění příkazu, kterému utekl trh](#přecenění-příkazu-kterému-utekl-trh)
  - [Ochrana proti druhému příkazu](#ochrana-proti-druhému-příkazu)
- [Výběr expirace a strike](#výběr-expirace-a-strike)
- [Výsledek pozice](#výsledek-pozice)
- [Konfigurace](#konfigurace)
- [Obnova po restartu](#obnova-po-restartu)
- [Struktura projektu](#struktura-projektu)
- [Testy](#testy)
- [Omezení](#omezení)

---

## Co aplikace dělá

1. Zadáte **ticker**, **množství** kontraktů a zvolíte **CALL / PUT**.
2. Program načte z TWS cenu podkladu, vybere expiraci a strike a ověří,
   že takový opční kontrakt v TWS existuje. Zobrazí jeho kotaci BID/ASK,
   střed trhu, spread, deltu a odhad nákladů.
3. Tlačítky **Koupit za ASK** nebo **Koupit za MID** zadáte limitní nákupní
   příkaz.
4. Po nakoupení nabídne pozice tlačítka pro prodej: celé pozice, nebo jen
   její základní části, takže v trhu zůstane **runner**.

Všechny příkazy jsou limitní. Cena se bere z aktuální kotace v okamžiku
stisku tlačítka a zaokrouhluje se na minimální tik kontraktu.

---

## Instalace a spuštění

Vyžaduje **Python 3.10 nebo novější** a běžící **TWS** (nebo IB Gateway)
s povoleným API.

**macOS / Linux**

```bash
./run.sh
```

**Windows**

```bat
run.bat
```

Skript při prvním spuštění založí virtuální prostředí `.venv`, doinstaluje
závislosti z `requirements.txt` a aplikaci spustí. Rozhraní pak běží na
<http://127.0.0.1:8081>.

Případné přepínače se předávají aplikaci:

| Přepínač | Význam |
| --- | --- |
| `--no-connect` | nepřipojovat se k TWS při startu (spojení lze navázat tlačítkem v hlavičce) |
| `--verbose` | podrobné logování včetně komunikace knihovny `ib_async` |
| `-c CESTA`, `--config CESTA` | jiný konfigurační soubor než `config.yaml` |

Při prvním spuštění se z `config.example.yaml` vytvoří `config.yaml`.
Upravujte `config.yaml`; šablona slouží jen jako komentovaný vzor.

---

## Nastavení TWS

V TWS: **File → Global Configuration → API → Settings**

- zaškrtnout *Enable ActiveX and Socket Clients*,
- ověřit **Socket port** (7497 papírový účet, 7496 ostrý; IB Gateway 4002 / 4001)
  a zapsat jej do `connection.port`,
- `connection.client_id` musí být jiné než u ostatních aplikací připojených
  ke stejné TWS.

Bez odběru tržních dat pro opce zůstane kotace prázdná a tlačítka pro nákup
se nepovolí - limitní cenu totiž není z čeho spočítat.

---

## Ovládání

### Zadání a náhled kontraktu

Kontrakt se hledá po opuštění pole s tickerem nebo po stisku Enter, aby
se do TWS neposílal dotaz po každém napsaném znaku. Náhled ukazuje:

- vybraný kontrakt a počet dní do expirace,
- cenu podkladu a deltu opce,
- BID / ASK / MID a spread,
- odhad nákladů na zadané množství (počítáno ze středu trhu).

Upozornění se vypisují pod náhledem - například když je spread nad limitem
z konfigurace, nebo když vybraný strike není pro danou expiraci
obchodovatelný a použil se nejbližší dostupný.

### Nákup

| Tlačítko | Limitní cena | Kdy se hodí |
| --- | --- | --- |
| **N ks za ASK** | poptávaná cena (+ volitelná tolerance) | příkaz má projít hned |
| **N ks za MID** | střed trhu | levnější vstup, nemusí se vyplnit |

Na tlačítku je vždy vidět cena, se kterou příkaz do trhu půjde - například
`3 ks za ASK · 3.50`.

### Prodej a runner

Po vyplnění nákupu nabídne karta pozice dva řádky tlačítek - v prvním se
prodává celá pozice, ve druhém jen její základní část:

```
BID (3 ks)  MID (3 ks)  ASK (3 ks)  ASK +1 %  +2 %  +3 %  +4 %  +5 %  +7 %  +9 %
BID (2 ks)  MID (2 ks)  ASK (2 ks)  ASK +1 %  +2 %  +3 %  +4 %  +5 %  +7 %  +9 %
```

Nabídka jde zleva doprava od nejjistějšího vyplnění k nejvyšší ceně: na BID
se prodá hned a zaplatí se celý spread, na MID se čeká na střed trhu, na ASK
se spread naopak inkasuje a přirážky míří ještě výš.

Horní řádek prodává celou drženou pozici, spodní jen její základní část, takže
v trhu zůstane runner - v příkladu výše 3 ks proti 2 ks. Oba řádky mají stejně
široké sloupce, takže tlačítka téhož druhu leží přesně nad sebou; který řádek
je který, prozradí barva (červená = celá pozice, oranžová = runner zůstává)
a bublina s nápovědou. Popisek začíná cenou a končí částkou, se kterou příkaz
skutečně půjde do trhu.


Na každém tlačítku je za popiskem ještě konkrétní cena, se kterou příkaz
půjde do trhu - například `BID (3 ks) · 3.00`.

**Přirážky nad poptávkou** (`ASK +1 %` až `+5 %`, dál `+7 %` a `+9 %`) zadají
prodej nad ASK: za kontrakt přijde víc peněz, ale příkaz se vyplní s menší
pravděpodobností. Pozor u kontraktů s hrubým rastrem - je-li tik 0,05
a ASK 3,20, vyjde +1 % i +2 % na stejnou cenu 3,25. Skutečná cena je proto
vždy napsaná na tlačítku.

Nabídku přirážek určuje `ASK_MARKUPS` v `tws_rucne/models.py`; řádek se
sloupci přizpůsobí sám, žádné další místo se kvůli tomu nepřepisuje.

Základní pozice je držené množství snížené o runner (`trading.runner_quantity`,
ve výchozím nastavení 1 kontrakt). Po jejím prodeji zbývá v pozici jen runner
a nabízejí se už jen tlačítka **Prodat vše** - přesně tak, jak má scale-out
fungovat. Kupovali-li jste kontraktů právě tolik, kolik činí runner (typicky
jeden), tlačítka pro prodej základní pozice se vůbec nezobrazí.

### Úklid přehledu

Ukončené pozice - uzavřené, zrušené i chybové - zůstávají v přehledu, dokud
je obchodník neodstraní. Slouží k tomu buď tlačítko **Odstranit z přehledu**
u jednotlivé pozice, nebo **Odstranit ukončené** v hlavičce přehledu, které
vyklidí všechny naráz. Otevřených pozic se úklid nedotkne a do TWS neposílá
nic; jde čistě o obrazovku.

### Přecenění příkazu, kterému utekl trh

Limitní příkaz se nemusí vyplnit - trh se mezitím pohne jinam. **Opakovaný
stisk téhož tlačítka proto nezakládá druhý příkaz, ale přecení ten stávající**
na aktuální cenu (v TWS jde o modifikaci příkazu se stejným `orderId`).

- U pozice s nevyplněným **nákupem** se tlačítka přepnou na
  *Přecenit nákup na ASK / MID*. Totéž udělá i nákupní tlačítko ve formuláři,
  pokud na daném tickeru a směru nevyplněný nákup čeká.
- U pozice s nevyplněným **prodejem** se prodejní tlačítka přepnou na
  *Přecenit prodej na BID / MID*. Přecenit lze i mezi rozsahy - z prodeje
  základní pozice na prodej všeho a zpět; příkaz se jen upraví.
- Přecenění mění i **množství**: přepsáním pole ve formuláři a novým stiskem
  se upraví počet kontraktů nákupního příkazu.
- **Částečně vyplněný příkaz se přecenit dá** - upraví se limit zbývajícího
  množství, už nakoupené (resp. prodané) kusy zůstávají za svou cenou.
  Celkový objem příkazu nesmí klesnout pod to, co je vyplněné; aplikace
  na to upozorní místo toho, aby poslala do TWS nesmyslnou úpravu.
- Nezměnila-li se cena ani počet kusů, do TWS se nic neposílá - zbytečná
  modifikace by příkaz jen vrátila na konec fronty.

Tlačítkem **Zrušit příkaz v trhu** lze příkaz kdykoliv stáhnout.

### Ochrana proti druhému příkazu

Vyplnění příkazu a stisk tlačítka se mohou potkat - TWS příkaz vyplní
o zlomek sekundy dřív, než se rozhraní překreslí. Aby z toho nevznikl
nechtěný druhý nákup nebo prodej:

- před každým zadáním se stav nevyřízených příkazů srovná s TWS,
- tlačítko nese stav, ve kterém bylo vykresleno; pokud se stav pozice
  mezitím změnil (příkaz se **celý** vyplnil, nebo naopak nový vznikl), akce
  se **odmítne s vysvětlením** a do trhu nic nejde (částečné vyplnění
  přecenění nebrání - viz výše),
- dokud se příkaz odesílá, další stisk se zahodí, takže z dvojkliku
  nevzniknou dva příkazy.

---

## Výběr expirace a strike

**Expirace** (`expiration.mode`):

- `nearest` - nejbližší expirace s alespoň `min_dte` dny do splatnosti
  (0 = i dnešní expirace). Není-li žádná vhodná, použije se nejvzdálenější
  dostupná.
- `fixed` - konkrétní datum z `expiration.fixed_date` ve tvaru `YYYYMMDD`.

**Strike** (`strike.mode`) se určuje z aktuální ceny podkladu:

- `otm_offset` - strike odsazený mimo peníze: u CALL nad cenou, u PUT pod ní.
  `strike.otm_steps` udává, kolikátý takový strike se vezme, a počítá se
  v **krocích rastru řetězce**, ne v bodech. Jedno nastavení tak platí pro
  všechny tickery: u SPY (rastr 1) je krok dolar, u AAPL (rastr 2,5) dva a půl.
- `atm` - nejbližší strike aktuální ceně podkladu.

Opční řetězec vrací strike ceny pro všechny expirace dohromady, takže vybraný
strike nemusí být pro zvolenou expiraci obchodovatelný. Aplikace proto zkouší
kandidáty podle vzdálenosti od cíle, dokud se některý v TWS neověří, a náhradu
ohlásí v náhledu.

---

## Výsledek pozice

Karta pozice ukazuje **P/L po provizích**. Zbývá-li v pozici zbytek po
částečném prodeji (typicky runner), jsou vidět dvě čísla - **výsledek
drženého zbytku a v závorce celek za celou pozici**:

```
P/L: 53.35 (153.66) USD     runner vydělal 53,35; celá pozice 153,66
P/L: 41.01 USD              nic prodáno není, jde o celý výsledek
```

Barvu určuje první číslo, protože podle něj se rozhoduje, zda runner ještě
držet. Do výpočtu vstupují:

- prodané kontrakty se počítají skutečnými prodejními cenami (i z několika
  prodejů za různé ceny),
- držené kontrakty se oceňují **středem trhu** - skutečný prodej ale proběhne
  za cenu, kterou trh v daný okamžik nabídne,
- provize se přebírají z TWS, jakmile dorazí zpráva o vyúčtování (obvykle
  krátce po vyplnění).

---

## Konfigurace

Popis všech voleb je v komentářích souboru `config.example.yaml`. Nejčastěji
se mění:

| Volba | Význam |
| --- | --- |
| `connection.port`, `connection.client_id` | připojení k TWS |
| `trading.runner_quantity` | kolik kontraktů zůstane jako runner |
| `trading.default_quantity`, `trading.default_right` | co je předvyplněné ve formuláři |
| `trading.ask_tolerance_pct`, `trading.bid_tolerance_pct` | o kolik procent smí limit přesáhnout ASK, resp. podlézt BID (0 = přesně na kotaci) |
| `trading.max_spread_pct` | nad kolik procent spreadu rozhraní upozorní (nákup nezakazuje) |
| `trading.tif` | `DAY` = do konce obchodního dne, `GTC` = do zrušení |
| `strike.mode`, `strike.otm_steps` | jak se vybírá strike |
| `expiration.mode`, `expiration.min_dte` | jak se vybírá expirace |
| `ui.port`, `ui.dark` | webové rozhraní |

---

## Obnova po restartu

Stav pozic se ukládá do `state.json` po každé změně. Po startu aplikace
(a po každém obnovení spojení) se uložené pozice **ověří proti TWS**:

- kontrakty se znovu ověří a naváže se odběr tržních dat,
- příkazy se dohledají podle značky `TWSRUCNE:<pozice>:<druh>` v poli `orderRef`,
- držené množství se přebírá z TWS - je závazné. Rozdíl proti uloženému stavu
  se srovná a **nahlásí** v hlášce pozice i v průběhu; chybějící prodejní cena
  se odhadne z posledního limitu, což je vždy vidět v logu.

Opční pozice na účtu, ke kterým aplikace nemá záznam, se vypíšou v červeném
pruhu nahoře. Aplikace k nim sama nic nezadává - patří do TWS.

---

## Struktura projektu

| Soubor | Obsah |
| --- | --- |
| `main.py` | vstupní bod, parametry příkazové řádky, start serveru |
| `tws_rucne/config.py` | načtení a validace konfigurace |
| `tws_rucne/calc.py` | výpočty bez závislosti na TWS (strike, expirace, limitní ceny, P/L) |
| `tws_rucne/models.py` | model pozice, stavy a dostupnost tlačítek |
| `tws_rucne/ib_service.py` | obálka nad `ib_async` - spojení, kontrakty, data, příkazy |
| `tws_rucne/engine.py` | obchodní logika a monitorovací smyčka |
| `tws_rucne/store.py` | ukládání a načítání stavu |
| `tws_rucne/ui.py` | webové rozhraní (NiceGUI) |
| `tws_rucne/static/styles.css` | styly |
| `tests/` | testy |

---

## Testy

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

Testy běží proti náhradě TWS (`tests/fake_ib.py`), která dědí z ostré služby -
sestavování příkazů knihovnou `ib_async` se tedy testuje doopravdy, jen se
nikam nepřipojuje. **Testy se nikdy nepřipojí k běžící TWS.**

---

## Omezení

- Aplikace hlídá jen příkazy, které sama zadala. Cokoliv zadaného ručně
  v TWS je mimo její dosah - upozorní na to pruhem s neřízenými pozicemi.
- Prodej je vždy limitní. Nevyplněný příkaz je potřeba přecenit nebo zrušit;
  tržní prodej aplikace nenabízí.
- Souběžně může na jednom tickeru a směru čekat jen jeden nevyplněný nákupní
  příkaz - další stisk jej přecení. Nová pozice na tomtéž kontraktu vznikne
  až po vyplnění té předchozí.
- Zkrácené obchodní dny ani stav burzy aplikace nesleduje.
