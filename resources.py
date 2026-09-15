"""Guild-scoped reconciliation; only recorded resources may be edited or deleted."""
from dataclasses import dataclass, field

import discord

import preset
from state import ResourceRecord


class ResourceConflict(RuntimeError):
    pass


@dataclass
class Snapshot:
    roles: dict
    channels: dict

    @classmethod
    async def fetch(cls, guild):
        # REST snapshots avoid reusing deleted objects or creating duplicates while
        # Gateway events are still in flight. Never add objects to Discord's cache.
        roles = await guild.fetch_roles()
        channels = await guild.fetch_channels()
        return cls({r.id: r for r in roles}, {ch.id: ch for ch in channels})

    def resolve(self, record):
        if record is None or not record.created:
            return None
        return (self.roles if record.kind == "role" else self.channels).get(record.id)


@dataclass
class ResetPlan:
    revision: int
    targets: list = field(default_factory=list)  # (logical key, record, display name)
    preserved: list = field(default_factory=list)

    @property
    def signature(self):
        return (self.revision, tuple((k, r, name) for k, r, name in self.targets), tuple(self.preserved))


def kind_matches(key, obj):
    if key in preset.ROLE_KEYS.values():
        return isinstance(obj, discord.Role) and not obj.is_default() and not obj.managed
    if key in preset.CATEGORY_BY_KEY:
        return isinstance(obj, discord.CategoryChannel)
    if key in preset.CHANNEL_BY_KEY:
        cls = discord.VoiceChannel if preset.CHANNEL_BY_KEY[key].is_voice else discord.TextChannel
        return isinstance(obj, cls)
    return False


def manageable(guild, role):
    return bool(guild.me and not role.managed and not role.is_default() and role < guild.me.top_role)


