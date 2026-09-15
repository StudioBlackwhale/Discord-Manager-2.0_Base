import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from dotenv import load_dotenv

import config
import preset
from preset import ROLE_OWNER, ROLE_MANAGER, ROLE_VIEWER, CH_BROADCAST, CH_NICKLOG, CH_LOG, text_key
from resources import ResourceConflict, ResourceManager
from state import SQLiteStateStore, StateError

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger(config.BOT_LOGGER_NAME)
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
MAX_MESSAGE = 2000

NICKNAME_SUBMIT_CUSTOM_ID = config.NICKNAME_SUBMIT_CUSTOM_ID
NICKNAME_BUTTON_LABEL = config.NICKNAME_BUTTON_LABEL
NICKNAME_GUIDE_TEXT = config.NICKNAME_GUIDE_TEXT
GUIDE_SCAN_LIMIT = config.GUIDE_SCAN_LIMIT
GUIDE_LOOKUP_FAILED = "닉네임 안내 상태를 확인하지 못했습니다. Discord API 오류."


class DiscordManager(discord.Client):
    def __init__(self, store=None):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.synced_guilds = set()
        self.setup_locks = {}
        self.command_sync_locks = {}
        self._nickname_view_registered = False
        self.store = store or SQLiteStateStore(os.getenv("DM_STATE_PATH", "data/dm.sqlite3"))
        self.resources = ResourceManager(self.store)

    async def setup_hook(self):
        if not self._nickname_view_registered:
            self.add_view(NicknameSubmissionView())
            self._nickname_view_registered = True


bot = DiscordManager()


def role(guild, name):
    key = preset.ROLE_KEYS.get(name)
    return bot.resources.cached(guild, key) if key else None


def text(guild, name):
    key = preset.CHANNEL_KEY_BY_NAME.get(name)
    return bot.resources.cached(guild, key) if key else None


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
        try:
            manager = role(interaction.guild, ROLE_MANAGER)
        except StateError as error:
            await respond(interaction, str(error))
            return False
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


async def find_nickname_guide(channel, record=None):
    bot_id = channel.guild.me.id
    if record is not None:
        if record.channel_id != channel.id:
            # A recreated channel cannot contain the old guide.
            record = None
        else:
            try:
                message = await channel.fetch_message(record.id)
            except discord.NotFound:
                pass
            else:
                if message.author.id != bot_id:
                    raise ResourceConflict("기록된 안내 메시지의 작성자가 다릅니다.")
                return message, message.pinned
    for message in await pinned_messages(channel):
        if is_guide_candidate(message, bot_id):
            return message, True
    async for message in channel.history(limit=GUIDE_SCAN_LIMIT):
        if is_guide_candidate(message, bot_id):
            return message, False
    return None, False


async def nickname_guide_problems(channel, record=None):
    message, pinned = await find_nickname_guide(channel, record)
    if message is None:
        return ["안내 메시지 없음"]
    return guide_problems(message, pinned=pinned)


async def inspect_nickname_guide(channel, record=None):
    if channel is None:
        return []
    try:
        problems = await nickname_guide_problems(channel, record)
    except discord.HTTPException:
        log.exception(
            "Nickname guide lookup failed in guild %s",
            getattr(getattr(channel, "guild", None), "id", None),
        )
        return [GUIDE_LOOKUP_FAILED]
    return [f"닉네임 안내 메시지 정리 ({', '.join(problems)})"] if problems else []


async def ensure_nickname_guide(channel, record, remember):
    message, pinned = await find_nickname_guide(channel, record)
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
    # Save the ID before pinning: a failed pin must not duplicate the message.
    if record is None or record.id != message.id or record.channel_id != channel.id:
        remember(message)
    if not pinned:
        await message.pin(reason="Discord Manager nickname guide")
    return message


# ---------------------------------------------------------------- setup 명령

setup_group = app_commands.Group(
    name="setup", description="방송 서버 설치·확인·복구·초기화", guild_only=True,
)


