// 第 6 轮审查（2026-11-18）：扫描 web/*.js 里**字符串字面量**中的
// markdown 强调残留（`**xxx**`）。
//
// 【为什么必须用真解析器】
// 本项目内部大量用 `**xxx**` 写注释。手写词法扫描器会把
// 「块注释续行」「正则字面量」「模板字符串里的 ${} 表达式」
// 误判成污染 —— 那样断言就变成假红（审查者会养成忽略它的习惯）。
// acorn 是 Node 自带的（npm ls -g 里就有），用它做**真 AST** 解析：
//   * Literal（字符串）-> 检查
//   * TemplateElement（模板字符串的静态部分）-> 检查
//   * 注释 -> 一律豁免（acorn 的 comments 要显式开 onComment 才拿得到，
//     我们**不开**，所以注释天然不在 AST 里）
//
// 【口径】只报**字符串字面量**里的 `**xxx**`。
// 正则字面量（如 /\*\*[^*]{1,60}\*\*/）不算 —— 它是代码不是文案。
//
// 用法：node scripts/cr6_scan_markdown_leaks.cjs [目录...]
// 退出码：0 = 无命中；1 = 有命中
const fs = require("fs");
const path = require("path");
const acorn = require("acorn");

const MD = /\*\*[^*\n]{1,60}\*\*/g;

function walk(node, hits, file) {
  if (!node || typeof node !== "object") return;
  if (Array.isArray(node)) {
    for (const child of node) walk(child, hits, file);
    return;
  }
  if (node.type === "Literal" && typeof node.value === "string") {
    const m = node.value.match(MD);
    if (m) hits.push({ file, line: node.loc.start.line, frags: m });
  }
  if (node.type === "TemplateElement") {
    const raw = node.value && node.value.raw ? node.value.raw : "";
    const cooked = node.value && node.value.cooked ? node.value.cooked : "";
    const m = (cooked || raw).match(MD);
    if (m) hits.push({ file, line: node.loc.start.line, frags: m });
  }
  for (const key of Object.keys(node)) {
    if (key === "loc" || key === "range" || key === "start" || key === "end") continue;
    walk(node[key], hits, file);
  }
}

function main() {
  const roots = process.argv.slice(2);
  const targets = roots.length
    ? roots
    : [path.resolve(__dirname, "..", "web")];

  const files = [];
  for (const root of targets) {
    const st = fs.statSync(root);
    if (st.isFile()) {
      if (root.endsWith(".js")) files.push(root);
      continue;
    }
    const walkDir = (dir) => {
      for (const name of fs.readdirSync(dir)) {
        const full = path.join(dir, name);
        const s = fs.statSync(full);
        if (s.isDirectory()) walkDir(full);
        else if (name.endsWith(".js")) files.push(full);
      }
    };
    walkDir(root);
  }
  files.sort();

  const all = [];
  for (const file of files) {
    const src = fs.readFileSync(file, "utf8");
    let ast;
    try {
      ast = acorn.parse(src, { ecmaVersion: 2022, sourceType: "script",
                               locations: true });
    } catch (err) {
      // 解析失败（例如浏览器全局语法）-> 退回宽松模式再试一次
      try {
        ast = acorn.parse(src, { ecmaVersion: 2022, sourceType: "script",
                                 locations: true, allowReturnOutsideFunction: true });
      } catch (err2) {
        console.error(`SKIP ${file} (解析失败: ${err2.message})`);
        continue;
      }
    }
    const hits = [];
    walk(ast, hits, file);
    for (const h of hits) all.push(h);
  }

  const byFile = new Map();
  for (const h of all) {
    if (!byFile.has(h.file)) byFile.set(h.file, []);
    byFile.get(h.file).push(h);
  }
  for (const [file, hits] of byFile) {
    console.log(`\n=== ${path.relative(process.cwd(), file)} ===`);
    const lines = fs.readFileSync(file, "utf8").split("\n");
    for (const h of hits) {
      console.log(`  ${path.basename(file)}:${h.line}  ${h.frags.join(", ")}`);
      console.log(`      ${(lines[h.line - 1] || "").trim().slice(0, 110)}`);
    }
  }
  console.log(`\n合计命中：${all.length}`);
  process.exit(all.length ? 1 : 0);
}

main();
