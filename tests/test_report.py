"""Testy souhrnu obchodního dne - dlaždice, křivka a součty po tickerech."""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tws_rucne import report
from tws_rucne.models import Position, PositionState
from tws_rucne.report_dialog import procenta, procenta_s_provizi


def pozice(
    id: str = "AAPL-1",
    symbol: str = "AAPL",
    filled: int = 2,
    fill_price: float = 3.00,
    sold: int = 0,
    sold_value: float = 0.0,
    state: PositionState = PositionState.OPEN,
    buy_provize: float = 0.0,
    sell_provize: float = 0.0,
    den: date | None = None,
    minuta: int = 0,
) -> Position:
    """Pozice pro testy souhrnu - časy se odvozují od zadaného dne."""
    zaklad = datetime.combine(den or date.today(), datetime.min.time())
    vznik = zaklad + timedelta(hours=15, minutes=minuta)
    position = Position(
        id=id,
        symbol=symbol,
        right="C",
        quantity=filled or 1,
        expiration="20260918",
        strike=230.0,
        filled_quantity=filled,
        fill_price=fill_price if filled else None,
        sold_quantity=sold,
        sold_value=sold_value,
        state=state,
        created_at=vznik,
        updated_at=vznik + timedelta(minutes=5),
    )
    if filled:
        position.fill_time = vznik + timedelta(seconds=30)
    if buy_provize:
        position.buy_commissions = {f"{id}-B": buy_provize}
    if sell_provize:
        position.sell_commissions = {f"{id}-S": sell_provize}
    # Držené kusy se oceňují středem trhu
    position.option_bid, position.option_ask = 3.40, 3.60
    return position


class TestVyberRozsahu(unittest.TestCase):
    """Rozsah rozhoduje, co se do přehledu dostane."""

    def test_dnes_bere_dnesni_a_vsechny_bezici(self):
        vcerejsi = pozice(id="A-1", state=PositionState.CLOSED, den=date(2020, 1, 1))
        stara_bezici = pozice(id="A-2", state=PositionState.OPEN, den=date(2020, 1, 1))
        dnesni = pozice(id="A-3", state=PositionState.CLOSED)

        vybrane = report.vyber([vcerejsi, stara_bezici, dnesni], report.ROZSAH_DNES)
        self.assertEqual({p.id for p in vybrane}, {"A-2", "A-3"})

    def test_dnes_bere_i_pozici_zalozenou_vcera_a_ukoncenou_dnes(self):
        # Aplikace běžela přes noc: pozice vznikla včera, prodala se ráno
        prespolni = pozice(
            id="A-1",
            sold=2,
            sold_value=7.0,
            state=PositionState.CLOSED,
            den=date.today() - timedelta(days=1),
        )
        prespolni.updated_at = datetime.now()

        vybrane = report.vyber([prespolni], report.ROZSAH_DNES)
        self.assertEqual([p.id for p in vybrane], ["A-1"])

    def test_vse_bere_uplne_vsechno(self):
        stare = pozice(id="A-1", state=PositionState.CLOSED, den=date(2020, 1, 1))
        vybrane = report.vyber([stare], report.ROZSAH_VSE)
        self.assertEqual(len(vybrane), 1)


