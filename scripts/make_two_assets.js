// 造两条**内容唯一**的测试素材（供 Playwright 注入页面执行）。
//
// 【为什么必须唯一】素材库按 sha256 去重：内容相同的两次上传会
// **复用同一条**（返回同一个 asset_id）。这带来两个真实事故：
//   1. 「勾选两条」实际只勾到 1 条 —— 断言永远不成立；
//   2. **更危险**：如果测试用的内容碰巧和库里某条已有素材相同，
//      第二次上传返回的是**那条已有素材的 id** —— 于是「删除我建的测试素材」
//      实际删掉了**用户的既有素材**。这真的发生过一次
//      （1x1 红 PNG 与库里的 clipboard_pasted.png 撞了）。
// ⇒ 内容必须每次运行都唯一。
//
// 【怎么做到唯一】在浏览器里用 canvas 画一张**随机颜色**的 PNG。
// 颜色取自 crypto.getRandomValues，sha256 几乎不可能撞上已有素材。
// （手写 base64 常量不行：内容固定，跑第二次就会撞上第一次。）
//
// 【安全闸】服务端对「撞到已有素材」的请求会返回 `duplicated: true`。
// 这里把 duplicated 的条目标成 unsafe —— 测试脚本**只删 safe 的**。
// 这样即使真的撞了，也只会「少删一条」并如实报错，绝不会误删用户数据。
(async () => {
  const out = [];
  const rand = () => crypto.getRandomValues(new Uint8Array(1))[0];

  const makePngBlob = () => new Promise((resolve) => {
    const c = document.createElement("canvas");
    c.width = 16; c.height = 16;
    const g = c.getContext("2d");
    g.fillStyle = `rgb(${rand()},${rand()},${rand()})`;
    g.fillRect(0, 0, 16, 16);
    for (let i = 0; i < 64; i++) {
      g.fillStyle = `rgba(${rand()},${rand()},${rand()},0.5)`;
      g.fillRect(rand() % 16, rand() % 16, 1, 1);
    }
    c.toBlob((b) => resolve(b), "image/png");
  });

  for (let i = 0; i < 2; i++) {
    const blob = await makePngBlob();
    const fd = new FormData();
    fd.append("file", new File([blob], `zz-viewer-test-${i}-${Date.now()}.png`,
                                {type: "image/png"}));
    const r = await fetch("/api/assets", {method: "POST", body: fd});
    const d = await r.json();
    const res = (d.results || [])[0] || {};
    out.push({
      asset_id: res.asset_id || "",
      duplicated: !!res.duplicated,
      // 只有「新建成功」的才标 safe —— duplicated 的那条是**别人的**素材，
      // 绝不能删。
      safe: !!res.asset_id && !res.duplicated,
    });
  }
  return out;
})()
