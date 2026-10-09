"""Neo-MoFox 原生 TOML / WebUI 配置。"""

from typing import Annotated, ClassVar

from pydantic import FiniteFloat
from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section

from .prompts import DEFAULT_ENTER_TEMPLATE, DEFAULT_EXIT_TEMPLATE


class ImmersiveControlConfig(BaseConfig):
    """所有配置由框架生成并验证，无需额外依赖。"""

    name: ClassVar[str] = "config"
    description: ClassVar[str] = "沉浸式控制配置"

    @config_section("plugin")
    class PluginSection(SectionBase):
        """触发、权限及会话设置。"""

        enabled: bool = Field(default=True, description="启用沉浸式控制")
        admin_only_mode: bool = Field(default=False, description="仅 Neo-MoFox 操作员及所有者可触发")
        require_mention: bool = Field(
            default=True, description="群聊中的无前缀关键词需要 @机器人；/关键词 不受此限制"
        )
        enter_keywords: list[str] = Field(
            default_factory=lambda: ["控制", "遥控", "我要控制你了", "td"],
            description="进入关键词，支持多词关键词和 / 前缀",
        )
        exit_keywords: list[str] = Field(
            default_factory=lambda: ["拿出来吧", "停止控制", "结束控制", "停止", "td stop"],
            description="退出关键词；优先于进入关键词匹配",
        )
        state_duration: int = Field(default=180, ge=1, le=86400, description="控制持续时间（秒）")
        cooldown_seconds: int = Field(
            default=30, ge=0, le=86400, description="从激活时开始计算的会话冷却（秒）"
        )
        max_concurrent: int = Field(default=10, ge=1, le=1000, description="全局最大激活会话数")
        exit_pending_ttl: int = Field(
            default=86400, ge=1, description="退出提示等待下一次模型请求的最长时间（秒）"
        )
        item_name: str = Field(default="特殊装置", min_length=1, description="提示词中的装置名称")
        sensitivity: int = Field(default=50, ge=0, le=100, description="基准敏感度，默认对应 3 档（0—100）")
        default_level: int = Field(
            default=3, ge=1, le=5, strict=True, description="未指定时使用的控制档位（1—5）"
        )
        level_multipliers: list[Annotated[FiniteFloat, Field(ge=0, le=10, strict=True)]] = Field(
            default_factory=lambda: [0.2, 0.6, 1.0, 1.5, 2.0],
            min_length=5,
            max_length=5,
            description="依次对应 1—5 档的敏感度倍率，实际敏感度限制在 0—100",
            input_type="list",
            item_type="number",
            min_items=5,
            max_items=5,
        )
        persist_state: bool = Field(default=True, description="将会话状态和冷却持久化，支持重启恢复")

    @config_section("prompts")
    class PromptsSection(SectionBase):
        """可自定义模板，留空使用默认模板。"""

        enter_template: str = Field(
            default=DEFAULT_ENTER_TEMPLATE,
            description="进入和持续控制模板，支持 {item_name}、{sensitivity}、{level}、{level_name}",
        )
        exit_template: str = Field(
            default=DEFAULT_EXIT_TEMPLATE,
            description="退出模板，支持 {item_name}、{sensitivity}、{level}、{level_name}",
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    prompts: PromptsSection = Field(default_factory=PromptsSection)
