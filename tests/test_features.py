from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import discord
import main
import preset
from resources import ResourceManager
from state import SQLiteStateStore, StateError
from fakes import World, interaction


class FeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.w = World()
        self.store = SQLiteStateStore(Path(self.tmp.name) / 'state.sqlite3')
        self.manager = ResourceManager(self.store)
        self.patcher = patch.multiple(main.bot, store=self.store, resources=self.manager, setup_locks={})
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        await self.manager.sync(self.w.guild, main.ensure_nickname_guide)

    async def test_button_and_slash_use_same_modal_and_persistent_startup_is_once(self):
        button, slash = interaction(self.w.guild), interaction(self.w.guild)
        await main.NicknameSubmissionView().submit(button)
        await main.nickname_submit.callback(slash)
        self.assertIs(type(button.response.send_modal.await_args.args[0]), main.NicknameModal)
        self.assertIs(type(slash.response.send_modal.await_args.args[0]), main.NicknameModal)
        client = main.DiscordManager(store=self.store)
        with patch.object(client, 'add_view', wraps=client.add_view) as add:
            await client.setup_hook()
            await client.setup_hook()
        add.assert_called_once()
        self.assertEqual(client.persistent_views[0].children[0].custom_id, main.NICKNAME_SUBMIT_CUSTOM_ID)
        await client.close()

    async def test_nickname_submission_keeps_private_db_and_detailed_audit_and_fallback(self):
        state = self.store.load(self.w.id)
        db = state.resources['channel:nickname_db'].id
        audit = state.resources['channel:audit_log'].id
        duplicate = self.w.add_channel(preset.CH_NICKLOG)
        for fallback in (False, True):
            if fallback:
                self.w.channel_data.pop(db)
                self.w.flush()
            modal = main.NicknameModal()
            modal.nickname._value = 'player#1234'
            modal.game._value = 'Game' if not fallback else ''
            i = interaction(self.w.guild, uid=77)
            before = set(self.w.messages)
            await modal.on_submit(i)
            messages = [m for mid, m in self.w.messages.items() if mid not in before]
            self.assertEqual([int(m['channel_id']) for m in messages], [audit] if fallback else [db, audit])
            self.assertTrue(i.response.defer.await_args.kwargs['ephemeral'])
            self.assertTrue(i.followup.send.await_args.kwargs['ephemeral'])
            self.assertIn('UTC+9', messages[-1]['content'])
            self.assertIn('player#1234', messages[-1]['content'])
            self.assertNotIn(duplicate.id, [int(m['channel_id']) for m in messages])

    async def test_member_auto_role_and_audit_route_by_id_after_rename(self):
        state = self.store.load(self.w.id)
        viewer = state.resources['role:viewer'].id
        audit = state.resources['channel:audit_log'].id
        self.w.role_data[viewer]['name'] = 'renamed viewer'
        self.w.channel_data[audit]['name'] = 'renamed log'
        self.w.flush()
        duplicate = self.w.user_role(preset.ROLE_VIEWER)
        user = interaction(self.w.guild, uid=77).user
        with patch.object(discord.Member, 'add_roles', new_callable=AsyncMock) as add_roles:
            await main.on_member_join(user)
        self.assertEqual(add_roles.await_args.args[0].id, viewer)
        self.assertNotEqual(add_roles.await_args.args[0].id, duplicate.id)
        self.assertTrue(any(int(m['channel_id']) == audit and '서버 입장' in m['content'] for m in self.w.messages.values()))

    async def test_broadcast_routes_to_id_and_blocks_mass_mentions(self):
        rec = self.store.load(self.w.id).resources['channel:broadcast']
        self.w.channel_data[rec.id]['name'] = 'renamed broadcast'
        self.w.flush()
        duplicate = self.w.add_channel(preset.CH_BROADCAST)
        deliveries = []
        async def send(channel, content, **kwargs):
            deliveries.append((channel.id, content, kwargs['allowed_mentions']))
        with patch.object(discord.TextChannel, 'send', new=send):
            await main.broadcast.callback(interaction(self.w.guild), 'hello')
        self.assertEqual(deliveries[0][0], rec.id)
        self.assertNotEqual(deliveries[0][0], duplicate.id)
        self.assertFalse(deliveries[0][2].everyone)
        self.assertFalse(deliveries[0][2].roles)
        self.assertTrue(deliveries[0][2].users)

    async def test_nickname_effective_permissions_for_viewer_and_streamer_are_read_only(self):
        state = self.store.load(self.w.id)
        channel = self.w.guild.get_channel(state.resources['channel:nickname'].id)
        for keys in ([], ['viewer'], ['streamer'], ['viewer', 'streamer']):
            member = interaction(self.w.guild, uid=77).user
            member._roles = discord.utils.SnowflakeList(state.resources['role:' + key].id for key in keys)
            perm = channel.permissions_for(member)
            self.assertTrue(perm.view_channel)
            self.assertTrue(perm.read_message_history)
            self.assertFalse(perm.send_messages)
            self.assertFalse(perm.create_public_threads)
            self.assertFalse(perm.create_private_threads)
            self.assertFalse(perm.send_messages_in_threads)

    async def test_confirmation_rechecks_manager_role_membership(self):
        state = self.store.load(self.w.id)
        manager_id = state.resources['role:manager'].id
        initiator = interaction(self.w.guild, uid=77)
        initiator.user._roles.add(manager_id)
        await main.setup_reset.callback(initiator)
        view = initiator.followup.send.await_args.kwargs['view']
        before = deepcopy(self.w.mutations)
        await view.confirm(interaction(self.w.guild, uid=77))
        self.assertEqual(self.w.mutations, before)

    async def test_untracked_manager_name_does_not_authorize_setup(self):
        user_role = self.w.user_role('Manager')
        i = interaction(self.w.guild, uid=77)
        i.user._roles.add(user_role.id)
        self.assertFalse(await main.require_setup_operator(i))

    async def test_same_guild_concurrent_sync_is_rejected(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def sync(*args):
            started.set()
            await release.wait()
            return 0
        with patch.object(self.manager, 'sync', new=AsyncMock(side_effect=sync)) as mocked:
            first = asyncio.create_task(main.setup_sync.callback(interaction(self.w.guild)))
            await asyncio.wait_for(started.wait(), 1)
            second = interaction(self.w.guild)
            await main.setup_sync.callback(second)
            release.set()
            await first
        mocked.assert_awaited_once()
        self.assertIn('진행 중', second.response.send_message.await_args.args[0])

    async def test_check_does_not_write_existing_state_or_repair_modified_guide(self):
        rec = self.store.load(self.w.id).resources[preset.GUIDE_KEY]
        self.w.messages[rec.id]['content'] = 'old guide'
        self.w.messages[rec.id]['components'] = []
        before = deepcopy(self.w.mutations), self.store.path.read_bytes(), self.store.path.stat().st_mtime_ns
        i = interaction(self.w.guild)
        await main.setup_check.callback(i)
        self.assertIn('안내', i.followup.send.await_args.args[0])
        self.assertEqual((self.w.mutations, self.store.path.read_bytes(), self.store.path.stat().st_mtime_ns), before)
        await self.manager.sync(self.w.guild, main.ensure_nickname_guide)
        self.assertEqual(self.store.load(self.w.id).resources[preset.GUIDE_KEY].id, rec.id)
        self.assertEqual(self.w.messages[rec.id]['content'], main.NICKNAME_GUIDE_TEXT)

    async def test_state_save_failure_aborts_before_discord_writes(self):
        before = deepcopy(self.w.mutations)
        with patch.object(self.store, 'save', side_effect=StateError('read only')):
            with self.assertRaises(StateError):
                await self.manager.sync(self.w.guild, main.ensure_nickname_guide)
        self.assertEqual(self.w.mutations, before)
