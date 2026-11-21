# -*- coding: utf-8 -*-
"""「创意内容设计」选项目录 —— 数据源与组装规则。

【这块是什么】
工作台「文生图」提示词输入框上方新增的一个**提示词构造器**：
用户从 9 个维度、48 个子类、871 个可下拉选项里挑条目，
面板按固定骨架顺序把它们拼成一段逗号分隔的**原始提示词**，
再交给工作台既有的「增强 / 二次增强」链路去适配模型。

【为什么目录数据放在服务端】
既有纪律：所有「清单类」数据都走服务端下发（/api/image-options、
/api/image-loras、/api/enhancer、/api/docs/flows）。
前端不硬编码清单 ⇒ 版本可控、可单测、可增量更新。

【数据来源】
调研产物 /root/prompt-catalog-research-20261121/ 的 _data_d1..d4.py
（9 维度 871 项：中文显示名 + 英文片段 + 写法示例 + 依据等级）
原样搬进 h3studio/prompt_catalog_data/，本模块只做
  1) 规范化（补 id、补「不指定」项、补 slot 归属）
  2) 冲突矩阵落成**可执行**的规则（a/b 指向真实选项 id）
  3) 自检（标签体系词必须命中 Danbooru 官方 post_count > 0）

【依据等级三级标注】（调研 B 报告 §1.1 的口径，不得删）
  通 = 通用术语（维基 / 教材 / 官方文档可查）
  模 = 模型特定偏好（术语成立但 token 强度因模型而异，需我方实测）
  标 = 标签体系词（Danbooru 官方 tag，已实测 post_count 频次）
「模」在 UI 上弱标注（needs_test=true），不删除。

【纪律】
  * 「不指定」= 真的**不输出**任何字段。
    写 `no hair` / `not wearing shoes` 这类否定式，模型极可能画出字面意思
    （BFL Technical Parameters 关于负向表述的官方说明）。
  * 拼装结果**不得**含 (x:1.5) 之类权重语法 —— MiniMax H3 是否支持权重
    语法尚未取证，模型适配交给既有增强链路。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DATA_DIR = _HERE / "prompt_catalog_data"
if str(_DATA_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_DIR))

from _data_d1 import D1, D2          # noqa: E402
from _data_d2 import D3, D4          # noqa: E402
from _data_d3 import D5, D6          # noqa: E402
from _data_d4 import D7, D8, D9      # noqa: E402

DIMS = [D1, D2, D3, D4, D5, D6, D7, D8, D9]

#: 依据等级 -> (中文标签, 是否需实测)
LEVELS = {
    "通": ("通用术语", False),
    "模": ("模型特定偏好（需实测）", True),
    "标": ("标签体系词（Danbooru）", False),
}

#: 子类 -> 归属的骨架槽位（拼装顺序）。
#:
#: 骨架（调研 A 报告结论 7 + 任务书给的 5–6 段式）：
#:   主体 → 造型 → 风格 → 场景 → 构图 → 光 → 色 → 氛围 → 质量 → 负向
#:
#: 「构图」槽同时装 D5 构图方式与 D2 镜头摄影术语 —— A 报告结论 7 写的是
#: 「构图/镜头」，两者描述的是同一层（画面怎么摆、用哪只眼睛看）。
SLOT_ORDER = ["subject", "styling", "style", "scene", "composition",
              "light", "color", "mood", "quality", "negative"]

SLOT_LABELS = {
    "subject": "主体",
    "styling": "人物造型",
    "style": "风格媒介",
    "scene": "场景与空间",
    "composition": "构图与镜头",
    "light": "光线",
    "color": "色彩",
    "mood": "氛围与环境",
    "quality": "画质",
    "negative": "负向",
}

#: 维度 id -> 槽位
DIM_SLOT = {
    "D1": "style",
    "D2": "composition",
    "D3": "subject",     # D3 内部再按子类细分（见 SUB_SLOT）
    "D4": "scene",
    "D5": "composition",
    "D6": "color",
    "D7": "light",
    "D8": "mood",
    "D9": None,          # D9 是规则维度，不进提示词
}

#: D3 子类 -> 槽位（人物从头到脚：发型/妆容/上装/下装/外套/鞋/配饰/材质 = 造型）
#:
#: 注意 3.11「表情与神态」也归造型 —— 它属于人物的一部分，
#: 且必须排在服装鞋履**之后**（调研 D3 的子类顺序即从头到脚的顺序）。
SUB_SLOT = {
    "3.0": "subject", "3.1": "subject", "3.2": "subject",
    "3.3": "styling", "3.4": "styling", "3.5": "styling", "3.6": "styling",
    "3.7": "styling", "3.8": "styling", "3.9": "styling", "3.10": "styling",
    "3.11": "styling",
    # 8.4 是「保留 / 淘汰对照」的画质清单 —— 它是**指导**不是提示词片段，
    # 单独放 quality 槽（不进拼装）。
    "8.4": "quality",
}

#: 子类在面板里的呈现方式。
#:
#: options = 一个下拉（默认）
#: guide   = **指导清单**：它不是「写进提示词的片段」，而是「怎么写更好」的
#:           对照（D8.4 保留 / 谨慎 / 淘汰）。下拉里没有可选项，
#:           但**仍然提供自由输入**——用户想自己写画质词照样能写。
GROUP_RENDER = {
    "8.4": "guide",
}

#: 自由输入框的中文占位提示（按维度 / 子类给具体例子）。
#:
#: 【为什么要有】「自行输入」这个入口如果只写「自定义」，用户不知道
#: 该写什么、写多细。给一个本维度的中文例子，比空占位有用得多。
CUSTOM_PLACEHOLDER = {
    "D1": "例：水墨写意、港风胶片、复古漫画网点",
    "D2": "例：85mm 人像镜头、低机位仰角、浅景深",
    "D3": "例：戴渔夫帽的中年男性、穿旧军大衣、左手拄拐",
    "3.0": "例：一只戴护目镜的柯基、一台老式缝纫机",
    "D4": "例：雨后菜市场的湿滑地面、窗外是旧式居民楼",
    "4.2": "例：前景虚化的铁丝网、压在画面下缘的旧报纸",
    "4.3": "例：中景三五个撑伞的路人",
    "4.4": "例：远景是雾中的高压电塔",
    "D5": "例：主体偏左下、大面积留白、镜像对称",
    "D6": "例：青橙对比调色、整体偏灰的低饱和",
    "D7": "例：左侧窗户进来的柔光、逆光轮廓发亮",
    "D8": "例：潮湿闷热的气氛、墙面斑驳起皮",
    "8.4": "例：85mm f/1.4 拍摄、织物纹理清晰（画质词最多 2 项）",
}

#: 每个子类的选取上限（调研 B 报告 §1.2「建议选取上限」）。
#:
#: 说明：调研里 D4 三层 / D6.4 / D8 氛围材质 等写的是「可多选但各限 1–2 项」。
#: 本面板把每个子类做成**单个下拉**（移动端原生选择器最稳），
#: 因此上限统一收敛到 1；「D4 三层」的前/中/远景是**三个独立子类**，
#: 三层依然可以同时选 —— 这正是任务书要求的「前景/中景/远景独立可选」。
GROUP_MAX = 1

#: 整个维度的选取上限（跨子类）。来自 D9.3「单选组规则 / 风格族上限 / 画质词预算」。
DIM_MAX = {
    "D1": 2,    # 风格族上限：主风格 1 + 修饰 1
    "D5": 1,    # 构图三族互斥，整体单选
    "D6.4": 2,  # 色彩分级可叠加，但方向一致
}

#: D8.4 里「淘汰」类词的建议预算（D9.3「画质词预算」）
QUALITY_BUDGET = 2


# --------------------------------------------------------------------------
# 冲突矩阵：把 D9 的自由文本规则落成**可执行**的 a/b 选项 id
# --------------------------------------------------------------------------
#
# 记法：(维度 id, 子类前缀, 中文显示名)
# 解析不到就是硬错误（见 _resolve），不会静默跳过。

#: 硬互斥（D9.1，23 条）—— 选了 A 就禁用 B，反之亦然。
HARD_RULES = [
    ("H01", "写实摄影 ↔ 赛璐璐 / 像素 / 低多边形",
     "渲染逻辑互斥：真实光照与平涂描边不能共存",
     [("D1", "1.2", "真实摄影")],
     [("D1", "1.1", "赛璐璐动画"), ("D1", "1.1", "像素风"), ("D1", "1.5", "低多边形")]),
    ("H02", "微距 ↔ 大远景 / 全景",
     "焦段与景别互斥",
     [("D2", "2.1", "微距"), ("D2", "2.2", "微距特写")],
     [("D2", "2.2", "大远景"), ("D2", "2.2", "远景"), ("D2", "2.2", "全景")]),
    ("H03", "深景深 ↔ 强散景 / 焦外虚化",
     "同一光学量的相反设定",
     [("D2", "2.1", "景深极深"), ("D2", "2.4", "深景深")],
     [("D2", "2.4", "焦外虚化"), ("D2", "2.4", "圆形散景"),
      ("D2", "2.4", "焦外虚化边缘"), ("D2", "2.4", "焦外拉丝"),
      ("D2", "2.4", "双重边缘")]),
    ("H04", "浅景深 ↔ 全画面锐利",
     "同一光学量的相反设定",
     [("D2", "2.1", "景深极浅"), ("D2", "2.4", "浅景深")],
     [("D2", "2.4", "深景深"), ("D2", "2.1", "景深极深")]),
    ("H05", "黄金时刻 ↔ 蓝调时刻",
     "色温与照度条件相反",
     [("D7", "7.1", "黄金时刻")], [("D7", "7.1", "蓝调时刻")]),
    ("H06", "高调 ↔ 低调",
     "影调范围相反",
     [("D6", "6.2", "高调")],
     [("D6", "6.2", "低调"), ("D6", "6.2", "暗调 / 阴郁")]),
    ("H07", "高饱和 ↔ 低饱和 / 褪色",
     "饱和度策略相反",
     [("D6", "6.3", "高饱和"), ("D6", "6.3", "过度饱和")],
     [("D6", "6.3", "低饱和 / 灰调"), ("D6", "6.3", "褪色 / 旧照感")]),
    ("H08", "柔光包裹 ↔ 硬光直射",
     "光源性质相反",
     [("D7", "7.2", "柔光包裹")], [("D7", "7.2", "硬光直射")]),
    ("H09", "三分法 ↔ 中心对称",
     "主体定位逻辑相反",
     [("D5", "5.A", "三分法")], [("D5", "5.A", "中心对称")]),
    ("H10", "框中框 ↔ 填满画面",
     "边留白要求相反",
     [("D5", "5.A", "框中框")], [("D5", "5.B", "填满画面")]),
    ("H11", "留白负空间 ↔ 散点 / 网格 / 拼贴",
     "元素密度要求相反",
     [("D5", "5.A", "留白 / 负空间")],
     [("D5", "5.B", "散点构图"), ("D5", "5.A", "棋盘 / 网格构图"),
      ("D1", "1.4", "拼贴"), ("D5", "5.C", "拼贴")]),
    ("H12", "俯拍 ↔ 仰拍",
     "机位方向相反",
     [("D2", "2.3", "俯拍"), ("D2", "2.3", "鸟瞰 / 航拍"), ("D2", "2.3", "顶视")],
     [("D2", "2.3", "仰拍"), ("D2", "2.3", "地面机位")]),
    ("H13", "室内 ↔ 户外",
     "场景物理规则相反",
     [("D4", "4.1", "室内")],
     [("D4", "4.1", "户外"), ("D4", "4.1", "森林"), ("D4", "4.1", "城市街道")]),
    ("H14", "纯色背景 ↔ 杂乱背景 / 城市天际线",
     "背景设定相反",
     [("D4", "4.1", "纯色背景（棚拍）"), ("D4", "4.1", "纯白背景"),
      ("D4", "4.1", "渐变背景")],
     [("D4", "4.4", "杂乱背景"), ("D4", "4.4", "城市天际线")]),
    ("H15", "短发 ↔ 长发 / 双马尾",
     "同一头发的互斥形态",
     [("D3", "3.3", "短发")], [("D3", "3.3", "长直发"), ("D3", "3.3", "双马尾")]),
    ("H16", "素颜 ↔ 浓妆",
     "妆容状态相反",
     [("D3", "3.4", "素颜")], [("D3", "3.4", "浓妆")]),
    ("H17", "单色 ↔ 互补色",
     "配色方案互斥",
     [("D6", "6.1", "单色"), ("D5", "5.C", "单色场")],
     [("D6", "6.1", "互补色")]),
    ("H18", "长焦压缩 ↔ 广角扩张",
     "透视方向相反",
     [("D5", "5.B", "长焦压缩"), ("D2", "2.1", "长焦")],
     [("D5", "5.B", "广角畸变"), ("D2", "2.1", "广角"), ("D2", "2.1", "超广角")]),
    ("H19", "高速快门冻结 ↔ 运动模糊 / 长曝",
     "曝光逻辑相反",
     [("D2", "2.5", "高速快门冻结"), ("D2", "2.5", "高速快门运动")],
     [("D2", "2.5", "运动模糊"), ("D2", "2.5", "长曝光"),
      ("D2", "2.5", "动态模糊拖尾")]),
    ("H20", "极简主义构成 ↔ 拼贴 / 蒙太奇",
     "元素数量要求相反",
     [("D5", "5.C", "极简主义构成"), ("D5", "5.A", "极简构图")],
     [("D5", "5.C", "拼贴"), ("D5", "5.C", "蒙太奇并置"), ("D5", "5.C", "照片蒙太奇")]),
    ("H22", "色彩分级方向冲突（暖金 vs 冷蓝）",
     "同屏色偏方向相反",
     [("D6", "6.4", "暖金")],
     [("D6", "6.4", "冷蓝"), ("D6", "6.4", "冷青灰蓝")]),
    # —— 以下两条来自调研里各维度的 conflicts（D9.1 未单列，但同样是硬互斥）——
    ("H24", "雪 ↔ 雨",
     "同一画面里两种降水互斥",
     [("D8", "8.2", "雪"), ("D8", "8.2", "大雪 / 暴雪")],
     [("D8", "8.2", "雨"), ("D8", "8.2", "大雨"), ("D8", "8.2", "小雨")]),
]

#: 软冲突（D9.2，13 条）—— 可共存，但要黄字提示。
SOFT_RULES = [
    ("S01", "自然光时段 + 影棚布光",
     "同选时影棚光通常胜出", ["D7:7.1"], ["D7:7.2"]),
    # 【S02 已改为数量规则】调研原文的「多风格叠加（2 族以内）」是一条
    # **数量**约束（选超过 2 个风格族就稀释），不是成对互斥 ——
    # 写成 a/b 对会让它报「赛璐璐动画 与 赛璐璐动画」这种自相矛盾的提示。
    # 它由 CAP_RULES 的 H21（D1 上限 2）承担。
    ("S03", "色调冲突方向（warm vs cool）",
     "同屏出现时画面会冷热分区", ["D6:6.4:暖金"], ["D6:6.4:冷蓝", "D6:6.4:冷青灰蓝"]),
    ("S04", "彩色点缀 + 高饱和",
     "单点色会被整体高饱和淹没", ["D6:6.1:黑白 + 单点色"], ["D6:6.3:高饱和"]),
    ("S05", "体积光 + 影棚均匀布光",
     "均匀布光会削弱光柱可见度",
     ["D7:7.3:体积光 / 光柱"], ["D7:7.2:柔光箱", "D7:7.2:柔光影棚", "D7:7.2:双侧补光"]),
    ("S06", "剪影 + 高调",
     "剪影要求主体暗，高调要求主体亮",
     ["D5:5.B:剪影", "D7:7.4:剪影"], ["D6:6.2:高调", "D7:7.2:蝴蝶光"]),
    ("S07", "镜头光晕 + 干净极简",
     "光晕是「不干净」的信号",
     ["D7:7.3:镜头光晕", "D2:2.6:耀斑"], ["D5:5.C:极简主义构成", "D5:5.A:极简构图"]),
    ("S08", "故障艺术 + 干净矢量",
     "故障本身的噪点破坏干净感",
     ["D5:5.C:故障艺术"], ["D1:1.1:扁平插画", "D1:1.1:线稿 / 漫画分色"]),
    ("S09", "深景深 + 背景虚化",
     "同时给会得到边缘略糊但整体清晰的妥协结果",
     ["D2:2.4:深景深", "D2:2.1:景深极深"], ["D4:4.5:虚化背景", "D4:4.4:虚化背景"]),
    ("S10", "大面积留白 + 密集主体",
     "主体过小会丢失可读性",
     ["D5:5.A:留白 / 负空间"], ["D3:3.0:人物（多人）", "D4:4.3:一群人"]),
    ("S11", "同一物件多个互斥材质",
     "磨砂玻璃 vs 镜面玻璃、哑光 vs 高光",
     ["D8:8.3:磨砂玻璃"], ["D8:8.3:玻璃"]),
    ("S11b", "同一物件多个互斥材质（哑光 vs 高光）",
     "哑光 vs 高光互斥",
     ["D8:8.3:哑光表面"], ["D8:8.3:高光光泽"]),
    ("S12", "天气与光线不匹配",
     "晴空 + 浓雾、雨天 + 硬影",
     ["D8:8.2:晴天"], ["D8:8.2:雾", "D8:8.2:薄雾 / 霭"]),
    ("S12b", "天气与光线不匹配（暴雨 + 柔光）",
     "暴雨 + 柔光无影弱互斥",
     ["D8:8.2:大雨"], ["D7:7.1:阴天柔光"]),
    ("S13", "「干净」与「脏污」并存",
     "同一物件上互斥",
     ["D5:5.A:极简构图"], ["D8:8.3:灰尘 / 污渍"]),
    # —— 以下三条来自 D3 维度的 conflicts（D9.2 未单列，属弱冲突）——
    ("S14", "校服 ↔ 职业装 / 礼服",
     "风格冲突", ["D3:3.5:校服"], ["D3:3.5:职业装 / 西装", "D3:3.5:礼服 / 晚装"]),
    ("S15", "和服 / 汉服 ↔ 运动鞋",
     "文化与年代冲突（模型会混搭出 cosplay 感）",
     ["D3:3.5:和服", "D3:3.5:汉服"], ["D3:3.8:运动鞋"]),
    ("S16", "连衣裙 ↔ 上装 + 下装",
     "连衣裙本身覆盖上下装",
     ["D3:3.6:连衣裙"],
     ["D3:3.5:T恤", "D3:3.5:衬衫", "D3:3.5:毛衣", "D3:3.6:牛仔裤",
      "D3:3.6:长裤", "D3:3.6:百褶裙"]),
]

#: 维度级上限规则（D9.1 的「风格稀释」与「画质词堆叠上限」两条，
#: 它们是**数量**规则，不是成对互斥 ⇒ 单独落成 cap 规则）。
#:
#: limit == 1 的 cap（构图三族互斥）在 UI 上等价于硬互斥：
#: 选了另一族会自动替换掉本族的选择，并给出可见说明。
CAP_RULES = [
    ("H21", "风格稀释：跨族风格同时选中",
     "官方与实测均指出这是画面发糊的头号原因；D1 同时最多 1 个主风格族 + 1 个修饰风格",
     "D1", 2, "样式族", ["D1:1.1:赛璐璐动画", "D1:1.2:真实摄影"]),
    ("H23", "画质词堆叠上限",
     "超过 3-4 个后稀释注意力、引发属性串味；保留类总预算 ≤ 2 项",
     "D8.4", QUALITY_BUDGET, "画质", ["D8:8.4:保留 - 主体与场景描述"]),
    ("H25", "构图三族互斥",
     "经典 / 镜头式 / 抽象三族描述的是同一层级的不同组织逻辑，官方参考表也是按"
     "「构图技法」单列的，不是叠加项",
     "D5", 1, "构图族", ["D5:5.A:三分法", "D5:5.B:填满画面", "D5:5.C:拼贴"]),
]


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

def _dim_items(d):
    for sub in d["subs"]:
        for it in sub["items"]:
            yield sub, it


def _is_skip(en: str) -> bool:
    """「不指定」项：en 以全角括号开头（调研数据里写作「（留空）」）。"""
    return not en or en.startswith("（") or en.startswith("(")


def _norm(s: str) -> str:
    s = re.sub(r"\(.*?\)", "", s)
    s = s.lower().strip()
    s = re.sub(r"[\s,]+", "_", s)
    s = re.sub(r"[^a-z0-9_\-]", "", s)
    return s.strip("_")


def _cands(en: str):
    e = en.split("/")[0].strip()
    out = [_norm(e)]
    if " " in e:
        out.append(_norm(e.replace(" ", "_")))
    return [x for x in out if x]


def _load_postcounts() -> dict:
    path = _DATA_DIR / "danbooru_postcounts.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# --------------------------------------------------------------------------
# 冲突规则的可执行化
# --------------------------------------------------------------------------

class _Resolver:
    """把 (维度 id, 子类前缀, 中文显示名) 解析成真实选项 id。"""

    def __init__(self, index):
        # index[(dim_id, sub_prefix, cn)] = item_id
        self.by_cn = index
        self._group_id = {}

    def bind_group(self, dim_id, sub_prefix, group_id):
        self._group_id[(dim_id, sub_prefix)] = group_id

    def group_id(self, dim_id, sub_prefix):
        return self._group_id[(dim_id, sub_prefix)]

    def group_ids(self):
        return dict(self._group_id)

    def item(self, dim_id, sub_prefix, cn):
        key = (dim_id, sub_prefix, cn)
        if key not in self.by_cn:
            raise KeyError(
                "冲突规则引用了不存在的选项：%s/%s/%s" % key)
        return self.by_cn[key]

    def group(self, dim_id, sub_prefix):
        ids = [i for (d, s, _c), i in self.by_cn.items()
               if d == dim_id and s == sub_prefix]
        if not ids:
            raise KeyError("冲突规则引用了不存在的子类：%s/%s" % (dim_id, sub_prefix))
        return ids

    def all_in_dim(self, dim_id):
        return [i for (d, _s, _c), i in self.by_cn.items() if d == dim_id]

    def parse_soft_ref(self, ref):
        """软冲突的引用写法：`D7:7.1` / `D1:all` / `D6:6.4:暖金`。"""
        parts = ref.split(":")
        dim_id = parts[0]
        if len(parts) == 2:
            if parts[1] == "all":
                return self.all_in_dim(dim_id)
            return self.group(dim_id, parts[1])
        cn = ":".join(parts[2:])
        return [self.item(dim_id, parts[1], cn)]


# --------------------------------------------------------------------------
# 构建
# --------------------------------------------------------------------------

def _status_for(level: str, cn: str, sub_prefix: str) -> str:
    """选项状态：normal / caution / deprecated。

    deprecated = 调研 D8.4 里「淘汰」类词（社区噪声词 / 对写实场景有害）——
    仍然**保留在目录里**（调研 B 报告要求逐条对照），但 UI 里不可选，
    只作为「要避免什么」的负向清单。
    """
    if sub_prefix == "8.4":
        if cn.startswith("淘汰"):
            return "deprecated"
        if cn.startswith("谨慎"):
            return "caution"
    return "normal"


def _rule_matrix() -> list:
    """D9 冲突矩阵元数据（调研原文 48 条：9.1 / 9.2 / 9.3）。

    【为什么原文保留】任务书要求「附 D9 冲突规则元数据」。
    这里原样保留调研写的规则文本（label / en / reason / 依据等级），
    前端可以展示「规则清单」，也让后续追溯有据可查。
    """
    out = []
    for sub in D9["subs"]:
        rules = []
        for cn, en, reason, level in sub["items"]:
            rules.append({
                "label": cn,
                "en": en,
                "reason": reason,
                "level": level,
                "level_label": LEVELS.get(level, (level, False))[0],
            })
        out.append({
            "name": sub["name"],
            "sub_prefix": sub["name"].split(" ")[0],
            "purpose": sub.get("purpose", ""),
            "rules": rules,
        })
    return out


#: 组在槽位内的细分顺序（同一槽位里按这个排）。
#:
#: 骨架把「构图/镜头」写成同一层（调研 A 报告结论 7），
#: 但组内的先后要定死，否则拼装结果会随维度遍历顺序漂移：
#:   构图（D5）在前 → 镜头（D2）在后（与骨架文字「构图/镜头」一致）
GROUP_ORDER = {
    "D5": 0,   # 构图方式
    "D2": 1,   # 镜头与摄影术语
    "D3": 0,   # 人物链：按子类顺序（3.0 → 3.11）
    "D4": 0,
    "D7": 0,
}


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in (s or ""))


def _ascii_ratio(s: str) -> float:
    if not s:
        return 0.0
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if ord(c) < 128) / len(letters)


def _zh_fragment_for(cn: str, sub_prefix: str, status: str) -> str:
    """算出这条选项**真正写进提示词**的中文片段（默认输出走这一份）。

    【为什么默认是中文】用户 2026-11-21 的明确要求：
      「组装成原始提示词时也中文优先：默认输出中文为主的提示词文本。
       英文片段只作为可选的辅助/增强信息保留。」
    中文片段的来源 = 调研 B 报告里的**中文显示名**（那份中文说法是调研
    逐条核过的），不引入任何未核验的英文术语。

    【规则】
      * 「A / B」并列 → 取第一个（与英文片段同一口径；中文里的「/」
        多数是同一事物的两个叫法，取第一个不丢语义）。
      * 括号注释（如「人物（单人）」）**原样保留** —— 它是对主体的限定，
        不是噪声；改写它等于改写语义。
      * 显示名里本来就带的拉丁专名（Unreal Engine / Art Deco / Y2K /
        35mm / Teal & Orange）保留 —— 那是该概念的通用写法，不是
        「未经核验的英文术语」。
      * 8.4（保留 / 谨慎 / 淘汰对照）→ 空串：它是**指导清单**不是片段
        （见 GROUP_RENDER）。
      * 淘汰项 → 空串。
    """
    if sub_prefix == "8.4":
        return ""
    if status == "deprecated":
        return ""
    frag = (cn or "").strip()
    if " / " in frag:
        # 【并列写法取哪一个】取**第一个带中文的**写法。
        # 与英文片段同一口径（取第一个）不同：中文这边，「IMAX / 宽银幕」
        # 取第一个会得到纯拉丁「IMAX」，把可用的中文说法丢了。
        parts = [p.strip() for p in frag.split(" / ") if p.strip()]
        zh = [p for p in parts if _has_cjk(p)]
        frag = zh[0] if zh else (parts[0] if parts else "")
    return frag


def _fragment_for(cn: str, en: str, example: str, sub_prefix: str, status: str) -> str:
    """算出这条选项的**英文片段**（辅助版本，供「含英文片段版本」/复制用）。

    【为什么要单独算】
    * 跳过项（「不指定」）→ 空串（真的不输出，绝不写 "no X"）。
    * 淘汰项（D8.4 的「淘汰 - xxx」）→ 空串（它是「别写什么」的清单）。
    * D8.4 的「保留 / 谨慎」条目 → 它的 **en** 是类别名（"camera and lens
      language"），不是提示词片段；真正的可用写法在 **example** 里
      （"85mm, f/1.4, 35mm film, shot on Kodak Portra 400"）。
      所以这一组取 example（且只有 example 是英文时才取）。
    * 其余条目 → 取 en（"a / b" 并列取第一个；去掉结尾句点）。
    """
    if status == "deprecated":
        return ""
    if sub_prefix == "8.4":
        if status == "caution" and _ascii_ratio(example) > 0.8:
            return example.strip().rstrip(".")
        if cn.startswith("保留") and _ascii_ratio(example) > 0.8:
            return example.strip().rstrip(".")
        return ""
    frag = (en or "").strip()
    if not frag:
        return ""
    if " / " in frag:
        frag = frag.split(" / ")[0].strip()
    return frag.rstrip(".").strip()


def build_manifest() -> dict:
    """构建完整目录（维度 → 子类 → 选项 + 冲突规则）。"""
    postcounts = _load_postcounts()

    dimensions = []
    index = {}            # (dim_id, sub_prefix, cn) -> item_id
    item_by_id = {}       # item_id -> item
    group_ids = {}        # group_id -> [item_id]
    total_items = 0
    research_items = 0    # 调研原文条目数（对账用）
    added_skips = 0       # 本次补的显式「不指定」项数
    total_groups = 0
    resolver_bind_pending = []
    level_counter = {"通": 0, "模": 0, "标": 0}
    tag_items = 0
    tag_verified = 0

    for d in DIMS:
        dim_id = d["id"]
        groups = []
        for gi, sub in enumerate(d["subs"]):
            sub_prefix = sub["name"].split(" ")[0]
            group_id = "%s-%02d" % (dim_id, gi + 1)
            items = []
            for ii, (cn, en, example, level) in enumerate(sub["items"]):
                item_id = "%s-%02d-%02d" % (dim_id, gi + 1, ii + 1)
                level_cn, needs_test = LEVELS.get(level, (level, False))
                status = _status_for(level, cn, sub_prefix)
                entry = {
                    "id": item_id,
                    "label": cn,
                    # 【归一化】跳过项的英文片段统一清空 ——
                    # 调研原文写的是「（留空）」这类中文占位说明，
                    # 直接塞进提示词会变成垃圾片段。归一后前端无从分辨，
                    # 只能靠 is_skip 判定 ⇒ 跳过项**永远**不输出。
                    "en": "" if _is_skip(en) else en,
                    "example": example,
                    "level": level,
                    "level_label": level_cn,
                    "needs_test": needs_test,
                    "status": status,
                    "is_skip": _is_skip(en),
                    # 【片段】这条选项真正写进提示词的那一段。
                    # zh_fragment = **默认**写进提示词的中文片段（中文优先）。
                    # en_fragment = 英文辅助版本，只在「含英文片段版本」/详情里出现，
                    #               默认不参与组装。
                    # 空串 = 这一项不输出（跳过项 / 淘汰项 / 指导清单项）。
                    "zh_fragment": "" if _is_skip(en) else _zh_fragment_for(
                        cn, sub_prefix, status),
                    "en_fragment": "" if _is_skip(en) else _fragment_for(
                        cn, en, example, sub_prefix, status),
                    # 兼容旧字段名（前端/测试里 `fragment` 一直指默认那份）。
                    "fragment": "" if _is_skip(en) else _zh_fragment_for(
                        cn, sub_prefix, status),
                }
                if level == "标" and not _is_skip(en):
                    tag_items += 1
                    pc = None
                    for cand in _cands(en):
                        v = postcounts.get(cand)
                        if isinstance(v, int) and v > 0:
                            pc = v
                            break
                    entry["danbooru_post_count"] = pc
                    if pc:
                        tag_verified += 1
                items.append(entry)
                item_by_id[item_id] = entry
                index[(dim_id, sub_prefix, cn)] = item_id
                total_items += 1
                research_items += 1
                level_counter[level] = level_counter.get(level, 0) + 1
            # 【显式「不指定」】调研原文里只有 20 个子类带「不指定」项，
            # 其余 28 个没有 —— 任务书要求「每个维度含显式「不指定/跳过」选项」。
            # 这里给没有的补一个**真的不输出**的跳过项（不是写 "no X"）。
            if not any(i["is_skip"] for i in items):
                skip_entry = {
                    "id": "%s-00" % group_id,
                    "label": "不指定",
                    "en": "",
                    "example": "不输出这一项",
                    "level": "通",
                    "level_label": LEVELS["通"][0],
                    "needs_test": False,
                    "status": "normal",
                    "is_skip": True,
                    "zh_fragment": "",
                    "en_fragment": "",
                    "fragment": "",
                }
                items.insert(0, skip_entry)
                item_by_id[skip_entry["id"]] = skip_entry
                index[(dim_id, sub_prefix, "不指定")] = skip_entry["id"]
                total_items += 1
                added_skips += 1
            group_ids[group_id] = [i["id"] for i in items]
            resolver_bind_pending.append((dim_id, sub_prefix, group_id))
            total_groups += 1
            slot = SUB_SLOT.get(sub_prefix) or DIM_SLOT.get(dim_id)
            if dim_id == "D9":
                render_mode = "rules"          # D9 是**冲突矩阵**本身，不是可选项
            else:
                render_mode = GROUP_RENDER.get(sub_prefix, "options")
            groups.append({
                "id": group_id,
                "name": sub["name"],
                "sub_prefix": sub_prefix,
                "purpose": sub.get("purpose", ""),
                "slot": slot,
                "max": GROUP_MAX,
                # 组内顺序（同一槽位内按它排）
                "order": GROUP_ORDER.get(dim_id, 0),
                "render": render_mode,
                # 【每个维度都允许自行输入】任务书 2026-11-21 新增约束：
                # 每个维度都要有自由文本入口，自定义项与词库选项一起参与组装。
                # 词库里没有「可信中文对应说法」的内容，用户自己写 = 合法路径。
                "allow_custom": dim_id != "D9",
                # 自定义内容的骨架槽位 = 本组的槽位（写在哪个位置由组决定）
                "custom_placeholder": CUSTOM_PLACEHOLDER.get(sub_prefix)
                                      or CUSTOM_PLACEHOLDER.get(dim_id, ""),
                "items": items,
            })
        dimensions.append({
            "id": dim_id,
            "name": d["name"],
            "purpose": d.get("purpose", ""),
            "slot": DIM_SLOT.get(dim_id),
            "anti": d.get("anti", ""),
            "notes": d.get("conflicts", []),
            "groups": groups,
        })

    resolver = _Resolver(index)
    for _d, _s, _g in resolver_bind_pending:
        resolver.bind_group(_d, _s, _g)

    conflicts = []
    for rid, label, reason, a_refs, b_refs in HARD_RULES:
        a = [resolver.item(*r) for r in a_refs]
        b = [resolver.item(*r) for r in b_refs]
        conflicts.append({
            "id": rid, "kind": "hard", "label": label, "reason": reason,
            "a": a, "b": b,
            "suggestion": "换成一个不冲突的选项，或取消已选的一侧",
        })
    for rid, label, reason, a_refs, b_refs in SOFT_RULES:
        a = []
        for r in a_refs:
            a.extend(resolver.parse_soft_ref(r))
        b = []
        for r in b_refs:
            b.extend(resolver.parse_soft_ref(r))
        conflicts.append({
            "id": rid, "kind": "soft", "label": label, "reason": reason,
            "a": a, "b": b,
            "suggestion": "可以共存，但会互相削弱，生成前值得再确认一次",
        })
    for rid, label, reason, scope, limit, scope_label, examples in CAP_RULES:
        if scope in DIM_MAX:
            scope_ids = [i for (dd, _s, _c), i in index.items() if dd == scope]
        elif "." in scope:
            # 形如 "D8.4" = 维度 D8 的子类 8.4
            scope_ids = []
            for (dd, ss), gid in resolver.group_ids().items():
                if dd == scope.split(".")[0] and ss == scope.split(".", 1)[1]:
                    scope_ids = group_ids.get(gid, [])
                    break
            if not scope_ids:
                for (dd, ss), gid in resolver.group_ids().items():
                    if dd == scope.split(".")[0] and ss.split(".")[-1] == scope.split(".")[-1]:
                        scope_ids = group_ids.get(gid, [])
                        break
        else:
            scope_ids = group_ids.get(scope, [])
        if not scope_ids:
            raise KeyError("上限规则引用了不存在的范围：%s" % scope)
        conflicts.append({
            "id": rid, "kind": "cap", "label": label, "reason": reason,
            "scope": scope, "scope_label": scope_label, "limit": limit,
            "a": scope_ids, "b": [],
            "examples": examples,
            "suggestion": "最多保留 %d 项，其余去掉" % limit,
        })

    # 冲突规则里引用的每个 id 都必须真实存在（防止规则指向已删项）
    for c in conflicts:
        for iid in list(c["a"]) + list(c["b"]):
            if iid not in item_by_id:
                raise KeyError("冲突规则 %s 引用了不存在的选项 id：%s" % (c["id"], iid))

    manifest = {
        "version": "",
        "generated_from": "h3studio/prompt_catalog_data/_data_d1..d4.py",
        "levels": {k: {"label": v[0], "needs_test": v[1]} for k, v in LEVELS.items()},
        "slots": [{"key": k, "label": SLOT_LABELS[k]} for k in SLOT_ORDER],
        "dimensions": dimensions,
        "conflicts": conflicts,
        "counts": {
            "dimensions": len(dimensions),
            "groups": total_groups,
            "items": total_items,
            # 【对账口径】research_items = 调研原文 871 项（逐条保留，无删减）
            # added_skips = 为「每个维度含显式不指定项」补的跳过项
            # items = research_items + added_skips（前端可选总数）
            "research_items": research_items,
            "added_skips": added_skips,
            "by_level": level_counter,
            "tag_items": tag_items,
            "tag_verified": tag_verified,
            "hard_rules": sum(1 for c in conflicts if c["kind"] == "hard"),
            "soft_rules": sum(1 for c in conflicts if c["kind"] == "soft"),
            "cap_rules": sum(1 for c in conflicts if c["kind"] == "cap"),
        },
        # 【D9 冲突矩阵元数据】任务书要求「附 D9 冲突规则元数据」。
        # 这里的 48 项是调研 D9 维度的原始条目（9.1 硬互斥 23 / 9.2 软冲突 13
        # / 9.3 UI 联动建议 12）—— 规则文本原文保留，便于前端展示与追溯。
        "rule_matrix": _rule_matrix(),
    }

    # 版本号 = 目录内容的 sha256 前 12 位（内容变了版本就变 ⇒ 前端可重建）
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    manifest["version"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return manifest


_CACHE = None


def get_manifest() -> dict:
    global _CACHE
    if _CACHE is None:
        _CACHE = build_manifest()
    return _CACHE


if __name__ == "__main__":  # pragma: no cover
    m = build_manifest()
    out = _HERE / "prompt_catalog_data" / "catalog.json"
    out.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    print("WROTE", out)
    print("counts:", json.dumps(m["counts"], ensure_ascii=False))
    print("version:", m["version"])
