"""沉浸式控制的 Neo-MoFox 插件入口。"""

from __future__ import annotations

import tomllib
from pathlib import Path
from uuid import uuid4

from src.app.plugin_system.api import config_api, send_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BasePlugin, register_plugin
from src.app.plugin_system.types import Message

from .commands import ImmClearCommand, ImmReloadCommand, ImmStatusCommand
from .config import ImmersiveControlConfig
from .handlers import ImmersiveMessageHandler, ImmersivePromptHandler
from .state import SessionStore

logger = get_logger("mofox_plugin_immersive_control")


@register_plugin
class ImmersiveControlPlugin(BasePlugin):
    """使用原生事件与命令扩展聊天，不替换框架的 Chatter。"""

    plugin_name = "mofox_plugin_immersive_control"
    plugin_description = "限时沉浸式互动、五档调节、虚拟电流与过载剧情、会话冷却、权限及持久化"
    plugin_version = "1.2.0"
    configs: list[type] = [ImmersiveControlConfig]
    dependent_components: list[str] = ["default_chatter:chatter:default_chatter"]

    def __init__(self, config: ImmersiveControlConfig | None = None) -> None:
        super().__init__(config if config is not None else ImmersiveControlConfig())
        self.state_path = Path("data/plugin_data") / self.plugin_name / "sessions.json"
        self.store = SessionStore(self.settings, self.state_path if self.settings.persist_state else None)

    @property
    def settings(self) -> ImmersiveControlConfig.PluginSection:
        """返回当前配置，热重载后所有组件读取同一实例。"""
        return self.config.plugin

    def get_components(self) -> list[type]:
        """配置由 configs 加载，返回事件和命令组件类。"""
        return [
            ImmersiveMessageHandler,
            ImmersivePromptHandler,
            ImmStatusCommand,
            ImmClearCommand,
            ImmReloadCommand,
        ]

    async def on_plugin_loaded(self) -> None:
        """恢复有效会话和冷却，按绝对时间处理离线期间的过期。"""
        await self.store.load()
        logger.info("沉浸式控制插件已加载")

    async def on_plugin_unloaded(self) -> None:
        """持久化状态，供重启或热加载恢复。"""
        await self.store.save()

    async def reload_settings(self) -> None:
        """通过框架重新验证配置，保留现有会话的到期时间。"""
        config_path = ImmersiveControlConfig.get_default_path()
        if config_path is None:
            raise RuntimeError("插件配置路径尚未由框架初始化")
        with config_path.open("rb") as source:
            # 框架 auto_update 会把部分类型错误替换成默认值；先严格验证，
            # 防止错误配置意外关闭管理员限制或改写用户文件。
            ImmersiveControlConfig.model_validate(tomllib.load(source))
        new_config = config_api.reload_config(self.plugin_name, ImmersiveControlConfig)
        self.config = new_config
        self.store.cfg = self.settings
        self.store.path = self.state_path if self.settings.persist_state else None
        await self.store.save()

    async def reply(self, text: str, incoming: Message, adapter_signature: str | None = None) -> bool:
        """显式设置收件目标，使首条消息也能收到反馈。"""
        target = (
            {"target_group_id": str(incoming.extra.get("group_id") or "")}
            if incoming.chat_type == "group"
            else {"target_user_id": incoming.sender_id}
        )
        outgoing = Message(
            message_id=f"immersive_{uuid4().hex}",
            content=text,
            processed_plain_text=text,
            stream_id=incoming.stream_id,
            platform=incoming.platform,
            chat_type=incoming.chat_type,
            reply_to=incoming.message_id or None,
            **target,
        )
        sent = await send_api.send_message(outgoing, adapter_signature=adapter_signature)
        if not sent:
            logger.warning("沉浸式控制反馈发送失败")
        return sent
