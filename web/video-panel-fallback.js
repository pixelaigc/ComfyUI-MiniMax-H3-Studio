/* ============================================================
   文生视频面板 —— **静态兜底清单**（离线 / 后端异常时用）
   ------------------------------------------------------------
   为什么需要这个文件：
     2026-11-11 用户实机报「文生视频面板下拉全部空白」。
     根因：loadVideoOptions() 在 /api/video-options 失败时直接进 catch，
     renderVideoPanel() **从未执行** —— 于是尺寸预设、模型、加速 LoRA、
     注意力机制**全部**是空下拉（控件在、选项没有），
     只有一个空的 .img-warn 元素还带着橙色背景挂在模型框下面。

     这份清单让面板在拿不到服务端时**仍能渲染出可用的选项**，
     而不是一片空白。

   ⚠️ 形状必须与服务端 h3studio/video_models.py 的 options_payload() **完全一致**
     （size_presets 是**扁平 list**，不是按 aspect 分组的 dict ——
     形状错了会在 renderSizePresetSelect 里抛
     "object is not iterable"，反而把面板搞崩）。

   ⚠️ 这不是「模型一定可用」的承诺 —— 兜底态下 status 一律是
     "unknown"，界面上会明确提示「未能确认，可用性未知」。

   ⚠️ 本文件由 scripts/gen_video_panel_fallback.py **从服务端真源生成**，
     不要手改 —— 手改会让兜底态与服务端不一致（尺寸算错）。
     改完请跑：/home/ubuntu/comfyui/venv/bin/python -m pytest
     tests/test_panel_fallback.py -q
   ============================================================ */
