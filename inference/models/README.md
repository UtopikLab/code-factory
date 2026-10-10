# Model storage layout for the llama.cpp servers

## Where weights live

| Path | Mount | Source |
| :--- | :--- | :--- |
| `/models/checkpoints/<model>/` | PVC `llm-models` (`/models`) | `k8s-sc-ssd-replicated` → `/mnt/k8s-data-ssd` |

Inside each model directory keep the **GGUF/GPTQ/AWQ checkpoint** plus its
`config.json` / `model.bin`. The server reads `--model-dir /models/checkpoints/<model>`
and auto-detects the format.

```text
/mnt/k8s-data-ssd/pvc-llm-models/
└── llm-models/
    └── data/
        └── models/
            ├── checkpoints/
            │   ├── qwen2.5-14b-instruct-awq/
            │   │   ├── config.json
            │   │   ├── model.gptq-w4-g128-fp8.safetensors
            │   │   └── tokenizer.model
            │   └── qwen2.5-7b-instruct-awq/
            │       ├── config.json
            │       ├── model.gptq-w4-g128-fp8.safetensors
            │       └── tokenizer.model
            └── cache/                 # GGUF/AWQ conversion output (llama-quantize)
```

## Downloading weights

The `k8s-sc-ssd-replicated` PVC is 1.9 TB, so model weights are safe to keep
here. To fetch a checkpoint onto the node:

```bash
# From the host (or an SSH session on k8s-node01):
mkdir -p ~/downloads && cd ~/downloads
curl -O https://huggingface.co/Qwen/Qwen2.5-14B-Instruct-AWQ/resolve/main/model.gptq-w4-g128-fp8.safetensors
curl -O https://huggingface.co/Qwen/Qwen2.5-14B-Instruct-AWQ/resolve/main/config.json
# ...then copy into the PVC:
kubectl cp ~/downloads/model.gptq-w4-g128-fp8.safetensors \
    llm:$(kubectl get pvc llm-models -o hostnamepath | cut -d: -f1)/checkpoints/qwen2.5-14b-instruct-awq/
kubectl cp ~/downloads/config.json \
    llm:$(kubectl get pvc llm-models -o hostnamepath | cut -d: -f1)/checkpoints/qwen2.5-14b-instruct-awq/
```

## Re-quantizing GGUF to 4-bit (if needed)

The P40 has no tensor cores, so **4-bit quantized models are mandatory**
(see `README-inference.md`). If you have a GGUF that is not already 4-bit:

```bash
kubectl exec -n llm deployment/llm-server-gpu0 -- \
  llama-quantize /models/checkpoints/<src>/model.gguf \
    /models/checkpoints/<dst>/model.gguf -q Q4_K_M
```

Or on the host, then `kubectl cp` it into the PVC.

## VRAM budget reminder (23 GB per P40)

| Format | Fits one P40 up to ~ |
| :--- | :--- |
| 4-bit (GGUF Q4_K_M / AWQ / GPTQ) | ~30–34B params |
| 8-bit | ~14–16B params |
| FP16/BF16 | ~7–8B params (**no tensor-core speed** — slower than 4-bit; avoid) |

Do not place multi-GB models on `k8s-sc-nvme-fast`; it is for small/fast local
PVs, not large weights.
