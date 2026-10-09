"""Integration checks against the real Neo-MoFox framework.

Set NEO_MOFOX_ROOT to a Neo-MoFox checkout, or put it in .reference/Neo-MoFox.
These tests never replace framework components with stubs. The only transport
mock is send_api.send_message; command permissions use a temporary SQLite DB.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
NEO_ROOT = Path(os.environ.get("NEO_MOFOX_ROOT", ROOT / ".reference" / "Neo-MoFox")).resolve()
PACKAGE = "mofox_plugin_immersive_control"
MARKER = "[mofox_immersive_control:transient]\n"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class CapturingClient:
    """Offline provider implementing the real ChatModelClient protocol."""

    def __init__(self, failures: int = 0) -> None:
        self.calls: list[dict[str, object]] = []
        self.failures = failures

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.failures:
            raise TimeoutError("offline provider timed out")
        return "captured offline response", [], None, None, None


@unittest.skipUnless((NEO_ROOT / "src" / "core").is_dir(), "Neo-MoFox source is not available")
class FrameworkIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(NEO_ROOT))
        # Import failures are deliberately visible when source is available:
        # install the checkout's runtime dependencies rather than using stubs.
        from src.app.plugin_system.api import adapter_api, send_api
        from src.core.components import loader, registry, state_manager
        from src.core.components.types import ComponentType, EventType, PermissionLevel
        from src.core.config import core_config
        from src.core.managers import (
            command_manager,
            config_manager,
            event_manager,
            permission_manager,
            plugin_manager,
        )
        from src.core.models.message import Message
        from src.core.models.sql_alchemy import Base, CommandPermissions, PermissionGroups
        from src.kernel import db, event
        from src.kernel.llm import ROLE, LLMPayload, LLMRequest, LLMTimeoutError, Text
        from src.kernel.llm.model_client import ModelClientRegistry

        cls.fw = SimpleNamespace(
            loader=loader,
            registry=registry,
            state_manager=state_manager,
            ComponentType=ComponentType,
            EventType=EventType,
            PermissionLevel=PermissionLevel,
            core_config=core_config,
            command_manager=command_manager,
            config_manager=config_manager,
            event_manager=event_manager,
            permission_manager=permission_manager,
            plugin_manager=plugin_manager,
            Message=Message,
            Base=Base,
            CommandPermissions=CommandPermissions,
            PermissionGroups=PermissionGroups,
            db=db,
            event=event,
            LLMPayload=LLMPayload,
            LLMRequest=LLMRequest,
            LLMTimeoutError=LLMTimeoutError,
            ModelClientRegistry=ModelClientRegistry,
            ROLE=ROLE,
            Text=Text,
            adapter_api=adapter_api,
            send_api=send_api,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        if str(NEO_ROOT) in sys.path:
            sys.path.remove(str(NEO_ROOT))

    async def asyncSetUp(self) -> None:
        f = self.fw
        self.temporary = tempfile.TemporaryDirectory(prefix="immersive_neo_integration_")
        self.workdir = Path(self.temporary.name)
        self.old_cwd = Path.cwd()
        os.chdir(self.workdir)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(os.chdir, self.old_cwd)
        self.saved_globals: list[tuple[object, str, object]] = []

        def replace(module: object, attribute: str, value: object) -> None:
            self.saved_globals.append((module, attribute, getattr(module, attribute)))
            setattr(module, attribute, value)

        replace(f.event, "_event_bus", f.event.EventBus(name="immersive-integration"))
        replace(f.registry, "_global_registry", f.registry.ComponentRegistry())
        replace(f.state_manager, "_global_state_manager", f.state_manager.StateManager())
        replace(f.plugin_manager, "_global_plugin_manager", f.plugin_manager.PluginManager())
        replace(f.config_manager, "_global_config_manager", f.config_manager.ConfigManager())
        replace(f.event_manager, "_event_manager", f.event_manager.EventManager())
        replace(f.permission_manager, "_global_permission_manager", f.permission_manager.PermissionManager())
        replace(f.core_config, "_global_config", f.core_config.CoreConfig())
        self.addCleanup(self.restore_globals)
        self.core_config = f.core_config.get_core_config()
        self.core_config.permissions.owner_list = ["test:owner"]

        await f.db.reset_session_factory()
        await f.db.reset_engine_state()
        f.db.reset_db_cache()
        f.db.configure_engine("sqlite+aiosqlite:///:memory:", apply_optimizations=False)
        engine = await f.db.get_engine()
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: f.Base.metadata.create_all(
                    sync_connection,
                    tables=[f.PermissionGroups.__table__, f.CommandPermissions.__table__],
                )
            )
        permission_manager = f.permission_manager.get_permission_manager()
        await permission_manager.set_user_permission_group(
            person_id=permission_manager.generate_person_id("test", "operator"),
            level=f.PermissionLevel.OPERATOR,
            reason="Offline integration fixture",
        )
        self.assertEqual(
            await permission_manager.get_user_permission_level(
                permission_manager.generate_person_id("test", "operator")
            ),
            f.PermissionLevel.OPERATOR,
        )
        self.manager = f.plugin_manager.get_plugin_manager()
        self.bus = f.event.get_event_bus()
        self.command_manager = f.command_manager.CommandManager()
        self.transport = AsyncMock(return_value=True)
        self.transport_patch = patch.object(f.send_api, "send_message", self.transport)
        self.transport_patch.start()
        self.addCleanup(self.transport_patch.stop)

        # Explicit importlib package bootstrap keeps tests independent of the
        # directory name into which this repository was cloned.
        self.remove_package_modules()
        spec = importlib.util.spec_from_file_location(
            PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
        )
        assert spec is not None and spec.loader is not None
        package = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE] = package
        spec.loader.exec_module(package)
        self.addCleanup(self.remove_package_modules)
        self.manifest = await f.loader.load_manifest(str(ROOT))
        self.assertIsNotNone(self.manifest, "real loader must read manifest.json")
        assert self.manifest is not None
        self.plugin_name = self.manifest.name
        f.loader.unregister_plugin(self.plugin_name)
        self.assertTrue(await self.manager.load_plugin_from_manifest(str(ROOT), self.manifest))
        self.plugin = self.manager.get_plugin(self.plugin_name)
        self.assertIsNotNone(self.plugin)
        self.clock = Clock()
        self.plugin.store.clock = self.clock
        self.plugin.settings.persist_state = False

    def restore_globals(self) -> None:
        for module, attribute, value in reversed(self.saved_globals):
            setattr(module, attribute, value)

    @staticmethod
    def remove_package_modules() -> None:
        for name in tuple(sys.modules):
            if name == PACKAGE or name.startswith(PACKAGE + "."):
                sys.modules.pop(name, None)

    async def asyncTearDown(self) -> None:
        if hasattr(self, "manager") and hasattr(self, "plugin_name"):
            if self.manager.is_plugin_loaded(self.plugin_name):
                self.assertTrue(await self.manager.unload_plugin(self.plugin_name))
        await self.fw.db.reset_session_factory()
        await self.fw.db.reset_engine_state()
        self.fw.db.reset_db_cache()

    def message(
        self,
        content: str,
        *,
        stream: str = "group-a",
        sender: str = "operator",
        group: str | None = "123456",
        **extra: object,
    ):
        return self.fw.Message(
            message_id="test-message",
            content=content,
            processed_plain_text=content,
            stream_id=stream,
            sender_id=sender,
            sender_name="Test operator",
            platform="test",
            chat_type="group" if group else "private",
            group_id=group,
            **extra,
        )

    async def receive(self, message):
        envelope = {"message_info": {"message_id": message.message_id}, "unrelated": "preserved"}
        params = {
            "message": message,
            "envelope": envelope,
            "adapter_signature": "integration:adapter:offline",
        }
        decision, returned = await self.bus.publish(self.fw.EventType.ON_MESSAGE_RECEIVED, params)
        self.assertEqual(set(returned), set(params), "EventBus requires unchanged top-level keys")
        self.assertIs(returned["message"], message)
        self.assertIs(returned["envelope"], envelope)
        self.assertEqual(returned["adapter_signature"], "integration:adapter:offline")
        return decision

    async def request(self, stream: str | None, payloads=None, *, name="default_chatter"):
        f = self.fw
        if payloads is None:
            payloads = [
                f.LLMPayload(f.ROLE.SYSTEM, f.Text("Existing persona and unrelated plugin instructions.")),
                f.LLMPayload(f.ROLE.USER, f.Text("Original user prompt, history and extra content.")),
            ]
        params = {
            "request_name": name,
            "model_identifier": "offline-test-model",
            "stream": True,
            "tools": [],
            "payloads": list(payloads),
            "meta_data": {"stream_id": stream} if stream is not None else {},
        }
        decision, returned = await self.bus.publish(f.EventType.BEFORE_LLM_REQUEST, params)
        self.assertEqual(set(returned), set(params))
        self.assertEqual(returned["model_identifier"], "offline-test-model")
        self.assertEqual(returned["tools"], [])
        self.assertTrue(returned["stream"])
        return returned["payloads"]

    def text(self, payloads) -> str:
        return "\n".join(
            part.text for payload in payloads for part in payload.content if isinstance(part, self.fw.Text)
        )

    def transient_text(self, payloads) -> list[str]:
        return [
            part.text
            for payload in payloads
            for part in payload.content
            if payload.role == self.fw.ROLE.SYSTEM
            and isinstance(part, self.fw.Text)
            and part.text.startswith(MARKER)
        ]

    def new_llm_request(self, client: CapturingClient, *, max_retry: int = 0):
        f = self.fw
        model = {
            "api_provider": "offline-provider",
            "base_url": "http://127.0.0.1:1",
            "model_identifier": "offline-model",
            "api_key": "offline-placeholder",
            "client_type": "openai",
            "max_retry": max_retry,
            "timeout": 2,
            "retry_interval": 0,
            "price_in": 0,
            "price_out": 0,
            "temperature": 0,
            "max_tokens": 128,
            "extra_params": {},
        }
        return f.LLMRequest(
            model_set=[model],
            request_name="default_chatter",
            meta_data={"stream_id": "group-a"},
            payloads=[
                f.LLMPayload(f.ROLE.SYSTEM, f.Text("Permanent persona.")),
                f.LLMPayload(f.ROLE.USER, f.Text("History and unrelated extra instructions.")),
            ],
            clients=f.ModelClientRegistry(openai=client, anthropic=client),
            enable_metrics=False,
        )

    async def test_loader_registers_native_components_and_generates_toml(self) -> None:
        f = self.fw
        config_path = self.workdir / "config" / "plugins" / self.plugin_name / "config.toml"
        self.assertTrue(config_path.is_file())
        self.assertIn("[plugin]", config_path.read_text(encoding="utf-8"))
        self.assertIn("[prompts]", config_path.read_text(encoding="utf-8"))
        self.assertEqual(self.plugin.settings.state_duration, 180)
        commands = f.registry.get_global_registry().get_by_plugin_and_type(
            self.plugin_name, f.ComponentType.COMMAND
        )
        self.assertEqual(set(commands), {"imm_status", "imm_clear", "imm_reload"})
        for command in commands.values():
            self.assertEqual(command.permission_level, f.PermissionLevel.OPERATOR)
        self.assertTrue(self.bus.get_subscribers(f.EventType.ON_MESSAGE_RECEIVED))
        self.assertTrue(self.bus.get_subscribers(f.EventType.BEFORE_LLM_REQUEST))
        self.assertTrue(await self.manager.unload_plugin(self.plugin_name))
        self.assertFalse(self.bus.get_subscribers(f.EventType.ON_MESSAGE_RECEIVED))
        self.assertFalse(self.bus.get_subscribers(f.EventType.BEFORE_LLM_REQUEST))

    async def test_trigger_continues_message_event_and_preserves_metadata(self) -> None:
        incoming = self.message("/td", mentioned=True, unrelated={"keep": "this"})
        extra_before = dict(incoming.extra)
        self.assertEqual(await self.receive(incoming), self.fw.event.EventDecision.SUCCESS)
        self.assertEqual(incoming.content, "td")
        self.assertEqual(incoming.processed_plain_text, "td")
        self.assertEqual(incoming.extra, extra_before)
        payloads = await self.request("group-a")
        self.assertEqual(len(self.transient_text(payloads)), 1)
        self.assertIn("已激活", self.transient_text(payloads)[0])
        self.assertIn("Existing persona", self.text(payloads))
        self.assertIn("Original user prompt, history and extra content.", self.text(payloads))
        self.transport.assert_not_awaited()

    async def test_group_mode_is_shared_but_streams_are_isolated_and_exit_once(self) -> None:
        await self.receive(self.message("/td", sender="member-1"))
        self.assertEqual(self.transient_text(await self.request("group-b")), [])
        self.assertEqual(self.transient_text(await self.request(None)), [])
        self.assertEqual(len(self.transient_text(await self.request("group-a"))), 1)
        await self.receive(self.message("/td stop", sender="member-2"))
        first_exit = await self.request("group-a")
        self.assertEqual(len(self.transient_text(first_exit)), 1)
        self.assertIn("已结束", self.transient_text(first_exit)[0])
        self.assertEqual(self.transient_text(await self.request("group-a")), [])

    async def test_expiry_emits_one_exit_reaction_without_background_timer(self) -> None:
        await self.receive(self.message("/控制"))
        self.clock.now += self.plugin.settings.state_duration + 1
        expired = await self.request("group-a")
        self.assertEqual(len(self.transient_text(expired)), 1)
        self.assertIn("已结束", self.transient_text(expired)[0])
        self.assertEqual(self.transient_text(await self.request("group-a")), [])

    async def test_inherited_markers_are_replaced_and_removed_from_other_requests(self) -> None:
        f = self.fw
        await self.receive(self.message("/td"))
        inherited = [
            f.LLMPayload(f.ROLE.SYSTEM, [f.Text("Retained system instruction."), f.Text(MARKER + "OLD")]),
            f.LLMPayload(f.ROLE.SYSTEM, f.Text(MARKER + "DUPLICATE")),
            f.LLMPayload(f.ROLE.USER, f.Text("Retained user content.")),
        ]
        updated = await self.request("group-a", inherited)
        self.assertEqual(len(self.transient_text(updated)), 1)
        self.assertNotIn("OLD", self.text(updated))
        self.assertNotIn("DUPLICATE", self.text(updated))
        self.assertIn("Retained system instruction.", self.text(updated))
        self.assertIn("Retained user content.", self.text(updated))
        for request_name in ("memory_summary", "default_chatter_sub_agent"):
            cleaned = await self.request("group-a", inherited, name=request_name)
            self.assertEqual(self.transient_text(cleaned), [])
            self.assertIn("Retained system instruction.", self.text(cleaned))
            self.assertIn("Retained user content.", self.text(cleaned))
        # Sanitizing derived requests must not mutate history payloads in place.
        self.assertIn(MARKER + "OLD", self.text(inherited))

    async def test_auxiliary_request_does_not_consume_pending_exit(self) -> None:
        await self.receive(self.message("/td"))
        await self.receive(self.message("/td stop"))
        self.assertEqual(self.transient_text(await self.request("group-a", name="summary")), [])
        self.assertIn("已结束", self.transient_text(await self.request("group-a"))[0])
        self.assertEqual(self.transient_text(await self.request("group-a")), [])

    async def test_cooldown_intercepts_and_sends_explicit_group_reply(self) -> None:
        await self.receive(self.message("/td"))
        decision = await self.receive(self.message("/td"))
        self.assertEqual(decision, self.fw.event.EventDecision.STOP)
        self.transport.assert_awaited_once()
        outgoing = self.transport.await_args.args[0]
        self.assertEqual(outgoing.stream_id, "group-a")
        self.assertEqual(outgoing.extra["target_group_id"], "123456")
        self.assertTrue(outgoing.content)

    async def test_commands_dispatch_through_real_manager_and_send_replies(self) -> None:
        message_ids: list[str] = []
        for command in ("imm_status", "imm_clear", "imm_reload"):
            with self.subTest(command=command):
                before = self.transport.await_count
                success, result = await self.command_manager.execute_command(self.message("/" + command))
                self.assertTrue(success, result)
                self.assertEqual(self.transport.await_count, before + 1)
                outgoing = self.transport.await_args.args[0]
                self.assertEqual(outgoing.extra["target_group_id"], "123456")
                self.assertTrue(outgoing.content)
                self.assertTrue(outgoing.message_id)
                message_ids.append(outgoing.message_id)
        success, result = await self.command_manager.execute_command(
            self.message("/imm_status", stream="private-a", group=None)
        )
        self.assertTrue(success, result)
        self.assertEqual(self.transport.await_args.args[0].extra["target_user_id"], "operator")
        message_ids.append(self.transport.await_args.args[0].message_id)
        self.assertEqual(len(message_ids), len(set(message_ids)), "Each outgoing response needs a unique ID")

    async def test_regular_user_cannot_dispatch_operator_commands(self) -> None:
        for command in ("imm_status", "imm_clear", "imm_reload"):
            with self.subTest(command=command):
                success, result = await self.command_manager.execute_command(
                    self.message("/" + command, sender="ordinary-user")
                )
                self.assertFalse(success)
                self.assertIn("权限", result)
        self.transport.assert_not_awaited()

    async def test_reload_command_reads_changed_toml_and_clear_removes_mode(self) -> None:
        await self.receive(self.message("/td"))
        config_path = self.workdir / "config" / "plugins" / self.plugin_name / "config.toml"
        original = config_path.read_text(encoding="utf-8")
        self.assertIn("sensitivity = 50", original)
        config_path.write_text(original.replace("sensitivity = 50", "sensitivity = 73"), encoding="utf-8")
        success, result = await self.command_manager.execute_command(self.message("/imm_reload"))
        self.assertTrue(success, result)
        self.assertEqual(self.plugin.settings.sensitivity, 73)
        self.assertIn("73%", self.transient_text(await self.request("group-a"))[0])
        success, result = await self.command_manager.execute_command(self.message("/imm_clear"))
        self.assertTrue(success, result)
        self.assertEqual(self.transient_text(await self.request("group-a")), [])

    async def test_real_llm_send_delivers_and_replaces_transient_payloads(self) -> None:
        client = CapturingClient()
        request = self.new_llm_request(client)
        await self.receive(self.message("/td"))
        response = await request.send(auto_append_response=False, stream=False)
        self.assertEqual(response.message, "captured offline response")
        captured = client.calls[-1]["payloads"]
        self.assertEqual(len(self.transient_text(captured)), 1)
        self.assertIn("已激活", self.transient_text(captured)[0])
        self.assertIn("Permanent persona.", self.text(captured))
        self.assertIn("History and unrelated extra instructions.", self.text(captured))
        await self.receive(self.message("/td stop"))
        await request.send(auto_append_response=False, stream=False)
        self.assertEqual(len(self.transient_text(client.calls[-1]["payloads"])), 1)
        self.assertIn("已结束", self.transient_text(client.calls[-1]["payloads"])[0])
        await request.send(auto_append_response=False, stream=False)
        self.assertEqual(self.transient_text(client.calls[-1]["payloads"]), [])
        self.assertEqual(len(client.calls), 3)

    async def test_exit_prompt_survives_real_provider_retry_once(self) -> None:
        await self.receive(self.message("/td"))
        await self.receive(self.message("/td stop"))
        client = CapturingClient(failures=1)
        request = self.new_llm_request(client, max_retry=1)
        response = await request.send(auto_append_response=False, stream=False)
        self.assertEqual(response.message, "captured offline response")
        self.assertEqual(len(client.calls), 2)
        first = self.transient_text(client.calls[0]["payloads"])
        second = self.transient_text(client.calls[1]["payloads"])
        self.assertEqual(len(first), 1)
        self.assertEqual(second, first)
        self.assertIn("已结束", first[0])
        self.assertNotIn("_mofox_immersive_exit_retry", request.meta_data)
        await request.send(auto_append_response=False, stream=False)
        self.assertEqual(self.transient_text(client.calls[-1]["payloads"]), [])

    async def test_exhausted_provider_retry_cleans_exit_metadata(self) -> None:
        await self.receive(self.message("/td"))
        await self.receive(self.message("/td stop"))
        client = CapturingClient(failures=2)
        request = self.new_llm_request(client, max_retry=1)
        with self.assertRaises(self.fw.LLMTimeoutError):
            await request.send(auto_append_response=False, stream=False)
        self.assertEqual(len(client.calls), 2)
        self.assertNotIn("_mofox_immersive_exit_retry", request.meta_data)

    async def test_invalid_reload_keeps_admin_restriction_and_config_file(self) -> None:
        previous_config = self.plugin.config
        previous_config.plugin.admin_only_mode = True
        config_path = self.workdir / "config" / "plugins" / self.plugin_name / "config.toml"
        original = config_path.read_text(encoding="utf-8")
        self.assertIn("admin_only_mode = false", original)
        invalid = original.replace("admin_only_mode = false", 'admin_only_mode = "invalid boolean"')
        config_path.write_text(invalid, encoding="utf-8")
        success, result = await self.command_manager.execute_command(self.message("/imm_reload"))
        self.assertFalse(success)
        self.assertIn("失败", result)
        self.assertIs(self.plugin.config, previous_config)
        self.assertTrue(self.plugin.settings.admin_only_mode)
        self.assertIs(self.plugin.store.cfg, self.plugin.settings)
        self.assertEqual(config_path.read_text(encoding="utf-8"), invalid)
        self.transport.assert_awaited_once()

    async def test_release_zip_installs_through_real_archive_loader(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "immersive_release_test", ROOT / "scripts" / "build_release.py"
        )
        assert spec is not None and spec.loader is not None
        release = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(release)
        archive_path = self.workdir / "installable-immersive.zip"
        with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
            for filename in release.FILES:
                archive.write(ROOT / filename, f"{self.plugin_name}/{filename}")
        self.assertTrue(await self.manager.unload_plugin(self.plugin_name))
        self.fw.config_manager.get_config_manager().remove_config(self.plugin_name)
        config_path = self.workdir / "config" / "plugins" / self.plugin_name / "config.toml"
        config_path.unlink()
        manifest = await self.fw.loader.load_manifest(str(archive_path))
        self.assertIsNotNone(manifest)
        self.assertTrue(await self.manager.load_plugin_from_manifest(str(archive_path), manifest))
        self.plugin = self.manager.get_plugin(self.plugin_name)
        self.plugin.store.clock = self.clock
        self.assertTrue(config_path.is_file())
        loaded_module = sys.modules[type(self.plugin).__module__]
        self.assertNotEqual(Path(loaded_module.__file__).resolve(), ROOT / "plugin.py")
        await self.receive(self.message("/td"))
        self.assertEqual(len(self.transient_text(await self.request("group-a"))), 1)

    async def test_group_keywords_require_the_correct_bot_mention(self) -> None:
        with patch.object(
            self.fw.adapter_api,
            "get_bot_info_by_platform",
            AsyncMock(return_value={"bot_id": "bot-123"}),
        ):
            for text, at_users in (
                ("控制", []),
                ("@<Other:other-id> 控制", [{"user_id": "other-id"}]),
                ("这是关于控制模式的普通对话", [{"user_id": "bot-123"}]),
            ):
                await self.receive(self.message(text, at_users=at_users))
                self.assertEqual(self.transient_text(await self.request("group-a")), [])
            mentioned = self.message("@<Robot:bot-123> 控制", at_users=[{"user_id": "bot-123"}])
            await self.receive(mentioned)
            self.assertEqual(mentioned.extra["at_users"], [{"user_id": "bot-123"}])
            self.assertEqual(len(self.transient_text(await self.request("group-a"))), 1)
        await self.receive(self.message("/td", stream="group-explicit"))
        self.assertEqual(len(self.transient_text(await self.request("group-explicit"))), 1)
        await self.receive(self.message("控制", stream="private-unprefixed", group=None))
        self.assertEqual(len(self.transient_text(await self.request("private-unprefixed"))), 1)
        self.transport.assert_not_awaited()

    async def test_admin_only_mode_uses_real_permission_levels(self) -> None:
        self.plugin.settings.admin_only_mode = True
        denied = await self.receive(self.message("/td", sender="ordinary-user"))
        self.assertEqual(denied, self.fw.event.EventDecision.STOP)
        self.assertEqual(self.transient_text(await self.request("group-a")), [])
        self.transport.assert_awaited_once()
        self.assertIn("操作员", self.transport.await_args.args[0].content)
        allowed = await self.receive(self.message("/td", sender="operator"))
        self.assertEqual(allowed, self.fw.event.EventDecision.SUCCESS)
        self.assertEqual(len(self.transient_text(await self.request("group-a"))), 1)
        owner_allowed = await self.receive(self.message("/td", stream="owner-stream", sender="owner"))
        self.assertEqual(owner_allowed, self.fw.event.EventDecision.SUCCESS)
        self.assertEqual(len(self.transient_text(await self.request("owner-stream"))), 1)

    async def test_disabling_plugin_cleans_inherited_mode_and_prevents_activation(self) -> None:
        await self.receive(self.message("/td"))
        inherited = await self.request("group-a")
        self.assertEqual(len(self.transient_text(inherited)), 1)
        self.plugin.settings.enabled = False
        await self.receive(self.message("/td", stream="group-disabled"))
        self.assertEqual(self.transient_text(await self.request("group-disabled")), [])
        self.assertEqual(self.transient_text(await self.request("group-a", inherited)), [])
        self.assertIn("Existing persona", self.text(await self.request("group-a", inherited)))

    async def test_gear_default_preserves_legacy_sensitivity_and_honors_config(self) -> None:
        self.assertEqual(self.plugin.settings.default_level, 3)
        self.assertEqual(self.plugin.settings.level_multipliers, [0.2, 0.6, 1.0, 1.5, 2.0])
        config_path = self.workdir / "config" / "plugins" / self.plugin_name / "config.toml"
        generated = config_path.read_text(encoding="utf-8")
        self.assertIn("default_level", generated)
        self.assertIn("level_multipliers", generated)
        self.plugin.config.prompts.enter_template = "LEVEL:{level} NAME:{level_name} EFFECTIVE:{sensitivity}"
        await self.receive(self.message("/td"))
        self.assertEqual((await self.plugin.store.get("group-a")).level, 3)
        self.assertIn("LEVEL:3 NAME:中档 EFFECTIVE:50", self.transient_text(await self.request("group-a"))[0])
        self.plugin.settings.default_level = 4
        await self.receive(self.message("/td", stream="configured-default"))
        self.assertEqual((await self.plugin.store.get("configured-default")).level, 4)
        self.assertIn(
            "LEVEL:4 NAME:高档 EFFECTIVE:75",
            self.transient_text(await self.request("configured-default"))[0],
        )

    async def test_gear_change_replaces_provider_prompt_immediately_and_clamps_sensitivity(self) -> None:
        self.plugin.config.prompts.enter_template = "LEVEL:{level} NAME:{level_name} EFFECTIVE:{sensitivity}"
        client = CapturingClient()
        request = self.new_llm_request(client)
        await self.receive(self.message("/控制 2"))
        await request.send(auto_append_response=False, stream=False)
        first = self.transient_text(client.calls[-1]["payloads"])
        self.assertEqual(len(first), 1)
        self.assertIn("LEVEL:2 NAME:低档 EFFECTIVE:30", first[0])
        await self.receive(self.message("/档位 4", sender="another-group-member"))
        await request.send(auto_append_response=False, stream=False)
        second = self.transient_text(client.calls[-1]["payloads"])
        self.assertEqual(len(second), 1)
        self.assertIn("LEVEL:4 NAME:高档 EFFECTIVE:75", second[0])
        self.assertNotIn("LEVEL:2", second[0])
        self.assertIn("Permanent persona.", self.text(client.calls[-1]["payloads"]))
        self.assertIn("History and unrelated extra instructions.", self.text(client.calls[-1]["payloads"]))
        self.plugin.settings.sensitivity = 80
        await self.receive(self.message("/档位 5"))
        await request.send(auto_append_response=False, stream=False)
        self.assertIn("LEVEL:5 NAME:强档 EFFECTIVE:100", self.transient_text(client.calls[-1]["payloads"])[0])
        self.assertEqual(len(client.calls), 3)

    async def test_gear_aliases_query_and_boundaries_preserve_active_state(self) -> None:
        await self.receive(self.message("/td 5", sender="ordinary-user"))
        original = await self.plugin.store.get("group-a")
        self.assertEqual(original.level, 5)
        for query in ("/档位", "/td level"):
            with self.subTest(query=query):
                before = self.transport.await_count
                await self.receive(self.message(query, sender="ordinary-user"))
                self.assertEqual(self.transport.await_count, before + 1)
                self.assertIn("5", self.transport.await_args.args[0].content)
                self.assertIn("强档", self.transport.await_args.args[0].content)
                self.assertEqual(await self.plugin.store.get("group-a"), original)
        for command, expected in (
            ("/td level 2", 2),
            ("/升档", 3),
            ("/降档", 2),
            ("/调档 1", 1),
            ("/降档", 1),
            ("/档位 5", 5),
            ("/升档", 5),
        ):
            with self.subTest(command=command):
                await self.receive(self.message(command, sender="ordinary-user"))
                current = await self.plugin.store.get("group-a")
                self.assertEqual(current.level, expected)
                self.assertEqual(current.end, original.end)
                self.assertEqual(current.cooldown_end, original.cooldown_end)
        success, result = await self.command_manager.execute_command(self.message("/imm_status"))
        self.assertTrue(success, result)
        self.assertIn("档位", result)
        self.assertIn("强档", result)

    async def test_gear_changes_do_not_extend_deadlines_or_reactivate_expired_sessions(self) -> None:
        await self.receive(self.message("/td 2"))
        original = await self.plugin.store.get("group-a")
        self.clock.now += 10
        await self.receive(self.message("/调档 5"))
        updated = await self.plugin.store.get("group-a")
        self.assertEqual(updated.level, 5)
        self.assertEqual(updated.end, original.end)
        self.assertEqual(updated.cooldown_end, original.cooldown_end)
        self.clock.now = original.cooldown_end + 1
        await self.receive(self.message("/档位 4"))
        changed_again = await self.plugin.store.get("group-a")
        self.assertEqual(changed_again.level, 4)
        self.assertEqual(changed_again.end, original.end)
        self.assertEqual(changed_again.cooldown_end, original.cooldown_end)
        self.clock.now = original.end + 1
        await self.receive(self.message("/升档"))
        expired = await self.plugin.store.get("group-a")
        self.assertFalse(expired.active)
        self.assertEqual(expired.level, 4)
        self.assertEqual(expired.end, original.end)
        self.assertIn("已结束", self.transient_text(await self.request("group-a"))[0])
        self.assertEqual(self.transient_text(await self.request("group-a")), [])

    async def test_gear_concurrent_up_commands_apply_both_increments_without_extending_state(self) -> None:
        await self.receive(self.message("/td"))
        original = await self.plugin.store.get("group-a")
        self.assertEqual(original.level, 3)
        tasks = []
        try:
            # Queue both real handlers behind the real state lock, as can happen
            # while another session is persisted. A get-then-set handler loses
            # an increment here; an atomic shift applies both operations.
            async with self.plugin.store._lock:
                tasks = [
                    asyncio.create_task(self.receive(self.message("/升档", sender="member-1"))),
                    asyncio.create_task(self.receive(self.message("/升档", sender="member-2"))),
                ]
                for _ in range(40):
                    waiters = getattr(self.plugin.store._lock, "_waiters", None) or ()
                    if len(waiters) >= 2:
                        break
                    await asyncio.sleep(0)
                self.assertGreaterEqual(len(waiters), 2, "Both handlers must be waiting before release")
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        changed = await self.plugin.store.get("group-a")
        self.assertEqual(changed.level, 5)
        self.assertEqual(changed.end, original.end)
        self.assertEqual(changed.cooldown_end, original.cooldown_end)
        self.assertEqual(self.transport.await_count, 2)

    async def test_gear_values_are_shared_within_one_stream_and_isolated_between_streams(self) -> None:
        await self.receive(self.message("/控制 2", sender="member-1"))
        await self.receive(self.message("/控制 5", stream="group-b", sender="member-2"))
        await self.receive(self.message("/档位 4", sender="member-3"))
        self.assertEqual((await self.plugin.store.get("group-a")).level, 4)
        self.assertEqual((await self.plugin.store.get("group-b")).level, 5)
        self.assertEqual(self.transient_text(await self.request("group-c")), [])
        await self.receive(self.message("/td level", stream="group-b", sender="member-4"))
        self.assertIn("强档", self.transport.await_args.args[0].content)
        self.assertIn("高档", self.transient_text(await self.request("group-a"))[0])
        self.assertIn("强档", self.transient_text(await self.request("group-b"))[0])

    async def test_gear_invalid_levels_and_inactive_adjustments_never_activate_sessions(self) -> None:
        for command in (
            "/控制 0",
            "/td 6",
            "/控制 档位",
            "/控制 六档",
            "/控制 两档",
            "/档位 4",
            "/升档",
            "/降档",
            "/td level 0",
        ):
            with self.subTest(command=command):
                before = self.transport.await_count
                await self.receive(self.message(command))
                self.assertEqual(self.transport.await_count, before + 1)
                self.assertIsNone(await self.plugin.store.get("group-a"))
                self.assertEqual(self.transient_text(await self.request("group-a")), [])
        await self.receive(self.message("/td 2"))
        original = await self.plugin.store.get("group-a")
        for command in ("/档位 0", "/调档 6", "/td level 6"):
            await self.receive(self.message(command))
            self.assertEqual(await self.plugin.store.get("group-a"), original)

    async def test_gear_query_and_change_obey_real_admin_only_permissions(self) -> None:
        await self.receive(self.message("/td 2"))
        self.plugin.settings.admin_only_mode = True
        original = await self.plugin.store.get("group-a")
        for command in ("/档位", "/档位 4", "/td level", "/升档"):
            with self.subTest(command=command):
                decision = await self.receive(self.message(command, sender="ordinary-user"))
                self.assertEqual(decision, self.fw.event.EventDecision.STOP)
                self.assertIn("操作员", self.transport.await_args.args[0].content)
                self.assertEqual(await self.plugin.store.get("group-a"), original)
        await self.receive(self.message("/档位 4", sender="operator"))
        self.assertEqual((await self.plugin.store.get("group-a")).level, 4)
        await self.receive(self.message("/档位", sender="operator"))
        self.assertIn("高档", self.transport.await_args.args[0].content)

    async def test_gear_group_query_and_change_require_correct_bot_mention(self) -> None:
        await self.receive(self.message("/td 2"))
        with patch.object(
            self.fw.adapter_api,
            "get_bot_info_by_platform",
            AsyncMock(return_value={"bot_id": "bot-123"}),
        ):
            for text, at_users in (
                ("档位 4", []),
                ("档位", []),
                ("@<Other:other-id> 档位 4", [{"user_id": "other-id"}]),
                ("@<Other:other-id> 档位", [{"user_id": "other-id"}]),
            ):
                before = self.transport.await_count
                await self.receive(self.message(text, at_users=at_users))
                self.assertEqual(self.transport.await_count, before)
                self.assertEqual((await self.plugin.store.get("group-a")).level, 2)
            mentioned = self.message("@<Robot:bot-123> 档位 4", at_users=[{"user_id": "bot-123"}])
            await self.receive(mentioned)
            self.assertEqual((await self.plugin.store.get("group-a")).level, 4)
            self.assertEqual(mentioned.extra["at_users"], [{"user_id": "bot-123"}])
            await self.receive(self.message("@<Robot:bot-123> 档位", at_users=[{"user_id": "bot-123"}]))
            self.assertIn("高档", self.transport.await_args.args[0].content)
        await self.receive(self.message("/降档"))
        self.assertEqual((await self.plugin.store.get("group-a")).level, 3)

    async def test_gear_native_config_rejects_invalid_bounds_and_multiplier_values(self) -> None:
        config_class = type(self.plugin.config)
        for values in (
            {"default_level": 0},
            {"default_level": 6},
            {"level_multipliers": [0.2, 0.6, 1.0, 1.5]},
            {"level_multipliers": [0.2, 0.6, 1.0, 1.5, 2.0, 3.0]},
            {"level_multipliers": [0.2, 0.6, -1.0, 1.5, 2.0]},
            {"level_multipliers": [0.2, 0.6, float("nan"), 1.5, 2.0]},
            {"level_multipliers": [0.2, 0.6, float("inf"), 1.5, 2.0]},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                config_class.model_validate({"plugin": values})
        config_path = self.workdir / "config" / "plugins" / self.plugin_name / "config.toml"
        original_file = config_path.read_text(encoding="utf-8")
        original_config = self.plugin.config
        for replacement in ("[0.2, 0.6, 1.0, 1.5]", "[0.2, 0.6, -1.0, 1.5, 2.0]"):
            with self.subTest(replacement=replacement):
                invalid_file = re.sub(
                    r"(?m)^level_multipliers\s*=.*$",
                    "level_multipliers = " + replacement,
                    original_file,
                    count=1,
                )
                self.assertNotEqual(invalid_file, original_file)
                config_path.write_text(invalid_file, encoding="utf-8")
                success, result = await self.command_manager.execute_command(self.message("/imm_reload"))
                self.assertFalse(success, result)
                self.assertIs(self.plugin.config, original_config)
                self.assertEqual(config_path.read_text(encoding="utf-8"), invalid_file)

    async def test_electric_provider_receives_voltage_changes_and_mode_off_immediately(self) -> None:
        self.plugin.config.prompts.enter_template = "TEST_CONTROL LEVEL:{level}"
        self.plugin.config.prompts.electric_template = "TEST_ELECTRIC V:{voltage} LIMIT:{overload_voltage}"
        client = CapturingClient()
        request = self.new_llm_request(client)
        await self.receive(self.message("/电流 20"))
        original = await self.plugin.store.get("group-a")
        self.assertTrue(original.active)
        self.assertEqual(original.voltage, 20)
        await request.send(auto_append_response=False, stream=False)
        captured = "\n".join(self.transient_text(client.calls[-1]["payloads"]))
        self.assertIn("TEST_CONTROL LEVEL:3", captured)
        self.assertIn("TEST_ELECTRIC V:20 LIMIT:100", captured)
        self.assertIn("Permanent persona.", self.text(client.calls[-1]["payloads"]))
        self.assertIn("History and unrelated extra instructions.", self.text(client.calls[-1]["payloads"]))
        await self.receive(self.message("/加压"))
        self.assertEqual((await self.plugin.store.get("group-a")).voltage, 30)
        await request.send(auto_append_response=False, stream=False)
        captured = "\n".join(self.transient_text(client.calls[-1]["payloads"]))
        self.assertIn("TEST_ELECTRIC V:30 LIMIT:100", captured)
        self.assertNotIn("TEST_ELECTRIC V:20", captured)
        await self.receive(self.message("/td voltage 45"))
        await request.send(auto_append_response=False, stream=False)
        captured = "\n".join(self.transient_text(client.calls[-1]["payloads"]))
        self.assertIn("TEST_ELECTRIC V:45 LIMIT:100", captured)
        self.assertNotIn("TEST_ELECTRIC V:30", captured)
        await self.receive(self.message("/断电"))
        switched_off = await self.plugin.store.get("group-a")
        self.assertTrue(switched_off.active)
        self.assertIsNone(switched_off.voltage)
        self.assertEqual(switched_off.end, original.end)
        self.assertEqual(switched_off.cooldown_end, original.cooldown_end)
        await request.send(auto_append_response=False, stream=False)
        captured = "\n".join(self.transient_text(client.calls[-1]["payloads"]))
        self.assertIn("TEST_CONTROL LEVEL:3", captured)
        self.assertNotIn("TEST_ELECTRIC", captured)
        await self.receive(self.message("/电压 50"))
        await request.send(auto_append_response=False, stream=False)
        self.assertIn(
            "TEST_ELECTRIC V:50 LIMIT:100",
            "\n".join(self.transient_text(client.calls[-1]["payloads"])),
        )

    async def test_electric_aliases_queries_and_zero_voltage_preserve_control_deadlines(self) -> None:
        self.assertEqual(self.plugin.settings.default_voltage, 10)
        self.assertEqual(self.plugin.settings.voltage_step, 10)
        self.assertEqual(self.plugin.settings.overload_voltage, 100)
        await self.receive(self.message("/td electric", sender="ordinary-user"))
        original = await self.plugin.store.get("group-a")
        self.assertEqual(original.voltage, 10)
        for query in ("/电压", "/td voltage"):
            before = self.transport.await_count
            await self.receive(self.message(query, sender="ordinary-user"))
            self.assertEqual(self.transport.await_count, before + 1)
            self.assertIn("10", self.transport.await_args.args[0].content)
            self.assertEqual(await self.plugin.store.get("group-a"), original)
        for command, expected in (
            ("/td electric 40", 40),
            ("/电压 25", 25),
            ("/减压", 15),
            ("/减压 5", 10),
            ("/加压 20", 30),
            ("/电压 0", 0),
        ):
            with self.subTest(command=command):
                await self.receive(self.message(command, sender="ordinary-user"))
                changed = await self.plugin.store.get("group-a")
                self.assertTrue(changed.active)
                self.assertEqual(changed.voltage, expected)
                self.assertEqual(changed.end, original.end)
                self.assertEqual(changed.cooldown_end, original.cooldown_end)
        await self.receive(self.message("/断电"))
        await self.receive(self.message("/电流 20"))
        changed = await self.plugin.store.get("group-a")
        self.assertTrue(changed.active)
        self.assertEqual(changed.voltage, 20)
        self.assertEqual(changed.end, original.end)
        self.assertEqual(changed.cooldown_end, original.cooldown_end)
        success, result = await self.command_manager.execute_command(self.message("/imm_status"))
        self.assertTrue(success, result)
        self.assertIn("电压", result)
        self.assertIn("20", result)
        self.assertIn("100", result)

    async def test_electric_overload_acknowledges_immediately_and_retries_only_one_storyline(self) -> None:
        self.plugin.config.prompts.enter_template = "TEST_CONTROL"
        self.plugin.config.prompts.electric_template = "TEST_ELECTRIC V:{voltage}"
        self.plugin.config.prompts.exit_template = "TEST_NORMAL_EXIT"
        self.plugin.config.prompts.overload_template = "TEST_OVERLOAD V:{voltage} LIMIT:{overload_voltage}"
        await self.receive(self.message("/电流 80"))
        original = await self.plugin.store.get("group-a")
        before = self.transport.await_count
        decision = await self.receive(self.message("/电压 120"))
        self.assertEqual(decision, self.fw.event.EventDecision.STOP)
        self.assertEqual(self.transport.await_count, before + 1)
        overloaded = await self.plugin.store.get("group-a")
        self.assertFalse(overloaded.active)
        self.assertEqual(overloaded.reason, "overload")
        self.assertEqual(overloaded.voltage, 120)
        self.assertEqual(overloaded.cooldown_end, original.cooldown_end)
        client = CapturingClient(failures=1)
        request = self.new_llm_request(client, max_retry=1)
        await request.send(auto_append_response=False, stream=False)
        self.assertEqual(len(client.calls), 2)
        first = self.transient_text(client.calls[0]["payloads"])
        self.assertTrue(first)
        self.assertEqual(self.transient_text(client.calls[1]["payloads"]), first)
        storyline = "\n".join(first)
        self.assertIn("TEST_OVERLOAD V:120 LIMIT:100", storyline)
        self.assertNotIn("TEST_NORMAL_EXIT", storyline)
        self.assertNotIn("TEST_CONTROL", storyline)
        self.assertNotIn("TEST_ELECTRIC", storyline)
        self.assertNotIn("_mofox_immersive_exit_retry", request.meta_data)
        await request.send(auto_append_response=False, stream=False)
        self.assertEqual(self.transient_text(client.calls[-1]["payloads"]), [])
        await self.receive(self.message("/电流 20"))
        cooling = await self.plugin.store.get("group-a")
        self.assertFalse(cooling.active)
        self.assertEqual(cooling.cooldown_end, original.cooldown_end)

    async def test_electric_concurrent_increases_reach_one_overload_without_losing_updates(self) -> None:
        self.plugin.config.prompts.overload_template = "TEST_OVERLOAD V:{voltage} LIMIT:{overload_voltage}"
        await self.receive(self.message("/电流 80"))
        original = await self.plugin.store.get("group-a")
        tasks = []
        try:
            async with self.plugin.store._lock:
                tasks = [
                    asyncio.create_task(self.receive(self.message("/加压", sender="member-1"))),
                    asyncio.create_task(self.receive(self.message("/加压", sender="member-2"))),
                ]
                for _ in range(40):
                    waiters = getattr(self.plugin.store._lock, "_waiters", None) or ()
                    if len(waiters) >= 2:
                        break
                    await asyncio.sleep(0)
                self.assertGreaterEqual(len(waiters), 2, "Both voltage handlers must wait before release")
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        overloaded = await self.plugin.store.get("group-a")
        self.assertFalse(overloaded.active)
        self.assertEqual(overloaded.voltage, 100)
        self.assertEqual(overloaded.reason, "overload")
        self.assertEqual(overloaded.cooldown_end, original.cooldown_end)
        first = "\n".join(self.transient_text(await self.request("group-a")))
        self.assertIn("TEST_OVERLOAD V:100 LIMIT:100", first)
        self.assertEqual(self.transient_text(await self.request("group-a")), [])

    async def test_electric_voltage_is_shared_in_one_stream_and_isolated_from_other_streams(self) -> None:
        self.plugin.config.prompts.electric_template = "TEST_ELECTRIC V:{voltage} LIMIT:{overload_voltage}"
        await self.receive(self.message("/电流 20", sender="member-1"))
        await self.receive(self.message("/控制 5", stream="group-b", sender="member-2"))
        await self.receive(self.message("/电压 40", sender="member-3"))
        shared = await self.plugin.store.get("group-a")
        unrelated = await self.plugin.store.get("group-b")
        self.assertEqual(shared.voltage, 40)
        self.assertIsNone(unrelated.voltage)
        self.assertEqual(unrelated.level, 5)
        self.assertIn("TEST_ELECTRIC V:40", "\n".join(self.transient_text(await self.request("group-a"))))
        self.assertNotIn("TEST_ELECTRIC", "\n".join(self.transient_text(await self.request("group-b"))))
        self.assertEqual(self.transient_text(await self.request("group-c")), [])
        await self.receive(self.message("/断电", sender="member-4"))
        self.assertTrue((await self.plugin.store.get("group-a")).active)
        self.assertIsNone((await self.plugin.store.get("group-a")).voltage)
        self.assertEqual(await self.plugin.store.get("group-b"), unrelated)

    async def test_electric_invalid_inputs_and_inactive_adjustments_do_not_create_sessions(self) -> None:
        for command in (
            "/电流 -1",
            "/电流 1000001",
            "/电流 abc",
            "/电压 -1",
            "/电压 abc",
            "/电压 20",
            "/电压",
            "/加压",
            "/减压",
            "/断电",
            "/加压 0",
            "/减压 -1",
        ):
            with self.subTest(command=command):
                before = self.transport.await_count
                await self.receive(self.message(command))
                self.assertEqual(self.transport.await_count, before + 1)
                self.assertIsNone(await self.plugin.store.get("group-a"))
                self.assertEqual(self.transient_text(await self.request("group-a")), [])
        await self.receive(self.message("/td 2"))
        original = await self.plugin.store.get("group-a")
        for command in ("/加压", "/减压", "/电压 -1", "/电流 abc"):
            await self.receive(self.message(command))
            self.assertEqual(await self.plugin.store.get("group-a"), original)

    async def test_electric_cold_start_obeys_capacity_and_existing_cooldown(self) -> None:
        self.plugin.settings.max_concurrent = 1
        await self.receive(self.message("/电流 20"))
        await self.receive(self.message("/电流 10", stream="group-b"))
        self.assertIsNone(await self.plugin.store.get("group-b"))
        await self.receive(self.message("/断电"))
        self.assertTrue((await self.plugin.store.get("group-a")).active)
        await self.receive(self.message("/电流 10", stream="group-b"))
        self.assertIsNone(await self.plugin.store.get("group-b"))
        await self.receive(self.message("/td stop"))
        original = await self.plugin.store.get("group-a")
        await self.receive(self.message("/电流 10"))
        cooling = await self.plugin.store.get("group-a")
        self.assertFalse(cooling.active)
        self.assertEqual(cooling.cooldown_end, original.cooldown_end)
        await self.receive(self.message("/电流 10", stream="group-b"))
        self.assertTrue((await self.plugin.store.get("group-b")).active)
        await self.receive(self.message("/td stop", stream="group-b"))
        self.clock.now = original.cooldown_end + 1
        await self.receive(self.message("/电流 30"))
        restarted = await self.plugin.store.get("group-a")
        self.assertTrue(restarted.active)
        self.assertEqual(restarted.voltage, 30)

    async def test_electric_commands_obey_enabled_and_real_admin_only_permissions(self) -> None:
        await self.receive(self.message("/电流 20"))
        original = await self.plugin.store.get("group-a")
        inherited = await self.request("group-a")
        self.plugin.settings.enabled = False
        for command in ("/电流 30", "/电压", "/电压 30", "/加压", "/断电"):
            before = self.transport.await_count
            await self.receive(self.message(command))
            self.assertEqual(self.transport.await_count, before)
            self.assertEqual(await self.plugin.store.get("group-a"), original)
        self.assertEqual(self.transient_text(await self.request("group-a", inherited)), [])
        self.plugin.settings.enabled = True
        self.plugin.settings.admin_only_mode = True
        for command in ("/电流 30", "/电压", "/电压 30", "/加压", "/断电"):
            decision = await self.receive(self.message(command, sender="ordinary-user"))
            self.assertEqual(decision, self.fw.event.EventDecision.STOP)
            self.assertIn("操作员", self.transport.await_args.args[0].content)
            self.assertEqual(await self.plugin.store.get("group-a"), original)
        await self.receive(self.message("/电压 30", sender="operator"))
        self.assertEqual((await self.plugin.store.get("group-a")).voltage, 30)
        await self.receive(self.message("/断电", sender="owner"))
        self.assertTrue((await self.plugin.store.get("group-a")).active)
        self.assertIsNone((await self.plugin.store.get("group-a")).voltage)

    async def test_electric_group_controls_require_the_correct_bot_mention(self) -> None:
        await self.receive(self.message("/电流 20"))
        with patch.object(
            self.fw.adapter_api,
            "get_bot_info_by_platform",
            AsyncMock(return_value={"bot_id": "bot-123"}),
        ):
            for text in ("电流 30", "电压", "电压 30", "加压", "断电"):
                for at_users in ([], [{"user_id": "other-id"}]):
                    before = self.transport.await_count
                    await self.receive(self.message(text, at_users=at_users))
                    self.assertEqual(self.transport.await_count, before)
                    self.assertEqual((await self.plugin.store.get("group-a")).voltage, 20)
            changed = self.message("@<Robot:bot-123> 电压 30", at_users=[{"user_id": "bot-123"}])
            await self.receive(changed)
            self.assertEqual((await self.plugin.store.get("group-a")).voltage, 30)
            self.assertEqual(changed.extra["at_users"], [{"user_id": "bot-123"}])
            await self.receive(self.message("@<Robot:bot-123> 电压", at_users=[{"user_id": "bot-123"}]))
            self.assertIn("30", self.transport.await_args.args[0].content)
        await self.receive(self.message("/加压 5"))
        self.assertEqual((await self.plugin.store.get("group-a")).voltage, 35)

    async def test_electric_native_config_rejects_invalid_bounds_and_preserves_invalid_reload(self) -> None:
        config_class = type(self.plugin.config)
        for values in (
            {"default_voltage": -1},
            {"default_voltage": 1000001},
            {"default_voltage": True},
            {"default_voltage": "10"},
            {"voltage_step": 0},
            {"voltage_step": 1000001},
            {"voltage_step": 10.0},
            {"overload_voltage": 0},
            {"overload_voltage": 10001},
            {"overload_voltage": False},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                config_class.model_validate({"plugin": values})
        accepted = config_class.model_validate(
            {"plugin": {"default_voltage": 0, "voltage_step": 1, "overload_voltage": 1}}
        )
        self.assertEqual(accepted.plugin.default_voltage, 0)
        config_path = self.workdir / "config" / "plugins" / self.plugin_name / "config.toml"
        original_file = config_path.read_text(encoding="utf-8")
        original_config = self.plugin.config
        invalid_file = re.sub(r"(?m)^default_voltage\s*=.*$", "default_voltage = -1", original_file, count=1)
        self.assertNotEqual(invalid_file, original_file)
        config_path.write_text(invalid_file, encoding="utf-8")
        success, result = await self.command_manager.execute_command(self.message("/imm_reload"))
        self.assertFalse(success, result)
        self.assertIs(self.plugin.config, original_config)
        self.assertEqual(config_path.read_text(encoding="utf-8"), invalid_file)


if __name__ == "__main__":
    unittest.main()
