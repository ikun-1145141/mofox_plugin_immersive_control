# Neo-MoFox 沉浸式控制插件

将 [AstrBot 原插件](https://github.com/muyouzhi6/astrbot_plugin_immersive_control) 移植到 [Neo-MoFox](https://github.com/MoFox-Studio/Neo-MoFox)。给当前会话添加限时角色互动状态，在正常聊天的大模型请求中追加原插件提示词，结束后恢复原本的人格和对话。

## 安装

需要 **Neo-MoFox 1.2.0 或更新版本**，并启用内置 `default_chatter`。本插件没有额外的 Python 包依赖。

把本项目放到 `Neo-MoFox/plugins/mofox_plugin_immersive_control/`，保证该目录直接包含 `manifest.json` 和 `plugin.py`。也可以解压 `dist/mofox_plugin_immersive_control-1.1.0.zip` 到 `Neo-MoFox/plugins/`。

发布本仓库的代码后，也可以在 Neo-MoFox 根目录运行：

```shell
git clone https://github.com/ikun-1145141/mofox_plugin_immersive_control.git plugins/mofox_plugin_immersive_control
```

重启 Neo-MoFox。首次加载会自动生成：

```text
config/plugins/mofox_plugin_immersive_control/config.toml
```

启用角色对话和模型配置后即可使用。无需 AstrBot，不要安装原插件的 `main.py` 或 `_conf_schema.json`。

## 使用

| 操作 | 私聊或显式命令 | 群聊无前缀形式 |
| --- | --- | --- |
| 进入 | `/控制`、`/遥控`、`/我要控制你了`、`/td` | `@机器人 控制` |
| 退出 | `/拿出来吧`、`/停止控制`、`/结束控制`、`/停止`、`/td stop` | `@机器人 停止控制` |

私聊也支持不加 `/`。群聊默认只有显式 `/关键词` 或 @机器人时才触发，避免普通聊天误触；`require_mention = false` 可以允许群聊无 @ 触发。

关键词支持后接空格和参数，退出关键词优先匹配。例如 `/td stop 现在` 会退出，`我想控制一下` 不会触发。进入和退出成功后交给 Neo-MoFox 正常聊天生成反应；机器人是否立即回复仍受框架的回复意愿、聊天调度及模型配置影响。状态会立即变更，不强制额外调用模型。

控制默认持续 180 秒，到期自动失效。下一次正常聊天模型请求会使用一次退出提示词；没有新请求时不会定时发送消息。重复进入不会续期。冷却、并发不足或权限不足会直接反馈。

**同一群内所有成员共享一份状态与冷却**，不同群、私聊互相隔离。冷却从激活时开始计算，默认 30 秒；提前退出或消费退出提示不会绕过仍有效的冷却。

## 档位调节

从 1.1.0 起支持会话内 **1—5 档**，默认 3 档。基准敏感度 `sensitivity = 50` 时：

| 档位 | 名称 | 默认倍率 | 实际反应强度 |
| --- | --- | --- | --- |
| 1 | 轻柔 | 0.2 | 10% |
| 2 | 低档 | 0.6 | 30% |
| 3 | 中档 | 1.0 | 50% |
| 4 | 高档 | 1.5 | 75% |
| 5 | 强档 | 2.0 | 100% |

| 指令 | 作用 |
| --- | --- |
| `/控制 2`、`/td 2`、`/遥控 2` | 启动时指定 2 档 |
| `/档位`、`/td level` | 查看当前档位和反应强度 |
| `/档位 4`、`/调档 4`、`/td level 4` | 将当前会话切换到 4 档 |
| `/升档`、`/降档` | 调高或调低一档，到 1、5 档时提示已到边界 |

也接受 `3档` 或 `三档`。群聊无前缀调档需要 @机器人，权限规则与进入、退出一致。未激活时调档只提示用法，不会偷偷启动会话；`/td stop` 仍然优先执行退出。

切档会立即更新当前会话的状态并反馈，下一次正常模型调用使用新档位。**切档不会续期或重置冷却**。同群共享档位，其他群和私聊不受影响，重启可恢复保存的档位。`/imm_status` 也会显示档位及强度。

实际强度为 `sensitivity × 对应档位倍率`，取整后限制在 0—100。例如基准敏感度设为 80 时，4、5 档均达到 100%。保留基准敏感度配置，默认 3 档的效果与旧版相同。

## 管理命令

以下命令要求 Neo-MoFox 的 `OPERATOR`（操作员）或 `OWNER`（所有者）权限。群管理员身份不自动等于机器人操作员；可使用框架的 `perm_plugin` 配置权限，或在核心配置中设置所有者。

| 命令 | 作用 |
| --- | --- |
| `/imm_status`、`/控制状态` | 查看当前会话的激活、到期与冷却状态 |
| `/imm_clear` | 清除**所有会话**的控制状态、待退出提示及冷却，并同步持久化文件 |
| `/imm_reload` | 重新读取并验证配置，不重启机器人 |

## 配置

编辑生成的 TOML，或在支持原生插件配置的 WebUI 中修改，再执行 `/imm_reload`。

```toml
[plugin]
enabled = true
admin_only_mode = false
require_mention = true
enter_keywords = ["控制", "遥控", "我要控制你了", "td"]
exit_keywords = ["拿出来吧", "停止控制", "结束控制", "停止", "td stop"]
state_duration = 180
cooldown_seconds = 30
max_concurrent = 10
exit_pending_ttl = 86400
item_name = "特殊装置"
sensitivity = 50
default_level = 3
level_multipliers = [0.2, 0.6, 1.0, 1.5, 2.0]
persist_state = true

[prompts]
# 空字符串使用原插件的默认模板；支持 {item_name}、{sensitivity}、{level}、{level_name}。
enter_template = ""
exit_template = ""
```

- `admin_only_mode`：限制进入、退出、查询档位和调档为机器人操作员或所有者，管理命令始终需要权限。
- `default_level`：进入时没选档则使用此档位，范围 1—5；热重载不改变已激活会话的档位。
- `level_multipliers`：依次对应五档，必须恰好五个有限数值，范围 0—10；模型实际使用的强度仍限制在 0—100。
- `max_concurrent`：所有聊天同时激活的会话总数；到期的会话会释放名额。
- `exit_pending_ttl`：退出后等待下一次模型请求的有效期，默认一天；离线期间也计算时间。
- 自定义模板只替换上述四个指定变量，`{sensitivity}` 为本档实际强度，`{level_name}` 为预设名称。JSON 或其他花括号会保留。控制中还会附加当前档位说明，因此旧模板也能表达档位变化。
- 热重载会立即应用模板、关键词及限制；已有会话的到期与冷却时间保持原值。`enabled = false` 停止触发并清理后续模型请求中的旧注入；会话时间仍正常流逝。

启用 `persist_state` 时，状态保存在 Neo-MoFox 根目录的：

```text
data/plugin_data/mofox_plugin_immersive_control/sessions.json
```

进入、退出、切档、消费退出提示和清空均使用原子文件替换写盘。旧版本没有档位字段的状态文件自动按 3 档读取。重启按绝对时间恢复有效状态，损坏的状态文件会保留为 `.corrupt-*` 供排查。关闭持久化后只使用当前进程内存，旧磁盘快照不再更新；重新启用时建议执行 `/imm_clear` 清理不需要的旧状态。

## 移植范围和兼容性

保留原插件默认进入/退出提示词、敏感度、装置名称、限时互动、会话冷却和并发限制，并实现原 README 中提到但当前源代码未提供的持久化、清除及重载。

使用公开 `BasePlugin`、`BaseConfig`、`BaseEventHandler`、`BaseCommand` 和插件 API，配置与组件签名遵守 Neo-MoFox 插件规范，不直接导入其他插件源码。针对默认 Chatter 的实际 `BEFORE_LLM_REQUEST` 事件按 `meta_data.stream_id` 注入独立系统消息，每次先剥离自身旧块，保留原人格、图片和工具调用。辅助模型及上下文摘要请求只清理旧块，不激活新模式。

退出提示按一次逻辑模型请求消费，同一次请求的内部重试会复用该提示；如果最终所有重试都失败，下一次请求会直接恢复正常状态。

当前适配和验证基于：

- 原插件：`a46e4c9d82af32a1e712612565393c8ff13d092d`（2.3.6）。
- Neo-MoFox：`e2ee2ff73b494428bbdfd983c7569c6f074a9c76`（1.2.0）。

自定义 Chatter 必须提供等价的请求名称与会话 metadata 才能直接兼容；此版本明确面向内置 `default_chatter`。测试不调用真实模型或聊天平台，实际反应由所配置模型生成。

## 开发验证与打包

状态和关键词测试只需 Python 3.11+ 标准库：

```shell
python -m unittest discover -s tests -p test_state.py -v
python -m unittest discover -s tests -p test_logic.py -v
```

在安装了 Neo-MoFox 运行依赖的 Python 环境里，设置 `NEO_MOFOX_ROOT` 指向其源码目录，再运行全部测试：

```powershell
$env:NEO_MOFOX_ROOT = "D:\Neo-MoFox"
python -m unittest discover -s tests -v
```

集成测试实际使用框架加载器、事件总线、配置模型、LLM payload 和命令管理器，以及临时 SQLite 权限库；发送适配器与模型调用用本地替身，不需要外部凭据。找不到 Neo-MoFox 源码时，集成测试会明确跳过；源码存在但依赖缺失时会报错。

```shell
python scripts/build_release.py
```

打包使用明确的文件清单，不包含参考仓库、虚拟环境、测试数据或 Git 历史。

## 致谢与授权

原插件作者：**木有知、Zhalslar**。原插件使用 MIT 协议，版权及完整许可保留于 [LICENSE.upstream](LICENSE.upstream)。本移植项目沿用仓库的 [AGPL-3.0](LICENSE) 协议；原作署名和 MIT 许可不变。
