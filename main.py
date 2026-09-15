import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from dotenv import load_dotenv

import config

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger(config.BOT_LOGGER_NAME)
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
MAX_MESSAGE = 2000

ROLE_OWNER = config.ROLE_NAMES["owner"]
ROLE_MANAGER = config.ROLE_NAMES["manager"]
ROLE_STREAMER = config.ROLE_NAMES["streamer"]
ROLE_VIEWER = config.ROLE_NAMES["viewer"]
PROTECTED_ROLES = {config.ROLE_NAMES[key] for key in config.PROTECTED_ROLE_KEYS}

CAT_INFO = config.CATEGORY_NAMES["info"]
CAT_COMMUNITY = config.CATEGORY_NAMES["community"]
CAT_COLLAB = config.CATEGORY_NAMES["collab"]
CAT_OPS = config.CATEGORY_NAMES["ops"]

CH_RULES = config.CHANNEL_NAMES["rules"]
CH_NOTICE = config.CHANNEL_NAMES["notice"]
CH_BROADCAST = config.CHANNEL_NAMES["broadcast"]
CH_CHAT = config.CHANNEL_NAMES["chat"]
CH_MEDIA = config.CHANNEL_NAMES["media"]
CH_NICKNAME = config.CHANNEL_NAMES["nickname"]
CH_WAITING = config.CHANNEL_NAMES["waiting"]
CH_PART = {
    1: config.CHANNEL_NAMES["participation_1"],
    2: config.CHANNEL_NAMES["participation_2"],
}
CH_COLLAB_TEXT = config.CHANNEL_NAMES["collab_text"]
CH_COLLAB = {
    1: config.CHANNEL_NAMES["collab_1"],
    2: config.CHANNEL_NAMES["collab_2"],
}
CH_STAFF = config.CHANNEL_NAMES["staff"]
CH_NICKLOG = config.CHANNEL_NAMES["nickname_db"]
CH_LOG = config.CHANNEL_NAMES["audit_log"]
OPS_CHANNEL_ORDER = (CH_STAFF, CH_NICKLOG, CH_LOG)

NICKNAME_SUBMIT_CUSTOM_ID = config.NICKNAME_SUBMIT_CUSTOM_ID
NICKNAME_BUTTON_LABEL = config.NICKNAME_BUTTON_LABEL
NICKNAME_GUIDE_TEXT = config.NICKNAME_GUIDE_TEXT
GUIDE_SCAN_LIMIT = config.GUIDE_SCAN_LIMIT
GUIDE_LOOKUP_FAILED = "닉네임 안내 상태를 확인하지 못했습니다. Discord API 오류."


@dataclass(frozen=True)
class CategorySpec:
    name: str
    key: str


@dataclass(frozen=True)
class ChannelSpec:
    name: str
    category: str
    key: str
    is_voice: bool = False
    topic: str = ""


CATEGORY_SPECS = (
    CategorySpec(CAT_INFO, "public_cat"),
    CategorySpec(CAT_COMMUNITY, "public_cat"),
    CategorySpec(CAT_COLLAB, "collab_cat"),
    CategorySpec(CAT_OPS, "ops_cat"),
)

CHANNEL_SPECS = (
    ChannelSpec(CH_RULES, CAT_INFO, "readonly"),
    ChannelSpec(CH_NOTICE, CAT_INFO, "announcement"),
    ChannelSpec(CH_BROADCAST, CAT_INFO, "announcement"),
    ChannelSpec(CH_CHAT, CAT_COMMUNITY, "normal"),
    ChannelSpec(CH_MEDIA, CAT_COMMUNITY, "normal"),
    ChannelSpec(
        CH_NICKNAME,
        CAT_COMMUNITY,
        "nickname",
        topic=config.CHANNEL_TOPICS.get("nickname", ""),
    ),
    ChannelSpec(CH_WAITING, CAT_COMMUNITY, "waiting", is_voice=True),
    ChannelSpec(CH_PART[1], CAT_COMMUNITY, "participation", is_voice=True),
    ChannelSpec(CH_PART[2], CAT_COMMUNITY, "participation", is_voice=True),
    ChannelSpec(CH_COLLAB_TEXT, CAT_COLLAB, "collab_text"),
    ChannelSpec(CH_COLLAB[1], CAT_COLLAB, "collab_voice", is_voice=True),
    ChannelSpec(CH_COLLAB[2], CAT_COLLAB, "collab_voice", is_voice=True),
    ChannelSpec(CH_STAFF, CAT_OPS, "operations"),
    ChannelSpec(CH_NICKLOG, CAT_OPS, "operations"),
    ChannelSpec(CH_LOG, CAT_OPS, "operations"),
)
SPEC_BY_NAME = {spec.name: spec for spec in CHANNEL_SPECS}


