
p = "/root/projects/h3s-assetlib-fix/web/app.js"
raw = open(p, "rb").read()
text = raw.decode("utf-8")
old = ('  //   只把视频产物列进「可加入片段」的候选，不动共享媒体库的过滤。\r\n'
       '  state.stitchMediaOverride = "video";')
new = ('  //   只把视频产物列进「可加入片段」的候选，不动共享媒体库的过滤。\r\n'
       '  state.stitchMediaOverride = "";')
assert old in text, "anchor not found"
open(p, "wb").write(text.replace(old, new).encode("utf-8"))
print("re-applied fix")
