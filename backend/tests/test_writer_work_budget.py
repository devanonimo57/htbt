"""Deterministic real-writer work budgets (not elapsed-time assertions)."""
import asyncio
import copy
import json
import os
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch
import main
import test_checkout_webhook_integration as fixture


class WriterWorkBudget(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main._DB_LOCK = asyncio.Lock()
        main._STATE_COMMITTED = {}
        main._STATE_COMMITTED_OWNER = None
        await fixture.CheckoutWebhookIntegration.asyncSetUp(self)

    async def asyncTearDown(self):
        await fixture.CheckoutWebhookIntegration.asyncTearDown(self)

    async def seed(self, population):
        state = copy.deepcopy(await main._read_state_from_db())
        state['memory']['leads'] = [
            {'telegram_id': str(i+1), 'bot_id': 'flow-a', 'origin_bot_numeric_id': '111',
             'created_at': '2026-10-01T00:00:00', 'stage': 'Conversando',
             'history': [{'role': 'user', 'content': 'lead-'+str(i)}]}
            for i in range(population)]
        state['tx_records'] = {
            'pix-'+str(i): {'tx_id': 'pix-'+str(i), 'user_id': i+1, 'bot_id': 'flow-a',
                           'bot_token': self.bot.token, 'status': 'pending', 'amount': 10,
                           'note': 'pix-'+str(i)}
            for i in range(population)}
        await main._write_state_to_db(state)
        return await main._read_state_from_db()

    async def test_routine_save_has_bounded_unrelated_copy_and_no_population_serialization(self):
        measurements = []
        for population in (8, 64):
            live = await self.seed(population)
            live['memory']['leads'][0]['first_name'] = 'Changed'
            serializations = Counter()
            original_dumps = json.dumps
            def dumps(value, *args, **kwargs):
                def count(item):
                    if isinstance(item, dict):
                        if item.get('tx_id', '').startswith('pix-'):
                            serializations[item['tx_id']] += 1
                        if item.get('telegram_id'):
                            serializations['lead-'+str(int(item['telegram_id'])-1)] += 1
                        for child in item.values(): count(child)
                    elif isinstance(item, (list, tuple)):
                        for child in item: count(child)
                count(value)
                return original_dumps(value, *args, **kwargs)
            copies = Counter()
            original_copy_dict = copy._deepcopy_dispatch[dict]
            def copy_dict(value, memo):
                if value.get('tx_id', '').startswith('pix-'):
                    copies[value['tx_id']] += 1
                if value.get('telegram_id'):
                    copies['lead-'+str(int(value['telegram_id'])-1)] += 1
                return original_copy_dict(value, memo)
            with patch.dict(copy._deepcopy_dispatch, {dict: copy_dict}), patch.object(main.json, 'dumps', new=dumps):
                returned = await main._write_state_to_db(live, runtime_only=True)
            unrelated = {key: count for key, count in copies.items()
                         if key not in ('lead-0', 'pix-0')}
            measurements.append({'population': population, 'copies': dict(copies),
                                 'serializations': dict(serializations)})
            if os.environ.get('HOTBOT_REPORT'):
                Path(os.environ['HOTBOT_REPORT'], 'work-budget.json').write_text(
                    original_dumps(measurements, indent=2), encoding='utf8')
            # Check budgets after collecting both populations, including on RED.
            self.assertEqual(returned['memory']['leads'][0]['first_name'], 'Changed')
            self.assertIsNot(returned['memory']['leads'][1], live['memory']['leads'][1])
            self.assertIsNot(returned['tx_records']['pix-1'], live['tx_records']['pix-1'])
        for measurement in measurements:
            unrelated = {key: count for key, count in measurement['copies'].items()
                         if key not in ('lead-0', 'pix-0')}
            self.assertLessEqual(max(unrelated.values(), default=0), 1,
                                 'Unrelated rows may detach once for safe publication, not be recopied by every helper')
            self.assertFalse({key: count for key, count in measurement['serializations'].items() if key != 'lead-0'},
                             'Config/runtime merge must not serialize the population')

    async def test_event_admission_detaches_unrelated_rows_only_once(self):
        live = await self.seed(8)
        before = copy.deepcopy(live['memory']['leads'][0])
        after = copy.deepcopy(before)
        after['stage'] = 'Escolheu um plano'
        copies = Counter()
        original = copy._deepcopy_dispatch[dict]
        def copy_dict(value, memo):
            if value.get('telegram_id'):
                copies[str(value['telegram_id'])] += 1
            return original(value, memo)
        with patch.dict(copy._deepcopy_dispatch, {dict: copy_dict}):
            returned = await main._write_state_to_db(live, runtime_only=True, checkout_start={
                'lead': after, 'before': before, 'allow_create': False})
        self.assertEqual(returned['memory']['leads'][0]['stage'], 'Escolheu um plano')
        self.assertEqual(copies['2'], 1, 'Admission must not build then discard a full detached state')
        self.assertIsNot(returned['memory']['leads'][1], live['memory']['leads'][1])

    async def test_committed_ancestry_is_recursively_immutable_and_shared_only_as_ancestry(self):
        live = await self.seed(8)
        detached = copy.deepcopy(live)
        self.assertIs(detached._row_base, live._row_base)
        self.assertIs(main._STATE_COMMITTED, live._row_base)
        mutations = [
            lambda: live._row_base.update(learning={}),
            lambda: live._row_base['memory']['leads'].clear(),
            lambda: live._row_base['memory']['leads'][0].update(first_name='bad'),
            lambda: live._row_base['memory']['leads'][0]['history'].append({'content': 'bad'}),
            lambda: live._row_base['tx_records']['pix-0'].update(status='paid'),
            lambda: live._row_base['flow_bots'][0]['plans'].reverse(),
        ]
        for mutate in mutations:
            with self.assertRaises(TypeError): mutate()
        detached['memory']['leads'][0]['history'].append({'content': 'detached'})
        self.assertNotEqual(detached['memory']['leads'][0], live['memory']['leads'][0])
        self.assertNotEqual(detached['memory']['leads'][0], detached._row_base['memory']['leads'][0])
        editable = copy.deepcopy(live._row_base)
        editable['tx_records']['pix-0']['status'] = 'paid'
        self.assertEqual(live._row_base['tx_records']['pix-0']['status'], 'pending')

    async def test_unchanged_config_is_not_serialized_for_pix_delivery_progress(self):
        live = await self.seed(8)
        live['tx_records']['pix-0']['automatic_delivery_attempts'] = 1
        original_dumps = json.dumps
        def dumps(value, *args, **kwargs):
            if isinstance(value, dict) and 'flow_bots' in value:
                raise AssertionError('Unchanged configuration must not be serialized')
            return original_dumps(value, *args, **kwargs)
        with patch.object(main.json, 'dumps', new=dumps):
            await main._write_state_to_db(live, runtime_only=True)
        main._STATE_CACHE = None
        self.assertEqual((await main._read_state_from_db())['tx_records']['pix-0']['automatic_delivery_attempts'], 1)

    async def test_legacy_shallow_runtime_input_cannot_delete_live_rows_before_failed_commit(self):
        live = await self.seed(8)
        before = copy.deepcopy(live)
        with patch.object(main, '_persist_records', side_effect=RuntimeError('writer failure')):
            with self.assertRaisesRegex(RuntimeError, 'writer failure'):
                await main._write_state_to_db(dict(live), runtime_only=True,
                                               delete_client_keys={('111', '1', False)})
        self.assertIs(await main._read_state_from_db(), live)
        self.assertEqual(live, before)
        self.assertEqual(live._row_base, before._row_base)

    async def test_live_runtime_commit_failure_never_mutates_cache_or_ancestry(self):
        from sqlalchemy.ext.asyncio import AsyncSession
        live = await self.seed(8)
        live['tx_records']['pix-0']['status'] = 'paid'
        before = copy.deepcopy(live)
        ancestry = live._row_base
        with patch.object(AsyncSession, 'commit', side_effect=RuntimeError('writer failure')):
            with self.assertRaisesRegex(RuntimeError, 'writer failure'):
                await main._write_state_to_db(live, runtime_only=True)
        self.assertIs(await main._read_state_from_db(), live)
        self.assertEqual(live, before)
        self.assertIs(live._row_base, ancestry)
        self.assertEqual(ancestry['tx_records']['pix-0']['status'], 'pending')

    async def test_runtime_live_late_config_and_pix_changes_remain_dirty_after_commit(self):
        import test_checkout_late_commit as late_fixture
        live = await self.seed(8)
        live['memory']['leads'][0]['first_name'] = 'before commit'
        def mutate(state):
            state['tx_records']['pix-1']['access_status'] = 'late marker'
            state['flow_bots'][0]['plans'][0]['price'] = 77
            state['flow_bots'][0]['payments']['generated_medias'] = [
                {'id': 'fresh-media', 'url': 'https://example.invalid/new', 'type': 'photo'}]
        returned = await late_fixture.CheckoutLateCommit.held(self, live, {'runtime_only': True}, mutate)
        self.assertEqual(returned['flow_bots'][0]['plans'][0]['price'], 77)
        self.assertEqual(returned._row_base['flow_bots'][0]['plans'][0]['price'], 10)
        self.assertNotIn('access_status', returned._row_base['tx_records']['pix-1'])
        await main._write_state_to_db(returned, runtime_only=True)
        main._STATE_CACHE = None
        cold = await main._read_state_from_db()
        self.assertEqual(cold['flow_bots'][0]['plans'][0]['price'], 77)
        self.assertEqual(cold['flow_bots'][0]['payments']['generated_medias'][0]['id'], 'fresh-media')
        self.assertEqual(cold['tx_records']['pix-1']['access_status'], 'late marker')

    async def test_noop_guard_does_not_ignore_pending_deletion_intents(self):
        from models import ClientRecord, PixRecord
        live = await self.seed(8)
        # A late deletion has already changed the visible cache; its intent must
        # survive even a state with no other row/config modifications.
        live['memory']['leads'].pop(0)
        live['tx_records'].pop('pix-0')
        live._pending_client_deletes = frozenset({('111', '1', False)})
        live._pending_pix_deletes = frozenset({'pix-0'})
        returned = await main._write_state_to_db(live, runtime_only=True)
        async with main.async_session() as session:
            self.assertIsNone(await session.get(ClientRecord, ('111', '1', False)))
            self.assertIsNone(await session.get(PixRecord, 'pix-0'))
        self.assertNotIn('pix-0', returned._row_base['tx_records'])
        self.assertNotIn(('111', '1', False), main._client_map(returned._row_base))

    async def test_noop_guard_keeps_remarketing_claim_projection_on_safe_fallback(self):
        live = await self.seed(8)
        live['remarketing_campaigns'] = [{'id': 'writer-budget-campaign', 'deliveries': {},
                                         'status': 'Rodando', 'total_targets': 1}]
        live = await main._write_state_to_db(live)
        self.assertEqual(live, live._row_base)
        self.assertTrue(await main._claim_remarketing_recipient('writer-budget-campaign', '111:1'))
        await main._mark_remarketing_recipient('writer-budget-campaign', '111:1', 'sent')
        returned = await main._write_state_to_db(live, runtime_only=True)
        self.assertEqual(returned['remarketing_campaigns'][0]['deliveries'], {'111:1': 'sent'})
        self.assertEqual(returned['remarketing_campaigns'][0]['sent_count'], 1)

    async def test_true_noop_checkpoint_does_not_open_sql_or_replace_cache(self):
        live = await self.seed(8)
        with patch.object(main, 'async_session', side_effect=AssertionError('No-op must not open SQL')):
            returned = await main._write_state_to_db(live, runtime_only=True)
        self.assertIs(returned, live)