class DiscordManager(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.synced_guilds = set()
        self.setup_locks = {}
        self.command_sync_locks = {}
        self._nickname_view_registered = False

    async def setup_hook(self):
        if not self._nickname_view_registered:
            self.add_view(NicknameSubmissionView())
            self._nickname_view_registered = True


bot = DiscordManager()


def role(guild, name, excluded_ids=()):
    return next(
        (r for r in guild.roles if r.id not in excluded_ids and r.name == name),
        None,
    )


def text_key(name):
    # Discord 텍스트 채널은 공백을 하이픈으로 정규화할 수 있다.
    return "-".join(name.strip().split()).casefold()


def text(guild, name, excluded_ids=()):
    channels = [ch for ch in guild.text_channels if ch.id not in excluded_ids]
    exact = discord.utils.get(channels, name=name)
    if exact:
        return exact
    key = text_key(name)
    return next((ch for ch in channels if text_key(ch.name) == key), None)


def voice(guild, name, excluded_ids=()):
    return next(
        (ch for ch in guild.voice_channels if ch.id not in excluded_ids and ch.name == name),
        None,
    )


def find_channel(guild, spec, excluded_ids=()):
    finder = voice if spec.is_voice else text
    return finder(guild, spec.name, excluded_ids)


async def respond(interaction, message):
    if len(message) > MAX_MESSAGE:
        message = message[: MAX_MESSAGE - 1] + "…"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.NotFound:
        log.warning("Interaction expired before response delivery: %s", message[:200])
    except discord.HTTPException:
        log.exception("Failed to deliver interaction response")


@asynccontextmanager
async def setup_operation(interaction):
    lock = bot.setup_locks.setdefault(interaction.guild.id, asyncio.Lock())
    if lock.locked():
        await respond(
            interaction,
            "이 서버에서 다른 /setup 작업이 진행 중입니다. 완료 후 다시 실행해 주세요.",
        )
        yield False
        return
    async with lock:
        await interaction.response.defer(ephemeral=True, thinking=True)
        yield True


def elevated(interaction):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.id == interaction.guild.owner_id:
        return True
    if interaction.user.guild_permissions.administrator:
        return True
    owner = role(interaction.guild, ROLE_OWNER)
    manager = role(interaction.guild, ROLE_MANAGER)
    return any(r and r in interaction.user.roles for r in (owner, manager))


async def require_elevated(interaction):
    if elevated(interaction):
        return True
    await respond(interaction, "이 명령은 서버 소유자 또는 관리자만 사용할 수 있습니다.")
    return False


async def require_setup_operator(interaction):
    if interaction.guild is not None and isinstance(interaction.user, discord.Member):
        if interaction.user.id == interaction.guild.owner_id:
            return True
        manager = role(interaction.guild, ROLE_MANAGER)
        if manager and manager in interaction.user.roles:
            return True
    await respond(interaction, "/setup 명령은 서버 소유자 또는 Manager만 사용할 수 있습니다.")
    return False


async def write_audit(guild, message):
    channel = text(guild, CH_LOG)
    if not channel:
        return
    try:
        await channel.send(
            message[:MAX_MESSAGE],
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        log.exception("Failed audit log in guild %s", guild.id)


async def audit(interaction, message):
    if interaction.guild:
        await write_audit(
            interaction.guild,
            f"`{interaction.user} ({interaction.user.id})` · {message}",
        )


def manageable(guild, target_role):
    return bool(guild.me and target_role < guild.me.top_role)


# ---------------------------------------------------------------- 권한 덮어쓰기

def managed_targets(guild, roles=None):
    if roles is None:
        roles = [role(guild, name) for name in PROTECTED_ROLES]
    return {guild.default_role} | {r for r in roles if r is not None}


def merge_overwrites(current, desired, managed, excluded_ids=()):
    # 멤버 개인 예외와 사용자가 별도로 만든 역할 권한은 보존한다.
    merged = {
        target: overwrite
        for target, overwrite in current.items()
        if target not in managed and target.id not in excluded_ids
    }
    merged.update(desired)
    return merged


def managed_overwrites_differ(channel, desired, managed):
    current = {t: o for t, o in channel.overwrites.items() if t in managed}
    return current != desired


def overwrite_sets(guild, streamer=None, viewer=None):
    everyone = guild.default_role
    streamer = streamer if streamer is not None else role(guild, ROLE_STREAMER)
    viewer = viewer if viewer is not None else role(guild, ROLE_VIEWER)
    OW = discord.PermissionOverwrite

    def pairs(*items):
        return {target: overwrite for target, overwrite in items if target is not None}

    no_write = dict(
        send_messages=False,
        add_reactions=False,
        mention_everyone=False,
        create_public_threads=False,
        create_private_threads=False,
        send_messages_in_threads=False,
    )
    viewer_reactions = dict(
        add_reactions=True,
        mention_everyone=False,
        send_messages=False,
        create_public_threads=False,
        create_private_threads=False,
        send_messages_in_threads=False,
    )

    return {
        "public_cat": pairs(
            (everyone, OW(view_channel=True, mention_everyone=False)),
            (viewer, OW(add_reactions=True, mention_everyone=False)),
        ),
        "collab_cat": pairs(
            (everyone, OW(view_channel=False, mention_everyone=False)),
            (streamer, OW(view_channel=True, mention_everyone=False)),
        ),
        "ops_cat": pairs(
            (everyone, OW(view_channel=False, mention_everyone=False)),
            (streamer, OW(view_channel=False, mention_everyone=False)),
        ),
        "readonly": pairs(
            (everyone, OW(view_channel=True, read_message_history=True, **no_write)),
            (viewer, OW(**viewer_reactions)),
        ),
        "announcement": pairs(
            (everyone, OW(view_channel=True, read_message_history=True, **no_write)),
            (viewer, OW(**viewer_reactions)),
        ),
        "normal": pairs(
            (
                everyone,
                OW(
                    view_channel=True,
                    read_message_history=True,
                    send_messages=True,
                    add_reactions=True,
                    mention_everyone=False,
                ),
            ),
            (viewer, OW(add_reactions=True, mention_everyone=False)),
        ),
        "nickname": pairs(
            (everyone, OW(view_channel=True, read_message_history=True, **no_write)),
            (viewer, OW(**viewer_reactions)),
            (streamer, OW(view_channel=True, read_message_history=True, **no_write)),
        ),
        "waiting": pairs(
            (
                everyone,
                OW(
                    view_channel=True,
                    connect=True,
                    speak=False,
                    stream=False,
                    send_messages=True,
                    mention_everyone=False,
                ),
            ),
            (viewer, OW(add_reactions=True, mention_everyone=False)),
        ),
        "participation": pairs(
            (
                everyone,
                OW(
                    view_channel=False,
                    connect=True,
                    speak=True,
                    stream=False,
                    send_messages=True,
                    mention_everyone=False,
                ),
            ),
            (
                streamer,
                OW(
                    view_channel=False,
                    connect=True,
                    speak=True,
                    stream=False,
                    send_messages=True,
                    mention_everyone=False,
                ),
            ),
        ),
        "collab_text": pairs(
            (everyone, OW(view_channel=False, send_messages=False, mention_everyone=False)),
            (
                streamer,
                OW(
                    view_channel=True,
                    read_message_history=True,
                    send_messages=True,
                    mention_everyone=False,
                ),
            ),
        ),
        "collab_voice": pairs(
            (
                everyone,
                OW(
                    view_channel=False,
                    connect=False,
                    speak=False,
                    stream=False,
                    mention_everyone=False,
                ),
            ),
            (
                streamer,
                OW(
                    view_channel=True,
                    connect=True,
                    speak=True,
                    stream=False,
                    send_messages=True,
                    mention_everyone=False,
                ),
            ),
        ),
        "operations": pairs(
            (everyone, OW(view_channel=False, send_messages=False, mention_everyone=False)),
            (streamer, OW(view_channel=False, send_messages=False, mention_everyone=False)),
        ),
    }


def expected_role_permissions():
    return {
        ROLE_OWNER: discord.Permissions(administrator=True),
        ROLE_MANAGER: discord.Permissions(administrator=True),
        ROLE_STREAMER: discord.Permissions.none(),
        ROLE_VIEWER: discord.Permissions(add_reactions=True),
    }


# ---------------------------------------------------------------- 구조 생성/동기화

async def ensure_role(guild, name, permissions, excluded_ids=()):
    target = role(guild, name, excluded_ids)
    if target is None:
        return (
            await guild.create_role(
                name=name,
                permissions=permissions,
                reason="Discord Manager /setup",
            ),
            True,
        )
    if target.permissions != permissions:
        if manageable(guild, target):
            target = await target.edit(
                permissions=permissions,
                reason="Discord Manager /setup",
            )
        else:
            log.warning("Role %s is above the bot role; permission edit skipped", name)
    return target, False


async def ensure_category(guild, spec, overwrites, excluded_ids=(), managed=frozenset()):
    categories = [ch for ch in guild.categories if ch.id not in excluded_ids]
    channel = discord.utils.get(categories, name=spec.name)
    if channel is None:
        return (
            await guild.create_category(
                spec.name,
                overwrites=overwrites,
                reason="Discord Manager /setup",
            ),
            True,
        )
    return (
        await channel.edit(
            overwrites=merge_overwrites(
                channel.overwrites,
                overwrites,
                managed,
                excluded_ids,
            ),
            reason="Discord Manager /setup",
        ),
        False,
    )


def topic_kwargs(spec):
    return {"topic": spec.topic} if spec.topic and not spec.is_voice else {}


async def ensure_channel(guild, spec, category, overwrites, excluded_ids=(), managed=frozenset()):
    channel = find_channel(guild, spec, excluded_ids)
    if channel is None:
        creator = guild.create_voice_channel if spec.is_voice else guild.create_text_channel
        return (
            await creator(
                spec.name,
                category=category,
                overwrites=overwrites,
                reason="Discord Manager /setup",
                **topic_kwargs(spec),
            ),
            True,
        )
    return (
        await channel.edit(
            category=category,
            overwrites=merge_overwrites(
                channel.overwrites,
                overwrites,
                managed,
                excluded_ids,
            ),
            reason="Discord Manager /setup",
            **topic_kwargs(spec),
        ),
        False,
    )


async def ensure_log(guild):
    channel = text(guild, CH_LOG)
    if channel is None:
        channel = await guild.create_text_channel(
            CH_LOG,
            overwrites={
                guild.default_role: discord.PermissionOverwrite(view_channel=False)
            },
            reason="Discord Manager persistent audit log",
        )
    if channel.category:
        channel = await channel.edit(
            category=None,
            reason="Discord Manager preserve audit log",
        )
    return channel


def media_is_misplaced(community, chat, media):
    if community is None or chat is None or media is None:
        return False
    ordered = community.text_channels
    if chat not in ordered or media not in ordered:
        return False
    return ordered.index(media) != ordered.index(chat) + 1


def ops_order_is_wrong(ops, channels):
    if ops is None or any(ch is None for ch in channels):
        return False
    ordered = ops.text_channels
    if any(ch not in ordered for ch in channels):
        return False
    indexes = [ordered.index(ch) for ch in channels]
    return indexes != list(range(indexes[0], indexes[0] + len(indexes))) or indexes[0] != 0


async def enforce_ops_order(ops, channels):
    managed_ids = {ch.id for ch in channels}
    ordered = list(channels) + [ch for ch in ops.text_channels if ch.id not in managed_ids]
    payload = [
        {"id": channel.id, "position": position}
        for position, channel in enumerate(ordered)
    ]
    # discord.py에 공개 bulk 정렬 API가 없어 REST 래퍼를 사용한다.
    await ops._state.http.bulk_channel_update(
        ops.guild.id,
        payload,
        reason="Discord Manager operations ordering",
    )


async def build_structure(guild, preserved_log=None, excluded_ids=()):
    role_objects = {}
    created = 0
    for name, permissions in expected_role_permissions().items():
        target, made = await ensure_role(guild, name, permissions, excluded_ids)
        role_objects[name] = target
        created += int(made)

    if guild.default_role.permissions.mention_everyone:
        permissions = discord.Permissions(guild.default_role.permissions.value)
        permissions.update(mention_everyone=False)
        await guild.default_role.edit(
            permissions=permissions,
            reason="Discord Manager block mass mentions for @everyone",
        )

    owner_role = role_objects[ROLE_OWNER]
    if guild.owner and owner_role not in guild.owner.roles and manageable(guild, owner_role):
        try:
            await guild.owner.add_roles(owner_role, reason="Discord Manager owner role")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not assign Owner role to guild owner")

    overwrites = overwrite_sets(
        guild,
        role_objects[ROLE_STREAMER],
        role_objects[ROLE_VIEWER],
    )
    managed = managed_targets(guild, list(role_objects.values()))

    categories = {}
    for spec in CATEGORY_SPECS:
        category, made = await ensure_category(
            guild,
            spec,
            overwrites[spec.key],
            excluded_ids,
            managed,
        )
        categories[spec.name] = category
        created += int(made)

    built = {}
    for spec in CHANNEL_SPECS:
        if spec.name == CH_LOG and preserved_log is not None:
            built[spec.name] = await preserved_log.edit(
                category=categories[spec.category],
                overwrites=merge_overwrites(
                    preserved_log.overwrites,
                    overwrites[spec.key],
                    managed,
                    excluded_ids,
                ),
                reason="Discord Manager restore audit log",
            )
            continue
        channel, made = await ensure_channel(
            guild,
            spec,
            categories[spec.category],
            overwrites[spec.key],
            excluded_ids,
            managed,
        )
        built[spec.name] = channel
        created += int(made)

    if media_is_misplaced(
        categories[CAT_COMMUNITY],
        built[CH_CHAT],
        built[CH_MEDIA],
    ):
        await built[CH_MEDIA].move(
            after=built[CH_CHAT],
            category=categories[CAT_COMMUNITY],
            sync_permissions=False,
            reason="Discord Manager channel ordering",
        )

    ops_channels = [built[name] for name in OPS_CHANNEL_ORDER]
    if ops_order_is_wrong(categories[CAT_OPS], ops_channels):
        await enforce_ops_order(categories[CAT_OPS], ops_channels)

    await guild.edit(
        system_channel=built[CH_LOG],
        reason="Discord Manager /setup",
    )
    return created, built[CH_NICKNAME]


# ---------------------------------------------------------------- 상태 점검

def sync_change_counts(guild):
    server_permissions = int(guild.default_role.permissions.mention_everyone)
    role_permissions = 0
    for name, permissions in expected_role_permissions().items():
        target = role(guild, name)
        if target and target.permissions != permissions and manageable(guild, target):
            role_permissions += 1

    channel_permissions = 0
    overwrites = overwrite_sets(guild)
    managed = managed_targets(guild)
    for spec in CATEGORY_SPECS:
        category = discord.utils.get(guild.categories, name=spec.name)
        if category and managed_overwrites_differ(category, overwrites[spec.key], managed):
            channel_permissions += 1
    for spec in CHANNEL_SPECS:
        channel = find_channel(guild, spec)
        if channel and managed_overwrites_differ(channel, overwrites[spec.key], managed):
            channel_permissions += 1
    return server_permissions, role_permissions, channel_permissions


def skipped_role_changes(guild):
    skipped = []
    for name, permissions in expected_role_permissions().items():
        target = role(guild, name)
        if target and target.permissions != permissions and not manageable(guild, target):
            skipped.append(target.name)
    return skipped


async def preview(guild):
    changes = []
    if guild.default_role.permissions.mention_everyone:
        changes.append("역할 `@everyone`의 @everyone/@here 멘션 권한 차단")

    for name, permissions in expected_role_permissions().items():
        target = role(guild, name)
        if not target:
            changes.append(f"역할 `{name}` 생성")
        elif target.permissions != permissions:
            suffix = " (봇 역할보다 위라 자동 수정 불가)" if not manageable(guild, target) else ""
            changes.append(f"역할 `{name}` 권한 수정{suffix}")

    for spec in CATEGORY_SPECS:
        if not discord.utils.get(guild.categories, name=spec.name):
            changes.append(f"분류 `{spec.name}` 생성")

    for spec in CHANNEL_SPECS:
        channel = find_channel(guild, spec)
        if not channel:
            changes.append(f"채널 `{spec.name}` 생성")
            continue
        if not channel.category or channel.category.name != spec.category:
            changes.append(f"채널 `{spec.name}`을 `{spec.category}` 분류로 이동")
        if spec.topic and getattr(channel, "topic", None) != spec.topic:
            changes.append(f"채널 `{spec.name}` 안내문 설정")

    overwrites = overwrite_sets(guild)
    managed = managed_targets(guild)
    for spec in CATEGORY_SPECS:
        category = discord.utils.get(guild.categories, name=spec.name)
        if category and managed_overwrites_differ(category, overwrites[spec.key], managed):
            changes.append(f"분류 `{spec.name}` 권한 수정")
    for spec in CHANNEL_SPECS:
        channel = find_channel(guild, spec)
        if channel and managed_overwrites_differ(channel, overwrites[spec.key], managed):
            changes.append(f"채널 `{spec.name}` 권한 수정")

    community = discord.utils.get(guild.categories, name=CAT_COMMUNITY)
    if media_is_misplaced(community, text(guild, CH_CHAT), text(guild, CH_MEDIA)):
        changes.append(f"채널 `{CH_MEDIA}`을 `{CH_CHAT}` 바로 아래로 이동")

    ops = discord.utils.get(guild.categories, name=CAT_OPS)
    ops_channels = [text(guild, name) for name in OPS_CHANNEL_ORDER]
    if ops_order_is_wrong(ops, ops_channels):
        changes.append(f"운영 채널 순서를 `{' → '.join(OPS_CHANNEL_ORDER)}`로 정렬")

    audit_log = text(guild, CH_LOG)
    if audit_log and guild.system_channel != audit_log:
        changes.append(f"시스템 메시지 채널을 `{CH_LOG}`로 변경")

    changes.extend(await inspect_nickname_guide(text(guild, CH_NICKNAME)))
    return changes


# ---------------------------------------------------------------- 초기화

@dataclass
class ResetResult:
    deleted_channels: int = 0
    deleted_roles: int = 0
    skipped_roles: list = field(default_factory=list)
    failed_channels: list = field(default_factory=list)
    excluded_ids: set = field(default_factory=set)


def brief_names(names):
    body = ", ".join(names[:5])
    return body + (f" 외 {len(names) - 5}개" if len(names) > 5 else "")


async def wipe_server(guild, command_channel, management_log):
    result = ResetResult()
    preserve = {command_channel.id, management_log.id}

    if command_channel.id != management_log.id:
        await command_channel.edit(
            name=f"discord-manager-reset-temp-{command_channel.id}",
            category=None,
            reason="Discord Manager reset temp",
        )
        result.excluded_ids.add(command_channel.id)

    if management_log.category:
        await management_log.edit(
            category=None,
            reason="Discord Manager preserve audit log",
        )

    for channel in list(guild.channels):
        if channel.id in preserve or isinstance(channel, discord.CategoryChannel):
            continue
        try:
            await channel.delete(reason="Discord Manager owner-confirmed reset")
            result.deleted_channels += 1
            result.excluded_ids.add(channel.id)
        except discord.NotFound:
            result.excluded_ids.add(channel.id)
        except discord.HTTPException:
            result.failed_channels.append(channel.name)
            log.exception("Reset could not delete channel %s (%s)", channel.name, channel.id)

    for category in list(guild.categories):
        try:
            await category.delete(reason="Discord Manager owner-confirmed reset")
            result.deleted_channels += 1
            result.excluded_ids.add(category.id)
        except discord.NotFound:
            result.excluded_ids.add(category.id)
        except discord.HTTPException:
            result.failed_channels.append(category.name)
            log.exception("Reset could not delete category %s (%s)", category.name, category.id)

    top_role = guild.me.top_role if guild.me else None
    for target in sorted(guild.roles, key=lambda r: r.position, reverse=True):
        if target.is_default() or target.managed or target.name in PROTECTED_ROLES:
            continue
        if top_role and target >= top_role:
            result.skipped_roles.append(target.name)
            continue
        try:
            await target.delete(reason="Discord Manager owner-confirmed reset")
            result.deleted_roles += 1
            result.excluded_ids.add(target.id)
        except discord.NotFound:
            result.excluded_ids.add(target.id)
        except discord.HTTPException:
            result.skipped_roles.append(target.name)
            log.exception("Reset could not delete role %s (%s)", target.name, target.id)

    return result


# ---------------------------------------------------------------- 닉네임 비공개 제출

class NicknameModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title=config.NICKNAME_MODAL_TITLE)
        self.nickname = discord.ui.TextInput(
            label=config.NICKNAME_FIELD_LABEL,
            placeholder=config.NICKNAME_FIELD_PLACEHOLDER,
            max_length=32,
            required=True,
        )
        self.game = discord.ui.TextInput(
            label=config.GAME_FIELD_LABEL,
            style=discord.TextStyle.short,
            max_length=100,
            required=False,
        )
        self.add_item(self.nickname)
        self.add_item(self.game)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        nickname = self.nickname.value.strip()
        if not nickname:
            return await respond(interaction, "닉네임은 공백만 입력할 수 없습니다.")

        db_channel = text(interaction.guild, CH_NICKLOG)
        audit_channel = text(interaction.guild, CH_LOG)
        if db_channel is None and audit_channel is None:
            return await respond(
                interaction,
                "제출을 접수할 기록 채널이 없습니다. 관리자에게 알려 주세요.",
            )

        server_name = interaction.user.display_name
        game_name_input = self.game.value.strip()
        game_name = game_name_input or "(미입력)"
        submitted_at = datetime.now(
            timezone(timedelta(hours=9))
        ).strftime("%Y-%m-%d %H:%M:%S")

        db_body = (
            f"서버 닉네임: **{server_name}**\n"
            f"게임 닉네임: **{nickname}**\n"
            f"게임명: **{game_name}**"
        )
        detail_body = (
            f"닉네임 제출 · {interaction.user.name}({interaction.user.id}) · "
            f"{server_name} · {nickname} · {game_name} · {submitted_at}(UTC+9)"
        )
        mentions = discord.AllowedMentions.none()

        if db_channel is not None:
            try:
                await db_channel.send(db_body[:MAX_MESSAGE], allowed_mentions=mentions)
            except discord.HTTPException:
                log.exception("Nickname DB submission failed in guild %s", interaction.guild.id)
                return await respond(
                    interaction,
                    "제출을 접수하지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
                )

        if audit_channel is not None:
            try:
                await audit_channel.send(detail_body[:MAX_MESSAGE], allowed_mentions=mentions)
            except discord.HTTPException:
                log.exception("Nickname audit failed in guild %s", interaction.guild.id)
                if db_channel is None:
                    return await respond(
                        interaction,
                        "제출을 접수하지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
                    )

        if game_name_input:
            message = config.NICKNAME_SUCCESS_WITH_GAME.format(
                game=game_name_input,
                nickname=nickname,
            )
        else:
            message = config.NICKNAME_SUCCESS_NO_GAME.format(nickname=nickname)
        await respond(interaction, message)

    async def on_error(self, interaction, error):
        log.exception("Nickname modal failed", exc_info=error)
        await respond(
            interaction,
            "제출을 처리하지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
        )


class NicknameSubmissionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label=NICKNAME_BUTTON_LABEL,
            style=discord.ButtonStyle.primary,
            custom_id=NICKNAME_SUBMIT_CUSTOM_ID,
        )
        button.callback = self.submit
        self.add_item(button)

    async def submit(self, interaction):
        if interaction.guild is None:
            return await respond(interaction, "서버 안에서만 사용할 수 있습니다.")
        await interaction.response.send_modal(NicknameModal())


