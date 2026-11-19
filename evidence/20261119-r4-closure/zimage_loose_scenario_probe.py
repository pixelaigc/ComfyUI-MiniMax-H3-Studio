
import sys, collections
sys.path.insert(0, "/root/projects/h3s-merge-20261119")
from h3studio import loras as L

dirs = L.comfyui_lora_dirs()
all_files = []
for d in dirs:
    try:
        all_files += list(d.rglob("*.safetensors"))
    except Exception:
        pass
machine = len(all_files)

# 场景 A：现状（层结构粗筛真的生效）
cur = L.list_z_image_loras()
print("A 现状 =", len(cur))

# 场景 B：层结构粗筛「完全放行」（_looks_like_z_image_layers 恒真）
#   -> 这就是断言要拦的「结构兜底的判据放太宽」
def loose_list():
    found = []
    seen = set()
    for d in dirs:
        try:
            candidates = sorted(d.rglob("*.safetensors"))
        except Exception:
            continue
        for path in candidates:
            rel = path.relative_to(d).as_posix()
            lowered = rel.lower()
            if (lowered.startswith(L.lora_damping.DERIVED_PREFIX)
                    or lowered.startswith("__p14_")):
                continue
            if rel in seen:
                continue
            seen.add(rel)
            name_hit = any(m in lowered for m in L.Z_IMAGE_MARKERS)
            by_prefix = rel.startswith("z-image-effects/")
            if name_hit or by_prefix:
                found.append(rel)
                continue
            # 粗筛恒真 -> 效果类全部进来
            info = L.LORA_CATALOG.get(rel) or L.LORA_CATALOG.get(path.name) or {}
            if L.classify_kind(rel, info) != L.KIND_EFFECT:
                continue
            found.append(rel)
    return found

loose = loose_list()
print("B 粗筛全放行 =", len(loose))
print("C 全机 =", machine)

# 三族
q = len(L.list_qwen_image_loras())
m = len(L.list_moss_image_loras())
print("qwen =", q, "moss =", m)
