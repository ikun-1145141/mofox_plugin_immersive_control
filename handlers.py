"""接收关键词，并在实际模型请求前同步临时角色状态。"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api import adapter_api, permission_api
from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import ROLE, EventType, LLMPayload, Message, PermissionLevel, Text
from src.kernel.event import EventDecision

from .config import DEFAULT_ENTER_TEMPLATE, DEFAULT_EXIT_TEMPLATE
from .logic import match_control, normalize_message, render_prompt

PROMPT_MARKER = "[mofox_immersive_control:transient]\n"
_EXIT_RETRY_METADATA_KEY = "_mofox_immersive_exit_retry"


class ImmersiveMessageHandler(BaseEventHandler):
    """在消息分发器之前处理状态，成功后由正常聊天生成反应。"""

    name = "immersive_message"
    description = "进入或退出沉浸式控制，处理关键词与权限"
    weight = 100
    init_subscribe = [EventType.ON_MESSAGE_RECEIVED]

    async def execute(self, event_name: str, params: dict[str, Any]) -> tuple[EventDecision, dict[str, Any]]:
        """仅匹配命令形式的关键词，不扫描普通对话中的子串。"""
        message = params.get("message")
        if not isinstance(message, Message) or not message.stream_id:
            return EventDecision.PASS, params
        text = message.processed_plain_text or message.content
        if not isinstance(text, str):
            return EventDecision.PASS, params
        normalized, explicit = normalize_message(text)
        if normalized == "控制状态" and explicit:
            message.content = message.processed_plain_text = "/imm_status"
            return EventDecision.SUCCESS, params

        settings = self.plugin.settings
        if not settings.enabled:
            return EventDecision.PASS, params
        operation = match_control(normalized, settings.enter_keywords, settings.exit_keywords)
        if operation is None:
            return EventDecision.PASS, params
        if message.chat_type == "group" and settings.require_mention and not explicit:
            bot_info = await adapter_api.get_bot_info_by_platform(message.platform) or {}
            bot_id = str(bot_info.get("bot_id") or "")
            at_users = message.extra.get("at_users") or []
            if not bot_id or not any(
                isinstance(user, dict) and str(user.get("user_id")) == bot_id for user in at_users
            ):
                return EventDecision.PASS, params
        if settings.admin_only_mode:
            allowed = False
            if message.platform and message.sender_id:
                person_id = permission_api.generate_person_id(message.platform, message.sender_id)
                level = await permission_api.get_user_permission_level(person_id)
                allowed = level >= PermissionLevel.OPERATOR
            if not allowed:
                await self.plugin.reply(
                    "仅机器人操作员或所有者可以使用控制功能", message, params.get("adapter_signature")
                )
                return EventDecision.STOP, params

        if operation == "exit":
            changed = await self.plugin.store.deactivate(message.stream_id)
            if not changed:
                record = await self.plugin.store.get(message.stream_id)
                if record is None or record.exit_ts is None:
                    await self.plugin.reply(
                        "当前没有激活的控制状态", message, params.get("adapter_signature")
                    )
                    return EventDecision.STOP, params
        else:
            success, result = await self.plugin.store.activate(message.stream_id)
            if not success:
                await self.plugin.reply(result, message, params.get("adapter_signature"))
                return EventDecision.STOP, params

        # 框架先检查已注册命令。移除 / 才能让自定义关键词进入正常 Chatter；
        # at_users 等元数据保留，机器人仍可按照原有策略识别群内提及。
        message.content = message.processed_plain_text = normalized
        return EventDecision.SUCCESS, params


def clean_payloads(payloads: list[LLMPayload]) -> list[LLMPayload]:
    """移除本插件上轮注入，保留原人格、图片及工具配对，不原地修改内容。"""
    cleaned: list[LLMPayload] = []
    for payload in payloads:
        if payload.role != ROLE.SYSTEM:
            cleaned.append(payload)
            continue
        parts = [
            part
            for part in payload.content
            if not (isinstance(part, Text) and part.text.startswith(PROMPT_MARKER))
        ]
        if parts:
            cleaned.append(payload if len(parts) == len(payload.content) else LLMPayload(payload.role, parts))
    return cleaned


class ImmersivePromptHandler(BaseEventHandler):
    """每次模型调用重新计算状态，防止旧状态留在工具续轮与摘要请求中。"""

    name = "immersive_prompt"
    description = "向当前会话的实际聊天请求注入临时角色状态"
    weight = 100
    init_subscribe = [
        EventType.BEFORE_LLM_REQUEST,
        EventType.AFTER_LLM_REQUEST,
        EventType.ON_LLM_REQUEST_FAILED,
    ]

    async def execute(self, event_name: str, params: dict[str, Any]) -> tuple[EventDecision, dict[str, Any]]:
        """使用既有 payloads 键，保持 EventBus 顶层参数集合不变。"""
        metadata = params.get("meta_data")
        if event_name in (EventType.AFTER_LLM_REQUEST, EventType.ON_LLM_REQUEST_FAILED):
            if isinstance(metadata, dict):
                metadata.pop(_EXIT_RETRY_METADATA_KEY, None)
            return EventDecision.SUCCESS, params
        payloads = params.get("payloads")
        if not isinstance(payloads, list):
            return EventDecision.PASS, params
        cleaned = clean_payloads(payloads)
        params["payloads"] = cleaned
        # 压缩、记忆、决策等请求只清理旧块，不获取其他聊天的状态。
        if not self.plugin.settings.enabled or params.get("request_name") != "default_chatter":
            return EventDecision.SUCCESS, params
        stream_id = metadata.get("stream_id") if isinstance(metadata, dict) else None
        if not isinstance(stream_id, str) or not stream_id:
            return EventDecision.SUCCESS, params
        cached_exit = metadata.get(_EXIT_RETRY_METADATA_KEY)
        if (
            isinstance(cached_exit, dict)
            and cached_exit.get("stream_id") == stream_id
            and isinstance(cached_exit.get("prompt"), str)
        ):
            prompt = cached_exit["prompt"]
        else:
            record = await self.plugin.store.get(stream_id)
            if record is None:
                return EventDecision.SUCCESS, params
            if record.active:
                template = self.plugin.config.prompts.enter_template.strip() or DEFAULT_ENTER_TEMPLATE
            elif record.exit_ts is not None:
                if await self.plugin.store.complete_exit(stream_id) is None:
                    return EventDecision.SUCCESS, params
                template = self.plugin.config.prompts.exit_template.strip() or DEFAULT_EXIT_TEMPLATE
            else:
                return EventDecision.SUCCESS, params
            settings = self.plugin.settings
            prompt = render_prompt(template, item_name=settings.item_name, sensitivity=settings.sensitivity)
            if not record.active:
                # EventBus 浅拷贝顶层字典，meta_data 仍是同一逻辑请求的对象。
                # 保留已消费的退出提示供 provider 重试；成功或重试耗尽即移除。
                metadata[_EXIT_RETRY_METADATA_KEY] = {"stream_id": stream_id, "prompt": prompt}
        index = next((i + 1 for i, payload in enumerate(cleaned) if payload.role == ROLE.SYSTEM), 0)
        cleaned.insert(index, LLMPayload(ROLE.SYSTEM, Text(PROMPT_MARKER + prompt)))
        return EventDecision.SUCCESS, params