class TestSouhrn(unittest.TestCase):
    """Souhrnná čísla nad přehledem."""

    def test_realizovany_i_otevreny_vysledek(self):
        # Uzavřená pozice: 2 ks nakoupené za 3,00 a prodané za 3,50 = +100
        uzavrena = pozice(
            id="A-1", filled=2, sold=2, sold_value=7.0, state=PositionState.CLOSED
        )
        # Otevřená pozice: 2 ks za 3,00, střed trhu 3,50 = +100
        otevrena = pozice(id="A-2", filled=2)

        podklad = report.sestav([uzavrena, otevrena])
        s = podklad.souhrn
        self.assertAlmostEqual(s.realizovano, 100.0)
        self.assertAlmostEqual(s.otevreno, 100.0)
        self.assertAlmostEqual(s.celkem, 200.0)
        self.assertEqual(s.otevrenych_pozic, 1)
        self.assertEqual(s.otevrenych_kusu, 2)

    def test_provize_se_deli_mezi_realizovanou_a_otevrenou_cast(self):
        # Ze čtyř kusů jsou dva prodané: nákupní provize se dělí napůl
        castecne = pozice(
            id="A-1", filled=4, sold=2, sold_value=7.0, buy_provize=4.0, sell_provize=2.0
        )
        s = report.sestav([castecne]).souhrn
        self.assertAlmostEqual(s.provize_otevrene, 2.0)
        self.assertAlmostEqual(s.provize_realizovane, 4.0)
        self.assertAlmostEqual(s.provize, 6.0)

    def test_uspesnost_pocita_jen_ukoncene_pozice(self):
        zisk = pozice(id="A-1", filled=1, sold=1, sold_value=4.0, state=PositionState.CLOSED)
        ztrata = pozice(id="A-2", filled=1, sold=1, sold_value=2.0, state=PositionState.CLOSED)
        bezici = pozice(id="A-3", filled=2)

        s = report.sestav([zisk, ztrata, bezici]).souhrn
        self.assertEqual(s.ziskovych, 1)
        self.assertEqual(s.ztratovych, 1)
        self.assertAlmostEqual(s.uspesnost, 50.0)
        self.assertEqual(s.uzavrenych_s_vysledkem, 2)

    def test_o_zisku_rozhoduje_vysledek_po_provizich(self):
        # Hrubý zisk 5 USD, provize 8 USD - obchod je ve skutečnosti ztrátový
        tesny = pozice(
            id="A-1",
            filled=1,
            fill_price=3.00,
            sold=1,
            sold_value=3.05,
            state=PositionState.CLOSED,
            buy_provize=4.0,
            sell_provize=4.0,
        )
        s = report.sestav([tesny]).souhrn
        self.assertEqual(s.ziskovych, 0)
        self.assertEqual(s.ztratovych, 1)
        self.assertAlmostEqual(s.hruba_ztrata, 3.0)

    def test_zrusena_pozice_bez_nakupu_nema_vysledek(self):
        zrusena = pozice(id="A-1", filled=0, state=PositionState.CANCELLED)
        s = report.sestav([zrusena]).souhrn
        self.assertEqual(s.bez_obchodu, 1)
        self.assertEqual(s.uzavrenych_s_vysledkem, 0)
        self.assertIsNone(s.uspesnost)

    def test_profit_factor_bez_ztraty_neexistuje(self):
        zisk = pozice(id="A-1", filled=1, sold=1, sold_value=4.0, state=PositionState.CLOSED)
        s = report.sestav([zisk]).souhrn
        self.assertIsNone(s.profit_factor)

    def test_nejlepsi_a_nejhorsi_obchod(self):
        dobry = pozice(
            id="A-1", symbol="AAPL", filled=1, sold=1, sold_value=5.0,
            state=PositionState.CLOSED,
        )
        spatny = pozice(
            id="B-1", symbol="MSFT", filled=1, sold=1, sold_value=1.0,
            state=PositionState.CLOSED,
        )
        s = report.sestav([dobry, spatny]).souhrn
        self.assertEqual(s.nejlepsi[0], "AAPL")
        self.assertEqual(s.nejhorsi[0], "MSFT")


