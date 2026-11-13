// 验证 web/qwen-refs.js 的占位语法 profile 隔离（由 tests/test_qwen_refs_profile.py 调起）
//
// 用法：node tests/verify_qwen_refs_profile.js <repo_root>
// 输出：最后一行是 JSON
const fs = require("fs");
const path = require("path");
const REPO = process.argv[2] || process.cwd();

// 极简 DOM stub：qwen-refs.js 只在 render() 里用 document，
// validate/placeholderFor/parsePlaceholders 不碰 DOM。
global.document = { querySelector: () => null, querySelectorAll: () => [] };
global.window = global;
global.fetch = () => Promise.reject(new Error("no network in test"));
global.alert = (m) => { throw new Error("alert called: " + m); };

const src = fs.readFileSync(path.join(REPO, "web/qwen-refs.js"), "utf8");
eval(src);
const Q = global.QwenRefs;

function mkRefs(n) {
  const out = [];
  for (let i = 0; i < n; i++) {
    out.push({ asset_id: "a" + i, name: "img" + i, description: "d" + i,
               description_source: "user", tags: [], missing: false });
  }
  return out;
}
function run(profile, n, prompt) {
  Q.setProfile(profile);
  Q.setRefs(mkRefs(n));
  return Q.validate(prompt);
}

const out = { cases: [], isolation: {} };
const cases = [
  ["h3image", 2, "把图1里的猫换成雪豹",           false, "h3_natural_ok"],
  ["h3image", 2, "参考 <Picture 1> 和 <Picture 2>", false, "h3_full_ok"],
  ["h3image", 2, "参考 <Picture 3> 的雪山",         true,  "h3_dangling_rejected"],
  ["h3image", 1, "加一点暖光",                     false, "h3_single_ok"],
  ["h3image", 3, "参考 <Picture 1> 的构图",         false, "h3_partial_ok"],
  ["qwen",    2, "把图1里的猫换成雪豹",           true,  "qwen_natural_rejected"],
  ["qwen",    2, "参考 <image1> 和 <image2>",     false, "qwen_full_ok"],
];
for (const [profile, n, prompt, shouldFail, name] of cases) {
  const problems = run(profile, n, prompt);
  out.cases.push({ name, profile, n, failed: problems !== null,
                   problems: problems || [] });
}

Q.setProfile("h3image");
out.isolation.h3_placeholder = Q.placeholderFor(0);
out.isolation.h3_parses_picture = Q.parsePlaceholders("参考 <Picture 2>");
out.isolation.h3_parses_image = Q.parsePlaceholders("参考 <image2>");
Q.setProfile("qwen");
out.isolation.qwen_placeholder = Q.placeholderFor(0);
out.isolation.qwen_parses_image = Q.parsePlaceholders("参考 <image2>");
out.isolation.qwen_parses_picture = Q.parsePlaceholders("参考 <Picture 2>");

console.log(JSON.stringify(out));
