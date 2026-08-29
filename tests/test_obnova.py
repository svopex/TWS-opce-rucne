"""Testy ukládání stavu a obnovy pozic po restartu aplikace."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from tests.fake_ib import OPTION_CONID
from tests.zaklad import ZakladSeStavem
from tws_rucne import store
from tws_rucne.engine import ManualEngine
from tws_rucne.models import SELL_SCOPE_ALL, SELL_SCOPE_BASE, Position, PositionState


class TestUlozeniStavu(ZakladSeStavem):
    """Uložený stav musí nést vše, co po restartu nelze dopočítat."""

    def test_prevod_pozice_tam_a_zpet(self):
        puvodni = Position(
            id="AAPL-1",
            symbol="AAPL",
            right="P",
            quantity=4,
            expiration="20260918",
            strike=227.5,
            option_conid=OPTION_CONID,
            min_tick=0.05,
            runner_quantity=2,
            buy_kind="mid",
            buy_limit=3.10,
            filled_quantity=4,
            fill_price=3.15,
            sold_quantity=2,
            sold_value=7.4,
            sell_seq=1,
            buy_commissions={"EXEC-1": 2.0},
            sell_commissions={"EXEC-2": 1.5},
            state=PositionState.OPEN,
        )
        obnovena = store.dict_to_position(store.position_to_dict(puvodni))
        self.assertEqual(obnovena.id, puvodni.id)
        self.assertEqual(obnovena.right, "P")
        self.assertEqual(obnovena.strike, 227.5)
        self.assertEqual(obnovena.runner_quantity, 2)
        self.assertEqual(obnovena.open_quantity, 2)
        self.assertAlmostEqual(obnovena.sold_value, 7.4)
        self.assertEqual(obnovena.buy_commissions, {"EXEC-1": 2.0})
        self.assertEqual(obnovena.sell_commissions, {"EXEC-2": 1.5})
        self.assertEqual(obnovena.state, PositionState.OPEN)

    async def test_nakup_se_zapise_do_souboru(self):
        position = await self.nakup_vyplnen(quantity=3)
        ulozene = store.load(self.cfg.state.file)
        self.assertEqual(len(ulozene), 1)
        self.assertEqual(ulozene[0].id, position.id)
        self.assertEqual(ulozene[0].filled_quantity, 3)

    async def test_tik_bez_zmeny_na_disk_nesaha(self):
        # Pohyb kotací není změna uloženého stavu - ten se přepisuje jen
        # při skutečné události, ne při každém průchodu smyčkou
        await self.nakup_vyplnen(quantity=3)
        with mock.patch.object(store, "save", wraps=store.save) as zapis:
            for _ in range(5):
                self.ib.price_bid = (self.ib.price_bid or 3.0) + 0.05
                await self.tik()
        self.assertEqual(zapis.call_count, 0)

    async def test_preceneni_beze_zmeny_ulozi_hlasku(self):
        # Do TWS se nic neposílá, hláška a čas změny se ale přepsaly
        position = await self.nakup()
        await self.engine.reprice_buy(position.id, "ask")
        self.assertIn("beze změny", position.message)
        ulozene = store.load(self.cfg.state.file)
        self.assertEqual(ulozene[0].message, position.message)


class TestPoskozenyUlozenyStav(ZakladSeStavem):
    """Poškozený soubor nesmí shodit start aplikace."""

    def test_json_bez_slovniku_se_ignoruje(self):
        for obsah in ("[]", "null", '"x"', "12"):
            Path(self.cfg.state.file).write_text(obsah, encoding="utf-8")
            with self.assertLogs("tws_rucne.store", "WARNING"):
                self.assertEqual(store.load(self.cfg.state.file), [])

    def test_nectitelny_soubor_se_ignoruje(self):
        Path(self.cfg.state.file).write_text("{ tohle není JSON", encoding="utf-8")
        with self.assertLogs("tws_rucne.store", "ERROR"):
            self.assertEqual(store.load(self.cfg.state.file), [])

    def test_po_neuspesnem_zapisu_nezustane_docasny_soubor(self):
        adresar = Path(self.cfg.state.file).parent
        pozice = Position(id="AAPL-1")
        # Neserializovatelná hláška shodí json.dump uprostřed zápisu
        pozice.message = object()
        with self.assertLogs("tws_rucne.store", "ERROR"):
            store.save([pozice], self.cfg.state.file)
        self.assertEqual(list(adresar.iterdir()), [])


class TestObnovaPoRestartu(ZakladSeStavem):
    """Nový engine přebírá pozice z uloženého stavu a ověřuje je v TWS."""

    async def _restartuj(self) -> ManualEngine:
        """Založí nový engine nad stejnou náhradou TWS a spustí obnovu."""
        novy = ManualEngine(self.cfg, self.ib)
        await novy.restore()
        return novy

    async def test_otevrena_pozice_se_obnovi_i_s_prikazem(self):
        position = await self.nakup_vyplnen(quantity=3)
        self.ib.held_positions = {OPTION_CONID: 3}

        novy = await self._restartuj()
        obnovena = novy.position(position.id)
        self.assertEqual(obnovena.state, PositionState.OPEN)
        self.assertEqual(obnovena.open_quantity, 3)
        # Kontrakty i odběry dat musí být po obnově znovu k dispozici
        self.assertIsNotNone(obnovena.option_contract)
        self.assertTrue(obnovena.subscribed)
        self.assertTrue(obnovena.can_sell_base)

    async def test_bezici_nakup_bez_prikazu_a_bez_pozice_konci_zrusenim(self):
        position = await self.nakup(quantity=3)
        # Příkaz v TWS už není a na účtu nic neleží
        self.ib.placed.clear()
        self.ib.held_positions = {}

        novy = await self._restartuj()
        obnovena = novy.position(position.id)
        self.assertEqual(obnovena.state, PositionState.CANCELLED)

    async def test_bezici_nakup_bez_prikazu_ale_s_pozici_se_povazuje_za_nakoupeny(self):
        position = await self.nakup(quantity=3)
        self.ib.placed.clear()
        self.ib.held_positions = {OPTION_CONID: 3}

        novy = await self._restartuj()
        obnovena = novy.position(position.id)
        self.assertEqual(obnovena.state, PositionState.OPEN)
        self.assertEqual(obnovena.open_quantity, 3)
        # Cenu ani přesné množství se z TWS nedozvíme, musí je ověřit obchodník
        self.assertIn("Ověřte množství i cenu", obnovena.message)

    async def test_nakup_bez_prikazu_prebira_mnozstvi_z_uctu(self):
        position = await self.nakup(quantity=5)
        # Příkaz z TWS zmizel a vyplnily se jen dva z pěti kusů
        self.ib.placed.clear()
        self.ib.held_positions = {OPTION_CONID: 2}

        novy = await self._restartuj()
        obnovena = novy.position(position.id)
        self.assertEqual(obnovena.state, PositionState.OPEN)
        self.assertEqual(obnovena.filled_quantity, 2)
        self.assertEqual(obnovena.open_quantity, 2)
        # Nic se neprodalo, evidence proto nesmí vykázat prodej ani výsledek
        self.assertEqual(obnovena.sold_quantity, 0)
        self.assertIsNone(obnovena.realized_pnl)

    async def test_mensi_pozice_v_tws_se_povazuje_za_castecne_prodanou(self):
        position = await self.nakup_vyplnen(quantity=3)
        # Mimo aplikaci se jeden kontrakt prodal
        self.ib.held_positions = {OPTION_CONID: 2}

        novy = await self._restartuj()
        obnovena = novy.position(position.id)
        self.assertEqual(obnovena.open_quantity, 2)
        self.assertEqual(obnovena.sold_quantity, 1)
        self.assertIn("POZOR", obnovena.message)

    async def test_prazdna_pozice_v_tws_se_uzavre(self):
        await self.nakup_vyplnen(quantity=2)
        self.ib.held_positions = {}

        novy = await self._restartuj()
        obnovena = list(novy.positions.values())[0]
        self.assertEqual(obnovena.state, PositionState.CLOSED)
        self.assertEqual(obnovena.open_quantity, 0)

    async def test_bezici_prodej_pokracuje_podle_prikazu_v_tws(self):
        position = await self.nakup_vyplnen(quantity=3)
        await self.engine.sell(position.id, "bid", SELL_SCOPE_BASE)
        self.ib.held_positions = {OPTION_CONID: 3}

        novy = await self._restartuj()
        obnovena = novy.position(position.id)
        self.assertEqual(obnovena.state, PositionState.SELLING)
        self.assertIsNotNone(obnovena.sell_trade)

        # Prodej se v TWS vyplní a smyčka jej dopočítá
        novy._synced = True
        self.ib.fill(obnovena.sell_trade, 2, 3.60)
        await novy._tick()
        self.assertEqual(obnovena.state, PositionState.OPEN)
        self.assertEqual(obnovena.open_quantity, 1)

    async def test_cizi_opcni_pozice_se_hlasi_jako_neridena(self):
        # Na účtu leží opce, ke které aplikace nemá záznam
        self.ib.held_positions = {999999: 5}
        novy = await self._restartuj()
        self.assertIn(999999, novy.unmanaged)

    async def test_selhani_obnovy_nechava_sezeni_nesrovnane(self):
        # Výjimka během obnovy nesmí sezení natrvalo označit za srovnané,
        # jinak by se držené množství s TWS už nikdy neporovnalo
        await self.nakup_vyplnen(quantity=3)

        async def rozbite_app_trades():
            raise RuntimeError("TWS neodpovídá")

        self.engine._synced = False
        self.ib.app_trades = rozbite_app_trades
        with self.assertRaises(RuntimeError):
            await self.engine.restore()
        self.assertFalse(self.engine._synced)

        # Jakmile TWS odpovídá, srovnání proběhne při dalším pokusu
        self.ib.app_trades = type(self.ib).app_trades.__get__(self.ib)
        await self.engine.restore()
        self.assertTrue(self.engine._synced)

    async def test_smycka_opakuje_neuspesne_srovnani(self):
        # Neúspěch se hlásí jednou, pokus se ale opakuje
        pokusy = {"n": 0}

        async def rozbite_app_trades():
            pokusy["n"] += 1
            raise RuntimeError("TWS neodpovídá")

        self.cfg.connection.reconnect_delay_sec = 0.0
        self.engine._synced = False
        self.ib.app_trades = rozbite_app_trades
        with self.assertLogs("tws_rucne.engine", "ERROR"):
            for _ in range(3):
                await self.tik()

        self.assertFalse(self.engine._synced)
        self.assertEqual(pokusy["n"], 3)
        hlasky = [t for _, t in self.engine.events if "srovnat s TWS" in t]
        self.assertEqual(len(hlasky), 1)

    async def test_nahled_po_obnove_spojeni_znovu_odebira_data(self):
        # Bez obnovy odběru by náhled zůstal bez kotací a nákup by nešel zadat
        nahled = await self.engine.prepare("AAPL", "C")
        self.assertTrue(nahled.owns_subscription)
        odebirano = dict(self.ib.subscribed)

        # Výpadek spojení odběry z minulého spojení zahodí
        self.ib.subscribed.clear()
        self.engine._synced = False
        await self.engine.restore()
        self.assertEqual(self.ib.subscribed, odebirano)

        # Opakovaná obnova počítadlo odběratelů nenafoukne
        self.engine._synced = False
        await self.engine.restore()
        self.assertEqual(self.ib.subscribed, odebirano)
        self.engine.release_preview()
        self.assertEqual(self.ib.subscribed, {})

    async def test_dalsi_pozice_navazuje_cislovanim(self):
        await self.nakup_vyplnen(quantity=1)
        self.ib.held_positions = {OPTION_CONID: 1}

        novy = await self._restartuj()
        novy._synced = True
        dalsi = await novy.buy("AAPL", 1, "C", "ask")
        # Identifikátor nesmí kolidovat s obnovenou pozicí
        self.assertEqual(dalsi.id, "AAPL-2")


class TestObnovaViceZapisuNaJednomKontraktu(ZakladSeStavem):
    """
    Na jednom opčním kontraktu může běžet víc pozic, TWS ale hlásí jediný
    součet. Obnova je proto musí srovnávat dohromady, ne každou zvlášť.
    """

    async def _restartuj(self) -> ManualEngine:
        """Založí nový engine nad stejnou náhradou TWS a spustí obnovu."""
        novy = ManualEngine(self.cfg, self.ib)
        await novy.restore()
        return novy

    async def _dve_pozice(self, prvni: int, druha: int):
        """Nakoupí dvě pozice na tomtéž kontraktu."""
        a = await self.nakup_vyplnen(quantity=prvni)
        b = await self.nakup_vyplnen(quantity=druha)
        return a, b

    async def test_soucet_sedi_a_pozice_zustavaji_beze_zmeny(self):
        # Pět a jeden kus na téže opci: TWS hlásí šest, obojí je v pořádku
        pet, jeden = await self._dve_pozice(5, 1)
        self.ib.held_positions = {OPTION_CONID: 6}

        novy = await self._restartuj()
        self.assertEqual(novy.position(pet.id).open_quantity, 5)
        self.assertEqual(novy.position(jeden.id).open_quantity, 1)
        # Žádná z pozic nesmí hlásit rozdíl proti TWS
        for position in novy.positions.values():
            self.assertNotIn("POZOR", position.message)

    async def test_chybejici_kusy_se_odepisou_od_nejnovejsi_pozice(self):
        pet, jeden = await self._dve_pozice(5, 1)
        # Mimo aplikaci se prodaly dva kusy
        self.ib.held_positions = {OPTION_CONID: 4}

        novy = await self._restartuj()
        # Nejnovější pozice odešla celá, zbytek ubral starší
        self.assertEqual(novy.position(jeden.id).open_quantity, 0)
        self.assertEqual(novy.position(jeden.id).state, PositionState.CLOSED)
        self.assertEqual(novy.position(pet.id).open_quantity, 4)

    async def test_prebyvajici_kusy_pripadnou_nejnovejsi_pozici(self):
        pet, jeden = await self._dve_pozice(5, 1)
        # Mimo aplikaci se dokoupily dva kusy
        self.ib.held_positions = {OPTION_CONID: 8}

        novy = await self._restartuj()
        self.assertEqual(novy.position(pet.id).open_quantity, 5)
        self.assertEqual(novy.position(jeden.id).open_quantity, 3)
        self.assertIn("POZOR", novy.position(jeden.id).message)

    async def test_prebytek_se_nepripisuje_pozici_s_bezicim_nakupem(self):
        drzena = await self.nakup_vyplnen(quantity=2)
        nakupovana = await self.nakup(quantity=3)
        # TWS stihl nákup vyplnit dřív, než se o tom aplikace dozvěděla
        self.ib.held_positions = {OPTION_CONID: 5}

        novy = await self._restartuj()
        # Kusy patří běžícímu příkazu, držená pozice si je nárokovat nesmí
        self.assertEqual(novy.position(drzena.id).open_quantity, 2)
        self.assertEqual(novy.position(nakupovana.id).filled_quantity, 0)
        self.assertIn("nákupní příkaz", self._zpravy(novy))

        # Jakmile TWS vyplnění ohlásí, převezme je pozice s příkazem - a jen ona
        novy._synced = True
        self.ib.fill(novy.position(nakupovana.id).buy_trade, 3, 3.20)
        await novy._tick()
        self.assertEqual(novy.position(nakupovana.id).open_quantity, 3)
        self.assertEqual(novy.position(drzena.id).open_quantity, 2)

    async def test_neodepsatelny_zbytek_schodku_se_hlasi(self):
        drzena = await self.nakup_vyplnen(quantity=1)
        castecna = await self.nakup(quantity=5)
        # Nákup je vyplněný jen zčásti, příkaz ale v trhu zůstává
        self.ib.fill(castecna.buy_trade, 3, 3.20, status="Submitted")
        await self.tik()
        self.assertEqual(castecna.filled_quantity, 3)

        # Mimo aplikaci se všechny kontrakty prodaly
        self.ib.held_positions = {}
        novy = await self._restartuj()

        # Odepsat lze jen z držené pozice; zbytek si nárokuje příkaz v trhu
        self.assertEqual(novy.position(drzena.id).open_quantity, 0)
        self.assertIn("chybí ještě 3 ks", self._zpravy(novy))

    def _zpravy(self, engine: ManualEngine) -> str:
        """Zaznamenané události enginu v jednom řetězci - pro hledání hlášek."""
        return " ".join(zprava for _, zprava in engine.events)

    async def test_soucet_odpovida_skutecnosti_i_po_srovnani(self):
        await self._dve_pozice(5, 1)
        self.ib.held_positions = {OPTION_CONID: 3}

        novy = await self._restartuj()
        drzeno = sum(p.open_quantity for p in novy.positions.values())
        self.assertEqual(drzeno, 3)


class TestMigraceStarychProvizi(ZakladSeStavem):
    """
    Starší zápis vedl provize v jediném slovníku bez rozlišení druhu.
    Po načtení skončí mezi nákupními a TWS je pošle znovu i s druhem -
    přerozdělení proto nesmí žádnou z nich započítat podruhé.
    """

    async def test_prodejni_provize_se_po_migraci_nezapocita_dvakrat(self):
        position = await self.nakup_vyplnen(quantity=2, cena=3.00)
        self.ib.record_commission(position.buy_trade, 2.0)
        await self.tik()

        await self.engine.sell(position.id, "bid", SELL_SCOPE_ALL)
        self.ib.fill(position.sell_trade, 2, 3.50, commission=1.5)
        await self.tik()
        self.assertAlmostEqual(position.commission_total, 3.5)

        # Uložený stav se přepíše do staršího tvaru - obě provize pohromadě
        cesta = Path(self.cfg.state.file)
        obsah = json.loads(cesta.read_text(encoding="utf-8"))
        zaznam = obsah["positions"][0]
        zaznam["commissions"] = {
            **zaznam.pop("buy_commissions"),
            **zaznam.pop("sell_commissions"),
        }
        cesta.write_text(json.dumps(obsah), encoding="utf-8")

        novy = ManualEngine(self.cfg, self.ib)
        await novy.restore()
        obnovena = novy.position(position.id)
        self.assertAlmostEqual(obnovena.commission_total, 3.5)

        # Průchod smyčkou provize jen přerozdělí podle druhu příkazu
        novy._synced = True
        await novy._tick()
        self.assertAlmostEqual(obnovena.buy_commission, 2.0)
        self.assertAlmostEqual(obnovena.sell_commission, 1.5)
        self.assertAlmostEqual(obnovena.commission_total, 3.5)
