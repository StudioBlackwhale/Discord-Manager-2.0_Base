"""In-memory Discord REST backend; tests exercise real discord.py model methods."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord


def http_error(cls=discord.Forbidden):
    return cls(SimpleNamespace(status=404 if cls is discord.NotFound else 403, reason='test'), {'code': 0, 'message': 'offline'})


class World:
    def __init__(self, guild_id=100, *, gateway=True):
        self.id = guild_id
        self.next_id = guild_id * 1000
        self.gateway = gateway
        self.mutations = []
        self.role_data = {}
        self.channel_data = {}
        self.messages = {}
        self.fail_create = False
        self.fail_delete = set()
        self.fail_pin = False
        self.bot_id = 99
        client = discord.Client(intents=discord.Intents.default())
        self.state = client._connection
        self.state.http = self
        self.state.user = discord.ClientUser(state=self.state, data=self.user(self.bot_id, bot=True))
        self.role_data[guild_id] = self.role_payload(guild_id, '@everyone', 0, 0)
        self.role_data[guild_id + 1] = self.role_payload(guild_id + 1, 'bot', 100, 8, managed=True)
        self.guild = discord.Guild(state=self.state, data={
            'id': str(guild_id), 'name': f'guild-{guild_id}', 'owner_id': '88',
            'roles': list(self.role_data.values()), 'channels': [],
            'members': [{'user': self.user(self.bot_id, bot=True), 'roles': [str(guild_id + 1)], 'flags': 0},
                        {'user': self.user(88), 'roles': [], 'flags': 0}],
        })
        self.state._guilds[guild_id] = self.guild

    @staticmethod
    def user(uid, bot=False):
        return {'id': str(uid), 'username': f'user-{uid}', 'discriminator': '0', 'avatar': None, 'bot': bot}

    @staticmethod
    def role_payload(rid, name, position=1, permissions=0, managed=False):
        return {'id': str(rid), 'name': name, 'position': position, 'permissions': str(permissions), 'managed': managed}

    def new_id(self):
        self.next_id += 1
        return self.next_id

    def user_role(self, name, permissions=0):
        rid = self.new_id()
        self.role_data[rid] = self.role_payload(rid, name, permissions=permissions)
        self.flush()
        return self.guild.get_role(rid)

    def add_channel(self, name, kind=0, parent=None, overwrites=None):
        cid = self.new_id()
        self.channel_data[cid] = {
            'id': str(cid), 'name': name, 'type': kind, 'position': len(self.channel_data),
            'parent_id': parent, 'permission_overwrites': overwrites or [], 'topic': None,
            'bitrate': 64000, 'user_limit': 0,
        }
        self.flush()
        return self.guild.get_channel(cid)

    def flush(self):
        self.guild._roles = {rid: discord.Role(guild=self.guild, state=self.state, data=deepcopy(data))
                             for rid, data in self.role_data.items()}
        classes = {0: discord.TextChannel, 2: discord.VoiceChannel, 4: discord.CategoryChannel}
        self.guild._channels = {cid: classes[data['type']](guild=self.guild, state=self.state, data=deepcopy(data))
                                for cid, data in self.channel_data.items()}

    def tick(self):
        if self.gateway:
            self.flush()

    async def get_roles(self, gid):
        assert gid == self.id
        return deepcopy(list(self.role_data.values()))

    async def get_all_guild_channels(self, gid):
        assert gid == self.id
        return deepcopy(list(self.channel_data.values()))

    async def create_role(self, gid, **kwargs):
        self.mutations.append(('create_role', kwargs['name']))
        rid = self.new_id()
        self.role_data[rid] = self.role_payload(rid, kwargs['name'], permissions=int(kwargs['permissions']))
        self.tick()
        return deepcopy(self.role_data[rid])

    async def edit_role(self, gid, rid, **kwargs):
        self.mutations.append(('edit_role', rid))
        self.role_data[rid].update({k: v for k, v in kwargs.items() if k != 'reason'})
        self.tick()
        return deepcopy(self.role_data[rid])

    async def create_channel(self, gid, channel_type, **kwargs):
        if self.fail_create:
            self.fail_create = False
            raise http_error()
        self.mutations.append(('create_channel', kwargs['name']))
        cid = self.new_id()
        self.channel_data[cid] = {
            'id': str(cid), 'type': channel_type, 'position': len(self.channel_data),
            'parent_id': None, 'permission_overwrites': [], 'topic': None, 'bitrate': 64000, 'user_limit': 0,
            **{k: v for k, v in kwargs.items() if k != 'reason'},
        }
        self.tick()
        return deepcopy(self.channel_data[cid])

    async def edit_channel(self, cid, **kwargs):
        self.mutations.append(('edit_channel', cid))
        self.channel_data[cid].update({k: v for k, v in kwargs.items() if k not in {'reason', 'overwrites'}})
        self.tick()
        return deepcopy(self.channel_data[cid])

    async def bulk_channel_update(self, gid, payload, **kwargs):
        self.mutations.append(('order', tuple(p['id'] for p in payload)))
        for p in payload:
            self.channel_data[p['id']]['position'] = p['position']
        self.tick()

    async def edit_guild(self, gid, **kwargs):
        self.mutations.append(('edit_guild', kwargs))
        if 'system_channel_id' in kwargs:
            self.guild._system_channel_id = kwargs['system_channel_id']
        return {'id': str(gid), 'name': self.guild.name, 'roles': list(self.role_data.values()),
                'owner_id': '88', 'system_channel_id': self.guild._system_channel_id}

    async def add_role(self, gid, uid, rid, **kwargs):
        self.mutations.append(('assign_role', uid, rid))
        member = self.guild.get_member(uid)
        if member is not None:
            member._roles.add(rid)

    async def remove_role(self, gid, uid, rid, **kwargs):
        self.guild.get_member(uid)._roles.remove(rid)

    async def delete_channel(self, cid, **kwargs):
        if cid in self.fail_delete:
            raise http_error()
        self.mutations.append(('delete_channel', cid))
        self.channel_data.pop(cid, None)
        self.messages = {mid: data for mid, data in self.messages.items() if int(data['channel_id']) != cid}
        self.tick()

    async def delete_role(self, gid, rid, **kwargs):
        self.mutations.append(('delete_role', rid))
        self.role_data.pop(rid, None)
        self.tick()

    async def send_message(self, cid, *, params):
        self.mutations.append(('send_message', cid))
        mid = self.new_id()
        payload = params.payload
        self.messages[mid] = {
            'id': str(mid), 'channel_id': str(cid), 'type': 0,
            'author': self.user(self.bot_id, bot=True), 'content': payload.get('content', ''),
            'components': payload.get('components', []), 'attachments': [], 'embeds': [],
            'mentions': [], 'mention_roles': [], 'edited_timestamp': None, 'pinned': False,
            'mention_everyone': False, 'tts': False,
        }
        return deepcopy(self.messages[mid])

    async def get_message(self, cid, mid):
        if mid not in self.messages or int(self.messages[mid]['channel_id']) != cid:
            raise http_error(discord.NotFound)
        return deepcopy(self.messages[mid])

    async def edit_message(self, cid, mid, *, params):
        self.mutations.append(('edit_message', mid))
        self.messages[mid].update(params.payload)
        return deepcopy(self.messages[mid])

    async def delete_message(self, cid, mid, **kwargs):
        self.mutations.append(('delete_message', mid))
        self.messages.pop(mid, None)

    async def pin_message(self, cid, mid, **kwargs):
        if self.fail_pin:
            raise http_error()
        self.mutations.append(('pin', mid))
        self.messages[mid]['pinned'] = True

    async def pins_from(self, channel_id, **kwargs):
        cid = channel_id
        messages = [deepcopy(m) for m in self.messages.values() if int(m['channel_id']) == cid and m['pinned']]
        # discord.py 2.6+ uses the paginated endpoint; 2.4 expects a list.
        if tuple(map(int, discord.__version__.split('.')[:2])) >= (2, 6):
            return {'items': [{'message': m, 'pinned_at': '2026-09-15T00:00:00+00:00'} for m in messages], 'has_more': False}
        return messages

    async def logs_from(self, cid, limit, before=None, after=None, around=None):
        messages = [deepcopy(m) for m in self.messages.values() if int(m['channel_id']) == cid]
        return sorted(messages, key=lambda m: int(m['id']), reverse=True)[:limit]


def interaction(guild, uid=88, channel=None):
    response = SimpleNamespace(is_done=MagicMock(return_value=False), send_message=AsyncMock(),
                               send_modal=AsyncMock(), edit_message=AsyncMock())
    async def defer(**kwargs):
        response.is_done.return_value = True
    response.defer = AsyncMock(side_effect=defer)
    user = guild.get_member(uid)
    if user is None:
        user = discord.Member(state=guild._state, guild=guild,
                              data={'user': World.user(uid), 'roles': [], 'flags': 0})
    return SimpleNamespace(guild=guild, user=user, channel=channel, response=response,
                           followup=SimpleNamespace(send=AsyncMock()))
