import sys
sys.path.insert(0, '/root/projects/h3s-flftruth')
from h3studio import accel_lora_policy as p

for mk in ("fl2va_pruned_int8", "fl2va_int8", "ref2va_pruned_int8"):
    print("=== 底模", mk, "===")
    ok = p.available_accel_loras(mk)
    print("  可用 %d 项:" % len(ok))
    for it in ok:
        print("    %-26s official=%-5s 3rd=%-5s matched=%s/%s loader=%s" % (
            it["key"], it["official_h3"], it["third_party"],
            it["matched"], it["total"], it["loader"]))
    rej = p.rejected_accel_loras(mk)
    print("  排除 %d 项:" % len(rej))
    for it in rej:
        print("    %-26s %s" % (it["key"], str(it["reason"])[:90]))
    print()
