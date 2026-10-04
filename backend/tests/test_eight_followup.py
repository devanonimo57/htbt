"""Deterministic held-transport regressions with real temporary SQLite."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
import main
import telegram_service
import test_checkout_webhook_integration as fixture


class EightFollowup(fixture.CheckoutWebhookIntegration):
    async def asyncSetUp(self):
        main._DB_LOCK = asyncio.Lock()
        await super().asyncSetUp()
        self.manager._schedule_flow_downsell = lambda *a, **kw: None

    async def held_crm(self):
        state = await main._read_state_from_db()
        await self.manager._get_or_create_lead(state, self.user, bot_token=self.bot.token)
        await main._write_state_to_db(state, runtime_only=True)
        started, finish = asyncio.Event(), asyncio.Event()
        async def send(**kw):
            started.set()
            await finish.wait()
            return SimpleNamespace(message_id=1)
        temp_bot = SimpleNamespace(send_message=AsyncMock(side_effect=send), session=SimpleNamespace(close=AsyncMock()))
        self.factory = patch.object(telegram_service, '_create_bot_instance', return_value=temp_bot)
        self.factory_mock = self.factory.start()
        self.addCleanup(self.factory.stop)
        main.app.dependency_overrides[main.verify_admin_auth] = lambda: None
        self.addCleanup(main.app.dependency_overrides.clear)
        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
                response = await client.post('/api/flow-bots/leads/action', json={
                    'telegram_id': '7', 'bot_id': 'flow-a', 'origin_bot_numeric_id': '111',
                    'action': 'send_message', 'message': 'CRM HELD MESSAGE'})
                self.assertEqual(response.status_code, 200)
                return response.json()
        task = asyncio.create_task(request())
        await asyncio.wait_for(started.wait(), 5)
        return task, finish

    async def test_crm_completion_preserves_new_configuration(self):
        task, finish = await self.held_crm()
        edited = main._panel_state(await main._read_state_from_db())
        edited['publish']['support_username'] = '@new_support'
        edited['flow_bots'][0]['plans'][0]['delivery_external_url'] = 'https://example.invalid/new'
        edited['flow_bots'][0].update(username='@fresh_verified', first_name='Fresh')
        await main._write_state_to_db(edited, preserve_runtime=True)
        finish.set()
        self.assertEqual((await task)['status'], 'ok')
        self.factory_mock.assert_called_once_with(self.bot.token)
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        self.assertEqual(saved['publish']['support_username'], '@new_support')
        self.assertEqual(saved['flow_bots'][0]['plans'][0]['delivery_external_url'], 'https://example.invalid/new')
        self.assertEqual(saved['flow_bots'][0]['username'], '@fresh_verified')
        self.assertEqual(saved['memory']['leads'][0]['history'][-1]['content'], 'CRM HELD MESSAGE')

    async def test_crm_completion_keeps_replaced_token_metadata(self):
        task, finish = await self.held_crm()
        edited = main._panel_state(await main._read_state_from_db())
        edited['flow_bots'][0].update(token='222:replacement-only', bot_id='222', username='@replacement', first_name='Replacement')
        await main._write_state_to_db(edited, preserve_runtime=True)
        finish.set()
        self.assertEqual((await task)['status'], 'ok')
        self.factory_mock.assert_called_once_with(self.bot.token)
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        self.assertEqual(saved['flow_bots'][0]['token'], '222:replacement-only')
        self.assertEqual(saved['flow_bots'][0]['bot_id'], '222')
        self.assertEqual(saved['flow_bots'][0]['username'], '@replacement')

    async def test_crm_completion_does_not_resurrect_archived_lead(self):
        task, finish = await self.held_crm()
        await main.delete_lead('7', bot_id='flow-a')
        archived_before = copy.deepcopy((await main._read_state_from_db())['memory']['deleted_leads'])
        finish.set()
        self.assertEqual((await task)['status'], 'ok')
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        self.assertFalse(saved['memory']['leads'])
        self.assertEqual(saved['memory']['deleted_leads'], archived_before)

    async def test_crm_completion_does_not_recreate_permanently_deleted_lead(self):
        task, finish = await self.held_crm()
        await main.delete_lead('7', bot_id='flow-a')
        await main.permanently_delete_lead('7', bot_id='flow-a')
        finish.set()
        self.assertEqual((await task)['status'], 'ok')
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        self.assertFalse(saved['memory']['leads'])
        self.assertFalse(saved['memory']['deleted_leads'])

    async def test_crm_completion_does_not_write_into_replacement_lead(self):
        task, finish = await self.held_crm()
        await main.delete_lead('7', bot_id='flow-a')
        edited = main._panel_state(await main._read_state_from_db())
        edited['flow_bots'][0].update(token='222:replacement-only', bot_id='222', username='@replacement')
        await main._write_state_to_db(edited, preserve_runtime=True)
        state = await main._read_state_from_db()
        await self.manager._get_or_create_lead(state, self.user, bot_token='222:replacement-only')
        await main._write_state_to_db(state, runtime_only=True)
        expected = copy.deepcopy((await main._read_state_from_db())['memory'])
        finish.set()
        self.assertEqual((await task)['status'], 'ok')
        main._STATE_CACHE = None
        self.assertEqual((await main._read_state_from_db())['memory'], expected)

    async def concurrent_settlements(self, reverse=False):
        for _ in range(2):
            state = await main._read_state_from_db()
            await self.manager._generate_flow_checkout(self.callback, state['flow_bots'][0], state, 'plan-a', 0, [])
        tx_ids = [x[0] for x in self.created]
        if reverse:
            tx_ids.reverse()
        started, finish, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = 0
        async def send(**kw):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await finish.wait()
            return SimpleNamespace(message_id=calls)
        self.bot.send_message.side_effect = send
        original_lock = self.manager._get_lead_lock
        requests = 0
        class ObservedLock:
            def __init__(self, lock, notify):
                self.lock, self.notify = lock, notify
            async def __aenter__(self):
                if self.notify:
                    queued.set()
                return await self.lock.__aenter__()
            async def __aexit__(self, *args):
                return await self.lock.__aexit__(*args)
        def observe(*args, **kw):
            nonlocal requests
            requests += 1
            return ObservedLock(original_lock(*args, **kw), requests == 2)
        self.manager._get_lead_lock = observe
        for tx in tx_ids:
            self.gateway_status[tx] = 'paid'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            first = asyncio.create_task(client.post('/api/webhook/syncpay', json={'id': tx_ids[0], 'status': 'paid'}))
            await asyncio.wait_for(started.wait(), 5)
            second = asyncio.create_task(client.post('/api/webhook/syncpay', json={'id': tx_ids[1], 'status': 'paid'}))
            await asyncio.wait_for(queued.wait(), 5)
            finish.set()
            for response in await asyncio.gather(first, second):
                self.assertEqual(response.json()['action'], 'approved')
            for tx in tx_ids:
                await client.post('/api/webhook/syncpay', json={'id': tx, 'status': 'paid'})
        self.assertEqual(calls, 2)
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        self.assertTrue(all(saved['tx_records'][tx]['delivered'] for tx in tx_ids))
        self.assertEqual(saved['flow_bots'][0]['sales_today'], 2)
        self.assertEqual(saved['flow_bots'][0]['revenue_today'], 20)
        self.assertEqual(sum('Entrega concluída' in h['content'] for h in saved['memory']['leads'][0]['history']), 2)

    async def test_distinct_paid_pix_keep_both_histories(self):
        await self.concurrent_settlements()

    async def test_reverse_distinct_paid_pix_keep_both_histories(self):
        await self.concurrent_settlements(reverse=True)

    async def cache_request(self, payload, warm=None):
        main.app.dependency_overrides[main.verify_admin_auth] = lambda: None
        self.addCleanup(main.app.dependency_overrides.clear)
        fake = SimpleNamespace(token=self.bot.token, session=SimpleNamespace(close=AsyncMock()))
        with patch.object(telegram_service, '_create_bot_instance', return_value=fake) as factory, \
             patch.object(self.manager, 'warm_media_item_in_channel', new=AsyncMock(side_effect=warm or (lambda *a, **kw: 'cached-A'))) as transport:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
                response = await client.post('/api/flow-bots/cache-media', json=payload)
        return response, factory, transport

    async def test_cache_explicit_unknown_bot_fails_before_side_effects(self):
        response, factory, transport = await self.cache_request({'bot_id': 'deleted-flow-b', 'channel_id': '-1002', 'media': {'id': 'm', 'url': 'data:image/png;base64,AAAA'}})
        self.assertEqual(response.status_code, 404)
        factory.assert_not_called()
        transport.assert_not_awaited()

    async def test_cache_reused_config_id_rejects_old_public_origin(self):
        state = await main._read_state_from_db()
        state['flow_bots'][0].update(token='222:replacement-only', bot_id='222')
        await main._write_state_to_db(state)
        response, factory, transport = await self.cache_request({'bot_id': 'flow-a', 'origin_bot_numeric_id': '111', 'media': {'id': 'm', 'url': 'data:image/png;base64,AAAA'}})
        self.assertEqual(response.status_code, 409)
        factory.assert_not_called()
        transport.assert_not_awaited()

    async def test_cache_explicit_bot_without_token_cannot_use_publish_token(self):
        state = await main._read_state_from_db()
        state['flow_bots'].append({'id': 'flow-no-token', 'token': '', 'bot_id': '222'})
        await main._write_state_to_db(state)
        response, factory, transport = await self.cache_request({'bot_id': 'flow-no-token', 'media': {'id': 'm', 'url': 'data:image/png;base64,AAAA'}})
        self.assertEqual(response.json()['status'], 'error')
        factory.assert_not_called()
        transport.assert_not_awaited()

    async def test_cache_omitted_bot_preserves_legacy_selection(self):
        response, factory, transport = await self.cache_request({'media': {'id': 'm', 'url': 'data:image/png;base64,AAAA'}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['telegram_file_id'], 'cached-A')
        factory.assert_not_called()  # Reuses the matching running origin.
        self.assertEqual(transport.call_args.kwargs['bot_instance'].token, self.bot.token)
        self.assertEqual(transport.call_args.kwargs['fb']['id'], 'flow-a')

    async def test_cache_pending_transport_keeps_captured_token_after_replacement(self):
        started, finish = asyncio.Event(), asyncio.Event()
        observed = []
        async def warm(media, state, fb, bot_instance):
            started.set()
            await finish.wait()
            observed.append((fb['id'], bot_instance.token))
            return 'cached-A'
        task = asyncio.create_task(self.cache_request({'bot_id': 'flow-a', 'origin_bot_numeric_id': '111', 'media': {'id': 'm', 'url': 'data:image/png;base64,AAAA'}}, warm=warm))
        await asyncio.wait_for(started.wait(), 5)
        edited = main._panel_state(await main._read_state_from_db())
        edited['flow_bots'][0].update(token='222:replacement-only', bot_id='222')
        await main._write_state_to_db(edited, preserve_runtime=True)
        finish.set()
        response, factory, transport = await task
        self.assertEqual(response.status_code, 200)
        self.assertEqual(observed, [('flow-a', self.bot.token)])
        factory.assert_not_called()
        main._STATE_CACHE = None
        self.assertEqual((await main._read_state_from_db())['flow_bots'][0]['bot_id'], '222')

    async def test_cache_pending_transport_keeps_captured_token_after_removal(self):
        started, finish = asyncio.Event(), asyncio.Event()
        observed = []
        async def warm(media, state, fb, bot_instance):
            started.set()
            await finish.wait()
            observed.append((fb['id'], bot_instance.token))
            return 'cached-A'
        task = asyncio.create_task(self.cache_request({'bot_id': 'flow-a', 'origin_bot_numeric_id': '111', 'media': {'id': 'm', 'url': 'data:image/png;base64,AAAA'}}, warm=warm))
        await asyncio.wait_for(started.wait(), 5)
        edited = main._panel_state(await main._read_state_from_db())
        edited['flow_bots'][0].update(id='remaining-peer', token='222:replacement-only', bot_id='222')
        await main._write_state_to_db(edited, preserve_runtime=True)
        finish.set()
        response, factory, transport = await task
        self.assertEqual(response.status_code, 200)
        self.assertEqual(observed, [('flow-a', self.bot.token)])
        factory.assert_not_called()
        main._STATE_CACHE = None
        bots = (await main._read_state_from_db())['flow_bots']
        self.assertEqual([b['id'] for b in bots], ['remaining-peer'])
        self.assertEqual(bots[0]['bot_id'], '222')

    async def checkout_media_state(self, medias=None):
        state = await main._read_state_from_db()
        state['flow_bots'][0]['cache_channel_id'] = '-1002'
        state['flow_bots'][0]['payments']['generated_medias'] = medias or [
            {'id': 'checkout-photo', 'type': 'photo', 'url': 'data:image/png;base64,AAAA'}]
        await main._write_state_to_db(state)
        self.bot.send_photo = AsyncMock(return_value=SimpleNamespace(
            photo=[SimpleNamespace(file_id='media-cached')]))
        return await main._read_state_from_db()

    async def test_checkout_generated_media_cache_survives_cold_read_and_is_reused(self):
        state = await self.checkout_media_state()
        await self.manager._generate_flow_checkout(self.callback, state['flow_bots'][0], state, 'plan-a', 0, [])
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        media = saved['flow_bots'][0]['payments']['generated_medias'][0]
        self.assertEqual(media.get('telegram_file_id'), 'media-cached')
        self.assertEqual(media['telegram_send_as'], 'photo')
        self.assertEqual(media['cached_channel_id'], '-1002')
        await self.manager._generate_flow_checkout(self.callback, saved['flow_bots'][0], saved, 'plan-a', 0, [])
        uploads = [call for call in self.bot.send_photo.call_args_list if not isinstance(call.kwargs['photo'], str)]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(self.bot.send_photo.call_args.kwargs['photo'], 'media-cached')
        self.assertEqual(len(self.created), 2)

    async def test_checkout_generated_media_without_item_id_persists_cache(self):
        state = await self.checkout_media_state([{'type': 'photo', 'url': 'data:image/png;base64,AAAA'}])
        await self.manager._generate_flow_checkout(self.callback, state['flow_bots'][0], state, 'plan-a', 0, [])
        main._STATE_CACHE = None
        media = (await main._read_state_from_db())['flow_bots'][0]['payments']['generated_medias'][0]
        self.assertEqual(media['telegram_file_id'], 'media-cached')

    async def test_checkout_generated_media_direct_send_persists_cache(self):
        state = await self.checkout_media_state()
        with patch.object(self.manager, 'warm_media_item_in_channel', new=AsyncMock(return_value=None)):
            await self.manager._generate_flow_checkout(self.callback, state['flow_bots'][0], state, 'plan-a', 0, [])
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        self.assertEqual(saved['flow_bots'][0]['payments']['generated_medias'][0]['telegram_file_id'], 'media-cached')
        await self.manager._generate_flow_checkout(self.callback, saved['flow_bots'][0], saved, 'plan-a', 0, [])
        self.assertEqual(self.bot.send_photo.call_args.kwargs['photo'], 'media-cached')
        self.assertEqual(self.bot.send_photo.await_count, 2)

    async def test_checkout_generated_video_persists_compacted_source(self):
        state = await self.checkout_media_state([{'id': 'video', 'type': 'video', 'url': 'data:video/mp4;base64,AAAA'}])
        self.bot.send_video = AsyncMock(return_value=SimpleNamespace(video=SimpleNamespace(file_id='video-cached')))
        await self.manager._generate_flow_checkout(self.callback, state['flow_bots'][0], state, 'plan-a', 0, [])
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        media = saved['flow_bots'][0]['payments']['generated_medias'][0]
        self.assertEqual(media['telegram_file_id'], 'video-cached')
        self.assertEqual(media['telegram_send_as'], 'video')
        self.assertEqual(media['url'], 'tg://file_id/video-cached')
        await self.manager._generate_flow_checkout(self.callback, saved['flow_bots'][0], saved, 'plan-a', 0, [])
        self.assertEqual(self.bot.send_video.await_count, 3)  # Warm + first recipient + cached second recipient.
        self.assertEqual(self.bot.send_video.call_args.kwargs['video'], 'video-cached')

    async def test_checkout_generated_album_persists_and_reuses_file_ids(self):
        state = await self.checkout_media_state([
            {'id': 'photo-a', 'type': 'photo', 'url': 'data:image/png;base64,AAAA'},
            {'id': 'photo-b', 'type': 'photo', 'url': 'data:image/png;base64,BBBB'}])
        self.bot.send_media_group = AsyncMock(return_value=[
            SimpleNamespace(photo=[SimpleNamespace(file_id='album-a')]),
            SimpleNamespace(photo=[SimpleNamespace(file_id='album-b')])])
        await self.manager._generate_flow_checkout(self.callback, state['flow_bots'][0], state, 'plan-a', 0, [])
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        medias = saved['flow_bots'][0]['payments']['generated_medias']
        self.assertEqual([media['telegram_file_id'] for media in medias], ['album-a', 'album-b'])
        self.assertTrue(all(media['album_cached'] for media in medias))
        await self.manager._generate_flow_checkout(self.callback, saved['flow_bots'][0], saved, 'plan-a', 0, [])
        self.assertEqual(self.bot.send_media_group.await_count, 3)
        self.assertEqual([media.media for media in self.bot.send_media_group.call_args.kwargs['media']], ['album-a', 'album-b'])

    async def held_checkout_media_edit(self, mutation, at_commit=False):
        state = await self.checkout_media_state()
        started, finish = asyncio.Event(), asyncio.Event()
        original_saver = self.manager.state_saver
        async def edit():
            edited = main._panel_state(await main._read_state_from_db())
            edited['publish']['support_username'] = '@edited_support'
            edited['flow_bots'][0]['plans'][0]['price'] = 27
            mutation(edited['flow_bots'][0])
            await main._write_state_to_db(edited, preserve_runtime=True)
            return copy.deepcopy(await main._read_state_from_db())
        expected = None
        if at_commit:
            async def save(candidate, **kw):
                nonlocal expected
                if kw.get('checkout_media_cache'):
                    expected = await edit()  # Candidate already read, writer not acquired yet.
                return await original_saver(candidate, **kw)
            self.manager.state_saver = save
        else:
            async def send(**kw):
                if not isinstance(kw['photo'], str):
                    started.set()
                    await finish.wait()
                return SimpleNamespace(photo=[SimpleNamespace(file_id='media-cached')])
            self.bot.send_photo.side_effect = send
        task = asyncio.create_task(self.manager._generate_flow_checkout(
            self.callback, state['flow_bots'][0], state, 'plan-a', 0, []))
        try:
            if not at_commit:
                await asyncio.wait_for(started.wait(), 5)
                expected = await edit()
                finish.set()
            await asyncio.wait_for(task, 5)
        finally:
            finish.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        self.assertEqual(saved['flow_bots'], expected['flow_bots'])
        self.assertEqual(saved['publish']['support_username'], '@edited_support')
        self.assertEqual(saved['tx_records'], expected['tx_records'])
        self.assertEqual(saved['memory'], expected['memory'])
        self.assertEqual(saved['tx_records'][self.created[-1][0]]['amount'], 10)

    async def test_checkout_media_cache_does_not_write_into_replaced_bot(self):
        await self.held_checkout_media_edit(lambda bot: bot.update(token='222:replacement-only', bot_id='222'))

    async def test_checkout_media_cache_does_not_resurrect_removed_bot(self):
        await self.held_checkout_media_edit(lambda bot: bot.update(id='remaining-peer', token='222:replacement-only', bot_id='222'))

    async def test_checkout_media_cache_does_not_resurrect_removed_media(self):
        await self.held_checkout_media_edit(lambda bot: bot['payments'].update(generated_medias=[]))

    async def test_checkout_media_cache_does_not_write_into_replaced_source(self):
        await self.held_checkout_media_edit(lambda bot: bot['payments']['generated_medias'][0].update(url='data:image/png;base64,BBBB'))

    async def test_checkout_media_cache_does_not_write_into_replaced_type(self):
        await self.held_checkout_media_edit(lambda bot: bot['payments']['generated_medias'][0].update(type='video'))

    async def test_checkout_media_cache_keeps_newer_completed_file_id(self):
        await self.held_checkout_media_edit(lambda bot: bot['payments']['generated_medias'][0].update(
            telegram_file_id='newer-cache', telegram_send_as='photo', cached_channel_id='-1003'))

    async def test_checkout_media_cache_matches_source_inside_writer_lock(self):
        await self.held_checkout_media_edit(lambda bot: bot['payments']['generated_medias'][0].update(url='data:image/png;base64,BBBB'), at_commit=True)

    async def test_checkout_media_cache_matches_bot_inside_writer_lock(self):
        await self.held_checkout_media_edit(lambda bot: bot.update(token='222:replacement-only', bot_id='222'), at_commit=True)

    async def test_checkout_media_cache_does_not_transfer_other_stale_cache_at_commit(self):
        state = await self.checkout_media_state()
        original_saver = self.manager.state_saver
        async def save(candidate, **kw):
            if kw.get('checkout_media_cache'):
                # Another completed upload appears in the fresh candidate,
                # then a panel replacement commits before this writer locks.
                candidate['flow_bots'][0]['payments']['generated_medias'][0]['telegram_file_id'] = 'other-upload'
                await original_saver(candidate, runtime_only=True)
                candidate = copy.deepcopy(await main._read_state_from_db())
                edited = main._panel_state(candidate)
                edited['flow_bots'][0]['payments']['generated_medias'] = [
                    {'id': 'checkout-photo', 'type': 'photo', 'url': 'data:image/png;base64,BBBB'}]
                await original_saver(edited)
            return await original_saver(candidate, **kw)
        self.manager.state_saver = save
        await self.manager._generate_flow_checkout(self.callback, state['flow_bots'][0], state, 'plan-a', 0, [])
        main._STATE_CACHE = None
        media = (await main._read_state_from_db())['flow_bots'][0]['payments']['generated_medias'][0]
        self.assertEqual(media['url'], 'data:image/png;base64,BBBB')
        self.assertNotIn('telegram_file_id', media)

    async def test_checkout_media_cache_preserves_concurrent_configuration_edits(self):
        state = await self.checkout_media_state()
        async def send(**kw):
            if not isinstance(kw['photo'], str):
                edited = main._panel_state(await main._read_state_from_db())
                edited['publish']['support_username'] = '@edited_support'
                edited['flow_bots'][0]['plans'][0]['price'] = 27
                edited['flow_bots'][0]['payments']['generated_medias'][0]['caption'] = 'new caption'
                await main._write_state_to_db(edited, preserve_runtime=True)
            return SimpleNamespace(photo=[SimpleNamespace(file_id='media-cached')])
        self.bot.send_photo.side_effect = send
        await self.manager._generate_flow_checkout(self.callback, state['flow_bots'][0], state, 'plan-a', 0, [])
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        media = saved['flow_bots'][0]['payments']['generated_medias'][0]
        self.assertEqual(media['telegram_file_id'], 'media-cached')
        self.assertEqual(media['caption'], 'new caption')
        self.assertEqual(saved['publish']['support_username'], '@edited_support')
        self.assertEqual(saved['flow_bots'][0]['plans'][0]['price'], 27)
        self.assertEqual(saved['tx_records'][self.created[-1][0]]['amount'], 10)

    async def test_checkout_cache_commit_failure_still_sends_durable_pix(self):
        state = await self.checkout_media_state()
        original_saver = self.manager.state_saver
        async def save(candidate, **kw):
            if kw.get('checkout_media_cache'):
                raise RuntimeError('fake cache commit failure')
            return await original_saver(candidate, **kw)
        self.manager.state_saver = save
        await self.manager._generate_flow_checkout(self.callback, state['flow_bots'][0], state, 'plan-a', 0, [])
        tx = self.created[-1][0]
        self.assertTrue(any('PIX-' + tx in call.kwargs['text'] for call in self.bot.send_message.call_args_list))
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        self.assertIn(tx, saved['tx_records'])
        self.assertNotIn('telegram_file_id', saved['flow_bots'][0]['payments']['generated_medias'][0])

    async def held_checkout(self):
        state = await main._read_state_from_db()
        state['flow_bots'][0]['payments'].update(secondary_enabled=True, secondary_message='SECONDARY')
        await main._write_state_to_db(state)
        started, finish = asyncio.Event(), asyncio.Event()
        self.texts = []
        async def send(**kw):
            self.texts.append(kw['text'])
            if kw['text'] == 'SECONDARY':
                started.set()
                await finish.wait()
            return SimpleNamespace(message_id=1)
        self.bot.send_message.side_effect = send
        task = asyncio.create_task(self.manager._generate_flow_checkout(self.callback, state['flow_bots'][0], state, 'plan-a', 0, []))
        await asyncio.wait_for(started.wait(), 5)
        return task, finish, self.created[-1][0]

    async def test_checkout_durable_before_qr_survives_cancellation(self):
        task, finish, tx = await self.held_checkout()
        self.assertTrue(any('PIX-' + tx in t for t in self.texts))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.manager.tx_records.clear()
        self.manager.active_tx_map.clear()
        main._STATE_CACHE = None
        self.assertIn(tx, (await main._read_state_from_db())['tx_records'])
        self.gateway_status[tx] = 'paid'
        self.bot.send_message.reset_mock(side_effect=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            response = await client.post('/api/webhook/syncpay', json={'id': tx, 'status': 'paid'})
        self.assertEqual(response.json()['action'], 'approved')
        self.assertEqual(self.bot.send_message.await_count, 1)
        main._STATE_CACHE = None
        self.assertTrue((await main._read_state_from_db())['tx_records'][tx]['delivered'])

    async def test_checkout_failed_commit_does_not_expose_pix(self):
        state = await main._read_state_from_db()
        self.manager.state_saver = AsyncMock(side_effect=RuntimeError('fake commit failure'))
        with self.assertRaisesRegex(RuntimeError, 'fake commit failure'):
            await self.manager._generate_flow_checkout(self.callback, state['flow_bots'][0], state, 'plan-a', 0, [])
        self.bot.send_message.assert_not_awaited()
        self.assertFalse(self.manager.tx_records)
        self.assertFalse(self.manager.active_tx_map)
        self.assertFalse((await main._read_state_from_db())['tx_records'])
        main._STATE_CACHE = None
        self.assertFalse((await main._read_state_from_db())['tx_records'])

    async def test_webhook_while_secondary_message_pending_is_not_regressed(self):
        task, finish, tx = await self.held_checkout()
        self.gateway_status[tx] = 'paid'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            response = await client.post('/api/webhook/syncpay', json={'id': tx, 'status': 'paid'})
            self.assertEqual(response.json()['action'], 'approved')
            finish.set()
            await task
            before = self.bot.send_message.await_count
            await client.post('/api/webhook/syncpay', json={'id': tx, 'status': 'paid'})
            self.assertEqual(self.bot.send_message.await_count, before)
        main._STATE_CACHE = None
        saved = await main._read_state_from_db()
        self.assertEqual(saved['tx_records'][tx]['status'], 'paid')
        self.assertTrue(saved['tx_records'][tx]['delivered'])
        self.assertEqual(saved['flow_bots'][0]['sales_today'], 1)
        self.assertEqual(sum('Entrega concluída' in h['content'] for h in saved['memory']['leads'][0]['history']), 1)
