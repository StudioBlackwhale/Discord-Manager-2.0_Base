import asyncio
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import discord
import main
import preset
from resources import ResourceConflict, ResourceManager
from state import SQLiteStateStore, ResourceRecord
from fakes import World, interaction


class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteStateStore(Path(self.tmp.name) / 'state.sqlite3')
        self.manager = ResourceManager(self.store)
        self.w = World()
        self.guild = self.w.guild
        self.patch = patch.multiple(main.bot, store=self.store, resources=self.manager,
                                   setup_locks={}, synced_guilds=set(), command_sync_locks={})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def install(self, world=None):
        world = world or self.w
        i = interaction(world.guild)
        before = deepcopy(world.mutations)
        await main.setup_start.callback(i)
        self.assertEqual(world.mutations, before, 'start preview must never mutate Discord')
        view = i.followup.send.await_args.kwargs['view']
        self.assertIsInstance(view, main.SetupConfirmation)
        await view.confirm(interaction(world.guild))
        self.assertTrue(self.store.load(world.guild.id).installed)
        return view

    async def test_install_check_repeat_start_missing_channel_sync_recovery(self):
        await self.install()
        state = self.store.load(self.guild.id)
        self.assertEqual(len(state.resources), 24)  # 4 roles + 4 categories + 15 channels + guide
        original_ids = {key: rec.id for key, rec in state.resources.items()}
        before = deepcopy(self.w.mutations)
        await main.setup_start.callback(interaction(self.guild))
        self.assertEqual(self.w.mutations, before)
        self.assertEqual(await self.manager.check(self.guild, main.inspect_nickname_guide), [])
        missing = state.resources['channel:chat'].id
        self.w.channel_data.pop(missing)
        self.w.flush()
        before = deepcopy(self.w.mutations)
        issues = await self.manager.check(self.guild, main.inspect_nickname_guide)
        self.assertTrue(any('자유채팅' in issue for issue in issues))
        self.assertEqual(self.w.mutations, before)
        await main.setup_sync.callback(interaction(self.guild))
        self.assertEqual(await self.manager.check(self.guild, main.inspect_nickname_guide), [])
        after = self.store.load(self.guild.id)
        self.assertNotEqual(after.resources['channel:chat'].id, missing)
        for key in original_ids.keys() - {'channel:chat'}:
            self.assertEqual(after.resources[key].id, original_ids[key])

    async def test_check_does_not_create_database_or_mutate_server(self):
        await main.setup_check.callback(interaction(self.guild))
        self.assertFalse(self.store.path.exists())
        self.assertEqual(self.w.mutations, [])

    async def test_setup_has_only_four_subcommands_and_no_bare_action(self):
        self.assertIsInstance(main.setup_group, discord.app_commands.Group)
        self.assertEqual({c.name for c in main.setup_group.commands}, {'start', 'sync', 'check', 'reset'})
        self.assertFalse(hasattr(main.setup_group, 'callback'))

    async def test_fresh_sync_requires_start_confirmation(self):
        await main.setup_sync.callback(interaction(self.guild))
        self.assertEqual(self.w.mutations, [])
        self.assertFalse(self.store.path.exists())

    async def test_user_resources_and_foreign_overwrites_survive_sync_and_reset(self):
        user_role = self.w.user_role('friends')
        user_channel = self.w.add_channel('my-room')
        user_category = self.w.add_channel('my-category', 4)
        await self.install()
        state = self.store.load(self.guild.id)
        managed_channel = state.resources['channel:chat'].id
        extra = [{'id': user_role.id, 'type': 0, 'allow': str(discord.Permissions(send_messages=True).value), 'deny': '0'},
                 {'id': '77', 'type': 1, 'allow': '0', 'deny': str(discord.Permissions(view_channel=True).value)}]
        self.w.channel_data[managed_channel]['permission_overwrites'].extend(extra)
        self.w.channel_data[managed_channel]['topic'] = 'user topic'
        self.w.flush()
        user_state = (deepcopy(self.w.role_data[user_role.id]), deepcopy(self.w.channel_data[user_channel.id]),
                      deepcopy(self.w.channel_data[user_category.id]))
        await main.setup_sync.callback(interaction(self.guild))
        self.assertEqual(self.w.channel_data[managed_channel]['permission_overwrites'][-2:], extra)
        self.assertEqual(self.w.channel_data[managed_channel]['topic'], 'user topic')
        i = interaction(self.guild, channel=user_channel)
        before = deepcopy(self.w.mutations)
        await main.setup_reset.callback(i)
        self.assertEqual(self.w.mutations, before)
        view = i.followup.send.await_args.kwargs['view']
        await view.confirm(interaction(self.guild))
        self.assertEqual((self.w.role_data[user_role.id], self.w.channel_data[user_channel.id], self.w.channel_data[user_category.id]), user_state)
        self.assertFalse(self.store.load(self.guild.id).installed)
        self.assertFalse(self.store.load(self.guild.id).resources)
        # reset does not automatically rebuild; a fresh confirmed start can.
        await self.install()

    async def test_same_name_user_resource_blocks_before_any_write(self):
        for name, kind in [('Manager', 'role'), ('닉네임 제출', 'channel'), ('커뮤니티', 'category')]:
            with self.subTest(name=name):
                world = World(200)
                if kind == 'role':
                    world.user_role(name)
                else:
                    world.add_channel(name, 4 if kind == 'category' else 0)
                with self.assertRaises(ResourceConflict):
                    await self.manager.sync(world.guild, main.ensure_nickname_guide)
                self.assertEqual(world.mutations, [])

    async def test_tracked_id_wins_over_duplicate_name_and_rename(self):
        await self.install()
        original = self.store.load(self.guild.id).resources['channel:chat'].id
        self.w.channel_data[original]['name'] = 'renamed'
        duplicate = self.w.add_channel(preset.CH_CHAT)
        before = deepcopy(self.w.channel_data[duplicate.id])
        await self.manager.sync(self.guild, main.ensure_nickname_guide)
        self.assertEqual(self.w.channel_data[original]['name'], preset.CH_CHAT)
        self.assertEqual(self.w.channel_data[duplicate.id], before)
        plan = await self.manager.reset_plan(self.guild)
        self.assertNotIn(duplicate.id, [r.id for _, r, _ in plan.targets])

    async def test_deleted_tracked_channel_is_not_replaced_by_user_same_name(self):
        await self.install()
        old = self.store.load(self.guild.id).resources['channel:chat'].id
        self.w.channel_data.pop(old)
        duplicate = self.w.add_channel(preset.CH_CHAT)
        before = deepcopy(self.w.mutations)
        with self.assertRaises(ResourceConflict):
            await self.manager.sync(self.guild, main.ensure_nickname_guide)
        self.assertEqual(self.w.mutations, before)
        self.assertEqual(self.store.load(self.guild.id).resources['channel:chat'].id, old)
        self.assertIn(duplicate.id, self.w.channel_data)

    async def test_two_guilds_are_independent_during_parallel_install_and_reset(self):
        other = World(200)
        await asyncio.gather(self.install(), self.install(other))
        first = self.store.load(self.guild.id)
        second = self.store.load(other.guild.id)
        self.assertTrue(set(r.id for r in first.resources.values()).isdisjoint(r.id for r in second.resources.values()))
        other_before = (deepcopy(other.channel_data), deepcopy(other.role_data), self.store.load(other.guild.id))
        await self.manager.reset(self.guild, await self.manager.reset_plan(self.guild))
        self.assertEqual((other.channel_data, other.role_data, self.store.load(other.guild.id)), other_before)
        self.assertEqual(await self.manager.check(other.guild, main.inspect_nickname_guide), [])

    async def test_confirmation_cannot_be_used_by_other_user_or_guild_or_twice(self):
        i = interaction(self.guild)
        await main.setup_start.callback(i)
        view = i.followup.send.await_args.kwargs['view']
        await view.confirm(interaction(self.guild, uid=77))
        await view.confirm(interaction(World(200).guild))
        self.assertEqual(self.w.mutations, [])
        await view.confirm(interaction(self.guild))
        before = deepcopy(self.w.mutations)
        await view.confirm(interaction(self.guild))
        self.assertEqual(self.w.mutations, before)

    async def test_cancel_and_expired_confirmation_do_not_install(self):
        for expired in (False, True):
            i = interaction(self.guild)
            await main.setup_start.callback(i)
            view = i.followup.send.await_args.kwargs['view']
            if expired:
                view.stop()
            else:
                await view.cancel(interaction(self.guild))
            await view.confirm(interaction(self.guild))
        self.assertEqual(self.w.mutations, [])

    async def test_stale_reset_plan_cannot_delete_new_resources(self):
        await self.install()
        plan = await self.manager.reset_plan(self.guild)
        await self.manager.sync(self.guild, main.ensure_nickname_guide)
        before = deepcopy(self.w.mutations)
        with self.assertRaises(ResourceConflict):
            await self.manager.reset(self.guild, plan)
        self.assertEqual(self.w.mutations, before)

    async def test_user_channel_in_dm_category_preserves_category(self):
        await self.install()
        category_id = self.store.load(self.guild.id).resources['category:community'].id
        user_channel = self.w.add_channel('user child', parent=category_id)
        before = deepcopy(self.w.channel_data[user_channel.id])
        plan = await self.manager.reset_plan(self.guild)
        self.assertNotIn(category_id, [r.id for _, r, _ in plan.targets])
        await self.manager.reset(self.guild, plan)
        self.assertEqual(self.w.channel_data[user_channel.id], before)
        self.assertIn(category_id, self.w.channel_data)

    async def test_role_used_by_user_channel_is_preserved(self):
        await self.install()
        viewer_id = self.store.load(self.guild.id).resources['role:viewer'].id
        ch = self.w.add_channel('user permission', overwrites=[{'id': viewer_id, 'type': 0, 'allow': '1024', 'deny': '0'}])
        before = deepcopy(self.w.channel_data[ch.id])
        plan = await self.manager.reset_plan(self.guild)
        self.assertNotIn(viewer_id, [r.id for _, r, _ in plan.targets])
        await self.manager.reset(self.guild, plan)
        self.assertIn(viewer_id, self.w.role_data)
        self.assertEqual(self.w.channel_data[ch.id], before)

    async def test_partial_creation_is_checkpointed_and_retry_does_not_duplicate_roles(self):
        self.w.fail_create = True
        with self.assertRaises(discord.Forbidden):
            await self.manager.sync(self.guild, main.ensure_nickname_guide)
        state = self.store.load(self.guild.id)
        self.assertFalse(state.installed)
        ids = {key: r.id for key, r in state.resources.items()}
        self.assertEqual(len(ids), 4)
        await self.manager.sync(self.guild, main.ensure_nickname_guide)
        for key, rid in ids.items():
            self.assertEqual(self.store.load(self.guild.id).resources[key].id, rid)
        self.assertEqual(len(self.w.role_data), 6)

    async def test_restart_uses_persisted_ids_and_guide_even_with_stale_gateway_cache(self):
        self.w.gateway = False
        await self.install()
        self.manager = ResourceManager(SQLiteStateStore(self.store.path))
        before = [x for x in self.w.mutations if x[0].startswith('create') or x[0] == 'send_message']
        await self.manager.sync(self.guild, main.ensure_nickname_guide)
        after = [x for x in self.w.mutations if x[0].startswith('create') or x[0] == 'send_message']
        self.assertEqual(after, before)
        self.assertEqual(await self.manager.check(self.guild, main.inspect_nickname_guide), [])

    async def test_pin_failure_keeps_message_id_and_retry_does_not_duplicate(self):
        self.w.fail_pin = True
        with self.assertRaises(discord.Forbidden):
            await self.manager.sync(self.guild, main.ensure_nickname_guide)
        rec = self.store.load(self.guild.id).resources[preset.GUIDE_KEY]
        self.assertIn(rec.id, self.w.messages)
        self.assertFalse(self.store.load(self.guild.id).installed)
        self.w.fail_pin = False
        await self.manager.sync(self.guild, main.ensure_nickname_guide)
        self.assertEqual(self.store.load(self.guild.id).resources[preset.GUIDE_KEY].id, rec.id)
        self.assertEqual(len(self.w.messages), 1)

    async def test_delete_failure_preserves_id_and_parent_for_retry(self):
        await self.install()
        state = self.store.load(self.guild.id)
        cid = state.resources['channel:chat'].id
        catid = state.resources['category:community'].id
        self.w.fail_delete.add(cid)
        _, skipped = await self.manager.reset(self.guild, await self.manager.reset_plan(self.guild))
        self.assertTrue(skipped)
        self.assertIn(cid, self.w.channel_data)
        self.assertIn(catid, self.w.channel_data)
        self.assertEqual(self.store.load(self.guild.id).resources['channel:chat'].id, cid)
        self.w.fail_delete.clear()
        await self.manager.reset(self.guild, await self.manager.reset_plan(self.guild))
        self.assertEqual(self.store.load(self.guild.id).resources, {})

    async def test_check_after_manual_guide_deletion_reports_and_sync_repairs(self):
        await self.install()
        old = self.store.load(self.guild.id).resources[preset.GUIDE_KEY].id
        self.w.messages.pop(old)
        before = deepcopy(self.w.mutations)
        self.assertTrue(any('안내' in issue for issue in await self.manager.check(self.guild, main.inspect_nickname_guide)))
        self.assertEqual(self.w.mutations, before)
        await self.manager.sync(self.guild, main.ensure_nickname_guide)
        self.assertNotEqual(self.store.load(self.guild.id).resources[preset.GUIDE_KEY].id, old)
        self.assertEqual(await self.manager.check(self.guild, main.inspect_nickname_guide), [])

    async def test_role_and_category_repairs_preserve_user_role_and_global_permissions(self):
        self.w.role_data[self.guild.id]['permissions'] = str(discord.Permissions(mention_everyone=True).value)
        self.w.flush()
        user_role = self.w.user_role('custom permissions', permissions=1234)
        await self.install()
        state = self.store.load(self.guild.id)
        manager_id = state.resources['role:manager'].id
        category_id = state.resources['category:collab'].id
        self.w.role_data[manager_id]['permissions'] = '0'
        self.w.channel_data[category_id]['permission_overwrites'] = []
        self.w.flush()
        issues = await self.manager.check(self.guild, main.inspect_nickname_guide)
        self.assertTrue(any('Manager' in item and '권한' in item for item in issues))
        self.assertTrue(any('합방' in item and '권한' in item for item in issues))
        await self.manager.sync(self.guild, main.ensure_nickname_guide)
        self.assertEqual(await self.manager.check(self.guild, main.inspect_nickname_guide), [])
        self.assertEqual(int(self.w.role_data[user_role.id]['permissions']), 1234)
        self.assertTrue(self.guild.default_role.permissions.mention_everyone)

    async def test_unknown_origin_record_is_not_edited_deleted_or_used_for_authorization(self):
        custom = self.w.user_role('Manager')
        state = self.store.load(self.guild.id)
        state.resources['role:manager'] = ResourceRecord('role', custom.id, created=False)
        self.store.save(state)
        self.assertIsNone(self.manager.cached(self.guild, 'role:manager'))
        with self.assertRaises(ResourceConflict):
            await self.manager.sync(self.guild, main.ensure_nickname_guide)
        plan = await self.manager.reset_plan(self.guild)
        self.assertEqual(plan.targets, [])
        self.assertTrue(plan.preserved)
        self.assertEqual(self.w.mutations, [])