class TestUkoncenaPoziceSDrzenymiKusy(unittest.TestCase):
    """
    Pozice ukončená chybou nebo zrušením může kontrakty držet dál. Její
    otevřená část z přehledu zmizet nesmí a mezi hotové obchody nepatří.
    """

    def test_drzene_kusy_zustavaji_v_souhrnu(self):
        # 2 ks nakoupené za 3,00, střed trhu 3,50, nic prodáno
        chybna = pozice(id="A-1", filled=2, state=PositionState.ERROR, buy_provize=6.0)

        s = report.sestav([chybna]).souhrn
        self.assertAlmostEqual(s.otevreno, 100.0)
        self.assertEqual(s.otevrenych_pozic, 1)
        self.assertEqual(s.otevrenych_kusu, 2)
        # Provize patří celá držené části - prodejní zatím nevznikla
        self.assertAlmostEqual(s.provize_otevrene, 6.0)
        self.assertAlmostEqual(s.provize_realizovane, 0.0)
        # Výsledek se s trhem dál mění, do statistik proto nevstupuje
        self.assertEqual(s.uzavrenych_s_vysledkem, 0)
        self.assertEqual(s.ztratovych, 0)
        self.assertIsNone(s.profit_factor)

    def test_souhrn_a_pozice_ukazuji_totez_cislo(self):
        # 3 ks za 3,00, jeden prodán za 4,00, zbytek se drží při středu 3,50
        chybna = pozice(
            id="A-1",
            filled=3,
            sold=1,
            sold_value=4.0,
            state=PositionState.ERROR,
            buy_provize=3.0,
            sell_provize=1.0,
        )
        podklad = report.sestav([chybna])
        s = podklad.souhrn
        # Dlaždice musí sedět s tím, co u pozice ukazuje seznam i křivka
        self.assertAlmostEqual(s.realizovano_s_provizi, chybna.realized_pnl_net)
        self.assertAlmostEqual(s.otevreno_s_provizi, chybna.open_pnl_net)
        self.assertAlmostEqual(podklad.krivka[-1][1], chybna.realized_pnl_net)

    def test_ticker_zna_i_drzenou_cast_ukoncene_pozice(self):
        chybna = pozice(id="A-1", filled=2, state=PositionState.ERROR)
        polozky = report.sestav([chybna]).podle_tickeru
        self.assertAlmostEqual(polozky[0].otevreno, 100.0)


class TestKrivka(unittest.TestCase):
    """Křivka průběhu dne kumuluje realizovaný výsledek po provizích."""

    def test_body_v_poradi_ukonceni(self):
        prvni = pozice(
            id="A-1", filled=1, sold=1, sold_value=4.0,
            state=PositionState.CLOSED, minuta=0,
        )
        druha = pozice(
            id="A-2", filled=1, sold=1, sold_value=2.0,
            state=PositionState.CLOSED, minuta=30,
        )
        krivka = report.sestav([druha, prvni]).krivka
        self.assertEqual(len(krivka), 2)
        # +100 a pak -100 zpět na nulu
        self.assertAlmostEqual(krivka[0][1], 100.0)
        self.assertAlmostEqual(krivka[1][1], 0.0)

    def test_pozice_bez_nakupu_se_do_krivky_nedostane(self):
        zrusena = pozice(id="A-1", filled=0, state=PositionState.CANCELLED)
        self.assertEqual(report.sestav([zrusena]).krivka, [])


class TestPodleTickeru(unittest.TestCase):
    """Součty po tickerech se řadí od nejlepšího po nejhorší."""

    def test_soucty_a_poradi(self):
        aapl = pozice(
            id="A-1", symbol="AAPL", filled=1, sold=1, sold_value=4.0,
            state=PositionState.CLOSED,
        )
        msft = pozice(
            id="B-1", symbol="MSFT", filled=1, sold=1, sold_value=2.0,
            state=PositionState.CLOSED,
        )
        polozky = report.sestav([msft, aapl]).podle_tickeru
        self.assertEqual([p.symbol for p in polozky], ["AAPL", "MSFT"])
        self.assertAlmostEqual(polozky[0].celkem_s_provizi, 100.0)
        self.assertAlmostEqual(polozky[1].celkem_s_provizi, -100.0)

    def test_ticker_secte_vic_pozic(self):
        prvni = pozice(
            id="A-1", filled=1, sold=1, sold_value=4.0, state=PositionState.CLOSED
        )
        druha = pozice(
            id="A-2", filled=1, sold=1, sold_value=3.5, state=PositionState.CLOSED
        )
        polozky = report.sestav([prvni, druha]).podle_tickeru
        self.assertEqual(len(polozky), 1)
        self.assertAlmostEqual(polozky[0].realizovano, 150.0)


