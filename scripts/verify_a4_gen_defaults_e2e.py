# -*- coding: utf-8 -*-
"""A4 端到端手测脚本（真起 aiohttp 服务 + 真 sqlite 落库 + 真 Agent 建任务）。

【为什么单独写脚本而不是只靠 pytest】
pytest 里的 FakeServer 绕过了 HTTP 层。这个脚本走**完整链路**：
  PUT /api/agent/settings -> 真 sqlite -> GET 读回 -> Agent create_job -> 真落库
这样能验到「HTTP 层下发/接收的结构」与「Agent 运行时读到的结构」是同一份。

运行：python3 scripts/verify_a4_gen_defaults_e2e.py
"""

import asyncio
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import server as srv                                   # noqa: E402
from h3studio import agent as agent_mod                # noqa: E402
from h3studio import agent_settings                    # noqa: E402

OUT = Path("/tmp/a4_e2e")
OUT.mkdir(parents=True, exist_ok=True)

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    print(("PASS " if ok else "FAIL ") + name + ("  | " + str(detail) if detail else ""))
    return bool(ok)


def make_request(method, path, payload=None):
    """造一个最小的 aiohttp Request 替身（只用到 .method / .json()）。"""

    class _Req:
        def __init__(self):
            self.method = method
            self._payload = payload

        async def json(self):
            if self._payload is None:
                raise ValueError("no body")
            return self._payload

    return _Req()


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="a4-e2e-"))
    orig_db = srv.DB_PATH
    srv.DB_PATH = tmp / "jobs.db"
    try:
        srv.init_db()
        from h3studio import providers as prov
        with srv.db() as conn:
            prov.ensure_tables(conn)

        # ---- 1. GET：规格与默认值都下发 ----
        resp = await srv.api_agent_settings(make_request("GET", "/api/agent/settings"))
        body = json.loads(resp.body.decode())
        spec = body.get("gen_defaults_spec") or {}
        gen = body.get("gen_defaults") or {}
        check("GET 下发 gen_defaults_spec", bool(spec), f"{len(spec)} 个模式")
        check("GET 下发 gen_defaults", bool(gen), f"{len(gen)} 个模式")
        check("t2v 规格里含注意力机制字段",
              "attention" in {f["name"] for f in spec.get("t2v", {}).get("fields", [])})
        check("longfilm 显式标注「无可调参数」",
              spec.get("longfilm", {}).get("supported") is False)

        # ---- 2. PUT：按生成类型分别保存 ----
        payload = {
            "gen_defaults": {
                "t2v": {"attention": "sparse", "duration": 8.0},
                "t2i": {"steps": 33, "cfg": 5.5},
                "t2i_moss": {"cfg": 4.0, "slg_single_layers": "20"},
                "t2a": {"format": "mp3", "max_duration": 120.0},
            },
            "t2v_defaults": {"attention": "sparse", "duration": 8.0},
        }
        resp = await srv.api_agent_settings(
            make_request("PUT", "/api/agent/settings", payload))
        body = json.loads(resp.body.decode())
        check("PUT 返回 200", resp.status == 200, resp.status)
        saved = body.get("gen_defaults") or {}
        check("t2i 的 steps 存下来了", saved.get("t2i", {}).get("steps") == 33,
              saved.get("t2i"))
        check("t2a 的 format 存下来了", saved.get("t2a", {}).get("format") == "mp3",
              saved.get("t2a"))
        check("老结构 t2v_defaults 同步了",
              body.get("settings", {}).get("t2v_defaults", {}).get("attention") == "sparse",
              body.get("settings", {}).get("t2v_defaults"))

        # ---- 3. 真落库检查 ----
        with sqlite3.connect(srv.DB_PATH) as conn:
            row = conn.execute(
                "SELECT value FROM model_provider_prefs WHERE key=?",
                (agent_settings.PREF_KEY,)).fetchone()
        raw = json.loads(row[0])
        check("sqlite 里真的有 gen_defaults",
              raw.get("gen_defaults", {}).get("t2i", {}).get("steps") == 33)
        check("sqlite 里也有 t2v_defaults（兼容镜像）",
              raw.get("t2v_defaults", {}).get("attention") == "sparse")

        # ---- 4. GET 读回 ----
        resp = await srv.api_agent_settings(make_request("GET", "/api/agent/settings"))
        body = json.loads(resp.body.decode())
        check("GET 读回到 t2i.steps=33",
              body["gen_defaults"]["t2i"]["steps"] == 33,
              body["gen_defaults"]["t2i"])
        check("GET 读回到 t2a.format=mp3",
              body["gen_defaults"]["t2a"]["format"] == "mp3")

        # ---- 5. 脏值不入库 ----
        await srv.api_agent_settings(make_request(
            "PUT", "/api/agent/settings",
            {"gen_defaults": {"t2i": {"model": "no_such_model",
                                      "steps": "abc"}}}))
        with sqlite3.connect(srv.DB_PATH) as conn:
            row = conn.execute(
                "SELECT value FROM model_provider_prefs WHERE key=?",
                (agent_settings.PREF_KEY,)).fetchone()
        raw = json.loads(row[0])
        check("脏模型名被归一掉", "no_such_model" not in json.dumps(raw))
        check("脏步数被归一成默认",
              isinstance(raw["gen_defaults"]["t2i"]["steps"], int),
              raw["gen_defaults"]["t2i"]["steps"])

        # ---- 6. Agent 运行时真的用上（真服务端模块）----
        # 恢复一份干净的配置
        await srv.api_agent_settings(make_request(
            "PUT", "/api/agent/settings",
            {"gen_defaults": {"t2i": {"steps": 33, "cfg": 5.5},
                              "t2a": {"format": "mp3"}}}))
        created = []

        class _Real:
            """把 Agent 需要的服务端入口指到真 server 模块上。"""

            def db(self):
                return srv.db()

            def agent_settings_snapshot(self):
                return srv.agent_settings_snapshot()

            def prepare_job_params(self, mode, params, asset_refs=None,
                                   saved_files=None):
                return srv.prepare_job_params(mode, params, asset_refs,
                                             saved_files)

            def create_job_record(self, mode, params):
                created.append((mode, dict(params)))
                return srv.create_job_record(mode, params)

        ctx = agent_mod.AgentContext(server_module=_Real(),
                                      provider={"name": "t", "model": "m"})
        await agent_mod.TOOLS["create_job"]["handler"](
            ctx, {"mode": "t2i", "prompt": "一只猫在雨里跑"})
        check("Agent 建出了 t2i 任务", len(created) == 1, len(created))
        params = created[0][1]
        check("Agent 用上了管理页配的 steps=33", params.get("steps") == 33,
              params.get("steps"))
        check("Agent 用上了管理页配的 cfg=5.5", params.get("cfg") == 5.5,
              params.get("cfg"))

        # 用户显式传的值优先
        created.clear()
        await agent_mod.TOOLS["create_job"]["handler"](
            ctx, {"mode": "t2i", "prompt": "一只猫", "steps": 12})
        params = created[0][1]
        check("用户显式传的 steps=12 压过默认值",
              params.get("steps") == 12, params.get("steps"))

        # ---- 7. 老配置兼容：只 PUT 老结构 ----
        await srv.api_agent_settings(make_request(
            "PUT", "/api/agent/settings",
            {"t2v_defaults": {"attention": "sparse", "duration": 9.0}}))
        with sqlite3.connect(srv.DB_PATH) as conn:
            row = conn.execute(
                "SELECT value FROM model_provider_prefs WHERE key=?",
                (agent_settings.PREF_KEY,)).fetchone()
        raw = json.loads(row[0])
        check("老结构写进去后 t2v_defaults 读回一致",
              raw["t2v_defaults"]["attention"] == "sparse",
              raw["t2v_defaults"]["attention"])
        check("老结构写进去后新结构也同步",
              raw["gen_defaults"]["t2v"]["attention"] == "sparse",
              raw["gen_defaults"]["t2v"]["attention"])
    finally:
        srv.DB_PATH = orig_db
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for r in RESULTS if r["ok"])
    (OUT / "result.json").write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{passed}/{len(RESULTS)} 项通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
