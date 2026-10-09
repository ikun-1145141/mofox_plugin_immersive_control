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
    def test_gear_placeholders_preserve_json_and_unknown_braces(self):
        template = (
            '{"device": "{item_name}", "gear": {level}, "name": "{level_name}", '
            '"intensity": {sensitivity}, "data": {"x": 1}}\n{unknown}'
        )
        self.assertEqual(
            LOGIC.render_prompt(template, item_name="装置", sensitivity=75, level=4),
            '{"device": "装置", "gear": 4, "name": "高档", "intensity": 75, "data": {"x": 1}}\n{unknown}',
        )
        self.assertEqual(
            LOGIC.render_prompt("{level}: {level_name}", item_name="装置", sensitivity=50),
            "3: 中档",
        )

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


class GearParsingTests(unittest.TestCase):
    def test_levels_accept_one_through_five_and_chinese_suffixes(self):
        for level in range(1, 6):
            for value in (str(level), f"{level}档", "一二三四五"[level - 1] + "档"):
                with self.subTest(value=value):
                    self.assertEqual(LOGIC.parse_level(value), level)
        self.assertEqual(LOGIC.parse_level("三"), 3)

    def test_invalid_level_text_and_extra_arguments_raise_helpful_error(self):
        for value in ("0", "6", "-1", "2.5", "true", "False", "3 now", "三档 extra", ""):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "1–5"):
                LOGIC.parse_level(value)

    def test_queries_and_sets_accept_all_gear_command_aliases(self):
        for keyword in ("档位", "调档", "td level"):
            with self.subTest(keyword=keyword):
                self.assertEqual(LOGIC.parse_gear_command(keyword), ("query", None))
                self.assertEqual(LOGIC.parse_gear_command(f"{keyword} 2"), ("set", 2))
                self.assertEqual(LOGIC.parse_gear_command(f"{keyword} 四档"), ("set", 4))
                with self.assertRaisesRegex(ValueError, "1–5"):
                    LOGIC.parse_gear_command(f"{keyword} 2 extra")

    def test_step_commands_and_invalid_arguments(self):
        self.assertEqual(LOGIC.parse_gear_command("升档"), ("up", None))
        self.assertEqual(LOGIC.parse_gear_command("降档"), ("down", None))
        for command in ("升档 2", "降档 now"):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, "无需附加参数"):
                LOGIC.parse_gear_command(command)

    def test_stop_is_classified_as_exit_and_never_as_a_gear_command(self):
        for message in ("/td stop", "@<机器人:100> /td stop now"):
            text, _ = LOGIC.normalize_message(message)
            with self.subTest(message=message):
                self.assertEqual(LOGIC.match_control(text, ["td"], ["td stop"]), "exit")
                self.assertIsNone(LOGIC.parse_gear_command(text))
        for text in ("td levelheaded", "普通消息", "查询档位", "档位器"):
            with self.subTest(text=text):
                self.assertIsNone(LOGIC.parse_gear_command(text))

    def test_longest_custom_enter_keyword_keeps_selected_numeric_level(self):
        for keywords in (["control", "control mode"], ["/control mode", "/control"]):
            with self.subTest(keywords=keywords):
                self.assertEqual(LOGIC.entry_level("control mode 2", keywords, 3), 2)
                self.assertEqual(LOGIC.entry_level("control mode 三档", keywords, 1), 3)
                self.assertEqual(LOGIC.entry_level("control mode 档位 4", keywords, 3), 4)

    def test_scene_descriptions_keep_configured_default_level(self):
        for text in ("控制", "控制 从远处按下按钮", "控制 慢慢开始", "control mode 按下遥控器"):
            with self.subTest(text=text):
                self.assertEqual(LOGIC.entry_level(text, ["控制", "control", "control mode"], 3), 3)
        self.assertEqual(LOGIC.entry_level("控制 描述情境", ["控制"], 2), 2)

    def test_explicit_invalid_entry_level_does_not_fall_back_to_default(self):
        for text in ("控制 0", "控制 6", "控制 2.5", "控制 档位 6", "控制 六档", "控制 两档"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "1–5"):
                LOGIC.entry_level(text, ["控制"], 3)
        with self.assertRaisesRegex(ValueError, "请指定档位"):
            LOGIC.entry_level("控制 档位", ["控制"], 3)


class GearSensitivityTests(unittest.TestCase):
    MULTIPLIERS = (0.2, 0.6, 1.0, 1.5, 2.0)

    def test_default_base_maps_all_five_levels_to_expected_strength(self):
        self.assertEqual(
            [LOGIC.effective_sensitivity(50, level, self.MULTIPLIERS) for level in range(1, 6)],
            [10, 30, 50, 75, 100],
        )

    def test_strength_clamps_at_one_hundred_and_zero_base_stays_zero(self):
        self.assertEqual(
            [LOGIC.effective_sensitivity(80, level, self.MULTIPLIERS) for level in range(1, 6)],
            [16, 48, 80, 100, 100],
        )
        self.assertEqual(
            [LOGIC.effective_sensitivity(0, level, self.MULTIPLIERS) for level in range(1, 6)],
            [0, 0, 0, 0, 0],
        )

    def test_level_names_and_feedback_agree_with_strength_calculation(self):
        self.assertEqual([LOGIC.level_name(level) for level in range(1, 6)], list(LOGIC.LEVEL_NAMES))
        self.assertEqual(
            LOGIC.describe_level(4, 50, self.MULTIPLIERS),
            "当前档位: 4/5（高档）\n反应强度: 75%",
        )
        for level in (True, 0, 6, 1.0):
            with self.subTest(level=level), self.assertRaises(ValueError):
                LOGIC.effective_sensitivity(50, level, self.MULTIPLIERS)


if __name__ == "__main__":
    unittest.main()
