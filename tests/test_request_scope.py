"""Standard-library regressions for logical-send scope resolution.

The small execution fixture reproduces the framework's local session lifetime.
It uses the real asyncio.wait_for, including its separate handler task on
Python 3.11. Framework integration tests cover actual Neo-MoFox requests.
"""

from __future__ import annotations

import asyncio
import importlib.util
import unittest
import weakref
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("immersive_request_scope_test", ROOT / "request_scope.py")
assert SPEC is not None and SPEC.loader is not None
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)


class Session:
    def __init__(self) -> None:
        self.token = object()


class UnhashableSession:
    __hash__ = None


EXECUTION_SOURCE = """
async def execute_request(request, emit, *, attempts=1, failure=None, scope_factory=Session):
    session = scope_factory()
    results = []
    for _ in range(attempts):
        results.append(await publish(request, emit))
    if failure == "error":
        raise RuntimeError("execution fixture failed")
    if failure == "cancel":
        raise asyncio.CancelledError("execution fixture cancelled")
    return results
"""


async def publish(request, emit):
    return await asyncio.wait_for(emit(request), timeout=5)


def execution_fixture(module_name="src.kernel.llm.request_execution", function_name="execute_request"):
    namespace = {"__name__": module_name, "Session": Session, "publish": publish, "asyncio": asyncio}
    exec(EXECUTION_SOURCE.replace("def execute_request(", f"def {function_name}("), namespace)
    return namespace[function_name]


async def send(request, emit, **kwargs):
    # Keep execute_request inside a send coroutine, as in the real framework.
    # An asyncio task must not itself retain the execution coroutine afterward.
    return await execution_fixture()(request, emit, **kwargs)


class RequestScopeTests(unittest.IsolatedAsyncioTestCase):
    def request(self, metadata=None):
        return SimpleNamespace(
            meta_data={"stream_id": "scope-review"} if metadata is None else metadata,
            request_name="default_chatter",
        )

    async def test_provider_attempts_share_one_scope_with_real_wait_for(self):
        cache = weakref.WeakKeyDictionary()

        async def emit(request):
            await asyncio.sleep(0)
            scope = HELPER.resolve_request_scope(request.meta_data, request.request_name)
            self.assertIsNotNone(scope)
            cache.setdefault(scope, "exit prompt")
            return weakref.ref(scope), scope.token, cache[scope]

        results = await send(self.request(), emit, attempts=3)
        self.assertTrue(all(result[1] is results[0][1] for result in results))
        self.assertEqual([result[2] for result in results], ["exit prompt"] * 3)
        self.assertTrue(all(result[0]() is None for result in results))
        self.assertEqual(len(cache), 0)

    async def test_shared_metadata_concurrent_requests_have_distinct_scopes(self):
        metadata = {"stream_id": "shared"}
        ready = asyncio.Event()
        started = 0

        async def emit(request):
            nonlocal started
            started += 1
            if started == 2:
                ready.set()
            await ready.wait()
            scope = HELPER.resolve_request_scope(request.meta_data, request.request_name)
            self.assertIsNotNone(scope)
            return scope.token

        first, second = await asyncio.gather(
            send(self.request(metadata), emit), send(self.request(metadata), emit)
        )
        self.assertIsNot(first[0], second[0])

    async def test_later_send_in_same_request_and_task_gets_a_new_scope(self):
        request = self.request()
        owner_task = asyncio.current_task()

        async def emit(incoming):
            scope = HELPER.resolve_request_scope(incoming.meta_data, incoming.request_name)
            self.assertIsNotNone(scope)
            return scope.token

        first = await send(request, emit)
        self.assertIs(asyncio.current_task(), owner_task)
        second = await send(request, emit)
        self.assertIs(asyncio.current_task(), owner_task)
        self.assertIsNot(first[0], second[0])

    async def test_failure_and_cancellation_cache_never_matches_a_later_send(self):
        for failure in ("error", "cancel"):
            with self.subTest(failure=failure):
                request = self.request()
                cache = weakref.WeakKeyDictionary()
                pending = True
                emitted = []

                async def emit(incoming):
                    nonlocal pending
                    await asyncio.sleep(0)
                    scope = HELPER.resolve_request_scope(incoming.meta_data, incoming.request_name)
                    self.assertIsNotNone(scope)
                    if pending:
                        cache[scope] = "exit prompt"
                        pending = False
                    emitted.append((scope.token, cache.get(scope)))

                retained_errors = []
                try:
                    await send(request, emit, attempts=2, failure=failure)
                except (RuntimeError, asyncio.CancelledError) as error:
                    # A caller may keep a traceback and its former session.
                    # A new send must still never match that session's cache.
                    retained_errors.append(error)
                self.assertEqual(len(retained_errors), 1)
                self.assertIs(emitted[0][0], emitted[1][0])
                self.assertEqual([item[1] for item in emitted], ["exit prompt"] * 2)
                await send(request, emit)
                self.assertIsNot(emitted[-1][0], emitted[0][0])
                self.assertIsNone(emitted[-1][1])
                self.assertEqual(request.meta_data, {"stream_id": "scope-review"})

    async def test_missing_execution_or_wrong_request_identity_returns_none(self):
        request = self.request()
        self.assertIsNone(HELPER.resolve_request_scope(request.meta_data, request.request_name))
        self.assertIsNone(HELPER.resolve_request_scope(None, request.request_name))
        self.assertIsNone(HELPER.resolve_request_scope(request.meta_data, ""))

        async def emit(incoming):
            self.assertIsNone(HELPER.resolve_request_scope(dict(incoming.meta_data), incoming.request_name))
            self.assertIsNone(HELPER.resolve_request_scope(incoming.meta_data, "other_request"))

        await send(request, emit)

    async def test_function_and_module_must_both_match(self):
        async def emit(request):
            self.assertIsNone(HELPER.resolve_request_scope(request.meta_data, request.request_name))

        for module_name, function_name in (
            ("another.execution", "execute_request"),
            ("src.kernel.llm.request_execution", "another_function"),
        ):
            with self.subTest(module=module_name, function=function_name):
                await execution_fixture(module_name, function_name)(self.request(), emit)

    async def test_unsupported_weak_keys_return_none(self):
        async def emit(request):
            self.assertIsNone(HELPER.resolve_request_scope(request.meta_data, request.request_name))

        for scope_factory in (list, UnhashableSession, lambda: None):
            with self.subTest(factory=scope_factory):
                await send(self.request(), emit, scope_factory=scope_factory)

    async def test_unrelated_task_with_same_metadata_cannot_be_selected(self):
        request = self.request()
        started = asyncio.Event()
        release = asyncio.Event()

        async def waiting_handler(incoming):
            started.set()
            await release.wait()
            self.assertIsNotNone(HELPER.resolve_request_scope(incoming.meta_data, incoming.request_name))

        task = asyncio.create_task(send(request, waiting_handler))
        try:
            await started.wait()
            self.assertIsNone(HELPER.resolve_request_scope(request.meta_data, request.request_name))
        finally:
            release.set()
            await task


if __name__ == "__main__":
    unittest.main()
