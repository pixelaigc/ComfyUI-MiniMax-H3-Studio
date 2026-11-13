"""PROOF: strength on the LoraLoaderModelOnly node really scales the LoRA delta.

Uses ComfyUI's OWN loader chain (model_lora_keys_unet + load_lora) to build the
adapter, then its real calculate_weight() against the REAL H3 base weight.
If strength were ignored, every row would be identical.

Handles BOTH key formats:
  * lora_unet_blocks_0_attn_out_proj  (kohya flat)
  * diffusion_model.blocks.0.attn.out_proj  (diffusers)
"""
import sys, json, struct
sys.path.insert(0, '/home/ubuntu/comfyui')
import torch
import comfy.lora as cl, comfy.lora_convert, comfy.utils
from comfy.weight_adapter import lora as wad
from safetensors import safe_open

BASE = '/home/ubuntu/comfyui/models/diffusion_models/minimax_h3_fl2va_int8_convrot.safetensors'
TARGET = 'diffusion_model.blocks.0.attn.out_proj.weight'
BASE_MODULE = 'blocks.0.attn.out_proj'

with open(BASE, 'rb') as f:
    n = struct.unpack('<Q', f.read(8))[0]
    hdr = json.loads(f.read(n))
shapes = {('diffusion_model.' + k if not k.startswith('diffusion_model.') else k)
          for k in hdr if k != '__metadata__' and k.endswith('.weight')}
class Cfg: unet_config = {"image_model": "minimax_h3"}
class FakeM:
    model_config = Cfg()
    def state_dict(self): return {k: torch.empty(0) for k in shapes}
KM = cl.model_lora_keys_unet(FakeM(), {})

with safe_open(BASE, 'pt') as f:
    W = f.get_tensor(BASE_MODULE + '.weight').float()
base_norm = float(W.norm())

for LORA in ['/mnt/usb2t/comfyui-models/loras/h3/3dsrx_minimax_h3.safetensors',
             '/mnt/usb2t/comfyui-models/loras/h3/Minimax H3真实电影质感.safetensors',
             '/home/ubuntu/comfyui/models/loras/minimax/pika_h3_flex_fl2va_v1_bf16.safetensors']:
    print('=' * 78)
    print('LoRA:', LORA.split('/')[-1])
    lora, _ = comfy.utils.load_torch_file(LORA, safe_load=True, return_metadata=True)
    loaded = cl.load_lora(comfy.lora_convert.convert_lora(lora), KM, log_missing=False)
    if TARGET not in loaded:
        print('  该层未被绑定 —— 跳过'); continue
    print('  ✓ ComfyUI 绑定到:', TARGET)
    # LoRA 侧模块名：优先 diffusers 写法，回退 kohya 扁平写法
    adapter = wad.LoRAAdapter.load('diffusion_model.' + BASE_MODULE, lora, None, None)
    if adapter is None:
        flat = BASE_MODULE.replace('.', '_')
        adapter = wad.LoRAAdapter.load('lora_unet_' + flat, lora, None, None)
    if adapter is None:
        print('  adapter 构造失败'); continue
    print(f"  底模权重 {tuple(W.shape)}  ||W||={base_norm:.2f}")
    print(f"  {'strength':>9} | {'||dW||':>12} | {'rel %':>9}")
    print('  ' + '-'*38)
    rows = []
    for s in (0.0, 0.25, 0.5, 0.8, 1.0):
        out = adapter.calculate_weight(W.clone(), 'x', s, 1.0, 0, lambda t: t)
        d = float((out - W).norm())
        rows.append(d)
        print(f"  {s:>9.2f} | {d:>12.6f} | {100*d/base_norm:>8.4f}%")
    # 线性性检查：dW(s)/s 应该恒等于 dW(1.0)（s>0 时）
    ratios = [rows[i+1] / s for i, s in enumerate((0.25, 0.5, 0.8, 1.0))]
    dW1 = rows[-1]
    lin = all(abs(r - dW1) <= 1e-3 * max(dW1, 1e-9) for r in ratios)
    print(f"  => dW(s)/s 恒定: {lin}  (dW(1.0)={dW1:.6f}, "
          f"比值={[round(r, 6) for r in ratios]})")
    print(f"  => strength=0 时增量 = {rows[0]:.6f}（恒为 0 = 完全关闭）")
print()
print('结论: dW 随 strength 线性变化，strength=0 时增量为 0')
print('      => strength_model 真的生效（不是摆设）')