class TestProcentaUctu(unittest.TestCase):
    """Výsledek dne přepočtený na procenta velikosti účtu."""

    def test_procenta_z_velikosti_uctu(self):
        # 2 ks nakoupené za 3,00 a prodané za 3,50 = +100 USD na účtu 10 000
        uzavrena = pozice(
            id="A-1", filled=2, sold=2, sold_value=7.0, state=PositionState.CLOSED
        )
        s = report.sestav([uzavrena], account_size=10000.0).souhrn
        self.assertAlmostEqual(s.account_size, 10000.0)
        self.assertAlmostEqual(s.procento_uctu(s.celkem_s_provizi), 1.0)

    def test_ztrata_vychazi_zaporne(self):
        ztratova = pozice(
            id="A-1", filled=2, sold=2, sold_value=5.0, state=PositionState.CLOSED
        )
        s = report.sestav([ztratova], account_size=10000.0).souhrn
        self.assertAlmostEqual(s.procento_uctu(s.celkem_s_provizi), -1.0)

    def test_bez_znameho_uctu_neni_z_ceho_pocitat(self):
        # Velikost účtu z TWS zatím nedorazila - procenta se nesmí odhadovat
        uzavrena = pozice(
            id="A-1", filled=2, sold=2, sold_value=7.0, state=PositionState.CLOSED
        )
        s = report.sestav([uzavrena]).souhrn
        self.assertEqual(s.account_size, 0.0)
        self.assertIsNone(s.procento_uctu(s.celkem_s_provizi))

    def test_procenta_pocitaji_i_provize(self):
        # Zisk 100 USD snížený o provize 10 USD je na účtu 10 000 přesně 0,9 %
        uzavrena = pozice(
            id="A-1",
            filled=2,
            sold=2,
            sold_value=7.0,
            state=PositionState.CLOSED,
            buy_provize=6.0,
            sell_provize=4.0,
        )
        s = report.sestav([uzavrena], account_size=10000.0).souhrn
        self.assertAlmostEqual(s.procento_uctu(s.celkem_s_provizi), 0.9)
        self.assertAlmostEqual(s.procento_uctu(s.celkem), 1.0)


class TestFormatuProcent(unittest.TestCase):
    """Zápis procent z účtu v dlaždici."""

    def test_procenta_maji_znamenko_i_jednotku(self):
        self.assertEqual(procenta(0.62), "+0.62 %")
        self.assertEqual(procenta(-1.5), "-1.50 %")
        self.assertEqual(procenta(None), "-")

    def test_procenta_bez_provizi_stoji_v_zavorce(self):
        self.assertEqual(procenta_s_provizi(0.62, 0.68), "(+0.68 %)")
        # Shodná procenta se neopakují dvakrát
        self.assertEqual(procenta_s_provizi(0.62, 0.62), "")
        self.assertEqual(procenta_s_provizi(None, 0.68), "")


class TestRozdeleniPozic(unittest.TestCase):
    """Přehled dělí pozice na běžící a ukončené."""

    def test_bezici_s_nakupem_jsou_nahore(self):
        bez_nakupu = pozice(id="A-2", filled=0, state=PositionState.BUYING)
        s_nakupem = pozice(id="A-1", filled=2, state=PositionState.OPEN)
        podklad = report.sestav([bez_nakupu, s_nakupem])
        self.assertEqual([p.id for p in podklad.bezici], ["A-1", "A-2"])

    def test_nevyplneny_prikaz_patri_dolu_i_s_limitni_cenou(self):
        # Nevyplněný nákup si nese cenu limitu, nakoupeno ale nemá nic
        ceka = pozice(id="B-1", symbol="BBBB", filled=0, state=PositionState.BUYING)
        ceka.fill_price = 3.10
        drzi = pozice(id="Z-1", symbol="ZZZZ", filled=2, state=PositionState.OPEN)

        podklad = report.sestav([ceka, drzi])
        self.assertEqual([p.id for p in podklad.bezici], ["Z-1", "B-1"])

    def test_ukoncene_od_nejnovejsi(self):
        starsi = pozice(id="A-1", state=PositionState.CLOSED, minuta=0)
        novejsi = pozice(id="A-2", state=PositionState.CLOSED, minuta=30)
        podklad = report.sestav([starsi, novejsi])
        self.assertEqual([p.id for p in podklad.ukoncene], ["A-2", "A-1"])


if __name__ == "__main__":
    unittest.main()
