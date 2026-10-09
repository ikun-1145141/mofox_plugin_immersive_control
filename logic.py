"""与平台无关的关键词匹配和提示词替换。"""

import re
from collections.abc import Sequence

_LEADING_MENTION = re.compile(r"^(?:@<[^>]+>|\[CQ:at,[^\]]+\])\s*")
# 匹配完整的框架回复前缀；预览可以含换行或 ]，直到首个 ]，说：。
# 不剥离普通引用、未知方括号或媒体标记，避免把正文误认成指令。
_LEADING_REPLY = re.compile(
    r"^(?:\[回复<[^>]*>：.*?\]，说：|\[回复:[^\]\r\n]*\]|\[回复\]|「回复：[^」]*」)\s*",
    re.DOTALL,
)
LEVEL_NAMES = ("轻柔", "低档", "中档", "高档", "强档")
_CHINESE_LEVELS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}


def normalize_message(text: str) -> tuple[str, bool]:
    """剥离框架前置 @ 和回复预览，记录是否显式使用 / 前缀。"""
    text = text.strip()
    while match := _LEADING_MENTION.match(text) or _LEADING_REPLY.match(text):
        text = text[match.end() :].lstrip()
    explicit = text.startswith("/")
    if explicit:
        text = text[1:].lstrip()
    return " ".join(text.split()), explicit


def body_mention_ids(envelope: object) -> set[str] | None:
    """提取正文 @，排除转换器合并到 at_users 中的引用提及。

    缺少可识别消息段时返回 None，供旧事件来源沿用已有元数据。
    真实空段列表返回空集；与转换器一致，最多解析 5 层。
    """
    if not isinstance(envelope, dict):
        return None
    raw = envelope.get("message_segment")
    if raw is None:
        raw = envelope.get("message_chain")
    if isinstance(raw, dict) and isinstance(raw.get("type"), str):
        segments = [raw]
    elif isinstance(raw, list):
        if raw and not any(
            isinstance(segment, dict) and isinstance(segment.get("type"), str) for segment in raw
        ):
            return None
        segments = raw
    else:
        return None

    def walk(parts: list, depth: int) -> tuple[set[str], bool, str | None]:
        if depth >= 5:
            return set(), False, None
        mentions: set[str] = set()
        has_reply = False
        first_text = None
        for segment in parts:
            if not isinstance(segment, dict):
                continue
            kind, data = segment.get("type"), segment.get("data", "")
            rendered = None
            if kind == "reply":
                has_reply = True
            elif kind == "at":
                if isinstance(data, str):
                    mentions.add(data.split(":", 1)[1] if ":" in data else data)
                rendered = "@"
            elif kind == "text":
                rendered = str(data)
            elif kind == "seglist" and isinstance(data, list):
                inner_mentions, inner_reply, rendered = walk(data, depth + 1)
                has_reply = has_reply or inner_reply
                preview = rendered.strip() if rendered is not None else ""
                if not inner_reply and not preview.startswith(("[回复<", "「回复：")):
                    mentions.update(inner_mentions)
            else:
                rendered = f"[{kind}]"
            if first_text is None and rendered is not None:
                first_text = rendered
        return mentions, has_reply, first_text

    mentions, _, _ = walk(segments, 0)
    return mentions


def matches_keyword(text: str, keywords: Sequence[str]) -> bool:
    """匹配完整关键词或带参数的指令，避免匹配普通句子中的子串。"""
    for keyword in keywords:
        normalized, _ = normalize_message(keyword)
        if normalized and (text == normalized or text.startswith(normalized + " ")):
            return True
    return False


def match_control(text: str, enter_keywords: Sequence[str], exit_keywords: Sequence[str]) -> str | None:
    """退出优先，正确区分 td 和 td stop 等共享前缀。"""
    if matches_keyword(text, exit_keywords):
        return "exit"
    if matches_keyword(text, enter_keywords):
        return "enter"
    return None


def parse_level(value: str) -> int:
    """接受 1—5 或对应中文数字，可以带“档”后缀。"""
    value = value.removesuffix("档").strip()
    if value in _CHINESE_LEVELS:
        return _CHINESE_LEVELS[value]
    if re.fullmatch(r"[+-]?\d+", value):
        level = int(value)
        if 1 <= level <= 5:
            return level
    raise ValueError("档位必须是 1–5 的整数，例如 /控制 2 或 /档位 4")


def parse_gear_command(text: str) -> tuple[str, int | None] | None:
    """识别独立调档指令，和 td level 别名；未知文本返回 None。"""
    for keyword in ("td level", "档位", "调档"):
        if text == keyword:
            return "query", None
        if text.startswith(keyword + " "):
            return "set", parse_level(text[len(keyword) + 1 :])
    for keyword, operation in (("升档", "up"), ("降档", "down")):
        if text == keyword:
            return operation, None
        if text.startswith(keyword + " "):
            raise ValueError(f"用法：/{keyword}，无需附加参数")
    return None


