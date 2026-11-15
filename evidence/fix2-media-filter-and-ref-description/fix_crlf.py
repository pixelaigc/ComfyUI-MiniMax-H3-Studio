
import io
p = "/root/projects/h3s-assetlib-fix/web/app.js"
data = open(p, "rb").read()
# 统一成 LF 再统一回 CRLF（避免重复 \r）
data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
open(p, "wb").write(data)
print("CRLF restored")
