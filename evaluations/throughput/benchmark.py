# evaluations/throughput/benchmark.py
import torch
import torch.nn as nn
import timm
import pandas as pd
import numpy as np
import math
from typing import Optional # <--- MODIFIED: Added for type hinting

# 添加必要的 import
from opentome.tome import tome as tm
from evaluations.utils.timer import Timer
# --- MODIFIED: Ensure tome_apply_patch is the updated version from the previous step ---
from opentome.timm import (
    tome_apply_patch,
    dtem_apply_patch,
    pitome_apply_patch,
    diffrate_apply_patch
)

ALGO_MAP = {
    "none": lambda model, **kwargs: model,
    "tome": tome_apply_patch,
    "dtem": dtem_apply_patch,
    "pitome": pitome_apply_patch,
    "diffrate": diffrate_apply_patch
}

class ThroughputBenchmark:
    """
    重构后的 Benchmark 类，负责运行指定配置的吞吐量和显存测试。
    """
    def __init__(self, device='cuda', dtype=torch.float16):
        self.device = device
        self.dtype = dtype
        self.results = []

    # --- MODIFIED: Added h and use_naive_local to the method signature ---
    def run(self,
            model_name: str,
            batch_size: int,
            seq_len: int,
            algorithm: str,
            total_merge_num: int,
            warmup_iters: int,
            benchmark_iters: int,
            inflect: float = -0.5,
            h: Optional[int] = None, # The locality window size
            use_naive_local: bool = False, # Flag to use the naive implementation
            verbose: bool = False):
        if algorithm not in ALGO_MAP:
            print(f"警告：找不到算法 '{algorithm}'，将跳过。")
            return

        torch.cuda.empty_cache()
        torch.cuda.synchronize()

        try:
            # 1. 加载模型
            patch_size = int(model_name.split('_patch')[-1].split('_')[0])
            img_size = int(math.sqrt(seq_len)) * patch_size
            model = timm.create_model(model_name, img_size=img_size, pretrained=False).to(self.device).eval()

            # --- MODIFIED: Dynamically build arguments for the patch function ---
            # 2. 应用补丁
            patch_function = ALGO_MAP[algorithm]
            patch_kwargs = {}
            if algorithm in ["tome", "pitome", "dtem"]:
                patch_kwargs['trace_source'] = True

            # Add local merging parameters ONLY for the 'tome' algorithm
            if algorithm == "tome":
                patch_kwargs['h'] = h
                patch_kwargs['use_naive_local'] = use_naive_local
                patch_kwargs['prop_attn'] = False 
            
            patch_function(model, **patch_kwargs)
            # --- END OF MODIFICATION ---

            # 3. 根据算法配置模型
            if total_merge_num > 0 and algorithm != "none":
                if not hasattr(model, '_tome_info'):
                        raise ValueError(f"模型 {model_name} 在打补丁后没有找到 _tome_info 属性。")

                num_blocks = len(model.blocks)

                if algorithm in ["tome", "pitome", "dtem"]:
                    merge_ratio_calculated = tm.check_parse_r(num_blocks, total_merge_num, seq_len, inflect)
                    
                    r_tuple = (merge_ratio_calculated, inflect)
                    model.r = r_tuple
                    model._tome_info["r"] = model.r
                    model._tome_info["total_merge"] = total_merge_num

                    print(f"  [最终配置] 算法 '{algorithm}' 配置成功 (严格遵循示例)。")
                    print(f"    - 目标总合并数: {total_merge_num}")
                    print(f"    - 计算出的 ratio: {merge_ratio_calculated:.4f}")
                    print(f"    - 设置的配置元组 _tome_info['r']: {model._tome_info['r']}")
                    
                    # --- MODIFIED: Add logging for local merging parameters ---
                    if algorithm == "tome":
                        if h is not None and h >= 0:
                            print(f"    - Local Merging: Enabled (h={h}, naive={use_naive_local})")
                        else:
                            print(f"    - Local Merging: Disabled (Global)")
                    # --- END OF MODIFICATION ---

                elif algorithm == "diffrate":
                    avg_merges_per_layer = total_merge_num / num_blocks
                    model.init_kept_num_using_r(int(avg_merges_per_layer))
                    print(f"  [最终配置] 算法 'diffrate' 配置成功: 平均每层合并 {avg_merges_per_layer:.2f} 个Token")

            # 4. 创建输入数据
            x = torch.randn(batch_size, 3, img_size, img_size, device=self.device, dtype=self.dtype)

            # 5. 详细模式验证 (Verbose mode)
            if verbose:
                if hasattr(model, 'blocks'):
                    print("\n" + "="*50)
                    print("详细模式: 正在验证Token合并路径...")
                    handles = []
                    def create_pre_hook(block_index):
                        def pre_hook(module, inputs):
                            print(f"  - 输入到 Block {block_index:02d}: {inputs[0].shape[1]} tokens")
                        return pre_hook
                    for i, block in enumerate(model.blocks):
                        handles.append(block.register_forward_pre_hook(create_pre_hook(i)))
                    with torch.no_grad(), torch.autocast(device_type='cuda', dtype=self.dtype):
                        _ = model(x)
                    for handle in handles:
                        handle.remove()
                    print("Token路径验证完毕。")
                    print("="*50 + "\n")
            
            # 6. 预热与性能测试
            with torch.no_grad(), torch.autocast(device_type='cuda', dtype=self.dtype):
                for _ in range(warmup_iters):
                    _ = model(x)
            torch.cuda.synchronize()

            timer = Timer(
                stmt=lambda: model(x),
                globals={'model': model, 'x': x},
                label=algorithm,
                sub_label=f"model={model_name}, bs={batch_size}, seq_len={seq_len}, total_merge={total_merge_num}"
            )
            
            torch.cuda.reset_peak_memory_stats(self.device)
            with torch.no_grad(), torch.autocast(device_type='cuda', dtype=self.dtype):
                measurement = timer.timeit(number=benchmark_iters)
            peak_mem_mb = torch.cuda.max_memory_allocated(self.device) / (1024 ** 2)
            latency_ms = measurement.mean * 1000
            throughput_samples_per_sec = batch_size / measurement.mean
            print(f"  完成: {str(measurement)}, Peak Mem: {peak_mem_mb:.2f} MB")
            status = 'success'

        except Exception as e:
            print(f"错误: model={model_name}, algo={algorithm}, total_merge={total_merge_num} 失败. Error: {e}")
            latency_ms = np.nan; throughput_samples_per_sec = np.nan; peak_mem_mb = np.nan; status = 'failed'
        
        # --- MODIFIED: Add local merging params to the results dictionary ---
        self.results.append({
            'model_name': model_name, 'algorithm': algorithm, 'batch_size': batch_size,
            'seq_len': seq_len, 'target_total_merge': total_merge_num,
            'h': h if algorithm == 'tome' else np.nan, # Record h only for tome
            'use_naive_local': use_naive_local if algorithm == 'tome' and h is not None else np.nan,
            'latency_ms': latency_ms, 'throughput_samples/s': throughput_samples_per_sec,
            'peak_mem_mb': peak_mem_mb, 'status': status
        })
        # --- END OF MODIFICATION ---

    def get_results(self):
        return pd.DataFrame(self.results)