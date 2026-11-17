# -*- coding: utf-8 -*-
"""
用例组 5：素材库（上传 / 标签 / 分析 / 被引用 / 入库 / 播放器）。

【覆盖用户要求 B.5 / B.6 / B.9】
- WebUI 上传（音频 / 图片 / 视频，多文件，**不二次转码**）；
- 标签（上传时打标签 + 之后加标签 + 按标签筛选）；
- 模型分析（深度分析）；
- 右键把生成媒体加入素材库；
- 素材被各生成面板引用；
- 媒体库**批次 / 非批次**两种情况；
- 播放器模式播放列表（来自素材库手动添加的项）。
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.request

import pytest


def _api(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.loads(r.read().decode())


def _upload_assets(sp, files, tags: str = "", note: str = ""):
    """走真实上传链路（WebUI 的多选文件框）。

    等的是**本次上传的那几个文件**出现在网格里 —— 不是「网格里有卡片」
    （隔离实例的素材库是 session 共用的，上一条用例的卡片一直都在，
    等「有卡片」会立刻返回、读到的还是上一次的旧卡片）。
    """
    sp.goto_page("navAssets")
    if tags:
        sp.fill("#alUploadTags", tags)
    if note:
        sp.fill("#alUploadNote", note)
    wanted = {f.name for f in files}
    sp.page.set_input_files("#alFileInput", [str(f) for f in files])

    names: list[str] = []
    for _ in range(120):
        names = sp.page.eval_on_selector_all(
            "#alGrid .al-card .al-name", "els => els.map(e => e.textContent.trim())")
        if wanted.issubset(set(names)):
            break
        sp.page.wait_for_timeout(500)
    return names


def _card_tags_for(sp, names: list[str]) -> list[str]:
    """只取**本次上传的那几张**卡片上的标签。

    隔离实例的素材库是整个 session 共用的 —— 网格里还有别的用例上传的
    卡片。读全部卡片的标签会读到别人的（断言假失败）。
    """
    return sp.page.evaluate(
        """(names) => names.map(n => {
             for (const card of document.querySelectorAll('#alGrid .al-card')) {
               const el = card.querySelector('.al-name');
               if (el && el.textContent.trim() === n) {
                 const tags = card.querySelector('.al-tags');
                 return tags ? tags.textContent.trim() : '';
               }
             }
             return '';
           })""",
        names)


def test_asset_upload_multiple_types_without_transcode(studio_page, evidence,
                                                      fixtures_dir):
    """多文件上传（图片/音频/视频），原文件原样保留（不二次转码）。"""
    sp = studio_page
    sp.open()
    files = [fixtures_dir["image"], fixtures_dir["audio"], fixtures_dir["video"]]
    names = _upload_assets(sp, files, tags="e2e,多类型", note="e2e 上传备注")
    evidence.step(f"上传后素材名 = {names}")
    wanted = {f.name for f in files}
    mine = [n for n in names if n in wanted]
    assert set(mine) == wanted, (
        f"上传后素材库里没有这几张：缺 {wanted - set(mine)}")
    for path in files:
        assert path.name in names, f"素材 {path.name} 没入库：{names}"

    # 「原文件原样保留」—— 落盘文件的 sha256 == 源文件 sha256
    stats = _api(sp.base_url, "/api/assets/stats")
    evidence.step(f"素材库统计 = {stats}")
    listing = _api(sp.base_url, "/api/assets?page_size=50")
    by_name = {a["name"]: a for a in listing["assets"]}
    for path in files:
        asset = by_name.get(path.name)
        assert asset, f"素材 {path.name} 不在列表里"
        # 下载回来比对哈希
        url = f"{sp.base_url}/api/assets/{asset['id']}/content"
        with urllib.request.urlopen(url, timeout=60) as r:
            got = hashlib.sha256(r.read()).hexdigest()
        want = hashlib.sha256(path.read_bytes()).hexdigest()
        evidence.step(f"{path.name}: 上传后 sha256 一致 = {got == want}")
        assert got == want, (
            f"{path.name} 上传后内容被改写了（转码/重编码）："
            f"源 {want[:12]} != 库 {got[:12]}")
    evidence.screenshot(sp.page, "asset-upload")


def test_asset_tags_saved_and_filterable(studio_page, evidence, fixtures_dir):
    """上传时打的标签会落库，且能按标签筛选。"""
    sp = studio_page
    sp.open()
    tag = "e2e标签甲"
    uploaded = _upload_assets(sp, [fixtures_dir["image"]], tags=tag,
                              note="带标签的素材")
    assert uploaded, "带标签的素材没上传成功"
    # _upload_assets 返回的是网格里的**全部**素材名（网格是共享的），
    # 这里要的是本次上传的那一个 —— 用文件名精确匹配。
    mine = [n for n in uploaded if n == fixtures_dir["image"].name]
    assert mine, f"本次上传的素材不在网格里：{uploaded}"
    evidence.step(f"本次上传的素材名 = {mine}")

    # 本次上传的那张卡片上能看到标签
    card_tags = _card_tags_for(sp, mine)
    evidence.step(f"本次上传卡片的标签 = {card_tags}")
    assert any(tag in t for t in card_tags), f"卡片上没看到标签 {tag}：{card_tags}"

    # 服务端也能查到（tags 是 [{tag, count}] 的列表）
    tags_api = _api(sp.base_url, "/api/assets/tags")
    evidence.step(f"服务端标签清单 = {tags_api}")
    names = [item["tag"] if isinstance(item, dict) else item
             for item in tags_api["tags"]]
    assert tag in names, f"服务端没有这个标签：{tags_api}"

    # 按标签筛选（搜索框同时匹配标签 / 名称 / 备注）
    sp.fill("#alSearch", tag)
    sp.page.wait_for_timeout(2000)
    found = sp.page.eval_on_selector_all(
        "#alGrid .al-card .al-name", "els => els.map(e => e.textContent.trim())")
    evidence.step(f"搜索 {tag} 后的素材 = {found}")
    assert found, f"按标签 {tag} 搜不到素材"
    # 搜出来的必须是**本次上传**的那张（素材名，不是标签）
    assert fixtures_dir["image"].name in found, (
        f"按标签搜出来的不是本次上传的素材：{found}（期望含 "
        f"{fixtures_dir['image'].name}）")
    evidence.screenshot(sp.page, "asset-tags")


def test_asset_analyze_records_model_result(studio_page, evidence, fixtures_dir):
    """模型分析：触发深度分析 -> 素材上出现「已分析」标记。"""
    sp = studio_page
    sp.open()
    _upload_assets(sp, [fixtures_dir["image"]])

    card = sp.page.query_selector("#alGrid .al-card")
    card.click(button="right")
    sp.page.wait_for_timeout(600)
    analyze = sp.page.query_selector('#alContext button[data-act="analyze"]')
    assert analyze is not None, "右键菜单里没有「深度分析」"
    analyze.click()
    sp.page.wait_for_timeout(2000)
    evidence.step("已点击深度分析")

    # 分析要走本地模型（慢），轮询等结果
    ok = False
    analyzed = False
    for _ in range(180):
        listing = _api(sp.base_url, "/api/assets?page_size=50")
        if not listing["assets"]:
            break
        asset = listing["assets"][0]
        analyzed = bool(asset.get("analyzed"))
        if analyzed:
            ok = True
            break
        time.sleep(2)
    evidence.step(f"分析后 analyzed = {analyzed}")
    assert ok, "深度分析没有产出结果"
    evidence.screenshot(sp.page, "asset-analyzed")


def test_asset_used_by_generation_panel(studio_page, evidence, fixtures_dir):
    """素材被生成面板引用（参考图槽位 -> 提交带 asset_refs）。"""
    sp = studio_page
    sp.open()
    _upload_assets(sp, [fixtures_dir["image"]])

    sp.goto_mode("r2v")
    slot = sp.page.query_selector('.up-slot[data-key="ref0"]')
    assert slot is not None, "没有参考图槽位"
    slot.click()
    sp.page.wait_for_timeout(1500)
    picker_card = sp.page.query_selector("#alPickerBody .al-card")
    assert picker_card is not None, "素材库选择器里没有素材"
    picker_card.click()
    sp.page.wait_for_timeout(400)
    sp.click("#alPickerOk")
    sp.page.wait_for_timeout(900)

    # 槽位被填上（class=filled）
    filled = sp.page.eval_on_selector(
        '.up-slot[data-key="ref0"]', "e => e.className")
    evidence.step(f"槽位 class = {filled}")
    assert "filled" in filled, f"槽位没被标记为已填：{filled}"

    sp.fill("#prompt", "e2e 素材被引用：参考图里的猫")
    sp.submit_generation()
    submitted = sp.last_submitted_params()
    evidence.step(f"提交字段 = {submitted.get('fieldNames')}")
    assert "asset_refs" in (submitted.get("fieldNames") or []), (
        "提交里没有 asset_refs —— 素材没被真正引用")

    job = sp.wait_for_job(timeout=240)
    evidence.step(f"任务 {job['id']} 状态 = {job['status']}")
    assert job["status"] == "done", f"任务没跑成功：{job.get('error')}"


def test_media_right_click_adds_to_asset_library(studio_page, evidence):
    """媒体库（生成结果）右键 -> 加入素材库。"""
    sp = studio_page
    sp.open()
    sp.goto_mode("t2i")
    sp.fill("#prompt", "e2e 右键入库：一只猫")
    sp.submit_generation()
    job = sp.wait_for_job(timeout=240)
    assert job["status"] == "done", f"任务没跑成功：{job.get('error')}"

    sp.goto_tab("done")
    card = None
    for _ in range(30):
        card = sp.page.query_selector(f'.job-card[data-jid="{job["id"]}"]')
        if card is not None:
            break
        sp.page.wait_for_timeout(1000)
    assert card is not None, "界面上找不到任务卡片"

    before = _api(sp.base_url, "/api/assets/stats")["total_count"]
    evidence.step(f"入库前素材数 = {before}")

    # 右键点卡片上的图片 -> 媒体库右键菜单
    image = card.query_selector(".job-preview img")
    assert image is not None, "卡片上没有图片"
    image.click(button="right")
    sp.page.wait_for_timeout(600)
    assert sp.visible("#mediaContext"), "右键没有打开媒体库菜单"
    items = sp.page.eval_on_selector_all(
        "#mediaContext button", "els => els.map(e => e.textContent.trim())")
    evidence.step(f"媒体库右键菜单 = {items}")
    assert any("素材库" in t for t in items), f"菜单里没有「添加到素材库」：{items}"

    sp.page.click('#mediaContext button[data-act="add-asset"]')
    sp.page.wait_for_timeout(2500)

    after = _api(sp.base_url, "/api/assets/stats")["total_count"]
    evidence.step(f"入库后素材数 = {after}")
    assert after > before, f"素材数没变（{before} -> {after}），右键入库没生效"
    evidence.screenshot(sp.page, "media-to-asset")


def test_player_playlist_from_manual_selection(studio_page, evidence,
                                               fixtures_dir):
    """播放器模式：播放列表来自**手动添加**的项，不是当前页全部素材。"""
    sp = studio_page
    sp.open()
    _upload_assets(sp, [fixtures_dir["image"], fixtures_dir["video"]])

    # 先看一眼播放器（此时应该是空的）
    sp.click("#alPlayerBtn")
    sp.page.wait_for_timeout(1200)
    assert sp.visible("#alPlayer"), "播放器没打开"
    tabs = sp.page.eval_on_selector_all(
        "#alPlayerTabs button", "els => els.map(e => e.textContent.trim())")
    evidence.step(f"初始播放列表标签 = {tabs}")
    assert all("0" in t for t in tabs), f"初始播放列表应为空：{tabs}"
    sp.click("#alPlayerClose")
    sp.page.wait_for_timeout(500)

    # 手动添加第一个素材到播放列表（右键）
    card = sp.page.query_selector("#alGrid .al-card")
    card.click(button="right")
    sp.page.wait_for_timeout(600)
    sp.page.click('#alContext button[data-act="add-player"]')
    sp.page.wait_for_timeout(1500)

    sp.click("#alPlayerBtn")
    sp.page.wait_for_timeout(1500)
    tabs = sp.page.eval_on_selector_all(
        "#alPlayerTabs button", "els => els.map(e => e.textContent.trim())")
    evidence.step(f"添加后的播放列表标签 = {tabs}")
    total = sum(int(t.split()[-1]) for t in tabs)
    assert total == 1, f"播放列表应有 1 项，实际 {tabs}"
    body = sp.text("#alPlayerBody")
    evidence.step(f"播放器内容 = {body[:200]}")
    assert "清单里还没有" not in body, "播放器说清单为空"
    evidence.screenshot(sp.page, "player-playlist")


def test_asset_batch_and_non_batch_media_cards(studio_page, evidence):
    """媒体库的**批次 / 非批次**两种卡片都能右键入库。"""
    sp = studio_page
    sp.open()

    # 非批次：一次出 1 张
    sp.goto_mode("t2i")
    sp.fill("#prompt", "e2e 非批次：一只猫")
    sp.submit_generation()
    single = sp.wait_for_job(timeout=240)
    assert single["status"] == "done", f"非批次任务没跑成功：{single.get('error')}"
    assert not (single.get("batches") or []), "非批次任务不该有批次"

    # 批次：一次出 2 张
    sp.goto_mode("t2i")
    chips = sp.page.query_selector_all("#imgBatchChips button")
    chips[1].click()
    sp.page.wait_for_timeout(300)
    sp.fill("#prompt", "e2e 批次：一只猫")
    sp.submit_generation()
    batched = sp.wait_for_job(timeout=240)
    assert batched["status"] == "done", f"批次任务没跑成功：{batched.get('error')}"
    assert len(batched.get("batches") or []) == 2, "批次任务应有 2 个批次"
    evidence.step(f"批次任务 {batched['id']} 有 {len(batched['batches'])} 个批次")

    # 批次任务的媒体库右键菜单要标出「批次 #N 的结果」
    sp.goto_tab("done")
    card = None
    for _ in range(30):
        card = sp.page.query_selector(f'.job-card[data-jid="{batched["id"]}"]')
        if card is not None:
            break
        sp.page.wait_for_timeout(1000)
    assert card is not None, "界面上找不到批次任务卡片"

    toggle = card.query_selector(".job-batches-toggle")
    assert toggle is not None, "批次卡片上没有「批次结果」折叠块"
    toggle.click()
    sp.page.wait_for_timeout(600)

    thumb = card.query_selector(".job-batch-thumb img")
    assert thumb is not None, "批次缩略图没渲染"
    thumb.click(button="right")
    sp.page.wait_for_timeout(600)
    items = sp.page.eval_on_selector_all(
        "#mediaContext button", "els => els.map(e => e.textContent.trim())")
    evidence.step(f"批次右键菜单 = {items}")
    assert any("批次" in t for t in items), f"批次右键菜单没有标注批次：{items}"
    evidence.screenshot(sp.page, "batch-right-click")
