"""Framework-independent session state for the Neo-MoFox port.

Adapted from astrbot_plugin_immersive_control (MIT License).
Original copyright (c) 2025 木有知; original authors: 木有知、Zhalslar.
The original copyright and permission notice are preserved in LICENSE.upstream.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class SessionConfig(Protocol):
    state_duration: int
    cooldown_seconds: int
    max_concurrent: int
    exit_pending_ttl: int


@dataclass(frozen=True, slots=True)
class Session:
    active: bool
    end: float | None = None
    exit_ts: float | None = None
    reason: str | None = None
    cooldown_end: float = 0.0


class SessionStore:
    """Serialize state changes and optionally persist absolute timestamps.

    Cooldown starts when a session is activated. Consuming an exit removes only
    its pending reaction; the cooldown remains until its deadline. Expiry and
    retention are checked on access, so no background task is required.
    """

    def __init__(
        self,
        config: SessionConfig,
        path: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = config
        self.path = Path(path) if path is not None else None
        self.clock = clock
        self._data: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    def _sweep(self, now: float) -> bool:
        changed = False
        for key, original in tuple(self._data.items()):
            session = original
            if session.active and session.end is not None and session.end <= now:
                session = replace(session, active=False, exit_ts=session.end, reason="expire")
            if (
                not session.active
                and session.exit_ts is not None
                and now - session.exit_ts > self.cfg.exit_pending_ttl
            ):
                session = replace(session, end=None, exit_ts=None, reason=None)
            if not session.active and session.exit_ts is None and session.cooldown_end <= now:
                del self._data[key]
                changed = True
            elif session != original:
                self._data[key] = session
                changed = True
        return changed

    async def _save_locked(self) -> None:
        if self.path is None:
            return
        document = {
            "version": 1,
            "sessions": {key: asdict(value) for key, value in self._data.items()},
        }
        payload = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)
        await asyncio.to_thread(self._atomic_write, self.path, payload)

    @staticmethod
    def _atomic_write(path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    @staticmethod
    def _number(value: object, field: str, *, nullable: bool = False) -> float | None:
        if value is None and nullable:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"invalid {field}")
        result = float(value)
        if not math.isfinite(result):
            raise ValueError(f"non-finite {field}")
        return result

    @classmethod
    def _decode(cls, payload: str) -> dict[str, Session]:
        document = json.loads(payload)
        if not isinstance(document, dict) or type(document.get("version")) is not int:
            raise ValueError("invalid session file format")
        if document["version"] != 1:
            raise ValueError("unsupported session file version")
        records = document.get("sessions")
        if not isinstance(records, dict):
            raise ValueError("invalid session mapping")
        decoded: dict[str, Session] = {}
        for key, record in records.items():
            if not isinstance(key, str) or not isinstance(record, dict):
                raise ValueError("invalid session record")
            active = record.get("active")
            if type(active) is not bool:
                raise ValueError("invalid active flag")
            end = cls._number(record.get("end"), "end", nullable=True)
            exit_ts = cls._number(record.get("exit_ts"), "exit_ts", nullable=True)
            cooldown_end = cls._number(record.get("cooldown_end", 0.0), "cooldown_end")
            reason = record.get("reason")
            if reason not in (None, "user", "expire"):
                raise ValueError("invalid exit reason")
            if active and (end is None or exit_ts is not None):
                raise ValueError("invalid active session timestamps")
            decoded[key] = Session(active, end, exit_ts, reason, float(cooldown_end))
        return decoded

    @staticmethod
    def _quarantine(path: Path) -> Path:
        destination = path.with_name(f"{path.name}.corrupt-{uuid.uuid4().hex}")
        path.rename(destination)
        return destination

    async def load(self) -> None:
        if self.path is None:
            return
        async with self._lock:
            try:
                payload = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
            except FileNotFoundError:
                return
            except UnicodeError as exc:
                await self._quarantine_locked(exc)
                return
            except OSError:
                logger.exception("Unable to read immersive-control state: %s", self.path)
                return
            try:
                records = self._decode(payload)
            except (ValueError, TypeError, OverflowError) as exc:
                await self._quarantine_locked(exc)
                return
            self._data = records
            if self._sweep(float(self.clock())):
                await self._save_locked()

    async def _quarantine_locked(self, error: Exception) -> None:
        assert self.path is not None
        logger.warning("Invalid immersive-control state %s: %s", self.path, error)
        try:
            destination = await asyncio.to_thread(self._quarantine, self.path)
        except OSError:
            logger.exception("Unable to quarantine state; original retained: %s", self.path)
        else:
            logger.warning("Corrupt immersive-control state preserved at %s", destination)

    async def save(self) -> None:
        async with self._lock:
            self._sweep(float(self.clock()))
            await self._save_locked()

    async def get(self, key: str) -> Session | None:
        async with self._lock:
            if self._sweep(float(self.clock())):
                await self._save_locked()
            session = self._data.get(key)
            return replace(session) if session is not None else None

    async def activate(self, key: str) -> tuple[bool, str]:
        async with self._lock:
            now = float(self.clock())
            changed = self._sweep(now)
            current = self._data.get(key)
            if current is not None and current.active:
                result = (False, "控制状态已激活")
            elif current is not None and current.cooldown_end > now:
                remaining = math.ceil(current.cooldown_end - now)
                result = (False, f"还在休息中，请等待 {remaining} 秒")
            elif sum(session.active for session in self._data.values()) >= self.cfg.max_concurrent:
                result = (False, "并发上限")
            else:
                self._data[key] = Session(
                    active=True,
                    end=now + self.cfg.state_duration,
                    cooldown_end=now + self.cfg.cooldown_seconds,
                )
                changed = True
                result = (True, "ok")
            if changed:
                await self._save_locked()
            return result

    async def deactivate(self, key: str) -> bool:
        async with self._lock:
            now = float(self.clock())
            changed = self._sweep(now)
            session = self._data.get(key)
            success = session is not None and session.active
            if success:
                self._data[key] = replace(session, active=False, exit_ts=now, reason="user")
                changed = True
            if changed:
                await self._save_locked()
            return success

    async def complete_exit(self, key: str) -> Session | None:
        async with self._lock:
            now = float(self.clock())
            changed = self._sweep(now)
            session = self._data.get(key)
            consumed = None
            if session is not None and not session.active and session.exit_ts is not None:
                consumed = replace(session)
                if session.cooldown_end > now:
                    self._data[key] = replace(session, end=None, exit_ts=None, reason=None)
                else:
                    del self._data[key]
                changed = True
            if changed:
                await self._save_locked()
            return consumed

    async def check_cooldown(self, key: str) -> int:
        async with self._lock:
            now = float(self.clock())
            if self._sweep(now):
                await self._save_locked()
            session = self._data.get(key)
            return max(0, math.ceil(session.cooldown_end - now)) if session else 0

    async def clear(self) -> int:
        async with self._lock:
            count = len(self._data)
            if count:
                self._data.clear()
                await self._save_locked()
            return count
