from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("immersive_control_logic_test", ROOT / "logic.py")
assert SPEC is not None and SPEC.loader is not None
LOGIC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LOGIC)


class MessageNormalizationTests(unittest.TestCase):
    def test_optional_slash_and_outer_whitespace(self):
        cases = [
            ("控制", ("控制", False)),
            (" /控制 ", ("控制", True)),
            (" /  控制 ", ("控制", True)),
            ("\t\u3000", ("", False)),
            ("/", ("", True)),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(LOGIC.normalize_message(text), expected)

    def test_repeated_leading_framework_mentions(self):
        self.assertEqual(
            LOGIC.normalize_message(" @<小狐狸:42>\t@<机器人:100>\u3000/td stop now "),
            ("td stop now", True),
        )
        self.assertEqual(
            LOGIC.normalize_message("[CQ:at,qq=42] @<机器人:100> /控制"),
            ("控制", True),
        )

    def test_full_width_spaces_tabs_and_newlines_become_word_boundaries(self):
        self.assertEqual(
            LOGIC.normalize_message("\u3000td\t\tstop\u3000now\n"),
            ("td stop now", False),
        )

    def test_mentions_inside_ordinary_text_are_preserved(self):
        self.assertEqual(
            LOGIC.normalize_message("请问 @<机器人:100> 控制怎么用"),
            ("请问 @<机器人:100> 控制怎么用", False),
        )


class KeywordMatchingTests(unittest.TestCase):
    @staticmethod
    def match(message, enter=("控制", "td"), exit=("停止控制", "td stop")):
        normalized, _ = LOGIC.normalize_message(message)
        return LOGIC.match_control(normalized, enter, exit)

    def test_custom_keywords_accept_optional_slash_in_config_and_message(self):
        for keyword in ("自定义", "/自定义", " / 自定义 "):
            for message in ("自定义", "/自定义", "@<机器人:100> /自定义"):
                with self.subTest(keyword=keyword, message=message):
                    self.assertEqual(self.match(message, enter=[keyword], exit=[]), "enter")

    def test_multiword_exit_has_priority_over_shared_enter_prefix(self):
        for message in ("td stop", "td stop now", "/td\tstop\u3000now"):
            with self.subTest(message=message):
                self.assertEqual(self.match(message), "exit")
        self.assertEqual(self.match("td speed 2"), "enter")

    def test_normalizes_multiword_custom_keywords(self):
        self.assertEqual(
            self.match("/switch\t off now", enter=["switch"], exit=["/switch\u3000off"]),
            "exit",
        )

    def test_keyword_requires_full_word_boundary(self):
        for message in ("控制器", "控制一下", "停止控制器", "tdx", "我想控制你", "请停止控制"):
            with self.subTest(message=message):
                self.assertIsNone(self.match(message))
        self.assertEqual(self.match("控制 参数"), "enter")

    def test_empty_keywords_never_match(self):
        for message in ("", "普通消息"):
            with self.subTest(message=message):
                self.assertIsNone(self.match(message, enter=["", " ", "/"], exit=[]))


class PromptRenderingTests(unittest.TestCase):
    def test_only_supported_placeholders_are_replaced_and_json_braces_survive(self):
        template = '{"item": "{item_name}", "level": {sensitivity}, "data": {"x": 1}}\n{unknown} {item_name}'
        rendered = LOGIC.render_prompt(template, item_name="魔法装置", sensitivity=75)
        self.assertEqual(
            rendered,
            '{"item": "魔法装置", "level": 75, "data": {"x": 1}}\n{unknown} 魔法装置',
        )

    def test_empty_template_and_no_placeholders_are_supported(self):
        self.assertEqual(LOGIC.render_prompt("", item_name="装置", sensitivity=0), "")
        self.assertEqual(
            LOGIC.render_prompt('{"nested": {"ok": true}}', item_name="装置", sensitivity=100),
            '{"nested": {"ok": true}}',
        )


if __name__ == "__main__":
    unittest.main()
