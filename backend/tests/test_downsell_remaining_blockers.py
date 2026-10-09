"""Positive offline regressions for DS-FINAL-01/02; real worker and SQL."""
import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import main
import test_gemini_downsell_contract as fixture


class RemainingDownsell(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.GeminiDownsell.asyncSetUp
    asyncTearDown = fixture.GeminiDownsell.asyncTearDown
    seq = fixture.GeminiDownsell.seq
    config = fixture.GeminiDownsell.config
    start = fixture.GeminiDownsell.start
    checkout = fixture.GeminiDownsell.checkout
    messages = fixture.GeminiDownsell.messages
    step = fixture.GeminiDownsell.step
    async def until(self, expected, limit=20):
        # Wait for messages AND a commit/sleep boundary. Do not assume that a
        # slow SQL completion must publish another sleep after the final card.
        wakes = 0
        for _ in range(1500):
            workers = [t for t in set(self.manager.downsell_tasks.values()) if not t.done()]
            at_boundary = all(t in self.gate.asleep for t in workers)
            if self.messages() == expected and at_boundary:
                return
            if at_boundary and not self.gate.waits.empty():
                self.assertLess(wakes, limit, 'Expected message sequence not reached in bounded wakeups')
                await self.step()
                wakes += 1
            elif not workers:
                self.assertEqual(self.messages(), expected)
                return
            else:
                await self.gate.real_sleep(0.01)
        self.fail(f'Worker did not reach expected sequence/commit boundary: {self.messages()} != {expected}')

    cold = fixture.GeminiDownsell.cold

    def media(self, sid='photo', cached=True):
        item = dict(id=sid, type='Fotos', url='data:image/png;base64,AAAA')
        if cached:
            item.update(telegram_file_id='offline-' + sid, telegram_send_as='photo')
        return item

    async def revoke(self, operation):
        state = await main._read_state_from_db()
        fb = state['flow_bots'][0]
        if operation == 'disable':
            fb['downsell_general']['enabled'] = False
        elif operation == 'card-delete':
            fb['downsell_general']['sequences'] = []
        elif operation == 'card-disable':
            fb['downsell_general']['sequences'][0]['enabled'] = False
        elif operation == 'reentry':
            await self.revoke('remove')
            fixture.Clock.value += fixture.timedelta(seconds=1)
            await self.config([self.seq('B', 10)])
            await self.start()
            await self.gate.held()
            return copy.deepcopy(await self.cold())
        elif operation == 'block':
            state['memory']['leads'][0]['blocked'] = True
        elif operation == 'remove':
            response = await self.client.delete('/api/leads/7?bot_id=flow-a')
            self.assertEqual(response.status_code, 200)
            response = await self.client.delete('/api/leads/7/permanent?bot_id=flow-a')
            self.assertEqual(response.status_code, 200)
            saved = await self.cold()
            self.assertEqual(saved['memory']['leads'], [])
            return copy.deepcopy(saved)
        elif operation == 'token':
            fb['token'] = '111:replacement-test-only'
        elif operation == 'paid':
            state['tx_records']['paid-during-warm'] = dict(tx_id='paid-during-warm', user_id=7,
                bot_id='flow-a', bot_token=self.bot.token, origin_bot_numeric_id='111', status='paid',
                amount=10, created_at=fixture.Clock.utcnow().isoformat(), paid_at=fixture.Clock.utcnow().isoformat())
        await main._write_state_to_db(state)
        return copy.deepcopy(await self.cold())

    async def warming_revocation(self, album, operation='disable'):
        reached, release = asyncio.Event(), asyncio.Event()
        async def warm(*args, **kw):
            self.assertFalse(main._DB_LOCK.locked())
            reached.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()  # An in-flight warmer can ignore cancellation.
            return 0 if album else None
        self.bot.send_media_group = AsyncMock(return_value=[])
        self.bot.send_photo = AsyncMock(return_value=SimpleNamespace(photo=[SimpleNamespace(file_id='sent')]))
        self.bot.send_document = AsyncMock()
        attr = 'warm_media_group_in_channel' if album else 'warm_media_item_in_channel'
        setattr(self.manager, attr, warm)
        medias = [self.media('one'), self.media('two')] if album else [self.media(cached=False)]
        await self.config([self.seq('A', medias=medias)])
        await self.start()
        task = self.manager.downsell_tasks['111:7']
        await self.step()
        try:
            await asyncio.wait_for(reached.wait(), 3)
            before = await self.revoke(operation)
            release.set()
            await asyncio.wait_for(task, 3)
            self.bot.send_media_group.assert_not_awaited()
            self.bot.send_photo.assert_not_awaited()
            self.bot.send_document.assert_not_awaited()
            self.assertEqual(self.messages(), [])
            self.assertEqual((await self.cold())['flow_bots'], before['flow_bots'])
            self.assertEqual((await self.cold())['memory'], before['memory'])
            self.peer.send_message.assert_not_awaited()
        finally:
            release.set()

    async def test_album_disable_during_warm_prevents_new_customer_send(self):
        await self.warming_revocation(True)

    async def test_single_disable_during_warm_prevents_new_customer_send(self):
        await self.warming_revocation(False)

    async def test_card_delete_during_album_warm_prevents_new_send(self):
        await self.warming_revocation(True, 'card-delete')

    async def test_paid_during_single_warm_prevents_new_send(self):
        await self.warming_revocation(False, 'paid')

    async def test_block_during_single_warm_prevents_new_send(self):
        await self.warming_revocation(False, 'block')

    async def test_remove_during_album_warm_prevents_new_send(self):
        await self.warming_revocation(True, 'remove')

    async def test_token_rotation_during_album_warm_prevents_old_token_send(self):
        await self.warming_revocation(True, 'token')

    async def transport_revocation(self, kind):
        reached, release = asyncio.Event(), asyncio.Event()
        async def held(**kw):
            self.assertFalse(main._DB_LOCK.locked())
            reached.set()
            await release.wait()
            if kind == 'second-item':
                return SimpleNamespace(photo=[SimpleNamespace(file_id='accepted')])
            raise RuntimeError('retry after 3' if kind == 'text-rate' else 'offline invalid transport')
        self.bot.send_photo = AsyncMock(side_effect=held)
        self.bot.send_document = AsyncMock()
        self.bot.send_video = AsyncMock()
        self.bot.send_animation = AsyncMock(side_effect=held)
        medias = [self.media()]
        if kind in ('album', 'album-fallback'):
            self.bot.send_media_group = AsyncMock(side_effect=held if kind == 'album'
                                                  else RuntimeError('offline album failure'))
            medias.append(self.media('two'))
        elif kind == 'second-item':
            medias.append(self.media('two'))
        elif kind == 'animation':
            medias[0]['telegram_send_as'] = 'animation'
        elif kind == 'video':
            medias[0]['telegram_send_as'] = 'video'
            self.bot.send_video.side_effect = held
        elif kind == 'direct':
            medias[0] = self.media(cached=False)
        elif kind.startswith('text'):
            medias = []
            self.bot.send_message.side_effect = held
        await self.config([self.seq('A', medias=medias)])
        await self.start()
        task = self.manager.downsell_tasks['111:7']
        self.bot.send_message.reset_mock()
        # Welcome is answered via message.answer, not this bot's send_message.
        await self.step()
        try:
            await asyncio.wait_for(reached.wait(), 3)
            before = await self.revoke('disable')
            release.set()
            await asyncio.wait_for(task, 3)
            total = sum(m.await_count for m in [self.bot.send_photo, self.bot.send_document,
                self.bot.send_video, self.bot.send_animation, self.bot.send_message])
            total += getattr(self.bot, 'send_media_group', AsyncMock()).await_count
            self.assertEqual(total, 3 if kind == 'album-fallback' else 1,
                             'Only already-started sends may finish; no later retry/fallback/second item')
            self.assertEqual((await self.cold())['memory'], before['memory'])
            self.assertEqual((await self.cold())['flow_bots'], before['flow_bots'])
        finally:
            release.set()

    async def test_cached_photo_failure_after_disable_has_no_document_fallback(self):
        await self.transport_revocation('photo')

    async def test_direct_photo_failure_after_disable_has_no_document_fallback(self):
        await self.transport_revocation('direct')

    async def test_animation_failure_after_disable_has_no_video_fallback(self):
        await self.transport_revocation('animation')

    async def test_video_failure_after_disable_has_no_document_fallback(self):
        await self.transport_revocation('video')

    async def test_album_failure_after_disable_has_no_retry_or_individual_fallback(self):
        await self.transport_revocation('album')

    async def test_inflight_first_item_finishes_but_second_item_cannot_start(self):
        await self.transport_revocation('second-item')

    async def test_text_failure_after_disable_has_no_plain_retry(self):
        await self.transport_revocation('text')

    async def test_text_rate_limit_after_disable_has_no_delayed_retry(self):
        async def send(**kw):
            self.assertFalse(main._DB_LOCK.locked())
            raise RuntimeError('retry after 3')
        await self.config([self.seq('A')])
        await self.start()
        self.bot.send_message.side_effect = send
        task = self.manager.downsell_tasks['111:7']
        await self.step()
        self.assertEqual(await self.gate.held(), 3.0)
        await self.revoke('disable')
        await self.gate.wake()
        await asyncio.wait_for(task, 3)
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def waiting_without_send(self, steps=4):
        try:
            for _ in range(steps):
                delay = await self.step()
                self.assertGreater(delay, 0)
                self.assertLessEqual(delay, 60)
            # Reach the next deterministic sleep boundary before inspecting SQL.
            await self.gate.held()
        except TimeoutError:
            self.fail('Worker prematurely finished while an active initial card still needs its event; '
                      f'observed sends: {self.messages()}')

    async def test_earlier_pix_card_does_not_block_later_start(self):
        pix = self.seq('P'); pix['trigger'] = 'pix'
        await self.config([pix, self.seq('A', loop_enabled=True)])
        await self.start()
        worker = self.manager.downsell_tasks['111:7']
        await self.until(['A'])
        self.assertEqual(self.messages(), ['A'], 'Start card must be sent without being blocked by unanchored Pix card')
        self.assertFalse(worker.done())
        await self.checkout()
        self.assertIs(self.manager.downsell_tasks['111:7'], worker)
        await self.gate.wake()
        await self.until(['A', 'P'])

    async def test_pix_first_does_not_wait_for_unanchored_start_card(self):
        pix = self.seq('P'); pix['trigger'] = 'pix'
        await self.config([self.seq('A'), pix])
        await self.checkout()
        worker = self.manager.downsell_tasks['111:7']
        await self.until(['P'])
        self.assertEqual(self.messages(), ['P'], 'Pix card must be sent without being blocked by unanchored start card')
        await self.start()
        self.assertIs(self.manager.downsell_tasks['111:7'], worker)
        await self.gate.wake()
        await self.until(['P', 'A'])
        task.cancel(); await asyncio.gather(task, return_exceptions=True)
        self.manager.downsell_tasks.clear()
        saved = await self.cold()
        self.manager._resume_flow_downsells(saved)
        await self.waiting_without_send(2)
        self.assertEqual(self.messages(), [])
        await self.checkout()
        await self.gate.wake()
        await self.until(['P', 'A'])
        await asyncio.wait_for(self.manager.downsell_tasks['111:7'], 3)
        self.assertEqual((await self.cold())['memory']['leads'][0]['flow_downsell']['sent'], ['P', 'A'])

    async def test_disabled_card_during_single_warm_prevents_new_send(self):
        await self.warming_revocation(False, 'card-disable')

    async def test_removed_customer_reentry_during_album_warm_preserves_new_incarnation(self):
        await self.warming_revocation(True, 'reentry')

    async def test_album_individual_fallback_cannot_continue_after_disable(self):
        await self.transport_revocation('album-fallback')

    async def group_download_revocation(self, retry):
        reached, release = asyncio.Event(), asyncio.Event()
        async def download(*args):
            self.assertFalse(main._DB_LOCK.locked())
            reached.set()
            await release.wait()
            return b'offline-photo'
        self.manager._download_file_by_id = download
        self.bot.send_media_group = AsyncMock(side_effect=RuntimeError('offline album failure'))
        self.bot.send_photo = AsyncMock()
        self.bot.send_document = AsyncMock()
        medias = [self.media('one'), self.media('two')]
        for m in medias:
            m['url'] = 'tg://file_id/' + m['telegram_file_id']
            m['album_cached'] = True
            if not retry:
                m['telegram_send_as'] = 'document'
        # Cache warming is deliberately isolated: exercise the customer's real
        # initial/retry group builder and its held download await.
        self.manager.warm_media_group_in_channel = AsyncMock(return_value=0)
        await self.config([self.seq('A', medias=medias)])
        await self.start(); await self.step()
        task = self.manager.downsell_tasks['111:7']
        try:
            await asyncio.wait_for(reached.wait(), 3)
            await self.revoke('disable')
            release.set(); await asyncio.wait_for(task, 3)
            self.assertEqual(self.bot.send_media_group.await_count, int(retry))
            self.bot.send_photo.assert_not_awaited()
            self.bot.send_document.assert_not_awaited()
            self.assertEqual(self.messages(), [])
        finally:
            release.set()

    async def test_initial_album_download_after_disable_has_no_customer_send(self):
        await self.group_download_revocation(False)

    async def test_retry_album_download_after_disable_has_no_new_customer_send(self):
        await self.group_download_revocation(True)

    async def test_live_media_and_text_edit_during_warm_uses_fresh_card_without_cache_rollback(self):
        reached, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def warm(m, *args, **kw):
            nonlocal calls
            calls += 1
            if calls == 1:
                reached.set(); await release.wait()
                m['telegram_file_id'] = 'obsolete-warm-result'
                return 'obsolete-warm-result'
            return None
        self.manager.warm_media_item_in_channel = warm
        self.bot.send_photo = AsyncMock(return_value=SimpleNamespace(photo=[SimpleNamespace(file_id='new-send')]))
        await self.config([self.seq('A', medias=[self.media(cached=False)])])
        await self.start(); await self.step()
        task = self.manager.downsell_tasks['111:7']
        try:
            await asyncio.wait_for(reached.wait(), 3)
            state = await main._read_state_from_db()
            card = state['flow_bots'][0]['downsell_general']['sequences'][0]
            card['message'] = 'NEW'
            card['medias'][0].update(url='data:image/png;base64,QkJC', telegram_file_id='new-panel-cache')
            await main._write_state_to_db(state)
            release.set(); await asyncio.wait_for(task, 3)
            self.assertEqual(self.messages(), ['NEW'])
            self.assertEqual(self.bot.send_photo.await_count, 1)
            self.assertEqual(self.bot.send_photo.call_args.kwargs['photo'], 'new-panel-cache')
            saved = (await self.cold())['flow_bots'][0]['downsell_general']['sequences'][0]
            self.assertEqual(saved['medias'][0]['telegram_file_id'], 'new-panel-cache')
            self.assertEqual(saved['medias'][0]['url'], 'data:image/png;base64,QkJC')
        finally:
            release.set()

    async def test_wrong_transport_bot_cannot_send_downsell(self):
        await self.config([self.seq('A')], peer=True)
        await self.start(); await self.gate.held()
        task = self.manager.downsell_tasks['111:7']
        self.manager._get_bot_by_token = lambda token: self.peer
        await self.gate.wake(); await asyncio.wait_for(task, 3)
        self.assertEqual(self.messages(), [])
        self.peer.send_message.assert_not_awaited()

    async def earlier_card_edit(self, operation):
        pix = self.seq('P'); pix['trigger'] = 'pix'
        await self.config([pix, self.seq('A', loop_enabled=True)])
        await self.start(); await self.waiting_without_send(2)
        self.assertEqual(self.messages(), [])
        state = await main._read_state_from_db()
        cards = state['flow_bots'][0]['downsell_general']['sequences']
        if operation == 'disable':
            cards[0]['enabled'] = False
        elif operation == 'delete':
            cards.pop(0)
        else:
            cards.reverse()
        await main._write_state_to_db(state)
        await self.gate.wake(); await self.until(['A'])
        if operation == 'reorder':
            await self.waiting_without_send(2)
            self.assertEqual(self.messages(), ['A'])
            await self.checkout(); await self.gate.wake()
            await self.until(['A', 'P', 'A'])
        else:
            await self.until(['A', 'A'])
            self.assertNotIn('P', self.messages())

    async def test_disabling_earlier_unanchored_card_unblocks_initial_and_loop(self):
        await self.earlier_card_edit('disable')

    async def test_deleting_earlier_unanchored_card_unblocks_initial_and_loop(self):
        await self.earlier_card_edit('delete')

    async def test_reordering_earlier_unanchored_card_respects_new_list_without_premature_loop(self):
        await self.earlier_card_edit('reorder')

    async def test_paid_audience_suppressed_unanchored_card_cannot_deadlock_all_card(self):
        pix = self.seq('P'); pix.update(trigger='pix', target_audience='new')
        await self.config([pix, self.seq('ALL', loop_enabled=True)])
        state = await main._read_state_from_db()
        state['tx_records']['historic'] = dict(tx_id='historic', user_id=7, bot_id='old-panel-id',
            bot_token=self.bot.token, origin_bot_numeric_id='111', status='paid', amount=10,
            created_at='2029-01-01T00:00:00', paid_at='2029-01-01T00:00:00')
        await main._write_state_to_db(state)
        await self.start(); await self.until(['ALL', 'ALL'])
        self.assertNotIn('P', self.messages())

    async def test_reorder_during_single_warm_selects_new_first_pending_card(self):
        reached, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def warm(*args, **kw):
            nonlocal calls
            calls += 1
            if calls == 1:
                reached.set(); await release.wait()
            return None
        self.manager.warm_media_item_in_channel = warm
        self.bot.send_photo = AsyncMock(return_value=SimpleNamespace(photo=[SimpleNamespace(file_id='sent')]))
        await self.config([self.seq('A', medias=[self.media(cached=False)]), self.seq('B')])
        await self.start(); await self.step()
        task = self.manager.downsell_tasks['111:7']
        try:
            await asyncio.wait_for(reached.wait(), 3)
            state = await main._read_state_from_db()
            state['flow_bots'][0]['downsell_general']['sequences'].reverse()
            await main._write_state_to_db(state)
            release.set(); await asyncio.wait_for(task, 5)
            self.assertEqual(self.messages(), ['B', 'A'])
        finally:
            release.set()

    async def test_unguarded_shared_media_caller_retains_normal_fallback(self):
        self.bot.send_photo = AsyncMock(side_effect=RuntimeError('offline photo failure'))
        self.bot.send_document = AsyncMock(return_value=SimpleNamespace(document=SimpleNamespace(file_id='fallback')))
        result = await self.manager._send_single_media_cached(7, self.media(), bot_instance=self.bot)
        self.assertEqual(result, 'offline-photo')
        self.bot.send_photo.assert_awaited_once()
        self.bot.send_document.assert_awaited_once()
