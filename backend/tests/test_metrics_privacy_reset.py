"""Regressions for saved profiles, event windows, and bot-scoped conversation reset."""
import copy
import unittest
from datetime import datetime, timedelta, timezone

import main
from tests.test_row_persistence import RowPersistenceTests

BRT = timezone(timedelta(hours=-3))


class ProfilePrivacyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = RowPersistenceTests.asyncSetUp
    async def test_saved_profile_secrets_are_masked_in_state_and_preserved_on_restore(self):
        state = copy.deepcopy(await main._read_state_from_db())
        state['saved_profiles'] = [{'id': 'profile-one', 'name': 'Demo', 'snapshot': {
            'persona': {'name': 'Demo'},
            'publish': {'bot_token': '111:sample-secret', 'pushin_pay_token': 'gateway-sample', 'gateway_secret': 'signing-sample'},
            'account': {'ai_api_key': 'ai-sample'},
        }}]
        state['publish'].update(bot_token='222:currently-active', pushin_pay_token='current-gateway', gateway_secret='current-signing')
        state['account']['ai_api_key'] = 'current-ai'
        await main._write_state_to_db(state)
        response = main._panel_state(await main._read_state_from_db())
        for secret in ('111:sample-secret', 'gateway-sample', 'signing-sample', 'ai-sample'):
            self.assertNotIn(secret, str(response))
        snapshot = response['saved_profiles'][0]['snapshot']
        self.assertEqual(snapshot['persona']['name'], 'Demo')
        self.assertEqual(snapshot['publish']['bot_token'], main.REDACTED_SECRET_MASK)
        # Applying a masked profile must never replace a functioning token with a mask or erase it.
        response['publish'] = copy.deepcopy(snapshot['publish'])
        response['account'] = copy.deepcopy(snapshot['account'])
        response['restore_profile_id'] = 'profile-one'
        saved = await main._write_state_to_db(response, preserve_runtime=True)
        self.assertNotIn('restore_profile_id', saved)
        self.assertEqual(saved['publish']['bot_token'], '111:sample-secret')
        self.assertEqual(saved['account']['ai_api_key'], 'ai-sample')
        self.assertEqual(saved['saved_profiles'][0]['snapshot']['publish']['bot_token'], '111:sample-secret')

    async def test_new_profile_snapshot_preserves_live_credential_only_on_server(self):
        state = copy.deepcopy(await main._read_state_from_db())
        state['publish']['bot_token'] = '111:live-fixture'
        state['saved_profiles'] = [{'id': 'new', 'snapshot': {'publish': {'bot_token': main.REDACTED_SECRET_MASK}}}]
        state['save_profile_id'] = 'new'
        saved = await main._write_state_to_db(state, preserve_runtime=True)
        self.assertEqual(saved['saved_profiles'][0]['snapshot']['publish']['bot_token'], '111:live-fixture')
        self.assertNotIn('save_profile_id', saved)
        self.assertNotIn('111:live-fixture', str(main._panel_state(saved)))

    async def test_reset_scopes_same_telegram_user_to_selected_bot(self):
        state = copy.deepcopy(await main._read_state_from_db())
        for lead in state['memory']['leads']:
            lead.update(history=[{'role': 'user', 'content': lead['bot_id']}], stage='Pagamento')
        await main._write_state_to_db(state)
        await main.reset_lead_conversation('7', bot_id='b')
        leads = (await main._read_state_from_db())['memory']['leads']
        self.assertEqual(next(l for l in leads if l['bot_id'] == 'a')['history'][0]['content'], 'a')
        self.assertEqual(next(l for l in leads if l['bot_id'] == 'b')['history'], [])
        self.assertEqual(next(l for l in leads if l['bot_id'] == 'a')['stage'], 'Pagamento')

    async def test_reset_route_honors_bot_query(self):
        import httpx
        state = copy.deepcopy(await main._read_state_from_db())
        for lead in state['memory']['leads']:
            lead['history'] = [{'role': 'user', 'content': lead['bot_id']}]
        await main._write_state_to_db(state)
        main.app.dependency_overrides[main.verify_admin_auth] = lambda: None
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
                response = await client.post('/api/leads/7/reset?bot_id=b')
                self.assertEqual(response.status_code, 200)
                profile = await client.get('/api/state')
                self.assertEqual(profile.status_code, 200)
            leads = (await main._read_state_from_db())['memory']['leads']
            self.assertEqual(next(l for l in leads if l['bot_id'] == 'a')['history'][0]['content'], 'a')
            self.assertEqual(next(l for l in leads if l['bot_id'] == 'b')['history'], [])
        finally:
            main.app.dependency_overrides.pop(main.verify_admin_auth, None)

    async def test_scoped_reset_never_cancels_other_bots_tasks(self):
        from unittest.mock import patch
        with patch.object(main.bot_manager, 'cancel_lead_tasks') as cancel_all, \
             patch.object(main.bot_manager, '_cancel_downsell') as cancel_downsell, \
             patch.object(main.bot_manager, '_cancel_upsell') as cancel_upsell:
            await main.reset_lead_conversation('7', bot_id='b')
        cancel_all.assert_not_called()
        cancel_downsell.assert_called_once_with('7', '222')
        cancel_upsell.assert_called_once_with('7', '222')

    async def test_reset_all_scope_is_explicit(self):
        state = copy.deepcopy(await main._read_state_from_db())
        for lead in state['memory']['leads']:
            lead['history'] = [{'role': 'user', 'content': 'hi'}]
        await main._write_state_to_db(state)
        await main.reset_lead_conversation('7', bot_id='all')
        self.assertTrue(all(not l['history'] for l in (await main._read_state_from_db())['memory']['leads']))


class CalendarWindowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = RowPersistenceTests.asyncSetUp
    async def test_seven_and_thirty_day_windows_count_calendar_days_not_elapsed_hours(self):
        today = datetime.now(BRT).date()
        state = copy.deepcopy(await main._read_state_from_db())
        for tx_id, age in [('p1', 30), ('p2', 6), ('p3', 29)]:
            record = state['tx_records'][tx_id]
            record.update(status='paid', created_at=datetime.combine(today - timedelta(days=age), datetime.min.time(), BRT).isoformat(),
                          paid_at=datetime.combine(today - timedelta(days=age), datetime.min.time(), BRT).isoformat())
        await main._write_state_to_db(state)
        seven = await main.get_panel_metrics(period='7 dias')
        thirty = await main.get_panel_metrics(period='30d')
        self.assertEqual(seven['paid_count'], 1)
        self.assertEqual(thirty['paid_count'], 2)
        self.assertEqual((await main.list_panel_transactions(period='7 dias'))['total'], 1)
        self.assertEqual((await main.list_panel_transactions(period='30d'))['total'], 2)

    async def test_paid_utc_crossing_brt_boundary_uses_payment_day(self):
        today = datetime.now(BRT).date()
        state = copy.deepcopy(await main._read_state_from_db())
        state['tx_records']['p1'].update(created_at=(today - timedelta(days=10)).isoformat() + 'T12:00:00-03:00',
            paid_at=(datetime.combine(today, datetime.min.time(), BRT) + timedelta(minutes=30)).astimezone(timezone.utc).isoformat())
        state['tx_records']['p2'].update(created_at=today.isoformat() + 'T12:00:00-03:00',
            paid_at=(datetime.combine(today + timedelta(days=1), datetime.min.time(), BRT) + timedelta(minutes=30)).astimezone(timezone.utc).isoformat())
        await main._write_state_to_db(state)
        result = await main.get_panel_metrics(period='Hoje', bot_id='a')
        self.assertEqual(result['paid_count'], 1)
        self.assertEqual(result['generated_count'], 1)
        self.assertEqual((await main.list_panel_transactions(period='Hoje', bot_id='a', status='paid'))['total'], 1)
