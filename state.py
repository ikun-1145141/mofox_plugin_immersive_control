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
    level: int = 3
    voltage: int | None = None


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

    @staticmethod
    def _valid_level(level: object) -> bool:
        return type(level) is int and 1 <= level <= 5

    @staticmethod
    def _valid_voltage(voltage: object) -> bool:
        return type(voltage) is int and 0 <= voltage <= 1_000_000

    @classmethod
    def _electric_error(cls, voltage: object, threshold: object) -> str | None:
        if not cls._valid_voltage(voltage):
            return "电压必须是 0–1000000 的整数"
        if type(threshold) is not int or not 1 <= threshold <= 10_000:
            return "过载阈值必须是 1–10000 的整数"
        return None

    def _activation_error(self, session: Session | None, now: float) -> str | None:
        if session is not None and session.cooldown_end > now:
            remaining = math.ceil(session.cooldown_end - now)
            return f"还在休息中，请等待 {remaining} 秒"
        if sum(record.active for record in self._data.values()) >= self.cfg.max_concurrent:
            return "并发上限"
        return None

    def _new_session(self, now: float, level: int) -> Session:
        return Session(
            active=True,
            end=now + self.cfg.state_duration,
            cooldown_end=now + self.cfg.cooldown_seconds,
            level=level,
        )

    @staticmethod
    def _with_voltage(session: Session, voltage: int, threshold: int, now: float) -> Session:
        updated = replace(session, voltage=voltage)
        if voltage >= threshold:
            updated = replace(updated, active=False, exit_ts=now, reason="overload")
        return updated

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
            if reason not in (None, "user", "expire", "overload"):
                raise ValueError("invalid exit reason")
            if active and (end is None or exit_ts is not None):
                raise ValueError("invalid active session timestamps")
            level = record.get("level", 3)
            if not cls._valid_level(level):
                raise ValueError("invalid session level")
            voltage = record.get("voltage")
            if voltage is not None and not cls._valid_voltage(voltage):
                raise ValueError("invalid session voltage")
            decoded[key] = Session(active, end, exit_ts, reason, float(cooldown_end), level, voltage)
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

    async def activate(self, key: str, level: int = 3) -> tuple[bool, str]:
        async with self._lock:
            now = float(self.clock())
            changed = self._sweep(now)
            current = self._data.get(key)
            if not self._valid_level(level):
                result = (False, "档位必须是 1–5 的整数")
            elif current is not None and current.active:
                result = (False, "控制状态已激活")
            elif error := self._activation_error(current, now):
                result = (False, error)
            else:
                self._data[key] = self._new_session(now, level)
                changed = True
                result = (True, "ok")
            if changed:
                await self._save_locked()
            return result

    async def enable_electric(
        self, key: str, voltage: int, overload_voltage: int, level: int = 3
    ) -> tuple[bool, str, Session | None]:
        """Enable virtual electrical roleplay, activating a new session if needed."""
        async with self._lock:
            now = float(self.clock())
            changed = self._sweep(now)
            current = self._data.get(key)
            error = self._electric_error(voltage, overload_voltage)
            if error is None and not self._valid_level(level):
                error = "档位必须是 1–5 的整数"
            if error is None and (current is None or not current.active):
                error = self._activation_error(current, now)
            if error is not None:
                result = (False, error, None)
            else:
                session = current if current is not None and current.active else self._new_session(now, level)
                updated = self._with_voltage(session, voltage, overload_voltage, now)
                if updated != current:
                    self._data[key] = updated
                    changed = True
                result = (True, "ok", replace(updated))
            if changed:
                await self._save_locked()
            return result

    async def set_voltage(self, key: str, voltage: int, threshold: int) -> tuple[bool, str, Session | None]:
        """Set virtual voltage atomically; reaching the threshold exits once."""
        async with self._lock:
            now = float(self.clock())
            changed = self._sweep(now)
            session = self._data.get(key)
            error = self._electric_error(voltage, threshold)
            if error is not None:
                result = (False, error, None)
            elif session is None or not session.active:
                result = (False, "当前未激活控制状态", None)
            else:
                updated = self._with_voltage(session, voltage, threshold, now)
                if updated != session:
                    self._data[key] = updated
                    changed = True
                result = (True, "ok", replace(updated))
            if changed:
                await self._save_locked()
            return result

    async def shift_voltage(self, key: str, delta: int, threshold: int) -> tuple[bool, str, Session | None]:
        """Apply a relative voltage step to an active virtual electrical session."""
        async with self._lock:
            now = float(self.clock())
            changed = self._sweep(now)
            session = self._data.get(key)
            if type(delta) is not int or delta == 0 or not -1_000_000 <= delta <= 1_000_000:
                result = (False, "调压幅度必须是 -1000000–1000000 范围内的非零整数", None)
            elif error := self._electric_error(0, threshold):
                result = (False, error, None)
            elif session is None or not session.active:
                result = (False, "当前未激活控制状态", None)
            elif session.voltage is None:
                result = (False, "当前未开启虚拟电击模式", None)
            elif session.voltage + delta > 1_000_000:
                result = (False, "电压不能超过 1000000", None)
            else:
                voltage = max(0, session.voltage + delta)
                updated = self._with_voltage(session, voltage, threshold, now)
                if updated != session:
                    self._data[key] = updated
                    changed = True
                result = (True, "ok", replace(updated))
            if changed:
                await self._save_locked()
            return result

    async def off_electric(self, key: str) -> tuple[bool, str, Session | None]:
        """Disable electrical roleplay while keeping the active control session."""
        async with self._lock:
            changed = self._sweep(float(self.clock()))
            session = self._data.get(key)
            if session is None or not session.active:
                result = (False, "当前未激活控制状态", None)
            else:
                updated = replace(session, voltage=None)
                if updated != session:
                    self._data[key] = updated
                    changed = True
                result = (True, "ok", replace(updated))
            if changed:
                await self._save_locked()
            return result

    async def set_level(self, key: str, level: int) -> tuple[bool, str]:
        """Change an active session's level without extending its deadlines."""
        async with self._lock:
            changed = self._sweep(float(self.clock()))
            session = self._data.get(key)
            if not self._valid_level(level):
                result = (False, "档位必须是 1–5 的整数")
            elif session is None or not session.active:
                result = (False, "当前未激活控制状态")
            else:
                if session.level != level:
                    self._data[key] = replace(session, level=level)
                    changed = True
                result = (True, "ok")
            if changed:
                await self._save_locked()
            return result

    async def shift_level(self, key: str, delta: int) -> tuple[bool, str, int | None]:
        """Apply one relative gear step atomically, including its bounds check."""
        async with self._lock:
            changed = self._sweep(float(self.clock()))
            session = self._data.get(key)
            if type(delta) is not int or delta not in (-1, 1):
                result = (False, "升降档参数必须是 -1 或 1 的整数", None)
            elif session is None or not session.active:
                result = (False, "当前未激活控制状态", None)
            elif session.level + delta > 5:
                result = (False, "已经是最高档", session.level)
            elif session.level + delta < 1:
                result = (False, "已经是最低档", session.level)
            else:
                level = session.level + delta
                self._data[key] = replace(session, level=level)
                changed = True
                result = (True, "ok", level)
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
