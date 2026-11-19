#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 tests/test_gen_status_defects.py 加两条回归用例（失败预览区不再是黑的）。

【为什么需要这个脚本】
用例里嵌了一层 JS 探针（要真的执行 web/app.js 的渲染分支），
用 shell heredoc 传会把引号层级搅乱。写成脚本文件最稳。
"""
from pathlib import Path

TARGET = Path("/root/projects/h3s-merge-20261119/tests/test_gen_status_defects.py")

ANCHOR = '    def test_no_hardcoded_30min_text_in_frontend(self):'

NEW_TESTS = '''    @unittest.skipUnless(NODE, "需要 node 才能执行真实 JS")
    def test_failed_job_preview_is_marked_failed(self):
        """status=failed -> 预览区必须加 is-failed 标记。

        【为什么要这条】`.job-preview` 的默认背景是 #0d0f12（近黑），
        尺寸是 16:9 的 padding-bottom 盒子。失败任务即使**渲染了错误文案**，
        用户看到的仍是一大片近黑背景 + 中间几行红字 —— 视觉上就是
        「一张全黑图」。实测截图（evidence/gen-status-20261119/
        oom-failed-card-e2e.png）里 vision 判定预览区「为全黑」。
        所以必须验证失败时预览区被标记为 is-failed。
        """
        script = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf-8');

const start = src.indexOf('let preview = "";');
if (start < 0) throw new Error('找不到预览渲染起点');
const end = src.indexOf('const topRight =', start);
if (end < 0) throw new Error('找不到预览渲染终点');
let block = src.slice(start, end);

// 包成函数（块里有 let 声明，不能直接 eval 到同一作用域）。
const fn = new Function(
  'job', 'runningOrQueued', 'pctText', 'prog', 'outputUrl',
  'friendlyError', 'info',
  block + '\\nreturn { preview: preview, state: previewState };');

function probe(status, outputs) {
  const job = { outputs: outputs, status: status, error: '显存不足',
                progress: 1, progress_text: '', started_at: 0,
                created_at: 0, params: {} };
  return fn(job, false, 'x', 1, () => '',
            (t) => t || '生成失败',
            { name: 'n', kind: 'video' });
}

console.log(JSON.stringify({
  failed: probe('failed', []),
  canceled: probe('canceled', []),
  done_no_output: probe('done', []),
  running: probe('running', []),
  done_with_image: probe('done', [{ kind: 'image', file: 'a.png' }]),
}));
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.js"
            path.write_text(script, encoding="utf-8")
            proc = subprocess.run(
                [NODE, str(path), str(REPO / "web" / "app.js")],
                capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout.strip().splitlines()[-1])

        # 失败 / 取消 / done 但无产物 -> 必须标记 is-failed
        for key in ("failed", "canceled", "done_no_output"):
            self.assertIn("is-failed", data[key]["state"],
                          f"{key} 的预览区必须加 is-failed 标记")
        # 运行中 / 有产物 -> 不得加（不能把正常状态归类成失败）
        for key in ("running", "done_with_image"):
            self.assertNotIn("is-failed", data[key]["state"],
                             f"{key} 不应被标记为 is-failed")

    def test_failed_preview_style_is_not_black(self):
        """CSS 里必须有明确的失败底色（不是黑的）。

        【为什么这条也要测】即使前端加了 is-failed 标记，如果 CSS 里
        没有对应规则，预览区依然是 #0d0f12 近黑底 —— 用户看到的仍是
        「一张全黑图」。这条把「失败占位不是黑的」锁住。
        """
        css = (REPO / "web" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".job-preview.is-failed", css,
                      "CSS 里必须有 .job-preview.is-failed 规则")
        m = re.search(r"\\.job-preview\\.is-failed\\s*\\{([^}]*)\\}", css)
        self.assertIsNotNone(m, "找不到 .job-preview.is-failed 规则块")
        block = m.group(1)
        self.assertNotIn("#0d0f12", block,
                         "失败占位不得继续用近黑底色")
        self.assertIn("background", block,
                      "失败占位必须显式指定背景")


'''


def main() -> int:
    text = TARGET.read_text(encoding="utf-8")
    if "test_failed_job_preview_is_marked_failed" in text:
        print("已存在，跳过")
        return 0
    if ANCHOR not in text:
        print("!! 锚点没找到")
        return 1
    text = text.replace(ANCHOR, NEW_TESTS + ANCHOR, 1)
    TARGET.write_text(text, encoding="utf-8")
    print("已插入两条回归用例")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
