"""flf 带「与 FL2VA 剪枝底模不兼容的 LoRA」时，现有服务端校验拦不拦。

用户第 3 条要求：提交时不兼容的 LoRA 必须被拦截并回传明确原因，
不得静默加载。这里先测现状。
"""
import sys
sys.path.insert(0, '/root/projects/h3s-flftruth')
import server

BASE = {"mode": "flf", "prompt": "x", "duration": 5.0,
        "width": 1216, "height": 672}


def case(**over):
    p = dict(BASE)
    p.update(over)
    return p


CASES = [
    ("flf 默认 lora=ckpt850（官方 turbo，兼容剪枝底模）",
     case(lora="ckpt850")),
    ("flf lora=v4step600（官方 v1.json 挂的那枚）",
     case(lora="v4step600")),
    ("flf accel_lora=lightx2v_4step_768p（第三方蒸馏）",
     case(accel_lora="lightx2v_4step_768p")),
    ("flf accel_lora=turbo_4step_cn（官方 turbo 但配剪枝底模不兼容）",
     case(accel_lora="turbo_4step_cn")),
    ("flf lora=lightx2v_4step_768p（legacy 字段带第三方 key）",
     case(lora="lightx2v_4step_768p")),
    ("flf model_key=fl2va_int8 + accel_lora=turbo_4step_cn（未剪枝，应兼容）",
     case(model_key="fl2va_int8", accel_lora="turbo_4step_cn")),
    ("flf lora=none（显式不挂）",
     case(lora="none")),
]

for label, params in CASES:
    try:
        normalized, err = server.normalize_t2v_params(dict(params), mode="flf")
        print("=== %s" % label)
        if err:
            print("   被拒绝: %s" % err)
        else:
            print("   通过 -> accel_lora=%r loader=%r lora=%r" % (
                normalized.get("accel_lora"), normalized.get("accel_lora_loader"),
                normalized.get("lora")))
    except Exception as e:
        print("=== %s\n   EXCEPTION: %s: %s" % (label, type(e).__name__, e))
    print()
