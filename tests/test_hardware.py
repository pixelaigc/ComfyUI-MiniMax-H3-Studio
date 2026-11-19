# -*- coding: utf-8 -*-
"""硬件参数测试 —— CPU 名称解析 + 显卡功耗上限读写/不可用分支。

运行：python3 -m pytest tests/test_hardware.py -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from h3studio import hardware as hw  # noqa: E402


# ------------------------------------------------------------------ CPU 名称解析
X86_CPUINFO = """processor\t: 0
vendor_id\t: GenuineIntel
cpu family\t: 6
model\t\t: 198
model name\t: Intel(R) Core(TM) Ultra 9 285K
stepping\t: 2

processor\t: 1
vendor_id\t: GenuineIntel
cpu family\t: 6
model\t\t: 198
model name\t: Intel(R) Core(TM) Ultra 9 285K
stepping\t: 2

processor\t: 2
vendor_id\t: GenuineIntel
cpu family\t: 6
model\t\t: 198
model name\t: Intel(R) Core(TM) Ultra 9 285K
stepping\t: 2
"""

# 异构：大小核两种 model name
HETERO_CPUINFO = """processor\t: 0
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 1
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 2
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 3
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 4
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 5
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 6
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 7
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 8
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 9
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 10
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 11
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 12
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 13
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 14
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 15
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 16
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 17
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 18
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 19
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 20
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 21
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 22
model name\t: Intel(R) Core(TM) i9-13900K
processor\t: 23
model name\t: Intel(R) Core(TM) i9-13900K
"""

# ARM 机器：没有 model name，用 Processor 字段
ARM_CPUINFO = """processor\t: 0
Processor\t: AArch64 Processor rev 4 (aarch64)
BogoMIPS\t: 50.00
Features\t: fp asimd