def guide_buttons(message):
    return [
        component
        for row in message.components
        for component in getattr(row, "children", ())
        if isinstance(component, discord.Button)
    ]


def submit_button(message):
    return next(
        (b for b in guide_buttons(message) if b.custom_id == NICKNAME_SUBMIT_CUSTOM_ID),
        None,
    )


def is_guide_candidate(message, bot_id):
    if message.author.id != bot_id:
        return False
    return submit_button(message) is not None or message.content == NICKNAME_GUIDE_TEXT


def guide_content_problems(message):
    problems = []
    if message.content != NICKNAME_GUIDE_TEXT:
        problems.append("안내문이 최신이 아님")
    button = submit_button(message)
    if button is None:
        problems.append("버튼 custom_id 불일치" if guide_buttons(message) else "버튼 없음")
    else:
        if button.label != NICKNAME_BUTTON_LABEL:
            problems.append("버튼 이름 불일치")
        if button.disabled:
            problems.append("버튼이 비활성 상태")
    return problems


def guide_problems(message, *, pinned):
    return ([] if pinned else ["고정(pin)되지 않음"]) + guide_content_problems(message)


async def pinned_messages(channel):
    pins = channel.pins()
    if hasattr(pins, "__aiter__"):
        return [message async for message in pins]
    return list(await pins)


