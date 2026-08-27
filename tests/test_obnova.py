"""Testy ukládání stavu a obnovy pozic po restartu aplikace."""

from __future__ import annotations

from tests.fake_ib import OPTION_CONID
from tests.zaklad import ZakladSeStavem
from tws_rucne import store
from tws_rucne.engine import ManualEngine
from tws_rucne.models import SELL_SCOPE_BASE, Position, PositionState


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

    async def test_soucet_odpovida_skutecnosti_i_po_srovnani(self):
        await self._dve_pozice(5, 1)
        self.ib.held_positions = {OPTION_CONID: 3}

        novy = await self._restartuj()
        drzeno = sum(p.open_quantity for p in novy.positions.values())
        self.assertEqual(drzeno, 3)
