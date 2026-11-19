// R2：扫描 web/*.js 里**字符串字面量与模板静态部分**中的
// 「用户可见的开发注释 / 调试文案 / 占位符 / 内部术语」。
//
// 【为什么必须用真解析器（acorn）】
// 手写词法扫描会大量误判（第 1 版手写扫描器 294 条命中里 99% 是误报）：
//   * 字符串里的 `//` 不是注释
//   * 模板字符串里的 `${}` 表达式不是「占位符泄漏」
//   * 块注释续行会被当成字符串
// acorn 是**真 AST**：注释根本不在 AST 里（不开 onComment），
// 拿到的 Literal / TemplateElement 就是**会被送进 DOM 的静态文本**。
//
// 【判定口径】
// 只报「静态文本里含开发者措辞」的字符串。命中不等于一定是缺陷
// （LoRA 产品名里带 XXX、官方提示词模板原文是 markdown），
// 但**每一条都必须人工判读**，所以这里把命中 + 源码位置一起给出。
//
// 用法：node scripts/r2_scan_visible_dev_text.cjs [目录=web]
// 退出码：0 = 无命中；1 = 有命中
const fs = require("fs");
const path = require("path");

let acorn;
try {
  acorn = require("acorn");
} catch (e) {
  // 仓库根 node_modules 里可能没有；试全局
  const { execSync } = require("child_process");
  try {
    const g = execSync("npm root -g", { encoding: "utf8" }).trim();
    acorn = require(path.join(g, "acorn"));
  } catch (e2) {
    console.error("需要 acorn: npm i acorn 或用全局 npm root -g 查找");
    process.exit(2);
  }
}

const ROOT = process.argv[2] || "web";

//: (类别, 正则源)
const MARKERS = [
  ["开发注释", /\b(?:TODO|FIXME|HACK|WIP)\b/],
  ["开发注释", /\bXXX\b/],
  ["开发注释", /待实现|待办|未实现|待补|待填|先这样|以后再|后面再/],
  ["调试文案", /调试/],
  ["调试文案", /\bdebug\b/i],
  ["调试文案", /\b(?:mock|stub|dummy|fake)\b/i],
  ["调试文案", /测试用|测试文案|假数据|模拟数据/],
  ["占位符", /lorem\s+ipsum/i],
  ["占位符", /\?{3,}/],
  ["内部术语", /内部专用|仅供内部|内部使用|内部标识/],
  ["内部术语", /开发用|开发阶段|实验性|临时方案/],
  ["内部术语", /接口待定|待联调|待对接|待完善|尚未实现/],
  ["机器标识", /\b(?:prompt_id|job_id|jobId|chain_id|chainId|asset_id)\b/],
  ["机器标识", /\b(?:ref_video_frame_count|chain_total_frames|latent_run_id|source_latent_path)\b/],
  ["英文口癖", /\b(?:for now|temp fix|workaround|quick fix|sanity check)\b/i],
  ["markdown强调", /\*\*[^*\n]{1,60}\*\*/],
  ["栈信息", /Traceback \(most recent call last\)|:\d+:\d+/],
];

function collectNodes(root) {
  const out = [];
  const seen = new Set();
  (function rec(node) {
    if (!node || typeof node !== "object") return;
    if (seen.has(node)) return;
    seen.add(node);
    if (typeof node.type === "string") out.push(node);
    for (const k of Object.keys(node)) {
      if (k === "type" || k === "start" || k === "end" || k === "loc" ||
          k === "value" || k === "raw") continue;
      const v = node[k];
      if (Array.isArray(v)) {
        for (const c of v) if (c && typeof c === "object") rec(c);
      } else if (v && typeof v === "object") {
        rec(v);
      }
    }
  })(root);
  return out;
}

const files = [];
for (const f of fs.readdirSync(ROOT)) {
  if (f.endsWith(".js")) files.push(path.join(ROOT, f));
}

const hits = [];
let scanned = 0;
for (const file of files) {
  const src = fs.readFileSync(file, "utf8");
  let ast;
  try {
    ast = acorn.parse(src, {
      ecmaVersion: 2022,
      sourceType: "script",
      allowReturnOutsideFunction: true,
      locations: true,
    });
  } catch (e) {
    // 可能是模块语法或太新语法；用 module 再试一次
    try {
      ast = acorn.parse(src, {
        ecmaVersion: "latest",
        sourceType: "module",
        allowReturnOutsideFunction: true,
        allowAwaitOutsideFunction: true,
        allowHashBang: true,
        locations: true,
      });
    } catch (e2) {
      console.error(`[skip] ${file}: ${e2.message}`);
      continue;
    }
  }
  scanned++;
  for (const node of collectNodes(ast)) {
    let text = null;
    let kind = null;
    if (node.type === "Literal" && typeof node.value === "string") {
      text = node.value;
      kind = "Literal";
    } else if (node.type === "TemplateElement") {
      const cooked = node.value && node.value.cooked;
      const raw = node.value && node.value.raw;
      text = cooked != null ? cooked : raw;
      kind = "TemplateElement";
    }
    if (text == null || !text.trim()) continue;
    if (text.length > 400) continue;
    for (const [cat, re] of MARKERS) {
      const m = re.exec(text);
      if (!m) continue;
      hits.push({
        file, line: node.loc.start.line, col: node.loc.start.column,
        category: cat, kind, marker: m[0],
        text: text.slice(0, 160),
      });
      break;
    }
  }
}

console.log(`═══ acorn AST 扫描 ${scanned} 个 JS 文件 ═══`);
console.log(`字符串字面量 + 模板静态部分里的开发者措辞命中: ${hits.length} 条\n`);
const byFile = {};
for (const h of hits) (byFile[h.file] ||= []).push(h);
for (const [file, list] of Object.entries(byFile)) {
  console.log(`── ${file} (${list.length}) ──`);
  for (const h of list) {
    console.log(`  L${h.line}:${h.col} [${h.category}] ${h.kind} ${JSON.stringify(h.marker)}`);
    console.log(`      ${JSON.stringify(h.text)}`);
  }
  console.log();
}
process.exit(hits.length ? 1 : 0);
