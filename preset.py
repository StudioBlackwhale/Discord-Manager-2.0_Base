"""The single base preset: names, structure and existing permission policy, no API calls."""
from dataclasses import dataclass

import discord
import config

ROLE_OWNER = config.ROLE_NAMES["owner"]
ROLE_MANAGER = config.ROLE_NAMES["manager"]
ROLE_STREAMER = config.ROLE_NAMES["streamer"]
ROLE_VIEWER = config.ROLE_NAMES["viewer"]

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

@dataclass(frozen=True)
class CategorySpec:
    name: str
    key: str

    @property
    def resource_key(self):
        return "category:" + next(k for k, v in config.CATEGORY_NAMES.items() if v == self.name)


@dataclass(frozen=True)
class ChannelSpec:
    name: str
    category: str
    key: str
    is_voice: bool = False
    topic: str = ""

    @property
    def resource_key(self):
        return "channel:" + next(k for k, v in config.CHANNEL_NAMES.items() if v == self.name)


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


def merge_overwrites(current, desired, managed, excluded_ids=()):
    # 멤버 개인 예외와 사용자가 별도로 만든 역할 권한은 보존한다.
    managed_ids = {target.id for target in managed}
    merged = {
        target: overwrite
        for target, overwrite in current.items()
        if target.id not in managed_ids and target.id not in excluded_ids
    }
    merged.update(desired)
    return merged


def managed_overwrites_differ(channel, desired, managed):
    managed_ids = {target.id for target in managed}
    current = {t.id: o for t, o in channel.overwrites.items() if t.id in managed_ids}
    return current != {t.id: o for t, o in desired.items()}


def overwrite_sets(guild, streamer=None, viewer=None):
    everyone = guild.default_role
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



ROLE_KEYS = {name: "role:" + key for key, name in config.ROLE_NAMES.items()}
CATEGORY_BY_KEY = {spec.resource_key: spec for spec in CATEGORY_SPECS}
CHANNEL_BY_KEY = {spec.resource_key: spec for spec in CHANNEL_SPECS}
CATEGORY_KEY_BY_NAME = {spec.name: spec.resource_key for spec in CATEGORY_SPECS}
CHANNEL_KEY_BY_NAME = {spec.name: spec.resource_key for spec in CHANNEL_SPECS}
GUIDE_KEY = "message:nickname_guide"

def text_key(name):
    return "-".join(name.strip().split()).casefold()

def validate_preset():
    for names in (config.ROLE_NAMES.values(), config.CATEGORY_NAMES.values(), config.CHANNEL_NAMES.values()):
        normalized = [text_key(name) for name in names]
        if len(set(normalized)) != len(normalized):
            raise ValueError("Preset resource names must be unique within each kind")
