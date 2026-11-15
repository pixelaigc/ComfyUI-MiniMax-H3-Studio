
import json, sys, time
from playwright.sync_api import sync_playwright
BASE="http://127.0.0.1:8190"; OUT=sys.argv[1]
console, pageerrors, posted = [], [], []

with sync_playwright() as p:
    b=p.chromium.launch(args=["--no-sandbox"]); ctx=b.new_context(viewport={"width":1600,"height":950}); pg=ctx.new_page()
    pg.on("console", lambda m: console.append(f"[{m.type}] {m.text}"))
    pg.on("pageerror", lambda e: pageerrors.append(str(e)))
    def on_req(r):
        if r.method=="POST" and "/api/jobs" in r.url:
            try: posted.append({"url":r.url, "body":r.post_data})
            except Exception as e: posted.append({"url":r.url, "err":str(e)})
    pg.on("request", on_req)
    pg.goto(BASE+"/", wait_until="domcontentloaded", timeout=60000); time.sleep(6)

    for MODE, LABEL in [("i2i_h3image","H3-IMAGE 图片编辑"), ("i2i_qwen","Qwen 图片编辑")]:
        print(f"\n########## {LABEL} ({MODE}) ##########")
        posted.clear()
        pg.evaluate(f"""() => {{ const items=[...document.querySelectorAll('.nav-item[data-mode]')]; const hit=items.find(x=>x.dataset.mode==='{MODE}'); if(hit) hit.click(); }}""")
        time.sleep(4)
        pg.evaluate("""() => {
            const m=document.querySelector('#enhanceGateModal');
            if(m){ m.classList.remove('open','show'); m.hidden=true; }
            document.querySelector('#imgRefAdd')?.click();
        }"""); time.sleep(3)
        pg.evaluate("""() => { const body=document.querySelector('.al-picker-body'); const cards=[...body.querySelectorAll('[data-id]')]; cards.slice(0,2).forEach(c=>c.click()); }""")
        time.sleep(1)
        pg.evaluate("""() => {
            const m=document.querySelector('#enhanceGateModal');
            if(m){ m.classList.remove('open','show'); m.hidden=true; }
            document.querySelector('#alPickerOk')?.click();
        }"""); time.sleep(5)

        # 写提示词（含占位标记）
        pg.evaluate("""() => {
            const p=document.querySelector('#prompt');
            const R = window.QwenRefs;
            let ph1='<image1>', ph2='<image2>';
            if(R){ ph1=R.placeholderFor(0); ph2=R.placeholderFor(1); }
            p.value = ph1 + ' 换成雪豹，' + ph2 + ' 保持不变';
            p.dispatchEvent(new Event('input', {bubbles:true}));
        }""")
        time.sleep(1)
        pg.screenshot(path=f"{OUT}/13_{MODE}_ready_to_submit.png")

        # 点「生成」
        clicked = pg.evaluate("""() => {
            const m=document.querySelector('#enhanceGateModal');
            if(m){ m.classList.remove('open','show'); m.hidden=true; }
            const b=document.querySelector('#genBtn');
            if(!b) return 'no #genBtn';
            if(b.disabled) return 'disabled';
            b.click();
            return 'clicked';
        }""")
        print("CLICK_GENBTN", clicked)
        time.sleep(2)
        # 「提示词尚未增强」确认框 -> 点「确认生成」
        pg.evaluate("""() => {
            const m=document.querySelector('#enhanceGateModal');
            if(!m) return;
            const btn=[...m.querySelectorAll('button')].find(b=>/确认生成/.test(b.textContent||''));
            if(btn) btn.click();
        }""")
        time.sleep(4)
        time.sleep(4)
        print("POSTED_COUNT", len(posted))
        for it in posted:
            print("POST_URL", it.get("url"))
            body = it.get("body") or ""
            print("POST_BODY_LEN", len(body))
            # body 是 multipart，提取 params 段
            m = body.find('name="params"')
            if m >= 0:
                seg = body[m:m+200000]
                j = seg.find("{")
                k = seg.rfind("}")
                try:
                    params = json.loads(seg[j:k+1])
                except Exception as e:
                    params = {"_parse_error": str(e), "_raw": seg[:500]}
                refs = params.get("ref_images") or []
                prompt = params.get("prompt") or ""
                print("PARAM_KEYS", sorted(params.keys())[:30])
                print("PROMPT_LEN", len(prompt))
                print("PROMPT_HEAD", repr(prompt[:200]))
                print("PROMPT_TAIL", repr(prompt[-200:]))
                print("REF_COUNT", len(refs))
                for i, r in enumerate(refs):
                    d = r.get("description") or ""
                    print(f"  REF[{i}] asset_id={r.get('asset_id')} desc_len={len(d)} desc_lines={d.count(chr(10))+1} src={r.get('description_source')}")
                    print(f"    desc_head={d[:70]!r}")
                    print(f"    desc_tail={d[-70:]!r}")
                # 落盘完整请求体
                with open(f"{OUT}/post_body_{MODE}.json","w",encoding="utf-8") as f:
                    json.dump({"mode":MODE,"prompt":prompt,"ref_images":refs,
                               "prompt_len":len(prompt)}, f, ensure_ascii=False, indent=2)
            else:
                print("NO_PARAMS_IN_BODY", body[:300])

    print("\nCONSOLE", json.dumps(console, ensure_ascii=False))
    print("PAGEERRORS", json.dumps(pageerrors, ensure_ascii=False))
    b.close()
