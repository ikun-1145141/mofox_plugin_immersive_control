from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("immersive_control_state_test", ROOT / "state.py")
assert SPEC is not None and SPEC.loader is not None
STATE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = STATE
SPEC.loader.exec_module(STATE)
SessionStore = STATE.SessionStore


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def config(**changes):
    values = dict(
        state_duration=180,
        cooldown_seconds=30,
        max_concurrent=10,
        exit_pending_ttl=86400,
    )
    values.update(changes)
    return SimpleNamespace(**values)


class SessionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_barrier_released_shifts_increment_twice_and_persist(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sessions.json"
            settings = config()
            store = SessionStore(settings, path, clock)
            await store.activate("group", level=3)
            original = await store.get("group")
            clock.advance(5)
            barrier = asyncio.Barrier(2)

            async def shift():
                self.assertEqual((await store.get("group")).level, 3)
                await barrier.wait()
                return await store.shift_level("group", 1)

            results = await asyncio.gather(shift(), shift())
            self.assertEqual(sorted(results), [(True, "ok", 4), (True, "ok", 5)])
            self.assertEqual(await store.get("group"), dataclasses.replace(original, level=5))
            self.assertEqual(await store.check_cooldown("group"), 25)
            restored = SessionStore(settings, path, clock)
            await restored.load()
            self.assertEqual((await restored.get("group")).level, 5)

    async def test_many_concurrent_shifts_stop_at_bounds_without_renewing_deadlines(self):
        clock = Clock()
        store = SessionStore(config(), clock=clock)
        await store.activate("group", level=3)
        original = await store.get("group")
        clock.advance(5)

        async def batch(delta):
            barrier = asyncio.Barrier(20)

            async def shift():
                await barrier.wait()
                return await store.shift_level("group", delta)

            return await asyncio.gather(*(shift() for _ in range(20)))

        up = await batch(1)
        self.assertEqual(sorted(level for ok, _, level in up if ok), [4, 5])
        self.assertEqual(sum(result == (False, "已经是最高档", 5) for result in up), 18)
        self.assertEqual(await store.get("group"), dataclasses.replace(original, level=5))
        down = await batch(-1)
        self.assertEqual(sorted(level for ok, _, level in down if ok), [1, 2, 3, 4])
        self.assertEqual(sum(result == (False, "已经是最低档", 1) for result in down), 16)
        self.assertEqual(await store.get("group"), dataclasses.replace(original, level=1))
        self.assertEqual(await store.check_cooldown("group"), 25)

    async def test_invalid_shifts_leave_active_state_unchanged(self):
        store = SessionStore(config(), clock=Clock())
        await store.activate("group", level=3)
        original = await store.get("group")
        for delta in (True, False, 0, 2, -2, 1.0, -1.0, "1", None):
            with self.subTest(delta=delta):
                self.assertEqual(
                    await store.shift_level("group", delta),
                    (False, "升降档参数必须是 -1 或 1 的整数", None),
                )
                self.assertEqual(await store.get("group"), original)

    async def test_inactive_and_expired_shifts_fail_without_level_or_reactivation(self):
        clock = Clock()
        store = SessionStore(config(state_duration=10), clock=clock)
        failure = (False, "当前未激活控制状态", None)
        self.assertEqual(await store.shift_level("missing", 1), failure)
        await store.activate("manual", level=2)
        await store.deactivate("manual")
        pending = await store.get("manual")
        self.assertEqual(await store.shift_level("manual", 1), failure)
        self.assertEqual(await store.get("manual"), pending)
        await store.activate("expired", level=2)
        clock.advance(11)
        self.assertEqual(await store.shift_level("expired", -1), failure)
        expired = await store.get("expired")
        self.assertFalse(expired.active)
        self.assertEqual((expired.reason, expired.exit_ts, expired.level), ("expire", 1010, 2))

    async def test_level_activation_and_original_positional_fields_are_compatible(self):
        store = SessionStore(config(), clock=Clock())
        self.assertEqual(await store.activate("group", level=2), (True, "ok"))
        self.assertEqual((await store.get("group")).level, 2)
        self.assertEqual(STATE.Session(True, 1180, None, None, 1030).level, 3)
        await store.activate("default")
        self.assertEqual((await store.get("default")).level, 3)

    async def test_switching_level_preserves_duration_cooldown_and_previous_snapshot(self):
        clock = Clock()
        store = SessionStore(config(), clock=clock)
        await store.activate("group", level=2)
        original = await store.get("group")
        clock.advance(5)
        self.assertEqual(await store.set_level("group", 5), (True, "ok"))
        updated = await store.get("group")
        self.assertEqual(updated, dataclasses.replace(original, level=5))
        self.assertEqual(original.level, 2)
        self.assertEqual(updated.end, 1180)
        self.assertEqual(await store.check_cooldown("group"), 25)
        self.assertEqual(await store.activate("group", 1), (False, "控制状态已激活"))
        self.assertEqual(await store.get("group"), updated)

    async def test_parallel_level_updates_preserve_deadlines_and_persist_final_snapshot(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sessions.json"
            settings = config()
            store = SessionStore(settings, path, clock)
            await store.activate("group", level=2)
            original = await store.get("group")
            clock.advance(5)
            results = await asyncio.gather(
                *(store.set_level("group", level) for level in (1, 5, 2, 4, 3, 1, 2, 5))
            )
            self.assertTrue(all(result == (True, "ok") for result in results))
            final = await store.get("group")
            self.assertIn(final.level, range(1, 6))
            self.assertEqual(dataclasses.replace(final, level=original.level), original)
            restored = SessionStore(settings, path, clock)
            await restored.load()
            self.assertEqual(await restored.get("group"), final)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 1)

    async def test_invalid_levels_do_not_activate_or_change_existing_session(self):
        store = SessionStore(config(), clock=Clock())
        await store.activate("active", level=2)
        original = await store.get("active")
        failure = (False, "档位必须是 1–5 的整数")
        for value in (True, False, 0, 6, -1, 1.0, 2.5, "3", None):
            with self.subTest(value=value):
                self.assertEqual(await store.activate("new", value), failure)
                self.assertIsNone(await store.get("new"))
                self.assertEqual(await store.activate("active", value), failure)
                self.assertEqual(await store.set_level("active", value), failure)
                self.assertEqual(await store.get("active"), original)

    async def test_switching_inactive_or_expired_session_fails_without_reactivation(self):
        clock = Clock()
        store = SessionStore(config(state_duration=10), clock=clock)
        failure = (False, "当前未激活控制状态")
        self.assertEqual(await store.set_level("missing", 4), failure)
        await store.activate("manual", level=2)
        await store.deactivate("manual")
        pending = await store.get("manual")
        self.assertEqual(await store.set_level("manual", 4), failure)
        self.assertEqual(await store.get("manual"), pending)
        await store.complete_exit("manual")
        self.assertEqual(await store.set_level("manual", 4), failure)
        await store.activate("expired", level=2)
        await store.activate("other_expired", level=1)
        clock.advance(11)
        self.assertEqual(await store.set_level("expired", 4), failure)
        expired = await store.get("expired")
        self.assertFalse(expired.active)
        self.assertEqual(expired.reason, "expire")
        self.assertEqual(expired.level, 2)
        self.assertEqual(expired.end, 1010)
        self.assertFalse(store._data["other_expired"].active)
        self.assertEqual(store._data["other_expired"].exit_ts, 1010)

    async def test_legacy_json_defaults_level_and_can_save_additive_version_one(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sessions.json"
            document = {
                "version": 1,
                "sessions": {
                    "legacy": {
                        "active": True,
                        "end": 1180.0,
                        "exit_ts": None,
                        "reason": None,
                        "cooldown_end": 1030.0,
                    }
                },
            }
            path.write_text(json.dumps(document), encoding="utf-8")
            store = SessionStore(config(), path, Clock())
            await store.load()
            self.assertEqual((await store.get("legacy")).level, 3)
            self.assertEqual(await store.set_level("legacy", 4), (True, "ok"))
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["version"], 1)
            self.assertEqual(saved["sessions"]["legacy"]["level"], 4)
            self.assertEqual(list(path.parent.glob("*.corrupt-*")), [])

    async def test_invalid_persisted_levels_are_quarantined_with_original_bytes(self):
        for value in (True, False, 0, 6, 1.0, "3", None):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "sessions.json"
                record = dataclasses.asdict(STATE.Session(True, end=1180, cooldown_end=1030))
                record["level"] = value
                payload = json.dumps({"version": 1, "sessions": {"group": record}}).encode("utf-8")
                path.write_bytes(payload)
                store = SessionStore(config(), path, Clock())
                with self.assertLogs(STATE.logger, level="WARNING"):
                    await store.load()
                self.assertIsNone(await store.get("group"))
                quarantined = list(path.parent.glob("sessions.json.corrupt-*"))
                self.assertEqual(len(quarantined), 1)
                self.assertEqual(quarantined[0].read_bytes(), payload)
                self.assertFalse(path.exists())

    async def test_activation_returns_immutable_independent_snapshot(self):
        clock = Clock()
        store = SessionStore(config(), clock=clock)
        self.assertEqual(await store.activate("group"), (True, "ok"))
        first = await store.get("group")
        second = await store.get("group")
        self.assertIsNot(first, second)
        self.assertEqual(first.end, 1180)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            first.active = False
        clock.advance(40)
        self.assertEqual(await store.activate("group"), (False, "控制状态已激活"))
        self.assertEqual((await store.get("group")).end, 1180)

    async def test_parallel_activation_enforces_global_limit(self):
        store = SessionStore(config(max_concurrent=3), clock=Clock())
        results = await asyncio.gather(*(store.activate(str(i)) for i in range(20)))
        self.assertEqual(sum(success for success, _ in results), 3)
        self.assertEqual(sum(message == "并发上限" for _, message in results), 17)

    async def test_parallel_same_session_has_single_activation(self):
        store = SessionStore(config(), clock=Clock())
        results = await asyncio.gather(*(store.activate("group") for _ in range(10)))
        self.assertEqual(sum(success for success, _ in results), 1)

    async def test_expired_other_chat_frees_capacity_and_keeps_real_expiry_time(self):
        clock = Clock()
        store = SessionStore(config(state_duration=10, max_concurrent=1), clock=clock)
        await store.activate("old")
        clock.advance(100)
        self.assertEqual(await store.activate("new"), (True, "ok"))
        old = await store.get("old")
        self.assertFalse(old.active)
        self.assertEqual(old.reason, "expire")
        self.assertEqual(old.exit_ts, 1010)

    async def test_old_expiry_reaction_does_not_revive_after_ttl(self):
        clock = Clock()
        store = SessionStore(config(state_duration=10, exit_pending_ttl=20), clock=clock)
        await store.activate("group")
        clock.advance(31)
        self.assertIsNone(await store.get("group"))
        self.assertIsNone(await store.complete_exit("group"))

    async def test_exit_consumption_is_once_and_preserves_cooldown(self):
        clock = Clock()
        store = SessionStore(config(cooldown_seconds=30), clock=clock)
        await store.activate("group")
        clock.advance(5)
        self.assertTrue(await store.deactivate("group"))
        reactions = await asyncio.gather(*(store.complete_exit("group") for _ in range(10)))
        self.assertEqual(sum(reaction is not None for reaction in reactions), 1)
        reaction = next(value for value in reactions if value is not None)
        self.assertEqual(reaction.reason, "user")
        self.assertEqual(await store.check_cooldown("group"), 25)
        self.assertEqual(await store.activate("group"), (False, "还在休息中，请等待 25 秒"))
        clock.advance(24.9)
        self.assertEqual(await store.check_cooldown("group"), 1)
        self.assertFalse((await store.activate("group"))[0])
        clock.advance(0.1)
        self.assertEqual(await store.activate("group"), (True, "ok"))

    async def test_expired_session_cannot_be_marked_as_manual_exit(self):
        clock = Clock()
        store = SessionStore(config(state_duration=10), clock=clock)
        await store.activate("group")
        clock.advance(11)
        self.assertFalse(await store.deactivate("group"))
        self.assertEqual((await store.complete_exit("group")).reason, "expire")

    async def test_pending_ttl_preserves_longer_cooldown(self):
        clock = Clock()
        store = SessionStore(config(cooldown_seconds=100, exit_pending_ttl=10), clock=clock)
        await store.activate("group")
        await store.deactivate("group")
        clock.advance(11)
        self.assertIsNone(await store.complete_exit("group"))
        self.assertEqual(await store.check_cooldown("group"), 89)
        clock.advance(89)
        self.assertIsNone(await store.get("group"))

    async def test_restart_preserves_active_exit_cooldown_and_clear(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nested" / "sessions.json"
            settings = config()
            first = SessionStore(settings, path, clock)
            await first.activate("active")
            await first.activate("exited")
            clock.advance(3)
            await first.deactivate("exited")
            second = SessionStore(settings, path, clock)
            await second.load()
            self.assertTrue((await second.get("active")).active)
            self.assertEqual((await second.complete_exit("exited")).reason, "user")
            third = SessionStore(settings, path, clock)
            await third.load()
            self.assertIsNone(await third.complete_exit("exited"))
            self.assertEqual(await third.check_cooldown("exited"), 27)
            self.assertEqual(await third.clear(), 2)
            fourth = SessionStore(settings, path, clock)
            await fourth.load()
            self.assertIsNone(await fourth.get("active"))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["sessions"], {})
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    async def test_load_expires_at_original_deadline_and_persists_transition(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sessions.json"
            settings = config(state_duration=10)
            first = SessionStore(settings, path, clock)
            await first.activate("group")
            clock.advance(15)
            restored = SessionStore(settings, path, clock)
            await restored.load()
            record = await restored.get("group")
            self.assertEqual(record.exit_ts, 1010)
            self.assertEqual(record.reason, "expire")
            self.assertFalse(json.loads(path.read_text(encoding="utf-8"))["sessions"]["group"]["active"])

    async def test_corrupt_and_invalid_files_are_quarantined_without_losing_bytes(self):
        samples = [
            b"{ broken",
            b"\xff\xfeinvalid utf8",
            b'{"version": 1, "sessions": {"x": {"active": true, "end": null}}}',
            b'{"version": 1, "sessions": {"x": {"active": true, "end": NaN}}}',
        ]
        for raw in samples:
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "sessions.json"
                path.write_bytes(raw)
                store = SessionStore(config(), path, Clock())
                with self.assertLogs(STATE.logger, level="WARNING"):
                    await store.load()
                quarantined = list(path.parent.glob("sessions.json.corrupt-*"))
                self.assertEqual(len(quarantined), 1)
                self.assertEqual(quarantined[0].read_bytes(), raw)
                self.assertFalse(path.exists())
                self.assertIsNone(await store.get("x"))
                await store.activate("new")
                self.assertTrue(path.exists())
                self.assertEqual(quarantined[0].read_bytes(), raw)

    async def test_missing_persistence_file_and_memory_only_save_are_allowed(self):
        store = SessionStore(config(), clock=Clock())
        await store.load()
        await store.save()
        self.assertEqual(await store.clear(), 0)
        with tempfile.TemporaryDirectory() as folder:
            persistent = SessionStore(config(), Path(folder) / "missing.json", Clock())
            await persistent.load()
            self.assertIsNone(await persistent.get("group"))


if __name__ == "__main__":
    unittest.main()