class ResourceManager:
    def __init__(self, store):
        self.store = store
        preset.validate_preset()

    def cached(self, guild, key):
        """Runtime commands/events never route to an untracked same-name channel."""
        if guild is None:
            return None
        record = self.store.load(guild.id).resources.get(key)
        if record is None or not record.created:
            return None
        obj = guild.get_role(record.id) if record.kind == "role" else guild.get_channel(record.id)
        return obj if obj is not None and kind_matches(key, obj) else None

    def remember(self, state, key, obj, *, channel_id=None):
        state.resources[key] = ResourceRecord(key.split(":")[0], obj.id, channel_id=channel_id)
        self.store.save(state)

    def forget(self, state, key):
        state.resources.pop(key, None)
        self.store.save(state)

    def definitions(self):
        return [(preset.ROLE_KEYS[name], name) for name in preset.expected_role_permissions()] + [
            (s.resource_key, s.name) for s in (*preset.CATEGORY_SPECS, *preset.CHANNEL_SPECS)]

    def conflicts(self, state, snapshot):
        issues = []
        for key, name in self.definitions():
            record = state.resources.get(key)
            obj = snapshot.resolve(record)
            if record and not record.created:
                issues.append(f"`{name}`: 생성 출처가 확인되지 않아 변경하지 않음")
                continue
            if obj is not None:
                if not kind_matches(key, obj):
                    issues.append(f"`{name}`: 기록된 ID의 리소스 종류 불일치")
                continue
            objects = snapshot.roles.values() if key.startswith("role:") else snapshot.channels.values()
            for other in objects:
                # Categories and channels have separate name spaces; text and
                # voice collisions are both blocked rather than guessed.
                if key.startswith("category:") != isinstance(other, discord.CategoryChannel) and not key.startswith("role:"):
                    continue
                if preset.text_key(other.name) == preset.text_key(name):
                    issues.append(f"`{name}` (ID {other.id}): 소유 기록 없는 동명 리소스. 자동 인수·복제하지 않음")
        return issues

    def permissions(self, guild, state, snapshot):
        role_objects = [snapshot.resolve(state.resources.get(key)) for key in preset.ROLE_KEYS.values()]
        managed = {guild.default_role} | {r for r in role_objects if r is not None}
        return preset.overwrite_sets(
            guild, snapshot.resolve(state.resources.get("role:streamer")),
            snapshot.resolve(state.resources.get("role:viewer"))), managed

    async def check(self, guild, inspect_guide):
        state = self.store.load(guild.id)
        snapshot = await Snapshot.fetch(guild)
        issues = self.conflicts(state, snapshot)
        if not state.installed:
            issues.insert(0, "설치가 완료되지 않았습니다. `/setup start`로 확인 후 설치하거나 부분 설치를 sync로 복구하세요.")
        for name, permissions in preset.expected_role_permissions().items():
            key = preset.ROLE_KEYS[name]
            obj = snapshot.resolve(state.resources.get(key))
            if obj is None:
                issues.append(f"역할 `{name}` 없음")
            elif kind_matches(key, obj):
                if obj.name != name:
                    issues.append(f"역할 `{name}` 이름 불일치 (ID {obj.id})")
                if obj.permissions != permissions:
                    issues.append(f"역할 `{name}` 권한 불일치")
                if not manageable(guild, obj):
                    issues.append(f"역할 `{name}`이 봇보다 위에 있어 관리 불가")
        overwrites, managed = self.permissions(guild, state, snapshot)
        for spec in (*preset.CATEGORY_SPECS, *preset.CHANNEL_SPECS):
            obj = snapshot.resolve(state.resources.get(spec.resource_key))
            if obj is None:
                issues.append(f"리소스 `{spec.name}` 없음")
                continue
            if not kind_matches(spec.resource_key, obj):
                continue
            if preset.text_key(obj.name) != preset.text_key(spec.name):
                issues.append(f"리소스 `{spec.name}` 이름 불일치 (ID {obj.id})")
            if preset.managed_overwrites_differ(obj, overwrites[spec.key], managed):
                issues.append(f"리소스 `{spec.name}` 권한 불일치")
            if isinstance(spec, preset.ChannelSpec):
                category = snapshot.resolve(state.resources.get(preset.CATEGORY_KEY_BY_NAME[spec.category]))
                if category is None or obj.category_id != category.id:
                    issues.append(f"채널 `{spec.name}` 분류 불일치")
                if spec.topic and getattr(obj, "topic", None) != spec.topic:
                    issues.append(f"채널 `{spec.name}` 안내문 불일치")
        if self.order_changes(state, snapshot):
            issues.append("사진/영상 또는 운영 채널 순서 불일치")
        log = snapshot.resolve(state.resources.get("channel:audit_log"))
        if log and guild.system_channel != log:
            issues.append("서버 시스템 메시지 채널 불일치")
        nickname = snapshot.resolve(state.resources.get("channel:nickname"))
        if nickname and kind_matches("channel:nickname", nickname):
            if preset.GUIDE_KEY not in state.resources:
                issues.append("닉네임 안내 메시지 ID 미등록")
            issues.extend(await inspect_guide(nickname, state.resources.get(preset.GUIDE_KEY)))
        return issues

    def order_changes(self, state, snapshot):
        """Only order DM channels; never move a user channel to another category."""
        moves = []
        for keys in (("chat", "media"), ("staff", "nickname_db", "audit_log")):
            objects = [snapshot.resolve(state.resources.get("channel:" + key)) for key in keys]
            if any(obj is None for obj in objects):
                continue
            first = objects[0]
            if any(obj.category_id != first.category_id for obj in objects):
                continue
            ordered = sorted((ch for ch in snapshot.channels.values()
                              if isinstance(ch, discord.TextChannel) and ch.category_id == first.category_id),
                             key=lambda ch: (ch.position, ch.id))
            indexes = [ordered.index(ch) for ch in objects]
            wanted = list(range(indexes[0], indexes[0] + len(indexes)))
            if indexes != wanted or (keys[0] == "staff" and indexes[0] != 0):
                # Shift within the same category; only DM IDs appear in PATCH.
                start = first.position if keys[0] == "chat" else min(ch.position for ch in ordered)
                moves.extend({"id": ch.id, "position": start + i} for i, ch in enumerate(objects))
        return moves

    async def sync(self, guild, ensure_guide):
        state = self.store.load(guild.id)
        snapshot = await Snapshot.fetch(guild)
        conflicts = self.conflicts(state, snapshot)
        if conflicts:
            raise ResourceConflict("\n".join(conflicts))
        for name, permissions in preset.expected_role_permissions().items():
            obj = snapshot.resolve(state.resources.get(preset.ROLE_KEYS[name]))
            if obj and (obj.name != name or obj.permissions != permissions) and not manageable(guild, obj):
                raise ResourceConflict(f"역할 `{name}`을 수정하려면 봇 역할을 위로 이동해 주세요.")
        # Verify storage is writable before the first Discord mutation.
        self.store.save(state)
        created = 0
        for name, permissions in preset.expected_role_permissions().items():
            key = preset.ROLE_KEYS[name]
            obj = snapshot.resolve(state.resources.get(key))
            if obj is None:
                obj = await guild.create_role(name=name, permissions=permissions, reason="Discord Manager install")
                self.remember(state, key, obj)
                created += 1
            elif obj.name != name or obj.permissions != permissions:
                if not manageable(guild, obj):
                    raise ResourceConflict(f"역할 `{name}`을 수정하려면 봇 역할을 위로 이동해 주세요.")
                obj = await obj.edit(name=name, permissions=permissions, reason="Discord Manager sync")
            snapshot.roles[obj.id] = obj

        overwrites, managed = self.permissions(guild, state, snapshot)
        for spec in (*preset.CATEGORY_SPECS, *preset.CHANNEL_SPECS):
            key = spec.resource_key
            obj = snapshot.resolve(state.resources.get(key))
            desired = overwrites[spec.key]
            kwargs = {}
            if isinstance(spec, preset.ChannelSpec):
                category = snapshot.resolve(state.resources[preset.CATEGORY_KEY_BY_NAME[spec.category]])
                if obj is None or obj.category_id != category.id:
                    kwargs["category"] = category
                if spec.topic and (obj is None or getattr(obj, "topic", None) != spec.topic):
                    kwargs["topic"] = spec.topic
            if obj is None:
                if isinstance(spec, preset.CategorySpec):
                    creator = guild.create_category
                else:
                    creator = guild.create_voice_channel if spec.is_voice else guild.create_text_channel
                obj = await creator(spec.name, overwrites=desired, reason="Discord Manager install", **kwargs)
                self.remember(state, key, obj)
                created += 1
            else:
                if preset.text_key(obj.name) != preset.text_key(spec.name):
                    kwargs["name"] = spec.name
                if preset.managed_overwrites_differ(obj, desired, managed):
                    kwargs["overwrites"] = preset.merge_overwrites(obj.overwrites, desired, managed)
                if kwargs:
                    obj = await obj.edit(reason="Discord Manager sync", **kwargs)
            snapshot.channels[obj.id] = obj

        moves = self.order_changes(state, snapshot)
        if moves:
            await guild._state.http.bulk_channel_update(guild.id, moves, reason="Discord Manager channel ordering")
        log = snapshot.resolve(state.resources["channel:audit_log"])
        if guild.system_channel != log:
            await guild.edit(system_channel=log, reason="Discord Manager system messages")

        nickname = snapshot.resolve(state.resources["channel:nickname"])
        def remember_guide(message):
            self.remember(state, preset.GUIDE_KEY, message, channel_id=nickname.id)
        await ensure_guide(nickname, state.resources.get(preset.GUIDE_KEY), remember_guide)
        owner = snapshot.resolve(state.resources["role:owner"])
        if guild.owner and owner not in guild.owner.roles and manageable(guild, owner):
            await guild.owner.add_roles(owner, reason="Discord Manager owner role")
        state.installed = True
        self.store.save(state)
        return created

    async def reset_plan(self, guild):
        state = self.store.load(guild.id)
        snapshot = await Snapshot.fetch(guild)
        plan = ResetPlan(state.revision)
        candidate_channels = {r.id for k, r in state.resources.items()
                              if r.kind == "channel" and r.created and kind_matches(k, snapshot.resolve(r))}
        for key, rec in state.resources.items():
            obj = snapshot.resolve(rec)
            if rec.kind == "message":
                channel = snapshot.channels.get(rec.channel_id)
                if not rec.created:
                    plan.preserved.append(f"{key}: 메시지 생성 출처 또는 채널을 확인할 수 없음")
                    continue
                if channel is None:
                    plan.targets.append((key, rec, f"안내 메시지 {rec.id} (채널이 이미 없음)"))
                    continue
                try:
                    msg = await channel.fetch_message(rec.id)
                except discord.NotFound:
                    # A missing message is safe to drop from the registry on confirm.
                    plan.targets.append((key, rec, f"안내 메시지 {rec.id} (이미 없음)"))
                    continue
                if not guild.me or msg.author.id != guild.me.id:
                    plan.preserved.append(f"메시지 {rec.id}: 봇 작성자가 아니므로 보존")
                    continue
                plan.targets.append((key, rec, f"안내 메시지 {rec.id}"))
                continue
            if not rec.created or key not in dict(self.definitions()):
                plan.preserved.append(f"{key}: 생성 출처 또는 정의를 확인할 수 없어 보존")
                continue
            if obj is None:
                plan.targets.append((key, rec, f"{key} (ID {rec.id}, 이미 없음)"))
                continue
            if not kind_matches(key, obj):
                plan.preserved.append(f"{key}: ID 종류 불일치, 보존")
                continue
            if rec.kind == "role":
                used_elsewhere = any(any(t.id == obj.id for t in ch.overwrites) for ch in snapshot.channels.values()
                                    if ch.id not in candidate_channels and ch.id not in {
                                        r.id for r in state.resources.values() if r.kind == "category" and r.created})
                if not manageable(guild, obj) or used_elsewhere:
                    plan.preserved.append(f"역할 `{obj.name}` ({obj.id}): 계층 제한 또는 사용자 채널에서 사용 중")
                    continue
            if rec.kind == "category" and any(
                    ch.category_id == obj.id and ch.id not in candidate_channels
                    for ch in snapshot.channels.values() if not isinstance(ch, discord.CategoryChannel)):
                plan.preserved.append(f"분류 `{obj.name}` ({obj.id}): 사용자 채널이 있어 보존")
                continue
            plan.targets.append((key, rec, f"{obj.name} (ID {obj.id})"))
        order = {"message": 0, "channel": 1, "category": 2, "role": 3}
        plan.targets.sort(key=lambda item: (order[item[1].kind], item[0]))
        return plan

    async def reset(self, guild, confirmed_plan):
        current = await self.reset_plan(guild)
        if current.signature != confirmed_plan.signature:
            raise ResourceConflict("삭제 대상 또는 서버 상태가 달라졌습니다. `/setup reset`으로 목록을 다시 확인해 주세요.")
        state = self.store.load(guild.id)
        state.installed = False
        self.store.save(state)
        deleted, skipped = 0, list(current.preserved)
        for key, rec, name in current.targets:
            try:
                # Re-fetch before each deletion: a user may have moved/created
                # channels since the preview or an earlier deletion may fail.
                snapshot = await Snapshot.fetch(guild)
                obj = snapshot.resolve(rec)
                if rec.kind == "message":
                    channel = snapshot.channels.get(rec.channel_id)
                    if channel is not None:
                        message = await channel.fetch_message(rec.id)
                        if message.author.id != guild.me.id:
                            skipped.append(f"{name}: 작성자 불일치")
                            continue
                        await message.delete()
                elif obj is not None:
                    if not kind_matches(key, obj):
                        skipped.append(f"{name}: 리소스 종류 불일치")
                        continue
                    if rec.kind == "category" and any(
                            ch.category_id == obj.id for ch in snapshot.channels.values()
                            if not isinstance(ch, discord.CategoryChannel)):
                        skipped.append(f"{name}: 남은 하위 채널이 있어 보존")
                        continue
                    if rec.kind == "role" and (not manageable(guild, obj) or any(
                            any(t.id == obj.id for t in ch.overwrites) for ch in snapshot.channels.values())):
                        skipped.append(f"{name}: 계층 제한 또는 남은 채널에서 사용 중")
                        continue
                    await obj.delete(reason="Discord Manager confirmed managed-resource reset")
                deleted += 1
            except discord.NotFound:
                pass
            except discord.HTTPException:
                skipped.append(f"{name}: Discord 삭제 실패, ID를 보존함")
                continue
            self.forget(state, key)
        return deleted, skipped
