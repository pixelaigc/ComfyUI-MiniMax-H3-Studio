// 校验前端兜底函数 localResolutionInfo 与后端 reso_label 规则一致。
// 用法：node scripts/check_reso_frontend.js
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const src = fs.readFileSync(path.join(root, "web", "app.js"), "utf8");

const start = src.indexOf("const RESO_KNOWN_ASPECTS");
const end = src.indexOf("// 把折叠状态落到 DOM");
if (start < 0 || end < 0) {
  console.error("FAIL: 找不到 localResolutionInfo 相关代码块");
  process.exit(1);
}
eval(src.slice(start, end));

// 与 tests/test_reso_label.py 的期望保持一致
const cases = [
  [{ width: 1344, height: 768, resolution: "544p", aspect: "16:9" },
   "768p · 1344×768 · 16:9"],
  [{ width: 768, height: 1344, aspect: "9:16" }, "768p · 768×1344 · 9:16"],
  [{ width: 2560, height: 1400, aspect: "16:9" }, null],
  [{ width: 1536, height: 2736, aspect: "9:16" }, null],
  [{ width: 1568, height: 672, aspect: "21:9" }, null],
  [{ width: 1024, height: 1024, aspect: "1:1" }, null],
  [{ width: 960, height: 544, aspect: "16:9" }, null],
  [{ width: 768, height: 3456, aspect: "" }, null],
  [{ width: 640, height: 480, aspect: "4:3" }, null],
  [{ width: 864, height: 480, aspect: "16:9" }, null],
];

let bad = 0;
for (const [p, expected] of cases) {
  const r = localResolutionInfo(p);
  const ok = expected ? r.label === expected : true;
  if (!ok) bad++;
  console.log((ok ? "OK  " : "BAD ") + JSON.stringify(p) + " -> " + r.label);
}

// 遗留档位字符串绝不能出现在有真实尺寸的标签里
const stale = localResolutionInfo(
  { width: 1344, height: 768, resolution: "544p", aspect: "16:9" });
if (stale.label.includes("544p")) {
  console.error("FAIL: 遗留档位 544p 泄漏进了标签: " + stale.label);
  bad++;
}

console.log("mismatch:", bad);
process.exit(bad ? 1 : 0);
