"""Resolve one logical LLM send without changing the framework.

Neo-MoFox's public lifecycle events expose neither the request object nor a
logical execution ID. A request's metadata and its asyncio task may both be
reused for later sends. Keep the necessary read-only compatibility inspection
here: each execute_request creates a distinct policy session, which remains
the same across its provider retries. No frame or task is retained.
"""

from __future__ import annotations

import asyncio
import inspect
import weakref
from collections.abc import Iterator
from types import FrameType
from typing import Any

_EXECUTION_MODULE = "src.kernel.llm.request_execution"


def _frame_scope(frame: FrameType | None, metadata: dict[str, Any], request_name: str) -> object | None:
    if (
        frame is None
        or frame.f_code.co_name != "execute_request"
        or frame.f_globals.get("__name__") != _EXECUTION_MODULE
    ):
        return None
    request = frame.f_locals.get("request")
    if (
        request is None
        or getattr(request, "meta_data", None) is not metadata
        or getattr(request, "request_name", None) != request_name
    ):
        return None
    scope = frame.f_locals.get("session")
    if scope is None:
        return None
    try:
        # WeakKeyDictionary requires both weak-reference support and hashing.
        # The framework's stock policy sessions provide identity-based keys.
        hash(weakref.ref(scope))
    except (TypeError, ValueError):
        return None
    return scope


def _await_chain(awaitable: object) -> Iterator[object]:
    seen: set[int] = set()
    while awaitable is not None and id(awaitable) not in seen:
        seen.add(id(awaitable))
        yield awaitable
        awaitable = (
            getattr(awaitable, "cr_await", None)
            or getattr(awaitable, "gi_yieldfrom", None)
            or getattr(awaitable, "ag_await", None)
        )


def _await_frame(awaitable: object) -> FrameType | None:
    return (
        getattr(awaitable, "cr_frame", None)
        or getattr(awaitable, "gi_frame", None)
        or getattr(awaitable, "ag_frame", None)
    )


def resolve_request_scope(metadata: dict[str, Any], request_name: str) -> object | None:
    """Return the current send's weak-referenceable session, or conservatively None.

    Newer Python versions await an event handler directly inside wait_for.
    Python 3.11 instead runs it in a separate task, so resolve its actual
    waiting parent through wait_for's ``fut`` before inspecting that parent's
    suspended coroutine chain. Metadata identity alone must never choose a
    task: independent requests can deliberately share the same dictionary.
    """
    if not isinstance(metadata, dict) or not isinstance(request_name, str) or not request_name:
        return None

    frame = inspect.currentframe()
    try:
        while frame is not None:
            scope = _frame_scope(frame, metadata, request_name)
            if scope is not None:
                return scope
            frame = frame.f_back
    finally:
        # inspect.currentframe creates a cycle unless its local reference is
        # released; retaining execution frames would also retain the request.
        del frame

    try:
        current_task = asyncio.current_task()
        tasks = asyncio.all_tasks()
    except RuntimeError:
        return None
    if current_task is None:
        return None

    candidate: object | None = None
    for task in tasks:
        if task is current_task:
            continue
        chain = list(_await_chain(task.get_coro()))
        owns_handler = any(
            (parent_frame := _await_frame(awaitable)) is not None
            and parent_frame.f_code.co_name == "wait_for"
            and parent_frame.f_globals.get("__name__") == "asyncio.tasks"
            and parent_frame.f_locals.get("fut") is current_task
            for awaitable in chain
        )
        if not owns_handler:
            continue
        # The nearest matching execution owns this event, including when a
        # request starts another request during context preparation.
        for awaitable in reversed(chain):
            scope = _frame_scope(_await_frame(awaitable), metadata, request_name)
            if scope is not None:
                if candidate is not None and candidate is not scope:
                    return None
                candidate = scope
                break
    return candidate
