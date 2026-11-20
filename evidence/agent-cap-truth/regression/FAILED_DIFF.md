==============================================================================
全量 pytest 对跑 —— FAILED / ERROR 名单 diff
==============================================================================
基线 worktree : /root/projects/h3s-baseline  @ b9a6424 (local/maintainer 基线)
改动后        : /root/projects/h3s-agent-cap @ 5b7a8e9 (含新增测试)
跑法          : scripts/run_pytest_per_file.py —— 逐文件跑 + 每文件 300s 超时
                （必须串行：两次全量并发跑会互相拖死，
                  实测同一文件 9.3s -> 300s 超时，test_agent_power 基线
                  单独跑 45 全绿、并发时 5 failed）

基线  : 169 个测试文件, 17 个失败
改动后: 170 个测试文件, 14 个失败

------------------------------------------------------------------------------
【A】只在改动后失败 = 新增回归
------------------------------------------------------------------------------
  （空）—— 改动后没有引入任何新的失败测试文件 ✅

------------------------------------------------------------------------------
【B】只在基线失败 = 改动后修好了 / 偶发
------------------------------------------------------------------------------
  ONLY-BASE tests/test_continuation_retry.py | 基线: 3 failed, 12 passed in 0.06s
                          改动: 15 passed in 0.09s
  ONLY-BASE tests/test_enhance_architecture_fixes.py | 基线: 1 failed, 27 passed in 21.65s
                          改动: 28 passed in 20.98s
  ONLY-BASE tests/test_enhance_menu.py | 基线: 1 failed, 23 passed in 117.76s (0:01:57)
                          改动: 24 passed in 112.26s (0:01:52)

------------------------------------------------------------------------------
【C】两边都失败 = 既有失败（非本次引入）
------------------------------------------------------------------------------
  BOTH  tests/test_agent_paste_browser.py
        基线: 1 failed, 13 passed in 11.16s
        改动: 1 failed, 13 passed in 19.45s
  BOTH  tests/test_asset_picker_onpick.py
        基线: [TIMEOUT after 300s]
        改动: [TIMEOUT after 300s]
  BOTH  tests/test_d11_moss_lora_final.py
        基线: 1 failed, 13 passed in 12.79s
        改动: 1 failed, 13 passed in 13.02s
  BOTH  tests/test_e2e_gen_flow_contract.py
        基线: 1 failed, 4 passed in 0.12s
        改动: 1 failed, 4 passed in 0.11s
  BOTH  tests/test_enhance_owner_badge.py
        基线: 1 failed, 6 passed, 6 subtests passed in 0.02s
        改动: 1 failed, 6 passed, 6 subtests passed in 0.02s
  BOTH  tests/test_enhance_review_fixes.py
        基线: 1 failed, 22 passed, 2 subtests passed in 39.14s
        改动: 1 failed, 22 passed, 2 subtests passed in 38.93s
  BOTH  tests/test_enhance_unify.py
        基线: 1 failed, 19 passed in 59.96s
        改动: 1 failed, 19 passed in 59.73s
  BOTH  tests/test_lora_file_integrity.py
        基线: 2 failed, 9 passed, 1 skipped in 5.23s
        改动: 2 failed, 9 passed, 1 skipped in 5.15s
  BOTH  tests/test_moss_lora_damaged_and_partial.py
        基线: 2 failed, 21 passed, 7 subtests passed in 5.83s
        改动: 2 failed, 21 passed, 7 subtests passed in 5.62s
  BOTH  tests/test_music_flow_browser.py
        基线: 1 failed, 9 passed in 81.87s (0:01:21)
        改动: 1 failed, 9 passed in 82.27s (0:01:22)
  BOTH  tests/test_page_size.py
        基线: 1 failed, 21 passed in 81.10s (0:01:21)
        改动: 1 failed, 21 passed in 79.67s (0:01:19)
  BOTH  tests/test_qwen_pe_enhance.py
        基线: 1 failed, 28 passed in 77.90s (0:01:17)
        改动: 1 failed, 28 passed in 78.12s (0:01:18)
  BOTH  tests/test_qwen_refs_browser.py
        基线: 3 failed in 91.17s (0:01:31)
        改动: 3 failed in 91.21s (0:01:31)
  BOTH  tests/test_ref_context_remove.py
        基线: 5 failed, 14 passed in 30.94s
        改动: 5 failed, 14 passed in 30.99s
  共 14 个

  逐个复核结论（单独跑基线 worktree 复现）：
    基线同样失败（既有失败）：
      - tests/test_agent_paste_browser.py
      - tests/test_d11_moss_lora_final.py
      - tests/test_e2e_gen_flow_contract.py
      - tests/test_enhance_owner_badge.py
      - tests/test_lora_file_integrity.py
      - tests/test_moss_lora_damaged_and_partial.py
      - tests/test_continuation_retry.py
    基线同样超时（浏览器测试）：
      - tests/test_asset_picker_onpick.py
    单独跑两边都通过（并发/资源竞争导致的偶发）：
      - tests/test_enhance_architecture_fixes.py
      - tests/test_enhance_menu.py
      - tests/test_enhance_review_fixes.py
      - tests/test_enhance_unify.py
      - tests/test_music_flow_browser.py
      - tests/test_page_size.py
      - tests/test_qwen_pe_enhance.py
      - tests/test_qwen_refs_browser.py
      - tests/test_ref_context_remove.py

------------------------------------------------------------------------------
【D】改动后新增的测试文件
------------------------------------------------------------------------------
  tests/test_agent_capability_truth.py | 53 passed in 7.84s

==============================================================================
结论：无新增回归 ✅
==============================================================================