window.VIDEO_PANEL_FALLBACK = {
  "size_presets": [
    {
      "key": "1:1_0.2",
      "label": "448p · 448×448",
      "aspect": "1:1",
      "megapixels": 0.2,
      "width": 448,
      "height": 448,
      "short_edge": 448
    },
    {
      "key": "1:1_0.3",
      "label": "576p · 576×576",
      "aspect": "1:1",
      "megapixels": 0.3,
      "width": 576,
      "height": 576,
      "short_edge": 576
    },
    {
      "key": "1:1_0.4",
      "label": "640p · 640×640",
      "aspect": "1:1",
      "megapixels": 0.4,
      "width": 640,
      "height": 640,
      "short_edge": 640
    },
    {
      "key": "1:1_0.5",
      "label": "736p · 736×736",
      "aspect": "1:1",
      "megapixels": 0.5,
      "width": 736,
      "height": 736,
      "short_edge": 736
    },
    {
      "key": "1:1_0.6",
      "label": "800p · 800×800",
      "aspect": "1:1",
      "megapixels": 0.6,
      "width": 800,
      "height": 800,
      "short_edge": 800
    },
    {
      "key": "1:1_0.7",
      "label": "864p · 864×864",
      "aspect": "1:1",
      "megapixels": 0.7,
      "width": 864,
      "height": 864,
      "short_edge": 864
    },
    {
      "key": "1:1_0.8",
      "label": "928p · 928×928",
      "aspect": "1:1",
      "megapixels": 0.8,
      "width": 928,
      "height": 928,
      "short_edge": 928
    },
    {
      "key": "1:1_0.9",
      "label": "960p · 960×960",
      "aspect": "1:1",
      "megapixels": 0.9,
      "width": 960,
      "height": 960,
      "short_edge": 960
    },
    {
      "key": "1:1_0.98",
      "label": "1024p · 1024×1024",
      "aspect": "1:1",
      "megapixels": 0.98,
      "width": 1024,
      "height": 1024,
      "short_edge": 1024
    },
    {
      "key": "1:1_1.0",
      "label": "1024p · 1024×1024",
      "aspect": "1:1",
      "megapixels": 1.0,
      "width": 1024,
      "height": 1024,
      "short_edge": 1024
    },
    {
      "key": "1:1_1.2",
      "label": "1120p · 1120×1120",
      "aspect": "1:1",
      "megapixels": 1.2,
      "width": 1120,
      "height": 1120,
      "short_edge": 1120
    },
    {
      "key": "1:1_1.5",
      "label": "1248p · 1248×1248",
      "aspect": "1:1",
      "megapixels": 1.5,
      "width": 1248,
      "height": 1248,
      "short_edge": 1248
    },
    {
      "key": "1:1_1.8",
      "label": "1376p · 1376×1376",
      "aspect": "1:1",
      "megapixels": 1.8,
      "width": 1376,
      "height": 1376,
      "short_edge": 1376
    },
    {
      "key": "1:1_2.0",
      "label": "1440p · 1440×1440",
      "aspect": "1:1",
      "megapixels": 2.0,
      "width": 1440,
      "height": 1440,
      "short_edge": 1440
    },
    {
      "key": "2:3_0.2",
      "label": "384p · 384×576",
      "aspect": "2:3",
      "megapixels": 0.2,
      "width": 384,
      "height": 576,
      "short_edge": 384
    },
    {
      "key": "2:3_0.3",
      "label": "448p · 448×672",
      "aspect": "2:3",
      "megapixels": 0.3,
      "width": 448,
      "height": 672,
      "short_edge": 448
    },
    {
      "key": "2:3_0.4",
      "label": "544p · 544×800",
      "aspect": "2:3",
      "megapixels": 0.4,
      "width": 544,
      "height": 800,
      "short_edge": 544
    },
    {
      "key": "2:3_0.5",
      "label": "576p · 576×896",
      "aspect": "2:3",
      "megapixels": 0.5,
      "width": 576,
      "height": 896,
      "short_edge": 576
    },
    {
      "key": "2:3_0.6",
      "label": "640p · 640×960",
      "aspect": "2:3",
      "megapixels": 0.6,
      "width": 640,
      "height": 960,
      "short_edge": 640
    },
    {
      "key": "2:3_0.7",
      "label": "704p · 704×1056",
      "aspect": "2:3",
      "megapixels": 0.7,
      "width": 704,
      "height": 1056,
      "short_edge": 704
    },
    {
      "key": "2:3_0.8",
      "label": "736p · 736×1120",
      "aspect": "2:3",
      "megapixels": 0.8,
      "width": 736,
      "height": 1120,
      "short_edge": 736
    },
    {
      "key": "2:3_0.9",
      "label": "800p · 800×1184",
      "aspect": "2:3",
      "megapixels": 0.9,
      "width": 800,
      "height": 1184,
      "short_edge": 800
    },
    {
      "key": "2:3_0.98",
      "label": "832p · 832×1248",
      "aspect": "2:3",
      "megapixels": 0.98,
      "width": 832,
      "height": 1248,
      "short_edge": 832
    },
    {
      "key": "2:3_1.0",
      "label": "832p · 832×1248",
      "aspect": "2:3",
      "megapixels": 1.0,
      "width": 832,
      "height": 1248,
      "short_edge": 832
    },
    {
      "key": "2:3_1.2",
      "label": "928p · 928×1376",
      "aspect": "2:3",
      "megapixels": 1.2,
      "width": 928,
      "height": 1376,
      "short_edge": 928
    },
    {
      "key": "2:3_1.5",
      "label": "1024p · 1024×1536",
      "aspect": "2:3",
      "megapixels": 1.5,
      "width": 1024,
      "height": 1536,
      "short_edge": 1024
    },
    {
      "key": "2:3_1.8",
      "label": "1120p · 1120×1696",
      "aspect": "2:3",
      "megapixels": 1.8,
      "width": 1120,
      "height": 1696,
      "short_edge": 1120
    },
    {
      "key": "2:3_2.0",
      "label": "1184p · 1184×1760",
      "aspect": "2:3",
      "megapixels": 2.0,
      "width": 1184,
      "height": 1760,
      "short_edge": 1184
    },
    {
      "key": "3:2_0.2",
      "label": "384p · 576×384",
      "aspect": "3:2",
      "megapixels": 0.2,
      "width": 576,
      "height": 384,
      "short_edge": 384
    },
    {
      "key": "3:2_0.3",
      "label": "448p · 672×448",
      "aspect": "3:2",
      "megapixels": 0.3,
      "width": 672,
      "height": 448,
      "short_edge": 448
    },
    {
      "key": "3:2_0.4",
      "label": "544p · 800×544",
      "aspect": "3:2",
      "megapixels": 0.4,
      "width": 800,
      "height": 544,
      "short_edge": 544
    },
    {
      "key": "3:2_0.5",
      "label": "576p · 896×576",
      "aspect": "3:2",
      "megapixels": 0.5,
      "width": 896,
      "height": 576,
      "short_edge": 576
    },
    {
      "key": "3:2_0.6",
      "label": "640p · 960×640",
      "aspect": "3:2",
      "megapixels": 0.6,
      "width": 960,
      "height": 640,
      "short_edge": 640
    },
    {
      "key": "3:2_0.7",
      "label": "704p · 1056×704",
      "aspect": "3:2",
      "megapixels": 0.7,
      "width": 1056,
      "height": 704,
      "short_edge": 704
    },
    {
      "key": "3:2_0.8",
      "label": "736p · 1120×736",
      "aspect": "3:2",
      "megapixels": 0.8,
      "width": 1120,
      "height": 736,
      "short_edge": 736
    },
    {
      "key": "3:2_0.9",
      "label": "800p · 1184×800",
      "aspect": "3:2",
      "megapixels": 0.9,
      "width": 1184,
      "height": 800,
      "short_edge": 800
    },
    {
      "key": "3:2_0.98",
      "label": "832p · 1248×832",
      "aspect": "3:2",
      "megapixels": 0.98,
      "width": 1248,
      "height": 832,
      "short_edge": 832
    },
    {
      "key": "3:2_1.0",
      "label": "832p · 1248×832",
      "aspect": "3:2",
      "megapixels": 1.0,
      "width": 1248,
      "height": 832,
      "short_edge": 832
    },
    {
      "key": "3:2_1.2",
      "label": "928p · 1376×928",
      "aspect": "3:2",
      "megapixels": 1.2,
      "width": 1376,
      "height": 928,
      "short_edge": 928
    },
    {
      "key": "3:2_1.5",
      "label": "1024p · 1536×1024",
      "aspect": "3:2",
      "megapixels": 1.5,
      "width": 1536,
      "height": 1024,
      "short_edge": 1024
    },
    {
      "key": "3:2_1.8",
      "label": "1120p · 1696×1120",
      "aspect": "3:2",
      "megapixels": 1.8,
      "width": 1696,
      "height": 1120,
      "short_edge": 1120
    },
    {
      "key": "3:2_2.0",
      "label": "1184p · 1760×1184",
      "aspect": "3:2",
      "megapixels": 2.0,
      "width": 1760,
      "height": 1184,
      "short_edge": 1184
    },
    {
      "key": "3:4_0.2",
      "label": "384p · 384×544",
      "aspect": "3:4",
      "megapixels": 0.2,
      "width": 384,
      "height": 544,
      "short_edge": 384
    },
    {
      "key": "3:4_0.3",
      "label": "480p · 480×640",
      "aspect": "3:4",
      "megapixels": 0.3,
      "width": 480,
      "height": 640,
      "short_edge": 480
    },
    {
      "key": "3:4_0.4",
      "label": "576p · 576×736",
      "aspect": "3:4",
      "megapixels": 0.4,
      "width": 576,
      "height": 736,
      "short_edge": 576
    },
    {
      "key": "3:4_0.5",
      "label": "640p · 640×832",
      "aspect": "3:4",
      "megapixels": 0.5,
      "width": 640,
      "height": 832,
      "short_edge": 640
    },
    {
      "key": "3:4_0.6",
      "label": "672p · 672×928",
      "aspect": "3:4",
      "megapixels": 0.6,
      "width": 672,
      "height": 928,
      "short_edge": 672
    },
    {
      "key": "3:4_0.7",
      "label": "736p · 736×992",
      "aspect": "3:4",
      "megapixels": 0.7,
      "width": 736,
      "height": 992,
      "short_edge": 736
    },
    {
      "key": "3:4_0.8",
      "label": "800p · 800×1056",
      "aspect": "3:4",
      "megapixels": 0.8,
      "width": 800,
      "height": 1056,
      "short_edge": 800
    },
    {
      "key": "3:4_0.9",
      "label": "832p · 832×1120",
      "aspect": "3:4",
      "megapixels": 0.9,
      "width": 832,
      "height": 1120,
      "short_edge": 832
    },
    {
      "key": "3:4_0.98",
      "label": "864p · 864×1184",
      "aspect": "3:4",
      "megapixels": 0.98,
      "width": 864,
      "height": 1184,
      "short_edge": 864
    },
    {
      "key": "3:4_1.0",
      "label": "896p · 896×1184",
      "aspect": "3:4",
      "megapixels": 1.0,
      "width": 896,
      "height": 1184,
      "short_edge": 896
    },
    {
      "key": "3:4_1.2",
      "label": "960p · 960×1280",
      "aspect": "3:4",
      "megapixels": 1.2,
      "width": 960,
      "height": 1280,
      "short_edge": 960
    },
    {
      "key": "3:4_1.5",
      "label": "1088p · 1088×1440",
      "aspect": "3:4",
      "megapixels": 1.5,
      "width": 1088,
      "height": 1440,
      "short_edge": 1088
    },
    {
      "key": "3:4_1.8",
      "label": "1184p · 1184×1600",
      "aspect": "3:4",
      "megapixels": 1.8,
      "width": 1184,
      "height": 1600,
      "short_edge": 1184
    },
    {
      "key": "3:4_2.0",
      "label": "1248p · 1248×1664",
      "aspect": "3:4",
      "megapixels": 2.0,
      "width": 1248,
      "height": 1664,
      "short_edge": 1248
    },
    {
      "key": "4:3_0.2",
      "label": "384p · 544×384",
      "aspect": "4:3",
      "megapixels": 0.2,
      "width": 544,
      "height": 384,
      "short_edge": 384
    },
    {
      "key": "4:3_0.3",
      "label": "480p · 640×480",
      "aspect": "4:3",
      "megapixels": 0.3,
      "width": 640,
      "height": 480,
      "short_edge": 480
    },
    {
      "key": "4:3_0.4",
      "label": "576p · 736×576",
      "aspect": "4:3",
      "megapixels": 0.4,
      "width": 736,
      "height": 576,
      "short_edge": 576
    },
    {
      "key": "4:3_0.5",
      "label": "640p · 832×640",
      "aspect": "4:3",
      "megapixels": 0.5,
      "width": 832,
      "height": 640,
      "short_edge": 640
    },
    {
      "key": "4:3_0.6",
      "label": "672p · 928×672",
      "aspect": "4:3",
      "megapixels": 0.6,
      "width": 928,
      "height": 672,
      "short_edge": 672
    },
    {
      "key": "4:3_0.7",
      "label": "736p · 992×736",
      "aspect": "4:3",
      "megapixels": 0.7,
      "width": 992,
      "height": 736,
      "short_edge": 736
    },
    {
      "key": "4:3_0.8",
      "label": "800p · 1056×800",
      "aspect": "4:3",
      "megapixels": 0.8,
      "width": 1056,
      "height": 800,
      "short_edge": 800
    },
    {
      "key": "4:3_0.9",
      "label": "832p · 1120×832",
      "aspect": "4:3",
      "megapixels": 0.9,
      "width": 1120,
      "height": 832,
      "short_edge": 832
    },
    {
      "key": "4:3_0.98",
      "label": "864p · 1184×864",
      "aspect": "4:3",
      "megapixels": 0.98,
      "width": 1184,
      "height": 864,
      "short_edge": 864
    },
    {
      "key": "4:3_1.0",
      "label": "896p · 1184×896",
      "aspect": "4:3",
      "megapixels": 1.0,
      "width": 1184,
      "height": 896,
      "short_edge": 896
    },
    {
      "key": "4:3_1.2",
      "label": "960p · 1280×960",
      "aspect": "4:3",
      "megapixels": 1.2,
      "width": 1280,
      "height": 960,
      "short_edge": 960
    },
    {
      "key": "4:3_1.5",
      "label": "1088p · 1440×1088",
      "aspect": "4:3",
      "megapixels": 1.5,
      "width": 1440,
      "height": 1088,
      "short_edge": 1088
    },
    {
      "key": "4:3_1.8",
      "label": "1184p · 1600×1184",
      "aspect": "4:3",
      "megapixels": 1.8,
      "width": 1600,
      "height": 1184,
      "short_edge": 1184
    },
    {
      "key": "4:3_2.0",
      "label": "1248p · 1664×1248",
      "aspect": "4:3",
      "megapixels": 2.0,
      "width": 1664,
      "height": 1248,
      "short_edge": 1248
    },
    {
      "key": "9:16_0.2",
      "label": "352p · 352×608",
      "aspect": "9:16",
      "megapixels": 0.2,
      "width": 352,
      "height": 608,
      "short_edge": 352
    },
    {
      "key": "9:16_0.3",
      "label": "416p · 416×736",
      "aspect": "9:16",
      "megapixels": 0.3,
      "width": 416,
      "height": 736,
      "short_edge": 416
    },
    {
      "key": "9:16_0.4",
      "label": "480p · 480×864",
      "aspect": "9:16",
      "megapixels": 0.4,
      "width": 480,
      "height": 864,
      "short_edge": 480
    },
    {
      "key": "9:16_0.5",
      "label": "544p · 544×960",
      "aspect": "9:16",
      "megapixels": 0.5,
      "width": 544,
      "height": 960,
      "short_edge": 544
    },
    {
      "key": "9:16_0.6",
      "label": "608p · 608×1056",
      "aspect": "9:16",
      "megapixels": 0.6,
      "width": 608,
      "height": 1056,
      "short_edge": 608
    },
    {
      "key": "9:16_0.7",
      "label": "640p · 640×1152",
      "aspect": "9:16",
      "megapixels": 0.7,
      "width": 640,
      "height": 1152,
      "short_edge": 640
    },
    {
      "key": "9:16_0.8",
      "label": "672p · 672×1216",
      "aspect": "9:16",
      "megapixels": 0.8,
      "width": 672,
      "height": 1216,
      "short_edge": 672
    },
    {
      "key": "9:16_0.9",
      "label": "736p · 736×1280",
      "aspect": "9:16",
      "megapixels": 0.9,
      "width": 736,
      "height": 1280,
      "short_edge": 736
    },
    {
      "key": "9:16_0.98",
      "label": "768p · 768×1344",
      "aspect": "9:16",
      "megapixels": 0.98,
      "width": 768,
      "height": 1344,
      "short_edge": 768
    },
    {
      "key": "9:16_1.0",
      "label": "768p · 768×1376",
      "aspect": "9:16",
      "megapixels": 1.0,
      "width": 768,
      "height": 1376,
      "short_edge": 768
    },
    {
      "key": "9:16_1.2",
      "label": "832p · 832×1504",
      "aspect": "9:16",
      "megapixels": 1.2,
      "width": 832,
      "height": 1504,
      "short_edge": 832
    },
    {
      "key": "9:16_1.5",
      "label": "928p · 928×1664",
      "aspect": "9:16",
      "megapixels": 1.5,
      "width": 928,
      "height": 1664,
      "short_edge": 928
    },
    {
      "key": "9:16_1.8",
      "label": "1024p · 1024×1824",
      "aspect": "9:16",
      "megapixels": 1.8,
      "width": 1024,
      "height": 1824,
      "short_edge": 1024
    },
    {
      "key": "9:16_2.0",
      "label": "1088p · 1088×1920",
      "aspect": "9:16",
      "megapixels": 2.0,
      "width": 1088,
      "height": 1920,
      "short_edge": 1088
    },
    {
      "key": "16:9_0.2",
      "label": "352p · 608×352",
      "aspect": "16:9",
      "megapixels": 0.2,
      "width": 608,
      "height": 352,
      "short_edge": 352
    },
    {
      "key": "16:9_0.3",
      "label": "416p · 736×416",
      "aspect": "16:9",
      "megapixels": 0.3,
      "width": 736,
      "height": 416,
      "short_edge": 416
    },
    {
      "key": "16:9_0.4",
      "label": "480p · 864×480",
      "aspect": "16:9",
      "megapixels": 0.4,
      "width": 864,
      "height": 480,
      "short_edge": 480
    },
    {
      "key": "16:9_0.5",
      "label": "544p · 960×544",
      "aspect": "16:9",
      "megapixels": 0.5,
      "width": 960,
      "height": 544,
      "short_edge": 544
    },
    {
      "key": "16:9_0.6",
      "label": "608p · 1056×608",
      "aspect": "16:9",
      "megapixels": 0.6,
      "width": 1056,
      "height": 608,
      "short_edge": 608
    },
    {
      "key": "16:9_0.7",
      "label": "640p · 1152×640",
      "aspect": "16:9",
      "megapixels": 0.7,
      "width": 1152,
      "height": 640,
      "short_edge": 640
    },
    {
      "key": "16:9_0.8",
      "label": "672p · 1216×672",
      "aspect": "16:9",
      "megapixels": 0.8,
      "width": 1216,
      "height": 672,
      "short_edge": 672
    },
    {
      "key": "16:9_0.9",
      "label": "736p · 1280×736",
      "aspect": "16:9",
      "megapixels": 0.9,
      "width": 1280,
      "height": 736,
      "short_edge": 736
    },
    {
      "key": "16:9_0.98",
      "label": "768p · 1344×768",
      "aspect": "16:9",
      "megapixels": 0.98,
      "width": 1344,
      "height": 768,
      "short_edge": 768
    },
    {
      "key": "16:9_1.0",
      "label": "768p · 1376×768",
      "aspect": "16:9",
      "megapixels": 1.0,
      "width": 1376,
      "height": 768,
      "short_edge": 768
    },
    {
      "key": "16:9_1.2",
      "label": "832p · 1504×832",
      "aspect": "16:9",
      "megapixels": 1.2,
      "width": 1504,
      "height": 832,
      "short_edge": 832
    },
    {
      "key": "16:9_1.5",
      "label": "928p · 1664×928",
      "aspect": "16:9",
      "megapixels": 1.5,
      "width": 1664,
      "height": 928,
      "short_edge": 928
    },
    {
      "key": "16:9_1.8",
      "label": "1024p · 1824×1024",
      "aspect": "16:9",
      "megapixels": 1.8,
      "width": 1824,
      "height": 1024,
      "short_edge": 1024
    },
    {
      "key": "16:9_2.0",
      "label": "1088p · 1920×1088",
      "aspect": "16:9",
      "megapixels": 2.0,
      "width": 1920,
      "height": 1088,
      "short_edge": 1088
    },
    {
      "key": "21:9_0.2",
      "label": "288p · 704×288",
      "aspect": "21:9",
      "megapixels": 0.2,
      "width": 704,
      "height": 288,
      "short_edge": 288
    },
    {
      "key": "21:9_0.3",
      "label": "352p · 864×352",
      "aspect": "21:9",
      "megapixels": 0.3,
      "width": 864,
      "height": 352,
      "short_edge": 352
    },
    {
      "key": "21:9_0.4",
      "label": "416p · 992×416",
      "aspect": "21:9",
      "megapixels": 0.4,
      "width": 992,
      "height": 416,
      "short_edge": 416
    },
    {
      "key": "21:9_0.5",
      "label": "480p · 1120×480",
      "aspect": "21:9",
      "megapixels": 0.5,
      "width": 1120,
      "height": 480,
      "short_edge": 480
    },
    {
      "key": "21:9_0.6",
      "label": "512p · 1216×512",
      "aspect": "21:9",
      "megapixels": 0.6,
      "width": 1216,
      "height": 512,
      "short_edge": 512
    },
    {
      "key": "21:9_0.7",
      "label": "576p · 1312×576",
      "aspect": "21:9",
      "megapixels": 0.7,
      "width": 1312,
      "height": 576,
      "short_edge": 576
    },
    {
      "key": "21:9_0.8",
      "label": "608p · 1408×608",
      "aspect": "21:9",
      "megapixels": 0.8,
      "width": 1408,
      "height": 608,
      "short_edge": 608
    },
    {
      "key": "21:9_0.9",
      "label": "640p · 1472×640",
      "aspect": "21:9",
      "megapixels": 0.9,
      "width": 1472,
      "height": 640,
      "short_edge": 640
    },
    {
      "key": "21:9_0.98",
      "label": "672p · 1536×672",
      "aspect": "21:9",
      "megapixels": 0.98,
      "width": 1536,
      "height": 672,
      "short_edge": 672
    },
    {
      "key": "21:9_1.0",
      "label": "672p · 1568×672",
      "aspect": "21:9",
      "megapixels": 1.0,
      "width": 1568,
      "height": 672,
      "short_edge": 672
    },
    {
      "key": "21:9_1.2",
      "label": "736p · 1728×736",
      "aspect": "21:9",
      "megapixels": 1.2,
      "width": 1728,
      "height": 736,
      "short_edge": 736
    },
    {
      "key": "21:9_1.5",
      "label": "832p · 1920×832",
      "aspect": "21:9",
      "megapixels": 1.5,
      "width": 1920,
      "height": 832,
      "short_edge": 832
    },
    {
      "key": "21:9_1.8",
      "label": "896p · 2112×896",
      "aspect": "21:9",
      "megapixels": 1.8,
      "width": 2112,
      "height": 896,
      "short_edge": 896
    }
  ],
  "models": [
    {
      "label": "MiniMax-H3 FL2VA · 剪枝 int8（默认）",
      "unet": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
      "size_mb": 20970,
      "location": "/mnt/nvme1t_colorful/comfyui-models/diffusion_models",
      "pruned": true,
      "steps": 4,
      "sampler": "euler",
      "scheduler": "beta",
      "cfg": 1.0,
      "note": "剪枝版 int8（20.9 GB），H3 Turbo 4 步的常规选择。剪枝把 AdaLN 输入压到 8 维，Turbo LoRA 的 AdaLN 部分由ComfyUI-MiniMax-H3-Turbo 在运行期重新注入。",
      "key": "fl2va_pruned_int8",
      "status": "available",
      "available": true,
      "comfy_listed": true,
      "on_external_disk": true,
      "status_note": "ComfyUI 模型列表已收录该权重，可直接选用。",
      "warn": ""
    },
    {
      "label": "MiniMax-H3 FL2VA · 完整 int8（保真最高）",
      "unet": "minimax_h3_fl2va_int8_convrot.safetensors",
      "size_mb": 34039,
      "location": "/home/ubuntu/comfyui/models/diffusion_models",
      "pruned": false,
      "steps": 4,
      "sampler": "euler",
      "scheduler": "beta",
      "cfg": 1.0,
      "note": "未剪枝 int8（34.0 GB），AdaLN 仍是完整的 2688 维，Turbo LoRA 的层结构 259/259 全部命中（实测），保真最高。",
      "warn": "体积接近完整版的一半，显存占用比剪枝版高约 12 GB。",
      "key": "fl2va_int8",
      "status": "available",
      "available": true,
      "comfy_listed": true,
      "on_external_disk": false,
      "status_note": "ComfyUI 模型列表已收录该权重，可直接选用。"
    },
    {
      "label": "MiniMax-H3 FL2VA · 剪枝 w4a8（最省显存）",
      "unet": "minimax_h3_fl2va_pruned_w4a8_mixed.safetensors",
      "size_mb": 12541,
      "location": "/mnt/nvme1t_colorful/comfyui-models/diffusion_models",
      "pruned": true,
      "steps": 4,
      "sampler": "euler",
      "scheduler": "beta",
      "cfg": 1.0,
      "note": "剪枝 + 4bit 激活（12.5 GB），显存最省。注意它把隐藏维折成 2688 —— LightX2V 系加速 LoRA 的 qkv 层维度对不上（实测 200/208 形状不匹配），只能用 Turbo 家族。",
      "warn": "与 LightX2V / DMAD 系加速 LoRA 不兼容（实测形状不匹配），只能配 Turbo 家族加速 LoRA。",
      "key": "fl2va_pruned_w4a8",
      "status": "available",
      "available": true,
      "comfy_listed": true,
      "on_external_disk": true,
      "status_note": "ComfyUI 模型列表已收录该权重，可直接选用。"
    },
    {
      "label": "MiniMax-H3 FL2VA · BF16（官方原始精度）",
      "unet": "minimax_h3_fl2va_bf16.safetensors",
      "size_mb": 66280,
      "location": "/mnt/nvme1t_colorful/comfyui-models/diffusion_models",
      "pruned": false,
      "steps": 4,
      "sampler": "euler",
      "scheduler": "beta",
      "cfg": 1.0,
      "note": "官方原始 BF16 权重（66.3 GB），层结构与 Turbo LoRA 259/259 命中。",
      "warn": "48 GB 显存的卡单靠这个权重放不下（实测 66.3 GB），只适合多卡或换机器时选。",
      "key": "fl2va_bf16",
      "status": "available",
      "available": true,
      "comfy_listed": true,
      "on_external_disk": true,
      "status_note": "ComfyUI 模型列表已收录该权重，可直接选用。"
    },
    {
      "label": "MiniMax-H3 混合版 · FL2VA+Ref2VA",
      "unet": "minimax_h3_hybrid_fl2va_ref2va_b30-49-int8.safetensors",
      "size_mb": 20970,
      "location": "/mnt/nvme1t_colorful/comfyui-models/diffusion_models",
      "pruned": true,
      "steps": 4,
      "sampler": "euler",
      "scheduler": "beta",
      "cfg": 1.0,
      "note": "把文生视频与参考生成合进一个权重的剪枝 int8 版（20.9 GB）。Turbo LoRA 259 个层里 208 个直接命中、51 个 AdaLN 由运行期注入。",
      "warn": "参考通道（Ref2VA）在文生视频面板里不暴露。",
      "key": "hybrid_fl2va_ref2va",
      "status": "available",
      "available": true,
      "comfy_listed": true,
      "on_external_disk": true,
      "status_note": "ComfyUI 模型列表已收录该权重，可直接选用。"
    }
  ],
  "default_model": "fl2va_pruned_int8",
  "attention_modes": [
    {
      "label": "默认（PyTorch 稠密注意力）",
      "node": null,
      "inputs": {},
      "note": "不做任何 patch。速度基准，最稳。",
      "key": "default"
    },
    {
      "label": "Comfy Kitchen INT8 量化注意力",
      "node": "ModelAttentionBackend",
      "inputs": {
        "attention": "comfy kitchen attention"
      },
      "note": "ComfyUI 内建的 INT8 量化稠密注意力，只支持 Nvidia / AMD。省显存、略掉精度，速度提升有限但最稳。",
      "key": "kitchen"
    },
    {
      "label": "SageAttention 显存优化",
      "node": "MiniMaxH3MemoryEfficientSageAttentionPatch",
      "inputs": {},
      "note": "KJNodes 的 SageAttention patch，覆盖 H3 自注意力，降峰值显存。需要较新的 sageattention（本机已装）。",
      "key": "sage"
    },
    {
      "label": "Sol-Attn 近似路由",
      "node": "MiniMaxH3MemoryEfficientSolAttentionPatch",
      "inputs": {
        "enabled": true,
        "tau": 1.3,
        "min_tokens": 4096,
        "strict": false,
        "thresh_type": "diag",
        "int8_qk": false,
        "int8_pv": false,
        "sink_conditioning": "exact_kv",
        "dense_blocks": ""
      },
      "note": "按阈值把部分 block 走近似路径：tau 越高越快、保真越低（论文默认 1.0，本机调优值 1.3）。文本/条件 KV 仍走精确路径。",
      "key": "sol"
    },
    {
      "label": "Sol-Attn 渐进调度（首尾密度不同）",
      "node": "MiniMaxH3ScheduledSolAttentionPatch",
      "inputs": {
        "enabled": true,
        "tau_start": 1.3,
        "tau_end": 0.8,
        "curve": "linear",
        "min_tokens": 4096,
        "strict": false,
        "dense_percent": 0.0,
        "thresh_type": "diag",
        "int8_qk": false,
        "int8_pv": false,
        "sink_conditioning": "exact_kv",
        "dense_blocks": ""
      },
      "note": "Sol-Attn 的调度版：高噪步 tau 1.3（更近似）、低噪步 tau 0.8（更稠密，细节成形更准）。比固定 tau 更稳。",
      "key": "sol_scheduled"
    },
    {
      "label": "稀疏视频注意力（预算制）",
      "node": "H3SparseAttention",
      "inputs": {
        "video_budget": 0.15,
        "denser_early_late_steps": true
      },
      "note": "只保留一部分视频注意力连接：video_budget 越低越快，但提示词贴合度与运动细节会掉。没有「安全值」，每条提示词要自己试。",
      "key": "sparse"
    }
  ],
  "default_attention": "default",
  "accel_lora_combos": [
    {
      "name": "h3_lightx2v/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors",
      "display": "LightX2V 4 步（768p 档）",
      "purpose": "LightX2V 的 4 步蒸馏权重，针对 768p 短边训练，在该分辨率下比 Turbo ckpt850 更稳。",
      "recommended_strength": 1.0,
      "recommended_steps": 4,
      "note": "208 个层全部命中（实测，形状也对得上），但不含 AdaLN —— 它假设底模是完整的 BF16/INT8 版本。",
      "key": "lightx2v_4step_768p",
      "available": true
    },
    {
      "name": "minimax_h3_fl2v_turbo_4step_v0.1_768p_sla_comfyui_bf16.safetensors",
      "display": "LightX2V SLA 4 步（768p 档）",
      "purpose": "LightX2V 的 SLA（Segment Level Attention）变体，4 步同时保住长时一致性。",
      "recommended_strength": 1.0,
      "recommended_steps": 4,
      "note": "208 个层全部命中（实测）。本机的 MiniMaxH3LightX2VSLAKJSageComposerT8Advanced 节点默认就指向它。",
      "key": "lightx2v_4step_sla_768p",
      "available": true
    },
    {
      "name": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
      "display": "LightX2V 8 步",
      "purpose": "8 步档蒸馏：比 4 步慢一倍，但细节与运动更稳，适合最终成片而不是快速试片。",
      "recommended_strength": 1.0,
      "recommended_steps": 8,
      "note": "208 个层全部命中（实测），训练 alpha=8 / scale=0.0625。这是官方模板 Note 指向的 8 步权重（根目录那一份）。",
      "key": "lightx2v_8step",
      "available": true
    },
    {
      "name": "dmad_minimax_h3_4step_full_critic_comfyui.safetensors",
      "display": "DMAD 4 步（critic 版）",
      "purpose": "DMAD 蒸馏 + 判别器 critic：4 步里质量更接近多步采样。",
      "recommended_strength": 1.0,
      "recommended_steps": 4,
      "note": "208 个层全部命中（实测），不含 AdaLN。",
      "key": "dmad_4step_critic",
      "available": true
    },
    {
      "name": "h3/h3_speed_slider_1.1.safetensors",
      "display": "速度滑杆（快/慢运动）",
      "purpose": "概念 LoRA：只 patch token_refiner 的 fc2，可把画面运动整体调快或调慢（负向词 'slow motion.'）。",
      "recommended_strength": 1.0,
      "recommended_steps": 0,
      "note": "体积极小（9 MB），只动 1 个层（实测），与其它加速 LoRA 不冲突。",
      "key": "speed_slider",
      "available": true
    },
    {
      "name": "h3/h3_better_motion_720x1280_w0.4-0.8_s15-30_vpakarinen_v1.safetensors",
      "display": "更好运动（720x1280 档 / 15-30 步）",
      "purpose": "针对 720x1280 竖屏、15~30 步训练的运动增强 LoRA，改善大动作与镜头运动。",
      "recommended_strength": 0.8,
      "recommended_steps": 20,
      "note": "208 个层全部命中（实测）。它训练时的步数区间是 15-30，配 4 步蒸馏档属于超出训练分布的用法。",
      "key": "better_motion",
      "available": true
    }
  ],
  "fixed_accel_loras": [
    {
      "name": "minimax_h3_turbo_4step_ema_ckpt850.safetensors",
      "display": "Turbo 4 步 · ckpt850（工作流固定）",
      "purpose": "H3 Turbo 4 步路径的固定加速 LoRA：把 20 步压到 4 步。",
      "recommended_strength": 1.0,
      "recommended_steps": 4,
      "fixed": true,
      "note": "259 个层全部命中（实测），AdaLN 输入 2688 维，与未剪枝 int8 / BF16 完全匹配。",
      "key": "ckpt850",
      "available": true
    },
    {
      "name": "minimax_h3_turbo_v4_step600_ema.safetensors",
      "display": "Turbo 4 步 · v4-step600（工作流固定）",
      "purpose": "同族的另一枚 EMA 权重，官方 4 步档的推荐版本。",
      "recommended_strength": 1.0,
      "recommended_steps": 4,
      "fixed": true,
      "note": "259 个层全部命中（实测），与 ckpt850 等价。",
      "key": "v4step600",
      "available": true
    }
  ],
  "default_fixed_accel_lora": "ckpt850",
  "aspects": [
    {
      "key": "1:1",
      "ratio": [
        1,
        1
      ]
    },
    {
      "key": "2:3",
      "ratio": [
        2,
        3
      ]
    },
    {
      "key": "3:2",
      "ratio": [
        3,
        2
      ]
    },
    {
      "key": "3:4",
      "ratio": [
        3,
        4
      ]
    },
    {
      "key": "4:3",
      "ratio": [
        4,
        3
      ]
    },
    {
      "key": "9:16",
      "ratio": [
        9,
        16
      ]
    },
    {
      "key": "16:9",
      "ratio": [
        16,
        9
      ]
    },
    {
      "key": "21:9",
      "ratio": [
        21,
        9
      ]
    }
  ],
  "size_multiple": 32,
  "size_min": 32,
  "size_max": 16384,
  "mp_table": [
    0.2,
    0.3,
    0.4,
    0.5,
    0.6,
    0.7,
    0.8,
    0.9,
    0.98,
    1.0,
    1.2,
    1.5,
    1.8,
    2.0
  ],
  "default_mp": 0.4
};