async def setup_error(interaction, error):
    log.error("Setup failed in guild %s: %s", getattr(interaction.guild, "id", None), error)
    await respond(interaction, f"작업을 완료하지 못했습니다.\n{error}\n`/setup check`로 상태를 확인해 주세요.")


async def send_listing(interaction, title, lines, *, view=None):
    # Every deletion target is shown, never silently truncated to the first 25.
    chunks, current = [], title
    for line in lines:
        line = f"• {line}"
        if len(current) + len(line) + 1 > 1800:
            chunks.append(current)
            current = ""
        current += "\n" + line
    chunks.append(current)
    for index, chunk in enumerate(chunks):
        await interaction.followup.send(
            chunk, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
            **({"view": view} if view is not None and index == len(chunks) - 1 else {}),
        )


class SetupConfirmation(discord.ui.View):
    """Short-lived confirmation boundary, replaceable by a future setup wizard."""
    def __init__(self, interaction, action, *, plan=None, revision=None):
        super().__init__(timeout=120)
        self.guild_id = interaction.guild.id
        self.user_id = interaction.user.id
        self.action = action
        self.plan = plan
        self.revision = revision
        self.used = False
        apply = discord.ui.Button(
            label="설치 적용" if action == "start" else "목록의 DM 리소스 삭제",
            style=discord.ButtonStyle.primary if action == "start" else discord.ButtonStyle.danger,
        )
        apply.callback = self.confirm
        cancel = discord.ui.Button(label="취소", style=discord.ButtonStyle.secondary)
        cancel.callback = self.cancel
        self.add_item(apply)
        self.add_item(cancel)

    async def interaction_check(self, interaction):
        if (interaction.guild is None or interaction.guild.id != self.guild_id
                or interaction.user.id != self.user_id):
            await respond(interaction, "이 확인 버튼은 명령을 실행한 사용자만 누를 수 있습니다.")
            return False
        return await require_setup_operator(interaction)

    async def confirm(self, interaction):
        # Recheck here as well so the security boundary is explicit and testable.
        if not await self.interaction_check(interaction):
            return
        if self.used or self.is_finished():
            return await respond(interaction, "이미 처리했거나 만료된 확인입니다. 명령을 다시 실행해 주세요.")
        if not interaction.guild.me or not interaction.guild.me.guild_permissions.administrator:
            return await respond(interaction, "봇에 Administrator 권한이 필요합니다.")
        async with setup_operation(interaction) as acquired:
            if not acquired:
                return
            self.used = True
            self.stop()
            try:
                if self.action == "start":
                    if bot.store.load(self.guild_id).revision != self.revision:
                        raise ResourceConflict("설치 상태가 변경되었습니다. `/setup start`로 다시 확인해 주세요.")
                    created = await bot.resources.sync(interaction.guild, ensure_nickname_guide)
                    await respond(interaction, f"설치 적용 완료. 새 리소스 {created}개를 만들었습니다.")
                    await audit(interaction, "DM Base 설치 적용")
                else:
                    deleted, preserved = await bot.resources.reset(interaction.guild, self.plan)
                    await send_listing(interaction, f"DM 리소스 초기화 처리 {deleted}개. 재설치는 `/setup start`를 사용하세요.",
                                       ["보존/미완료: " + item for item in preserved])
            except (discord.HTTPException, ResourceConflict, StateError) as error:
                await setup_error(interaction, error)

    async def cancel(self, interaction):
        if not await self.interaction_check(interaction):
            return
        if self.used:
            return await respond(interaction, "이미 처리된 확인입니다.")
        self.used = True
        self.stop()
        await interaction.response.edit_message(content="취소했습니다. 서버를 변경하지 않았습니다.", view=None)


