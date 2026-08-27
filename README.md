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
  - [Hlavička](#hlavička)
  - [Zadání a náhled kontraktu](#zadání-a-náhled-kontraktu)
  - [Nákup](#nákup)
  - [Prodej a runner](#prodej-a-runner)
  - [Přecenění příkazu, kterému utekl trh](#přecenění-příkazu-kterému-utekl-trh)
  - [Ochrana proti druhému příkazu](#ochrana-proti-druhému-příkazu)
- [Přehled výsledků](#přehled-výsledků)
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
4. Po nakoupení nabídne pozice tlačítka pro prodej: celé pozice, její
   základní části (v trhu zůstane **runner**), nebo jediného kontraktu.

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

### Hlavička

Vlevo stojí název aplikace, vpravo přepínač světlého a tmavého vzhledu,
stav spojení s TWS, ukazatel kvality spojení a tlačítko připojení.

Ukazatel `TWS 2,1 ms · data 0,1 s` říká, že spojení nejen stojí, ale i žije:

- **odezva TWS** je doba, za kterou TWS odpoví na dotaz na aktuální čas.
  Neměří síť k IB - běží-li TWS na tomtéž stroji, je to odezva samotné
  aplikace, tedy známka, že není zatuhlá. Měří se ve vlastním, řidším tempu
  (`ui.latency_interval_sec`, výchozí 5 s), protože jde o skutečný dotaz do
  TWS; nulou se měření vypne.
- **stáří dat** je doba od nejčerstvější kotace ze všech odebíraných
  kontraktů. Bere se nejnovější čas, ne nejstarší - nelikvidní opce se
  aktualizuje zřídka i při zdravém spojení, kdežto stojící maximum znamená,
  že nepřichází nic.

Při odezvě nad 500 ms se ukazatel zvýrazní žlutě; stojící kotace (nad 15 s)
se hlásí jen během seance - mimo obchodní hodiny trh nic neposílá a varování
by svítilo pořád. Bez spojení se ukazatel skrývá.

Mimo obchodní hodiny se v hlavičce ukazuje odpočet do nejbližšího otevření
burzy (`Otevření trhu za ...`). Časuje se v časové zóně burzy podle voleb
`trading.exchange_timezone`, `exchange_open_time` a `exchange_close_time`,
takže posun letního a zimního času vůči času počítače nehraje roli. Po
zavření a o víkendu míří odpočet na otevření následujícího obchodního dne -
u delších pauz proto vypisuje i počet dní. Svátky ani zkrácené obchodní dny
aplikace nezná. Během seance je odpočet skrytý a obchodování neovlivňuje.

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

Po vyplnění nákupu nabídne karta pozice tři řádky tlačítek, které se liší
jen množstvím - u pozice se třemi kontrakty a runnerem 1 ks vypadají takto:

```
BID (3 ks)  MID (3 ks)  ASK (3 ks)  ASK +1 %  +2 %  +3 %  +4 %  +5 %  +7 %  +9 %
BID (2 ks)  MID (2 ks)  ASK (2 ks)  ASK +1 %  +2 %  +3 %  +4 %  +5 %  +7 %  +9 %
BID (1 ks)  MID (1 ks)  ASK (1 ks)  ASK +1 %  +2 %  +3 %  +4 %  +5 %  +7 %  +9 %
```

Nabídka jde zleva doprava od nejjistějšího vyplnění k nejvyšší ceně: na BID
se prodá hned a zaplatí se celý spread, na MID se čeká na střed trhu, na ASK
se spread naopak inkasuje a přirážky míří ještě výš.

| Řádek | Barva | Co prodá |
| --- | --- | --- |
| první | červená | celou drženou pozici |
| druhý | oranžová | základní pozici, v trhu zůstane runner |
| třetí | tyrkysová | jediný kontrakt - pro odprodávání po kusech |

Všechny řádky mají stejně široké sloupce, takže tlačítka téhož druhu leží
přesně nad sebou; co které udělá, řekne bublina s nápovědou. Popisek začíná
cenou a končí částkou, se kterou příkaz skutečně půjde do trhu.

Řádek se nezobrazí, pokud by dělal totéž co jiný: u pozice o jednom kontraktu
zbude jen ten první a prodej po kusech se nenabízí ani tehdy, když základní
pozice vychází právě na jeden kus.

**Přirážky nad poptávkou** (`ASK +1 %` až `+5 %`, dál `+7 %` a `+9 %`) zadají
prodej nad ASK: za kontrakt přijde víc peněz, ale příkaz se vyplní s menší
pravděpodobností. Pozor u kontraktů s hrubým rastrem - je-li tik 0,05
a ASK 3,20, vyjde +1 % i +2 % na stejnou cenu 3,25. Skutečná cena je proto
vždy napsaná na tlačítku.

Nabídku přirážek určuje `ASK_MARKUPS` v `tws_rucne/models.py`; řádek se
sloupci přizpůsobí sám, žádné další místo se kvůli tomu nepřepisuje.

Základní pozice je držené množství snížené o runner (`trading.runner_quantity`,
ve výchozím nastavení 1 kontrakt). Po jejím prodeji zbývá v pozici jen runner
a zůstane jediný řádek, kterým se doprodá - přesně tak, jak má scale-out
fungovat.

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

- U pozice s nevyplněným **nákupem** k tomu slouží nákupní tlačítka na její
  kartě. Totéž udělá i tlačítko ve formuláři, pokud na daném tickeru a směru
  nevyplněný nákup čeká.
- U pozice s nevyplněným **prodejem** přecení příkaz kterékoliv prodejní
  tlačítko. Přejít lze i mezi řádky - z prodeje základní pozice na prodej
  všeho a zpět; příkaz se jen upraví, druhý nevzniká.
- Tlačítka přitom vypadají stejně jako při novém příkazu. Že jde o přecenění,
  řekne bublina s nápovědou a hláška pozice, která vypisuje, co v trhu leží.
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

## Přehled výsledků

Tlačítko **Výsledky** v hlavičce přehledu pozic otevře souhrn obchodního dne
přes celou obrazovku. Ukazuje na jednom místě, co se dnes obchodovalo, co se
ještě drží a s jakým výsledkem:

- **šest dlaždic** nahoře - výsledek dne, realizovaná část, otevřené pozice,
  úspěšnost, profit factor a počty pozic,
- **seznam držených pozic** s živým P/L, který tiká spolu se zbytkem aplikace,
- **seznam ukončených pozic** s nákupní a průměrnou prodejní cenou, dobou
  držení a pruhem, který obchody porovnává mezi sebou,
- **křivku průběhu dne** - kumulovaný realizovaný výsledek po provizích,
- **sloupcový graf podle tickeru**.

Přepínač **Dnes / Vše** rozhoduje o rozsahu: *Dnes* bere pozice založené
nebo ukončené dnešního dne a všechny dosud běžící - aplikace může běžet přes
noc a pozice otevřená před půlnocí a prodaná ráno patří do dnešního výsledku.
*Vše* ukazuje celý obsah přehledu bez ohledu na datum.

Přehled zabírá celou obrazovku a hlavičku aplikace tím zakryje, proto má
vedle přepínače rozsahu **vlastní tlačítko světlého a tmavého vzhledu** -
přepíná tentýž režim jako tlačítko v hlavičce, včetně barev obou grafů.

Hlavní hodnota je vždy ta **po provizích** - v závorce vedle ní stojí drobněji
tatáž částka bez nich. Provize se přitom dělí mezi prodanou a drženou část
pozice: držené kusy nesou jen poměrnou část nákupní provize, protože prodejní
u nich ještě nevznikla. O tom, jestli obchod skončil v zisku, proto rozhoduje
výsledek po provizích - těsný zisk umí provize otočit ve ztrátu a statistika
by jinak lhala.

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
| `trading.exchange_timezone`, `exchange_open_time`, `exchange_close_time` | hodiny burzy pro odpočet v hlavičce (obchodování neovlivňují) |
| `strike.mode`, `strike.otm_steps` | jak se vybírá strike |
| `expiration.mode`, `expiration.min_dte` | jak se vybírá expirace |
| `ui.port`, `ui.dark` | webové rozhraní |
| `ui.latency_interval_sec` | jak často se měří odezva TWS pro ukazatel v hlavičce (0 = vypnuto) |

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
| `tws_rucne/report.py` | výpočet souhrnu obchodního dne (bez vykreslování) |
| `tws_rucne/report_dialog.py` | popup s přehledem výsledků - dlaždice, seznamy, grafy |
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
- Zkrácené obchodní dny ani stav burzy aplikace nesleduje - odpočet do
  otevření trhu se řídí jen hodinami z konfigurace a svátky nezná.
