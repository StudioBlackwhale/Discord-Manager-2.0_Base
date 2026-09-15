"""Small replaceable persistence boundary. Reading never creates or updates files."""
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol


class StateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResourceRecord:
    kind: str
    id: int
    created: bool = True
    channel_id: int | None = None


@dataclass
class GuildState:
    guild_id: int
    installed: bool = False
    resources: dict[str, ResourceRecord] = field(default_factory=dict)
    revision: int = 0


class StateStore(Protocol):
    def load(self, guild_id: int) -> GuildState: ...
    def save(self, state: GuildState) -> None: ...


class SQLiteStateStore:
    """One bot process per database; guild mutations additionally use async locks.

    Each successful Discord creation/deletion is checkpointed immediately. A crash
    between Discord and SQLite cannot be made atomic: unrecorded objects are never
    adopted/deleted by name. Corrupt or incompatible state fails closed.
    """
    def __init__(self, path):
        self.path = Path(path).resolve()

    def load(self, guild_id):
        if not self.path.exists():
            return GuildState(guild_id)
        try:
            with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True) as db:
                row = db.execute(
                    "SELECT payload, revision FROM guild_state WHERE guild_id = ?",
                    (str(guild_id),),
                ).fetchone()
            if row is None:
                return GuildState(guild_id)
            data = json.loads(row[0])
            if data["version"] != 1 or data["guild_id"] != guild_id:
                raise ValueError("Incompatible guild state")
            records = {k: ResourceRecord(**v) for k, v in data["resources"].items()}
            for key, rec in records.items():
                if (rec.kind not in {"role", "category", "channel", "message"}
                        or not key.startswith(rec.kind + ":")
                        or type(rec.id) is not int or rec.id <= 0
                        or type(rec.created) is not bool
                        or (rec.kind == "message" and (type(rec.channel_id) is not int or rec.channel_id <= 0))):
                    raise ValueError("Invalid resource record")
            if type(data["installed"]) is not bool:
                raise ValueError("Invalid installation state")
            return GuildState(guild_id, data["installed"], records, row[1])
        except (sqlite3.Error, ValueError, KeyError, TypeError) as error:
            raise StateError("서버 상태를 읽을 수 없습니다. 저장 파일을 확인해 주세요.") from error

    def save(self, state):
        payload = json.dumps({
            "version": 1, "guild_id": state.guild_id, "installed": state.installed,
            "resources": {k: asdict(v) for k, v in state.resources.items()},
        }, ensure_ascii=False)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path, timeout=5) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE IF NOT EXISTS guild_state "
                           "(guild_id TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL)")
                row = db.execute("SELECT revision FROM guild_state WHERE guild_id = ?",
                                 (str(state.guild_id),)).fetchone()
                if (row[0] if row else 0) != state.revision:
                    raise StateError("서버 상태가 변경되었습니다. 다시 확인해 주세요.")
                db.execute("INSERT INTO guild_state VALUES (?, ?, ?) "
                           "ON CONFLICT(guild_id) DO UPDATE SET payload=excluded.payload, revision=excluded.revision",
                           (str(state.guild_id), payload, state.revision + 1))
            state.revision += 1
        except (sqlite3.Error, OSError) as error:
            raise StateError("서버 상태를 저장하지 못했습니다. 디스크와 저장 경로를 확인해 주세요.") from error
