"""由 Neo-MoFox 权限系统保护的管理员命令。"""

from __future__ import annotations

import math

from src.app.plugin_system.api import send_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseCommand, cmd_route
from src.app.plugin_system.types import PermissionLevel

from .logic import describe_level

logger = get_logger("immersive_commands")


class _AdminCommand(BaseCommand):
    """命令返回值不自动发送，统一显式回复。"""

    permission_level = PermissionLevel.OPERATOR

    async def respond(self, text: str, success: bool = True) -> tuple[bool, str]:
        """向命令所在会话发送结果。"""
        if self._message is not None:
            await self.plugin.reply(text, self._message)
        else:
            await send_api.send_text(text, self.stream_id, reply_to=self.message_id or None)
        return success, text


class ImmStatusCommand(_AdminCommand):
    """查询当前会话，避免公开其他聊天的状态。"""

    name = "imm_status"
    description = "查看当前会话的沉浸式控制状态"

    @cmd_route()
    async def status(self) -> tuple[bool, str]:
        """查看控制状态、剩余时间和冷却。"""
        record = await self.plugin.store.get(self.stream_id)
        lines = [f"控制状态: {'激活中' if record and record.active else '未激活'}"]
        now = self.plugin.store.clock()
        if record:
            settings = self.plugin.settings
            lines.append(describe_level(record.level, settings.sensitivity, settings.level_multipliers))
            if record.active and record.end is not None:
                lines.append(f"剩余时间: {max(0, math.ceil(record.end - now))}秒")
            elif record.exit_ts is not None:
                reason = "主动结束" if record.reason == "user" else "时间到期"
                lines.append(f"退出原因: {reason}（等待下一次回复恢复）")
            cooldown = max(0, math.ceil(record.cooldown_end - now))
            if cooldown:
                lines.append(f"冷却剩余: {cooldown}秒")
        if not self.plugin.settings.enabled:
            lines.append("插件已禁用")
        return await self.respond("\n".join(lines))


class ImmClearCommand(_AdminCommand):
    """与原 README 的约定一致，清空全局插件会话。"""

    name = "imm_clear"
    description = "清除所有会话的沉浸状态和冷却"

    @cmd_route()
    async def clear(self) -> tuple[bool, str]:
        """清除全局状态，包括持久化的冷却记录。"""
        count = await self.plugin.store.clear()
        return await self.respond(f"已清除 {count} 个会话的控制状态和冷却")


class ImmReloadCommand(_AdminCommand):
    """重新读取 TOML，无需重启机器人。"""

    name = "imm_reload"
    description = "重载沉浸式控制配置"

    @cmd_route()
    async def reload(self) -> tuple[bool, str]:
        """重载并验证配置，保留正在进行的会话。"""
        try:
            await self.plugin.reload_settings()
        except Exception:
            logger.error("沉浸式控制配置重载失败", exc_info=True)
            return await self.respond("配置重载失败，请检查 TOML 文件和机器人日志", False)
        return await self.respond("沉浸式控制配置已重载")
