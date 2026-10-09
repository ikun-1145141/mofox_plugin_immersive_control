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
    def test_all_seven_supported_variables_preserve_json_and_unknown_placeholders(self):
        template = (
            '{"item": "{item_name}", "sensitivity": {sensitivity}, "level": {level}, '
            '"level_name": "{level_name}", "voltage": {voltage}, '
            '"threshold": {overload_voltage}, "ratio": {voltage_ratio}, "nested": {"x": 1}}\n{unknown}'
        )
        self.assertEqual(
            LOGIC.render_prompt(
                template, item_name="虚拟装置", sensitivity=75, level=4, voltage=50, overload_voltage=100
            ),
            '{"item": "虚拟装置", "sensitivity": 75, "level": 4, "level_name": "高档", '
            '"voltage": 50, "threshold": 100, "ratio": 50, "nested": {"x": 1}}\n{unknown}',
        )

    def test_voltage_zero_differs_from_disabled_and_ratio_caps_at_one_hundred(self):
        template = "{voltage} / {overload_voltage} ({voltage_ratio}%)"
        for voltage, expected in (
            (None, "未开启 / 100 (0%)"),
            (0, "0 / 100 (0%)"),
            (100, "100 / 100 (100%)"),
            (150, "150 / 100 (100%)"),
        ):
            with self.subTest(voltage=voltage):
                self.assertEqual(
                    LOGIC.render_prompt(
                        template, item_name="虚拟装置", sensitivity=50, voltage=voltage, overload_voltage=100
                    ),
                    expected,
                )

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


class ElectricParsingTests(unittest.TestCase):
    def test_virtual_voltage_accepts_units_zero_and_upper_bound(self):
        for value, expected in (("0V", 0), ("10v", 10), ("10伏", 10), ("10伏特", 10), (" 10 V ", 10)):
            with self.subTest(value=value):
                self.assertEqual(LOGIC.parse_voltage(value), expected)
        self.assertEqual(LOGIC.parse_voltage("1000000"), 1_000_000)
        self.assertEqual(LOGIC.parse_voltage("1000000V", positive=True), 1_000_000)

    def test_voltage_and_positive_steps_reject_invalid_or_extra_text(self):
        for value in ("-1", "2.5", "True", "false", "10V extra", "10 伏 now", "1000001", ""):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "0–1000000"):
                    LOGIC.parse_voltage(value)
                with self.assertRaisesRegex(ValueError, "1–1000000"):
                    LOGIC.parse_voltage(value, positive=True)
        for value in ("0", "0V", "0伏特"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "1–1000000"):
                LOGIC.parse_voltage(value, positive=True)
        self.assertEqual(LOGIC.parse_voltage("10伏特", positive=True), 10)

    def test_enable_query_and_set_aliases(self):
        for keyword in ("td electric", "电流"):
            with self.subTest(keyword=keyword):
                self.assertEqual(LOGIC.parse_electric_command(keyword), ("enable", None))
                self.assertEqual(LOGIC.parse_electric_command(f"{keyword} 0V"), ("enable", 0))
                self.assertEqual(LOGIC.parse_electric_command(f"{keyword} 20伏特"), ("enable", 20))
        for keyword in ("td voltage", "电压"):
            with self.subTest(keyword=keyword):
                self.assertEqual(LOGIC.parse_electric_command(keyword), ("query", None))
                self.assertEqual(LOGIC.parse_electric_command(f"{keyword} 0"), ("set", 0))
                self.assertEqual(LOGIC.parse_electric_command(f"{keyword} 40v"), ("set", 40))

    def test_relative_commands_use_optional_strictly_positive_step(self):
        for keyword, kind in (("加压", "increase"), ("减压", "decrease")):
            with self.subTest(keyword=keyword):
                self.assertEqual(LOGIC.parse_electric_command(keyword), (kind, None))
                self.assertEqual(LOGIC.parse_electric_command(f"{keyword} 10V"), (kind, 10))
                for argument in ("0", "-10", "2.5", "true", "10 extra", "1000001"):
                    with self.subTest(argument=argument), self.assertRaises(ValueError):
                        LOGIC.parse_electric_command(f"{keyword} {argument}")

    def test_off_command_rejects_extra_arguments(self):
        self.assertEqual(LOGIC.parse_electric_command("断电"), ("off", None))
        with self.assertRaisesRegex(ValueError, "无需附加参数"):
            LOGIC.parse_electric_command("断电 now")

    def test_unknown_ordinary_text_and_near_names_pass_through(self):
        for text in ("普通话", "今天电压很高", "电压foo", "电流foo", "断电器", "加压阀", "td voltagefoo"):
            with self.subTest(text=text):
                self.assertIsNone(LOGIC.parse_electric_command(text))
        for message in ("/td stop", "@<机器人:100> /td stop now"):
            text, _ = LOGIC.normalize_message(message)
            with self.subTest(message=message):
                self.assertEqual(LOGIC.match_control(text, ["td"], ["td stop"]), "exit")
                self.assertIsNone(LOGIC.parse_electric_command(text))

    def test_known_electric_commands_with_bad_parameters_raise_instead_of_passing(self):
        for command in ("电流 nope", "td electric 2.5", "电压 20V extra", "td voltage -1"):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, "虚拟电压"):
                LOGIC.parse_electric_command(command)

    def test_electric_command_uses_shared_message_normalization(self):
        text, explicit = LOGIC.normalize_message("@<机器人:100> /td\telectric\u30000V")
        self.assertTrue(explicit)
        self.assertEqual(LOGIC.parse_electric_command(text), ("enable", 0))


class ElectricDescriptionTests(unittest.TestCase):
    def test_active_zero_and_inactive_voltages_have_distinct_phase_descriptions(self):
        self.assertEqual(
            LOGIC.describe_electric(20, 100),
            "虚拟电流模式: 已开启\n虚拟电压: 20V / 过载阈值: 100V",
        )
        self.assertEqual(
            LOGIC.describe_electric(0, 100),
            "虚拟电流模式: 已开启\n虚拟电压: 0V / 过载阈值: 100V",
        )
        self.assertEqual(
            LOGIC.describe_electric(120, 100, active=False),
            "虚拟电流模式: 已结束\n虚拟电压: 120V / 过载阈值: 100V",
        )

    def test_disabled_mode_is_described_as_off_regardless_of_control_phase(self):
        for active in (True, False):
            with self.subTest(active=active):
                self.assertEqual(LOGIC.describe_electric(None, 100, active=active), "虚拟电流模式: 未开启")


if __name__ == "__main__":
    unittest.main()
