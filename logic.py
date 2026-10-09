"""与平台无关的关键词匹配和提示词替换。"""

import re
from collections.abc import Sequence

_LEADING_MENTION = re.compile(r"^(?:@<[^>]+>|\[CQ:at,[^\]]+\])\s*")


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


def render_prompt(template: str, *, item_name: str, sensitivity: int) -> str:
    """仅替换支持的变量，保留自定义模板中的 JSON 和其他花括号。"""
    return template.replace("{item_name}", item_name).replace("{sensitivity}", str(sensitivity))