@setup_group.command(name="start", description="설치 내용을 확인한 뒤 DM Base를 구축합니다.")
async def setup_start(interaction):
    if not await require_setup_operator(interaction):
        return
    async with setup_operation(interaction) as acquired:
        if not acquired:
            return
        try:
            state = bot.store.load(interaction.guild.id)
            issues = await bot.resources.check(interaction.guild, inspect_nickname_guide)
            if state.installed and not issues:
                return await respond(interaction, "DM Base가 이미 정상 설치되어 있습니다. 추가로 만들지 않았습니다.")
            view = SetupConfirmation(interaction, "start", revision=state.revision)
            await send_listing(
                interaction,
                "DM Base 설치 확인 — 아직 서버를 변경하지 않았습니다.\n"
                "Owner/Manager에는 Administrator 권한을 부여하며 시스템 메시지를 관리기록에 연결합니다.\n"
                "DM 리소스만 생성·복구하고 동명 사용자 리소스가 있으면 중단합니다. 2분 안에 적용 여부를 선택해 주세요.",
                issues, view=view,
            )
        except (discord.HTTPException, ResourceConflict, StateError) as error:
            await setup_error(interaction, error)


@setup_group.command(name="check", description="설치·리소스·권한·안내 상태를 읽기 전용으로 진단합니다.")
async def setup_check(interaction):
    if not await require_setup_operator(interaction):
        return
    async with setup_operation(interaction) as acquired:
        if not acquired:
            return
        try:
            issues = await bot.resources.check(interaction.guild, inspect_nickname_guide)
            await send_listing(interaction, f"진단 결과 {len(issues)}건. 서버를 변경하지 않았습니다." if issues
                               else "현재 서버 구조가 설계와 일치합니다.", issues)
        except (discord.HTTPException, ResourceConflict, StateError) as error:
            await setup_error(interaction, error)


@setup_group.command(name="sync", description="DM이 관리하는 리소스만 생성하거나 복구합니다.")
async def setup_sync(interaction):
    if not await require_setup_operator(interaction):
        return
    if not interaction.guild.me or not interaction.guild.me.guild_permissions.administrator:
        return await respond(interaction, "봇에 Administrator 권한이 필요합니다.")
    async with setup_operation(interaction) as acquired:
        if not acquired:
            return
        try:
            state = bot.store.load(interaction.guild.id)
            if not state.installed and not state.resources:
                return await respond(interaction, "최초 설치는 `/setup start`에서 확인 후 진행해 주세요.")
            created = await bot.resources.sync(interaction.guild, ensure_nickname_guide)
            await respond(interaction, f"DM 리소스를 복구했습니다. 새 리소스 {created}개. 사용자 리소스와 개인 권한 예외는 보존했습니다.")
            await audit(interaction, "DM 리소스 sync")
        except (discord.HTTPException, ResourceConflict, StateError) as error:
            await setup_error(interaction, error)


@setup_group.command(name="reset", description="삭제할 DM 리소스 목록을 확인한 뒤 초기화합니다.")
async def setup_reset(interaction):
    if not await require_setup_operator(interaction):
        return
    async with setup_operation(interaction) as acquired:
        if not acquired:
            return
        try:
            plan = await bot.resources.reset_plan(interaction.guild)
            if not plan.targets:
                return await send_listing(interaction, "삭제할 것으로 확인된 DM 리소스가 없습니다.", plan.preserved)
            await send_listing(
                interaction,
                "DM 리소스 삭제 확인 — 아직 삭제하지 않았습니다.\n"
                "아래 채널의 메시지 기록(닉네임_DB·관리기록 포함)과 삭제되는 역할의 멤버 배정도 사라집니다.\n"
                "재구축은 자동 실행하지 않습니다. 2분 안에 확인하거나 취소해 주세요.",
                ["삭제: " + name for _, _, name in plan.targets] + ["보존: " + item for item in plan.preserved],
                view=SetupConfirmation(interaction, "reset", plan=plan),
            )
        except (discord.HTTPException, ResourceConflict, StateError) as error:
            await setup_error(interaction, error)


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
    for locks in (bot.setup_locks, bot.command_sync_locks):
        lock = locks.get(guild.id)
        if lock is not None and not lock.locked():
            locks.pop(guild.id, None)


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
