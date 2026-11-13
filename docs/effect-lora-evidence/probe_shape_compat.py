"""AUTHORITATIVE shape-compat probe.

Uses ComfyUI's OWN key_map (comfy.lora.model_lora_keys_unet) against the REAL
base-model header, exactly like comfy.sd.load_lora_for_models does, then
compares the LoRA's A/B shapes with the bound base weight shape.

  loaded modules -> adapter bound to a base weight key (via key_map)
  A:[rank,in]  B:[out,rank]  base W:[out,in]
  -> ok iff A[1]==W[1] and B[0]==W[0]
"""
import sys, os, json, struct, glob, collections
sys.path.insert(0, '/home/ubuntu/comfyui')
import torch
import comfy.lora as cl, comfy.lora_convert, comfy.utils

BASES = {
 'pruned_int8': '/mnt/nvme1t_colorful/comfyui-models/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors',
 'full_int8':   '/home/ubuntu/comfyui/models/diffusion_models/minimax_h3_fl2va_int8_convrot.safetensors',
}

def keymap_for(base_path):
    with open(base_path,'rb') as f:
        n=struct.unpack('<Q',f.read(8))[0]; hh=json.loads(f.read(n))
    sh = {}
    for k,v in hh.items():
        if k=='__metadata__': continue
        if k.endswith('.weight'):
            sh['diffusion_model.'+k if not k.startswith('diffusion_model.') else k] = tuple(v.get('shape') or [])
    class Cfg: unet_config = {"image_model":"minimax_h3"}
    class FakeM:
        model_config = Cfg()
        def state_dict(self): return {k: torch.empty(0) for k in sh}
    km = cl.model_lora_keys_unet(FakeM(), {})
    return km, sh

KMAPS = {b: keymap_for(p) for b,p in BASES.items() if os.path.isfile(p)}

def lora_mod_shapes(path):
    """-> {module_name: {'A':shape,'B':shape}}"""
    _, lora, _ = None, None, None
    lora, meta = comfy.utils.load_torch_file(path, safe_load=True, return_metadata=True)
    out = {}
    for k,v in lora.items():
        for m,slot in (('.lora_A.default.weight','A'),('.lora_B.default.weight','B'),
                       ('.lora_A.weight','A'),('.lora_B.weight','B'),
                       ('.lora_down.weight','A'),('.lora_up.weight','B')):
            if k.endswith(m):
                mod=k[:-len(m)]
                out.setdefault(mod,{})[slot]=tuple(v.shape)
                break
    return out

LORA_DIRS = ['/mnt/usb2t/comfyui-models/loras/h3',
             '/mnt/usb2t/comfyui-models/loras/h3_keyframe',
             '/home/ubuntu/comfyui/models/loras/minimax']

print(f"{'LoRA':58s} | {'pruned_int8':34s} | full_int8")
print('-'*140)
for d in LORA_DIRS:
    for p in sorted(glob.glob(os.path.join(d,'*.safetensors'))):
        name = os.path.relpath(p, '/mnt/usb2t/comfyui-models/loras')
        if '..' in name: name = os.path.basename(p)
        try:
            mods = lora_mod_shapes(p)
        except Exception as e:
            print(f"{name:58s} | ERR {e}"); continue
        cells=[]
        for bname,(km,sh) in KMAPS.items():
            ok=bad=miss=0; adaln=0
            for mod,slots in mods.items():
                # find the base weight key this module binds to
                bound=None
                for cand in (mod, 'diffusion_model.'+mod):
                    if cand in km:
                        bound=km[cand]; break
                if bound is None:
                    # try the underscore/flattened form the key_map uses
                    flat = mod.replace('diffusion_model.','').replace('.','_')
                    for cand in (f"lora_unet_{flat}", flat):
                        if cand in km:
                            bound=km[cand]; break
                if bound is None or bound not in sh:
                    miss+=1; continue
                w=sh[bound]; A=slots.get('A'); B=slots.get('B')
                if A is None or B is None: miss+=1; continue
                if 'adaln' in mod: adaln+=1; continue
                if A[1]==w[1] and B[0]==w[0]: ok+=1
                else: bad+=1
            cells.append(f"ok={ok} mismatch={bad} miss={miss} adaln={adaln}")
        print(f"{name:58s} | {cells[0]:34s} | {cells[1] if len(cells)>1 else ''}")
