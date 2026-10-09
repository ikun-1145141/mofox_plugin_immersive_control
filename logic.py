"""与平台无关的关键词匹配和提示词替换。"""

import re
from collections.abc import Sequence

_LEADING_MENTION = re.compile(r"^(?:@<[^>]+>|\[CQ:at,[^\]]+\])\s*")
LEVEL_NAMES = ("轻柔", "低档", "中档", "高档", "强档")
_CHINESE_LEVELS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}


def normalize_message(text: str) -> tuple[str, bool]:
    """剥离框架格式的前置 @，记录是否显式使用 / 前缀。"""
    text = text.strip()
    while match := _LEADING_MENTION.match(text):
        text = text[match.end() :].lstrip()
    explicit = text.startswith("/")
    if explicit:
        text = text[1:].lstrip()
    return " ".join(text.split()), explicit


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


def render_prompt(template: str, *, item_name: str, sensitivity: int, level: int = 3) -> str:
    """仅替换支持的变量，保留自定义模板中的 JSON 和其他花括号。"""
    return (
        template.replace("{item_name}", item_name)
        .replace("{sensitivity}", str(sensitivity))
        .replace("{level}", str(level))
        .replace("{level_name}", level_name(level))
    )
