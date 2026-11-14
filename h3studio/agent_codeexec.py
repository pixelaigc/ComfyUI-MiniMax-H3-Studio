# -*- coding: utf-8 -*-
"""
内置 Agent 的 **代码生成与执行**能力（默认关闭，需用户在「agent 管理」里显式启用）。

【为什么默认关闭】
这是本清单里唯一**有副作用**的能力：执行任意 Python。
默认打开等于「模型一句话就能在本机跑代码」—— 那不是能力，是风险。
所以：
  * 默认 disabled（agent_settings.DEFAULT_CAPABILITIES["code_exec"] = False）；
  * 开启后仍然有边界：独立进程、超时、输出裁剪、工作目录受限。

【边界】
  * 用 subprocess 起独立进程（不 eval 在服务端进程里 —— 崩了会带走服务）。
  * 无网络/文件系统限制是**做不到**的（用户自己开的沙箱另说），
    所以这里的边界是「超时 + 输出上限 + 工作目录 + 明确的告警」。
  * 超时后 kill 整个进程组（否则子进程会变孤儿继续跑）。
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import tempfile
from pathlib import Path

#: 默认超时（秒）。
DEFAULT_TIMEOUT = 30.0

#: 单次输出的最大字符数（回给模型的部分）。
MAX_OUTPUT_CHARS = 4000

#: 单次执行允许的最大源码长度。
MAX_CODE_CHARS = 20000


class CodeExecError(Exception):
    """代码执行失败。"""

    def __init__(self, message, code="code_exec_failed", status=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


async def run_python(code: str, timeout: float = DEFAULT_TIMEOUT,
                     workdir: str = "") -> dict:
    """
    执行一段 Python，返回 stdout / stderr / 退出码。

    timeout <= 0 时用默认值；workdir 为空时用系统临时目录。
    """
    source = str(code or "")
    if not source.strip():
        raise CodeExecError("代码不能为空", code="bad_request", status=400)
    if len(source) > MAX_CODE_CHARS:
        raise CodeExecError(
            f"代码过长（{len(source)} 字符，上限 {MAX_CODE_CHARS}）",
            code="code_too_long", status=413)
    try:
        limit = float(timeout)
    except (TypeError, ValueError):
        limit = DEFAULT_TIMEOUT
    limit = max(1.0, min(300.0, limit))

    if workdir:
        base = Path(workdir).expanduser()
        try:
            base = base.resolve()
            base.mkdir(parents=True, exist_ok=True)
        except (OSError, RuntimeError) as exc:
            raise CodeExecError(f"工作目录不可用：{exc}",
                                code="workdir_invalid") from exc
        holder = None
        script_path = base / f"_agent_exec_{os.getpid()}.py"
    else:
        holder = tempfile.TemporaryDirectory(prefix="h3agent-code-")
        base = Path(holder.name)
        script_path = base / "main.py"

    try:
        script_path.write_text(source, encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(script_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(base),
                env=env,
                start_new_session=True,   # 独立进程组 -> 超时能整组 kill
            )
        except OSError as exc:
            raise CodeExecError(f"无法启动解释器：{exc}",
                                code="spawn_failed", status=500) from exc
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(),
                                                    timeout=limit)
            timed_out = False
        except asyncio.TimeoutError:
            timed_out = True
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(),
                                                        timeout=10)
            except asyncio.TimeoutError:
                stdout, stderr = b"", b""
        code_exit = proc.returncode if proc.returncode is not None else -1
        out_text = (stdout or b"").decode("utf-8", "replace")
        err_text = (stderr or b"").decode("utf-8", "replace")
        result = {
            "exit_code": code_exit,
            "stdout": out_text[:MAX_OUTPUT_CHARS],
            "stderr": err_text[:MAX_OUTPUT_CHARS],
            "timed_out": timed_out,
            "timeout_sec": limit,
            "workdir": str(base),
            "truncated": len(out_text) > MAX_OUTPUT_CHARS
                        or len(err_text) > MAX_OUTPUT_CHARS,
        }
        if timed_out:
            result["error"] = f"执行超时（{int(limit)} 秒），已终止"
        return result
    finally:
        try:
            script_path.unlink(missing_ok=True)
        except OSError:
            pass
        if holder is not None:
            holder.cleanup()
