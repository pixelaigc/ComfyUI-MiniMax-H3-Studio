// R7：抽出 web/ 下**会被渲染**的静态文本（供 Python 断言用）。
//
// 【为什么用真解析器而不是正则】
//   * JS：注释在 AST 里根本不存在（不开 onComment）—— 拿到的
//     Literal / TemplateElement 就是**会被送进 DOM 的静态文本**。
//     手写行扫描会把「行尾 // 注释」当成可见文本（假阳性）。
//   * HTML：注释块 `<!-- -->` 与 <script>/<style> 里的内容不渲染。
//
// 输出：JSON {"js": {file: [文本...]}, "html": {file: [文本...]}}
const fs = require("fs");
const path = require("path");

let acorn;
try {
  acorn = require("acorn");
} catch (e) {
  const { execSync } = require("child_process");
  const g = execSync("npm root -g", { encoding: "utf8" }).trim();
  acorn = require(path.join(g, "acorn"));
}

const ROOT = process.argv[2] || "web";
const OUT = process.argv[3] || "";

function walk(node, cb) {
  if (!node || typeof node !== "object") return;
  if (Array.isArray(node)) {
    node.forEach((n) => walk(n, cb));
    return;
  }
  if (typeof node.type === "string") cb(node);
  for (const key of Object.keys(node)) {
    if (key === "type" || key === "start" || key === "end") continue;
    const value = node[key];
    if (value && typeof value === "object") walk(value, cb);
  }
}

function extractJs(file) {
  const src = fs.readFileSync(file, "utf8");
  const out = [];
  let ast;
  try {
    ast = acorn.parse(src, { ecmaVersion: 2022, sourceType: "script",
      locations: false });
  } catch (e) {
    // 解析失败（可能是 module 或语法太新）-> 退回宽松扫描
    const re = /"((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)'/g;
    let m;
    while ((m = re.exec(src))) out.push(m[1] || m[2] || "");
    return out;
  }
  walk(ast, (node) => {
    if (node.type === "Literal" && typeof node.value === "string") {
      out.push(node.value);
    } else if (node.type === "TemplateElement") {
      const cooked = node.value && node.value.cooked;
      if (typeof cooked === "string") out.push(cooked);
    }
  });
  return out;
}

function extractHtml(file) {
  const src = fs.readFileSync(file, "utf8");
  const out = [];
  // 1) 去掉注释块
  let text = src.replace(/<!--[\s\S]*?-->/g, "");
  // 2) 去掉 <script> / <style> 整块（不渲染）
  text = text.replace(/<script\b[\s\S]*?<\/script>/gi, "");
  text = text.replace(/<style\b[\s\S]*?<\/style>/gi, "");
  // 3) 标签之间的文本节点
  const textRe = />([^<>]+)</g;
  let m;
  while ((m = textRe.exec(text))) {
    const value = m[1].trim();
    if (value) out.push(value);
  }
  // 4) 可见属性值（title / placeholder / aria-label / alt / value）
  const attrRe = /\b(title|placeholder|aria-label|alt|value)\s*=\s*"([^"]*)"/gi;
  while ((m = attrRe.exec(text))) {
    const value = m[2].trim();
    if (value) out.push(value);
  }
  return out;
}

const result = { js: {}, html: {} };
for (const name of fs.readdirSync(ROOT).sort()) {
  const file = path.join(ROOT, name);
  if (!fs.statSync(file).isFile()) continue;
  if (name.endsWith(".js")) result.js[name] = extractJs(file);
  else if (name.endsWith(".html")) result.html[name] = extractHtml(file);
}

const json = JSON.stringify(result, null, 2);
if (OUT) {
  fs.writeFileSync(OUT, json, "utf8");
  console.log(`已写出 ${OUT}`);
} else {
  process.stdout.write(json);
}