async def find_nickname_guide(channel):
    bot_id = channel.guild.me.id
    for message in await pinned_messages(channel):
        if is_guide_candidate(message, bot_id):
            return message, True
    async for message in channel.history(limit=GUIDE_SCAN_LIMIT):
        if is_guide_candidate(message, bot_id):
            return message, False
    return None, False


async def nickname_guide_problems(channel):
    message, pinned = await find_nickname_guide(channel)
    if message is None:
        return ["안내 메시지 없음"]
    return guide_problems(message, pinned=pinned)


async def inspect_nickname_guide(channel):
    if channel is None:
        return []
    try:
        problems = await nickname_guide_problems(channel)
    except discord.HTTPException:
        log.exception(
            "Nickname guide lookup failed in guild %s",
            getattr(getattr(channel, "guild", None), "id", None),
        )
        return [GUIDE_LOOKUP_FAILED]
    return [f"닉네임 안내 메시지 정리 ({', '.join(problems)})"] if problems else []


async def ensure_nickname_guide(channel):
    message, pinned = await find_nickname_guide(channel)
    if message is None:
        message = await channel.send(
            NICKNAME_GUIDE_TEXT,
            view=NicknameSubmissionView(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    elif guide_content_problems(message):
        message = await message.edit(
            content=NICKNAME_GUIDE_TEXT,
            view=NicknameSubmissionView(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    elif pinned:
        return message

    if not pinned:
        await message.pin(reason="Discord Manager nickname guide")
    return message


async def apply_nickname_guide(channel):
    if channel is None:
        log.warning("Nickname channel unavailable; skipped guide update")
        return ""
    try:
        await ensure_nickname_guide(channel)
    except discord.HTTPException:
        log.exception(
            "Nickname guide update failed in guild %s",
            getattr(getattr(channel, "guild", None), "id", None),
        )
        return " 다만 닉네임 안내 메시지 생성/갱신에 실패했습니다. `/setup check`로 상태를 확인해 주세요."
    return ""


# ---------------------------------------------------------------- 명령

setup_group = app_commands.Group(
    name="setup",
    description="방송 서버 구조 확인·적용·초기화",
)


@setup_group.command(name="check", description="변경 예정 항목만 확인합니다.")
async def setup_check(interaction):
    if not await require_setup_operator(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    changes = await preview(interaction.guild)
    if not changes:
        return await respond(interaction, "현재 서버 구조가 설계와 일치합니다.")
    shown = changes[:25]
    body = "\n".join(f"• {item}" for item in shown)
    if len(changes) > 25:
        body += f"\n• 외 {len(changes) - 25}개"
    await respond(
        interaction,
        f"현재 설계와 다른 항목은 {len(changes)}개입니다. 아직 아무것도 수정하지 않았습니다.\n\n{body}",
    )


@setup_group.command(name="sync", description="기존 구조를 유지하면서 현재 설계를 적용합니다.")
async def setup_sync(interaction):
    if not await require_setup_operator(interaction):
        return
    guild = interaction.guild
    if not guild.me or not guild.me.guild_permissions.administrator:
        return await respond(interaction, "봇에 Administrator 권한이 필요합니다.")

    async with setup_operation(interaction) as acquired:
        if not acquired:
            return
        try:
            server_perm, role_perm, channel_perm = sync_change_counts(guild)
            skipped = skipped_role_changes(guild)
            created, nickname_channel = await build_structure(guild)
            guide_warning = await apply_nickname_guide(nickname_channel)
            extra = ""
            if skipped:
                extra += f" 봇 역할 계층 때문에 적용하지 못한 역할: {brief_names(skipped)}."
            extra += guide_warning
            await respond(
                interaction,
                "서버 구성을 적용했습니다. "
                f"새로 만든 항목 {created}개, 서버 기본 권한 수정 {server_perm}건, "
                f"역할 권한 수정 {role_perm}건, 채널/분류 권한 수정 {channel_perm}건입니다. "
                f"멤버 개인 예외와 추가 역할 권한은 그대로 두었습니다.{extra}",
            )
            await audit(interaction, "서버 역할·채널·권한 sync" + extra)
        except discord.Forbidden as error:
            log.exception("sync forbidden")
            await respond(interaction, f"설정 도중 Discord가 권한을 거부했습니다. 오류: {error}")
        except discord.HTTPException as error:
            log.exception("sync failed")
            await respond(interaction, f"Discord API 오류: {error}")


@setup_group.command(name="reset", description="서버를 완전 초기화한 뒤 재구성합니다.")
@app_commands.describe(confirm="완전 초기화를 실행하려면 RESET을 입력하세요.")
async def setup_reset(interaction, confirm: str):
    if not await require_setup_operator(interaction):
        return
    guild = interaction.guild
    if confirm != "RESET":
        return await respond(
            interaction,
            "완전 초기화를 계속하려면 confirm에 정확히 RESET을 입력하세요.",
        )
    if not guild.me or not guild.me.guild_permissions.administrator:
        return await respond(interaction, "봇에 Administrator 권한이 필요합니다.")
    if not isinstance(interaction.channel, discord.TextChannel):
        return await respond(interaction, "완전 초기화는 일반 텍스트 채널에서 실행해 주세요.")

    async with setup_operation(interaction) as acquired:
        if not acquired:
            return
        command_channel = interaction.channel
        try:
            skipped_permissions = skipped_role_changes(guild)
            log_channel = await ensure_log(guild)
            result = await wipe_server(guild, command_channel, log_channel)
            created, nickname_channel = await build_structure(
                guild,
                log_channel,
                result.excluded_ids,
            )
            guide_warning = await apply_nickname_guide(nickname_channel)
            extra = ""
            if result.failed_channels:
                extra += f" 삭제하지 못한 채널/분류: {brief_names(result.failed_channels)}."
            if result.skipped_roles:
                extra += f" 삭제하지 못한 역할: {brief_names(result.skipped_roles)}."
            if skipped_permissions:
                extra += f" 권한을 적용하지 못한 역할: {brief_names(skipped_permissions)}."
            status = "초기화 일부 미완료." if extra else "완전 초기화 완료."
            extra += guide_warning
            await respond(
                interaction,
                f"{status} `{CH_LOG}`과 {', '.join(PROTECTED_ROLES)} 역할 및 기존 멤버 배정은 보존했습니다. "
                f"기존 채널/카테고리 {result.deleted_channels}개와 기타 역할 {result.deleted_roles}개를 삭제했고 "
                f"새 구조 항목 {created}개를 만들었습니다.{extra}",
            )
            await audit(interaction, status + " 서버 초기화 및 재구성" + extra)

            if command_channel.id != log_channel.id:
                await asyncio.sleep(5)
                try:
                    await command_channel.delete(reason="Discord Manager temp cleanup")
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    log.exception("Could not delete reset temporary channel")
                    await respond(
                        interaction,
                        f"임시 채널 {command_channel.mention} 삭제에 실패했습니다. 직접 정리해 주세요.",
                    )
        except discord.Forbidden as error:
            log.exception("reset forbidden")
            await respond(interaction, f"재구성 도중 Discord가 권한을 거부했습니다. 오류: {error}")
        except discord.HTTPException as error:
            log.exception("reset failed")
            await respond(interaction, f"Discord API 오류: {error}")


@bot.tree.command(name="방송공지", description="방송공지 채널에 공지를 보냅니다.")
@app_commands.describe(content="공지 내용")
async def broadcast(interaction, content: app_commands.Range[str, 1, 2000]):
    if not await require_elevated(interaction):
        return
    channel = text(interaction.guild, CH_BROADCAST)
    if not channel:
        return await respond(
            interaction,
            "방송공지 채널이 없습니다. 먼저 `/setup sync`를 실행해 주세요.",
        )
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        await channel.send(
            content,
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                roles=False,
                users=True,
            ),
        )
    except discord.Forbidden:
        log.exception("Broadcast permission denied in guild %s", interaction.guild.id)
        return await respond(interaction, "Discord가 공지 전송 권한을 거부했습니다.")
    except discord.HTTPException:
        log.exception("Broadcast failed in guild %s", interaction.guild.id)
        return await respond(
            interaction,
            f"Discord API 오류로 전송 결과를 확인하지 못했습니다. 다시 실행하기 전에 {channel.mention}을 확인해 주세요.",
        )
    await respond(interaction, f"{channel.mention}에 방송 공지를 보냈습니다.")
    await audit(interaction, f"방송공지 전송 → #{channel.name}")


@bot.tree.command(name="닉네임", description="방송에서 사용할 닉네임을 비공개로 제출합니다.")
async def nickname_submit(interaction):
    if interaction.guild is None:
        return await respond(interaction, "서버 안에서만 사용할 수 있습니다.")
    await interaction.response.send_modal(NicknameModal())


bot.tree.add_command(setup_group)


# ---------------------------------------------------------------- 이벤트

@bot.event
async def on_member_join(member):
    await write_audit(member.guild, f"서버 입장 · `{member} ({member.id})`")
    if member.bot:
        return
    viewer = role(member.guild, ROLE_VIEWER)
    if viewer is None:
        await write_audit(
            member.guild,
            f"{ROLE_VIEWER} 자동 부여 실패 · `{member} ({member.id})` · 역할을 찾을 수 없음",
        )
        return
    if viewer in member.roles:
        return
    if not manageable(member.guild, viewer):
        await write_audit(
            member.guild,
            f"{ROLE_VIEWER} 자동 부여 실패 · `{member} ({member.id})` · 봇 역할보다 위에 있음",
        )
        return
    try:
        await member.add_roles(viewer, reason="Discord Manager newcomer auto role")
    except (discord.Forbidden, discord.HTTPException):
        log.exception("Failed to auto-assign Viewer role to member %s", member.id)
        await write_audit(
            member.guild,
            f"{ROLE_VIEWER} 자동 부여 실패 · `{member} ({member.id})` · Discord 권한/API 오류",
        )


@bot.event
async def on_member_remove(member):
    await write_audit(member.guild, f"서버 퇴장 · `{member} ({member.id})`")


@bot.event
async def on_member_update(before, after):
    before_roles = {r.id: r for r in before.roles if not r.is_default()}
    after_roles = {r.id: r for r in after.roles if not r.is_default()}
    added = [r.name for rid, r in after_roles.items() if rid not in before_roles]
    removed = [r.name for rid, r in before_roles.items() if rid not in after_roles]
    if not added and not removed:
        return
    parts = []
    if added:
        parts.append("추가: " + ", ".join(added))
    if removed:
        parts.append("제거: " + ", ".join(removed))
    await write_audit(
        after.guild,
        f"역할 변경 · `{after} ({after.id})` · " + " / ".join(parts),
    )


async def sync_guild_commands(guild):
    lock = bot.command_sync_locks.setdefault(guild.id, asyncio.Lock())
    async with lock:
        if guild.id in bot.synced_guilds:
            return
        try:
            obj = discord.Object(id=guild.id)
            bot.tree.copy_global_to(guild=obj)
            synced = await bot.tree.sync(guild=obj)
            bot.synced_guilds.add(guild.id)
            log.info(
                "Synced %d top-level slash commands to guild %s (%s)",
                len(synced),
                guild.name,
                guild.id,
            )
        except discord.HTTPException:
            log.exception("Failed command sync in guild %s", guild.id)


@bot.event
async def on_guild_join(guild):
    await sync_guild_commands(guild)


@bot.event
async def on_guild_remove(guild):
    bot.synced_guilds.discard(guild.id)
    bot.setup_locks.pop(guild.id, None)
    bot.command_sync_locks.pop(guild.id, None)


@bot.event
async def on_ready():
    if bot.user:
        log.info("Logged in as %s (%s)", bot.user, bot.user.id)
    for guild in bot.guilds:
        await sync_guild_commands(guild)
    log.info("Bot ready")


MEMBERS_INTENT_HELP = (
    "Server Members Intent가 꺼져 있어 봇을 시작할 수 없습니다. "
    "Discord Developer Portal > Bot > Privileged Gateway Intents에서 "
    "SERVER MEMBERS INTENT를 켜 주세요. Viewer 자동 부여와 입장·퇴장·역할 변경 기록에 필요합니다."
)


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set")
    try:
        bot.run(TOKEN)
    except discord.PrivilegedIntentsRequired:
        raise SystemExit(MEMBERS_INTENT_HELP) from None