processor\t: 1
Processor\t: AArch64 Processor rev 4 (aarch64)
BogoMIPS\t: 50.00
Features\t: fp asimd
"""

LSCPU_EN = """Architecture:            x86_64
CPU op-mode(s):          32-bit, 64-bit
Vendor ID:               GenuineIntel
Model name:              Intel(R) Core(TM) Ultra 9 285K
CPU family:              6
Model:                   198
"""

LSCPU_ZH = """架构：                                   x86_64
厂商 ID：                                GenuineIntel
型号名称：                               Intel(R) Core(TM) Ultra 9 285K
CPU(s)：                                 24
"""


class TestCPUInfoParsing(unittest.TestCase):
    """解析 /proc/cpuinfo。"""

    def test_parses_model_name_and_counts(self):
        models = hw.parse_cpuinfo_models(X86_CPUINFO)
        self.assertEqual(len(models), 1)
        name, count = models[0]
        self.assertEqual(name, "Intel(R) Core(TM) Ultra 9 285K")
        self.assertEqual(count, 3)

    def test_processor_index_is_not_taken_as_model(self):
        """x86 的 'processor : 0' 是序号，绝不能被当成型号名。"""
        models = hw.parse_cpuinfo_models(X86_CPUINFO)
        for name, _ in models:
            self.assertNotEqual(name.strip(), "0")
            self.assertIn("Intel", name)

    def test_detects_heterogeneous_cpu(self):
        models = hw.parse_cpuinfo_models(HETERO_CPUINFO)
        self.assertEqual(len(models), 1, "本样本同型号，应合并为一条")

    def test_arm_processor_field_is_used(self):
        models = hw.parse_cpuinfo_models(ARM_CPUINFO)
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0][0], "AArch64 Processor rev 4 (aarch64)")
        self.assertEqual(models[0][1], 2)

    def test_empty_input_returns_empty(self):
        self.assertEqual(hw.parse_cpuinfo_models(""), [])
        self.assertEqual(hw.parse_cpuinfo_models(None), [])

    def test_vendor_extracted(self):
        self.assertEqual(hw.parse_cpuinfo_vendor(X86_CPUINFO), "GenuineIntel")
        self.assertIsNone(hw.parse_cpuinfo_vendor(ARM_CPUINFO))


class TestLscpuParsing(unittest.TestCase):
    def test_english_model_name(self):
        self.assertEqual(hw.parse_lscpu_model(LSCPU_EN),
                         "Intel(R) Core(TM) Ultra 9 285K")

    def test_localized_model_name(self):
        """本机 lscpu 输出中文（型号名称），必须能解析。"""
        self.assertEqual(hw.parse_lscpu_model(LSCPU_ZH),
                         "Intel(R) Core(TM) Ultra 9 285K")

    def test_no_model_returns_none(self):
        self.assertIsNone(hw.parse_lscpu_model("Architecture: x86_64\nCPU(s): 24\n"))


class TestDetectCPU(unittest.TestCase):
    """detect_cpu 的多级降级。"""

    def test_prefers_cpuinfo(self):
        info = hw.detect_cpu(
            cpuinfo_text=X86_CPUINFO, lscpu_text=LSCPU_EN,
            platform_processor="", machine="x86_64",
            physical_cores=3, logical_cores=3)
        self.assertIn("Intel(R) Core(TM) Ultra 9 285K", info["name"])
        self.assertEqual(info["model"], "Intel(R) Core(TM) Ultra 9 285K")
        # 【2026-11-19 第 3 轮 C 项审查修】
        # 原来断言 `hw.CPUINFO_PATH`（= "/proc/cpuinfo"）—— 那条断言
        # **锁死了路径泄漏**：source 会经 /api/hardware 下发，被前端
        # web/app.js 渲染成「型号来源 /proc/cpuinfo」。
        # 现在 source 只保留**来源名**，语义（怎么读到的型号）不丢。
        self.assertEqual(info["source"], "cpuinfo")
        self.assertEqual(info["cores"], 3)
        self.assertEqual(info["threads"], 3)
        self.assertEqual(info["architecture"], "x86_64")
        self.assertEqual(info["vendor"], "GenuineIntel")
        self.assertFalse(info["heterogeneous"])

    def test_source_is_never_a_path(self):
        """source **绝不能**是本机路径（它会被原样显示给用户）。

        【为什么这条要独立存在】
        `source` 是自由文本字段 —— 将来加新取值时很容易又写成
        某个 /proc/... 或 /sys/... 路径。而这个字段是**直接显示**在
        「硬件信息」面板里的（web/app.js 的「型号来源」行）。

        判据：无论走哪条分支，source 都不能以 "/" 开头。
        """
        cases = [
            # cpuinfo 命中
            dict(cpuinfo_text=X86_CPUINFO, lscpu_text=LSCPU_EN),
            # lscpu 命中
            dict(cpuinfo_text="", lscpu_text=LSCPU_EN),
            # platform 兜底
            dict(cpuinfo_text="", lscpu_text="", platform_processor=""),
            # 全空兜底
            dict(cpuinfo_text="", lscpu_text="", platform_processor=""),
        ]
        for kw in cases:
            info = hw.detect_cpu(**kw)
            src = str(info.get("source") or "")
            self.assertFalse(src.startswith("/"),
                             f"source 不能是路径：{src!r}（参数 {kw}）")

    def test_falls_back_to_lscpu_when_cpuinfo_missing(self):
        info = hw.detect_cpu(
            cpuinfo_text="", lscpu_text=LSCPU_EN,
            platform_processor="", machine="x86_64",
            physical_cores=24, logical_cores=24)
        self.assertEqual(info["model"], "Intel(R) Core(TM) Ultra 9 285K")
        self.assertEqual(info["source"], "lscpu")

    def test_falls_back_to_platform_processor(self):
        info = hw.detect_cpu(
            cpuinfo_text="", lscpu_text="",
            platform_processor="Intel64 Family 6 Model 142", machine="x86_64",
            physical_cores=4, logical_cores=8)
        self.assertEqual(info["model"], "Intel64 Family 6 Model 142")
        self.assertEqual(info["source"], "platform.processor()")

    def test_final_fallback_is_arch_plus_cores_not_unknown(self):
        """全部来源都失败时，必须给出可辨识兜底，绝不出现「未知」。"""
        info = hw.detect_cpu(
            cpuinfo_text="", lscpu_text="", platform_processor="",
            machine="aarch64", physical_cores=8, logical_cores=8)
        self.assertIn("aarch64", info["name"])
        self.assertIn("8", info["name"])
        self.assertNotIn("未知", info["name"])
        self.assertNotIn("unknown", info["name"].lower())

    def test_heterogeneous_name_lists_both_models(self):
        """异构机器：名称里应同时出现大核与小核型号与各自线程数。"""
        big = "Intel(R) Core(TM) i9-13900K"
        small = "Intel(R) Core(TM) i9-13900K (E-core)"
        blocks = []
        for i in range(24):
            blocks.append(f"processor\t: {i}\nmodel name\t: {big}\n")
        for i in range(24, 32):
            blocks.append(f"processor\t: {i}\nmodel name\t: {small}\n")
        hetero_text = "\n\n".join(blocks)

        info = hw.detect_cpu(
            cpuinfo_text=hetero_text, lscpu_text="", platform_processor="",
            machine="x86_64", physical_cores=32, logical_cores=32)
        self.assertTrue(info["heterogeneous"])
        self.assertEqual(len(info["models"]), 2)
        self.assertIn(big, info["name"])
        self.assertIn(small, info["name"])
        self.assertIn("24", info["name"])
        self.assertIn("8", info["name"])

    def test_real_machine_cpu_is_not_unknown(self):
        """真机自检：本机 CPU 名称不能是「未知」。"""
        info = hw.detect_cpu()
        self.assertTrue(info["name"])
        self.assertNotIn("未知", info["name"])
        self.assertNotIn("unknown", info["name"].lower())
        self.assertGreaterEqual(info["threads"], 1)


# ------------------------------------------------------------------ 功耗上限
POWER_CSV_OK = """0, NVIDIA GeForce RTX 4090 D, 425.00, 150.00, 425.00, 425.00
1, Tesla P40, 250.00, 125.00, 250.00, 250.00
2, NVIDIA GeForce RTX 2080 Ti, 280.00, 100.00, 280.00, 250.00
"""

# 驱动不暴露 min/max → 不可修改
POWER_CSV_UNSUPPORTED = """0, Some Integrated GPU, 25.00, [N/A], [N/A], [N/A]
"""


class TestPowerLimitParsing(unittest.TestCase):
    def test_parses_supported_gpu(self):
        rows = hw.parse_power_limit_csv(POWER_CSV_OK)
        self.assertEqual(len(rows), 3)
        g0 = rows[0]
        self.assertEqual(g0["index"], 0)
        self.assertEqual(g0["name"], "NVIDIA GeForce RTX 4090 D")
        self.assertEqual(g0["current"], 425.0)
        self.assertEqual(g0["min"], 150.0)
        self.assertEqual(g0["max"], 425.0)
        self.assertTrue(g0["supported"])
        self.assertIsNone(g0["reason"])

    def test_marks_unsupported_gpu_with_reason(self):
        rows = hw.parse_power_limit_csv(POWER_CSV_UNSUPPORTED)
        self.assertEqual(len(rows), 1)
        g0 = rows[0]
        self.assertFalse(g0["supported"])
        self.assertTrue(g0["reason"], "不支持时必须给出明确原因文案")
        self.assertIn("不支持", g0["reason"])

    def test_empty_csv(self):
        self.assertEqual(hw.parse_power_limit_csv(""), [])


class TestQueryPowerLimits(unittest.TestCase):
    def test_reports_available(self):
        with mock.patch.object(hw, "_run_cmd",
                               return_value=(0, POWER_CSV_OK, "")):
            caps = hw.query_power_limits()
        self.assertTrue(caps["available"])
        self.assertIsNone(caps["reason"])
        self.assertEqual(len(caps["gpus"]), 3)

    def test_nvidia_smi_missing_is_reported(self):
        with mock.patch.object(hw, "_run_cmd",
                               return_value=(127, "", "command not found")):
            caps = hw.query_power_limits()
        self.assertFalse(caps["available"])
        self.assertIn("nvidia-smi", caps["reason"])

    def test_no_gpu_reported(self):
        with mock.patch.object(hw, "_run_cmd", return_value=(0, "", "")):
            caps = hw.query_power_limits()
        self.assertFalse(caps["available"])
        self.assertTrue(caps["reason"])


class TestSetPowerLimit(unittest.TestCase):
    """写入分支：范围校验、安全约束、回读校验、不可用分支。"""

    def _patch(self, csv=POWER_CSV_OK, set_rc=0, set_out="", set_err="",
               read_rc=0, read_out=""):
        calls = []

        def fake_run(cmd, timeout=6, env_extra=None):
            calls.append(list(cmd))
            if "-pl" in cmd:
                return set_rc, set_out, set_err
            if "--query-gpu=power.limit" in cmd:
                return read_rc, read_out, ""
            return 0, csv, ""

        return mock.patch.object(hw, "_run_cmd", side_effect=fake_run), calls

    def test_successful_lowering_is_verified_by_readback(self):
        patcher, calls = self._patch(read_out="200.00\n")
        with patcher:
            result = hw.set_power_limit(1, 200)
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["current"], 200.0)
        self.assertEqual(result["previous"], 250.0)
        # 必须真的调用了 nvidia-smi -pl
        self.assertTrue(any("-pl" in c for c in calls))

    def test_refuses_to_raise_without_allow_raise(self):
        """安全约束：默认不允许提高功耗上限。"""
        # GPU 3: current=200（被调低过），max=280 → 目标 280 属于「提高」
        csv = ("0, NVIDIA GeForce RTX 4090 D, 425.00, 150.00, 425.00, 425.00\n"
               "1, Tesla P40, 250.00, 125.00, 250.00, 250.00\n"
               "2, NVIDIA GeForce RTX 2080 Ti, 280.00, 100.00, 280.00, 250.00\n"
               "3, NVIDIA GeForce RTX 2080 Ti, 200.00, 100.00, 280.00, 250.00\n")
        patcher, calls = self._patch(csv=csv)
        with patcher:
            result = hw.set_power_limit(3, 280)
        self.assertFalse(result["ok"])
        self.assertFalse(result["verified"])
        self.assertIn("拒绝提高", result["message"])
        # 不允许提高时不应发出任何 -pl 写入
        self.assertFalse(any("-pl" in c for c in calls))

    def test_raising_allowed_with_explicit_flag(self):
        csv = ("3, NVIDIA GeForce RTX 2080 Ti, 200.00, 100.00, 280.00, 250.00\n")
        patcher, calls = self._patch(csv=csv, read_out="280.00\n")
        with patcher:
            result = hw.set_power_limit(3, 280, allow_raise=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertTrue(result["raised"])
        self.assertTrue(any("-pl" in c for c in calls))

    def test_rejects_out_of_range_value(self):
        patcher, calls = self._patch()
        with patcher:
            result = hw.set_power_limit(1, 300)  # 范围 125–250
        self.assertFalse(result["ok"])
        self.assertIn("超出", result["message"])
        self.assertFalse(any("-pl" in c for c in calls))

    def test_rejects_value_below_min(self):
        patcher, calls = self._patch()
        with patcher:
            result = hw.set_power_limit(1, 50)  # 范围 125–250
        self.assertFalse(result["ok"])
        self.assertIn("超出", result["message"])
        self.assertFalse(any("-pl" in c for c in calls))

    def test_unsupported_gpu_returns_clear_reason(self):
        patcher, calls = self._patch(csv=POWER_CSV_UNSUPPORTED)
        with patcher:
            result = hw.set_power_limit(0, 20)
        self.assertFalse(result["ok"])
        self.assertIn("不支持", result["message"])
        self.assertFalse(any("-pl" in c for c in calls))

    def test_nvidia_smi_missing_returns_clear_reason(self):
        def fake_run(cmd, timeout=6, env_extra=None):
            if "--query-gpu" in cmd:
                return 127, "", "command not found"
            return 127, "", "command not found"

        with mock.patch.object(hw, "_run_cmd", side_effect=fake_run):
            result = hw.set_power_limit(0, 200)
        self.assertFalse(result["ok"])
        self.assertIn("nvidia-smi", result["message"])

    def test_unknown_index_returns_error(self):
        patcher, _ = self._patch()
        with patcher:
            result = hw.set_power_limit(99, 200)
        self.assertFalse(result["ok"])
        self.assertIn("未找到", result["message"])

    def test_write_failure_is_reported(self):
        patcher, _ = self._patch(set_rc=2,
                                 set_err="Provided power limit is not a valid power limit")
        with patcher:
            result = hw.set_power_limit(1, 200)
        self.assertFalse(result["ok"])
        self.assertFalse(result["verified"])
        self.assertIn("设置失败", result["message"])

    def test_readback_mismatch_is_reported(self):
        """写入命令成功但回读不一致 → 必须判定失败，不能报成功。"""
        patcher, _ = self._patch(read_out="150.00\n")
        with patcher:
            result = hw.set_power_limit(1, 200)
        self.assertFalse(result["ok"])
        self.assertFalse(result["verified"])
        self.assertIn("回读不一致", result["message"])

    def test_unreadable_after_write_is_reported(self):
        patcher, _ = self._patch(read_rc=1, read_out="")
        with patcher:
            result = hw.set_power_limit(1, 200)
        self.assertFalse(result["ok"])
        self.assertFalse(result["verified"])
        self.assertIn("无法读回", result["message"])

    def test_invalid_arguments(self):
        self.assertFalse(hw.set_power_limit("x", 200)["ok"])
        self.assertFalse(hw.set_power_limit(0, "abc")["ok"])


class TestQueryGPUs(unittest.TestCase):
    """query_gpus 把功耗上限能力挂到每张卡上。"""

    def test_gpus_carry_power_limit_info(self):
        status_csv = (
            "0, NVIDIA GeForce RTX 4090 D, 24576, 1024, 23552, 30, 45, "
            "60.00, 425.00, 580.82.09, 2100\n"
        )
        calls = []

        def fake_run(cmd, timeout=6, env_extra=None):
            calls.append(list(cmd))
            if "--query-gpu=power.limit" in cmd:
                return 0, "425.00\n", ""
            if "power.min_limit" in " ".join(cmd):
                return 0, POWER_CSV_OK, ""
            return 0, status_csv, ""

        with mock.patch.object(hw, "_run_cmd", side_effect=fake_run):
            with mock.patch.object(hw.subprocess, "check_output",
                                   return_value=status_csv):
                gpus = hw.query_gpus()
        self.assertEqual(len(gpus), 1)
        g = gpus[0]
        self.assertEqual(g["index"], 0)
        self.assertEqual(g["name"], "NVIDIA GeForce RTX 4090 D")
        info = g["power_limit_info"]
        self.assertTrue(info["supported"])
        self.assertEqual(info["min"], 150.0)
        self.assertEqual(info["max"], 425.0)
        self.assertEqual(info["current"], 425.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