def parse_voltage(value: str, *, positive: bool = False) -> int:
    """解析剧情用的整数电压或步长，支持 V/v/伏/伏特 后缀。"""
    value = re.sub(r"(?:[Vv]|伏特|伏)$", "", value.strip()).strip()
    if re.fullmatch(r"\d+", value):
        voltage = int(value)
        if (1 if positive else 0) <= voltage <= 1000000:
            return voltage
    minimum = 1 if positive else 0
    raise ValueError(f"{'增减量' if positive else '虚拟电压'}必须是 {minimum}–1000000 的整数")


def parse_electric_command(text: str) -> tuple[str, int | None] | None:
    """识别虚拟电流指令，未知文本放行，已识别的错误参数给出反馈。"""
    for keyword in ("td electric", "电流"):
        if text == keyword:
            return "enable", None
        if text.startswith(keyword + " "):
            return "enable", parse_voltage(text[len(keyword) + 1 :])
    for keyword in ("td voltage", "电压"):
        if text == keyword:
            return "query", None
        if text.startswith(keyword + " "):
            return "set", parse_voltage(text[len(keyword) + 1 :])
    for keyword, kind in (("加压", "increase"), ("减压", "decrease")):
        if text == keyword:
            return kind, None
        if text.startswith(keyword + " "):
            return kind, parse_voltage(text[len(keyword) + 1 :], positive=True)
    if text == "断电":
        return "off", None
    if text.startswith("断电 "):
        raise ValueError("用法：/断电，无需附加参数")
    return None


def entry_level(text: str, keywords: Sequence[str], default_level: int) -> int:
    """按最长进入关键词解析选档参数，保留普通场景描述的旧用法。"""
    normalized_keywords = sorted(
        (normalize_message(keyword)[0] for keyword in keywords), key=len, reverse=True
    )
    for keyword in normalized_keywords:
        if keyword and (text == keyword or text.startswith(keyword + " ")):
            suffix = text[len(keyword) :].strip()
            if not suffix:
                return default_level
            if suffix == "档位":
                raise ValueError("请指定档位，例如 /控制 档位 2")
            if suffix.startswith("档位 "):
                return parse_level(suffix[3:])
            first = suffix.split()[0]
            if re.match(r"[+-]?\d", first) or re.fullmatch(r"[零〇一二三四五六七八九十百两]+档?", first):
                return parse_level(first)
            return default_level
    return default_level


def level_name(level: int) -> str:
    """返回稳定的预设名称，配置重载只调整强度而不改变会话档位。"""
    if type(level) is not int or not 1 <= level <= 5:
        raise ValueError("档位必须是 1–5 的整数")
    return LEVEL_NAMES[level - 1]


def effective_sensitivity(base: int, level: int, multipliers: Sequence[float]) -> int:
    """根据档位倍率计算本轮强度，保持默认 3 档与旧版基准一致。"""
    level_name(level)
    return max(0, min(100, round(base * multipliers[level - 1])))


def describe_level(level: int, base: int, multipliers: Sequence[float]) -> str:
    """查询、切档反馈与管理状态共用同一强度计算。"""
    return (
        f"当前档位: {level}/5（{level_name(level)}）\n"
        f"反应强度: {effective_sensitivity(base, level, multipliers)}%"
    )


def describe_electric(voltage: int | None, threshold: int, *, active: bool = True) -> str:
    """显示游戏电压和模式状态，0V 与未开启是不同状态。"""
    if voltage is None:
        return "虚拟电流模式: 未开启"
    return f"虚拟电流模式: {'已开启' if active else '已结束'}\n虚拟电压: {voltage}V / 过载阈值: {threshold}V"


def render_prompt(
    template: str,
    *,
    item_name: str,
    sensitivity: int,
    level: int = 3,
    voltage: int | None = None,
    overload_voltage: int = 100,
) -> str:
    """仅替换支持的变量，保留自定义模板中的 JSON 和其他花括号。"""
    return (
        template.replace("{item_name}", item_name)
        .replace("{sensitivity}", str(sensitivity))
        .replace("{level}", str(level))
        .replace("{level_name}", level_name(level))
        .replace("{voltage}", str(voltage) if voltage is not None else "未开启")
        .replace("{overload_voltage}", str(overload_voltage))
        .replace(
            "{voltage_ratio}",
            str(min(100, round(voltage / overload_voltage * 100))) if voltage is not None else "0",
        )
    )
