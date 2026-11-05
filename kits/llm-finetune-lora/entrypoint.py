"""llm-finetune-lora Kit entrypoint: LoRA with peft + PyTorch DDP (container contract: kitrt). Multi-GPU capable (U13).

Two roles in one file. The LAUNCHER (no RANK in the environment) validates everything, then runs itself under torchrun:
`--standalone --nproc_per_node=N` for N GPUs of one host (N from COMPUTECOIN_GPU_COUNT, set by the agent), or the
multi-node form when the agent supplied NNODES > 1 with MASTER_ADDR / MASTER_PORT / NODE_RANK. A WORKER (RANK set) trains.
Only rank 0 writes outputs and checkpoints. The launcher writes output.json from rank 0's metrics.json.

The effective batch does not depend on the GPU count: accumulation = global_batch_size / (micro_batch_size * world_size),
and the job is refused unless that divides exactly. Seeds are seed + rank. Not bit-reproducible across GPU models.
APIs to verify against current docs before use (L4): torchrun CLI flags; peft LoraConfig / get_peft_model /
PeftModel.save_pretrained / set_peft_model_state_dict; transformers AutoModelForCausalLM / AutoTokenizer.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys

import kitrt

WEIGHTS = os.environ.get("WEIGHTS_DIR", "/opt/weights")
MANIFEST = os.environ.get("WEIGHTS_MANIFEST", "/opt/weights.sha256")
SCHEMA = os.environ.get("PARAMS_SCHEMA", "/app/params.schema.json")
MAX_ROWS = 200_000
MAX_BYTES = 512 * 1024 * 1024
MAX_ROW_BYTES = 64 * 1024
MAX_GPUS = 8


def load_examples(path: str) -> list[str]:
    """JSONL rows -> training strings. {"text"} or {"prompt","completion"}. Caps enforced. Pure given the file."""
    if os.path.getsize(path) > MAX_BYTES:
        raise kitrt.KitError("invalid_input", "dataset too large")
    out: list[str] = []
    with open(path, "r", encoding="utf-8", errors="strict") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            if len(line.encode("utf-8")) > MAX_ROW_BYTES:
                raise kitrt.KitError("invalid_input", f"row {n} too long")
            try:
                obj = json.loads(line)
            except ValueError:
                raise kitrt.KitError("invalid_input", f"row {n} is not JSON") from None
            if isinstance(obj, dict) and isinstance(obj.get("text"), str):
                out.append(obj["text"])
            elif isinstance(obj, dict) and isinstance(obj.get("prompt"), str) and isinstance(obj.get("completion"), str):
                out.append(obj["prompt"] + obj["completion"])
            else:
                raise kitrt.KitError("invalid_input", f"row {n} needs text, or prompt and completion")
            if len(out) > MAX_ROWS:
                raise kitrt.KitError("invalid_input", "too many rows")
    if not out:
        raise kitrt.KitError("invalid_input", "empty dataset")
    return out


def plan_accumulation(global_bs: int, micro_bs: int, world: int) -> int:
    """Gradient accumulation steps so that micro * world * accum == global. Refuses anything inexact."""
    per_step = micro_bs * world
    if world < 1 or per_step < 1 or global_bs % per_step != 0:
        raise kitrt.KitError("invalid_param", f"global_batch_size {global_bs} is not a multiple of micro_batch_size*world_size {per_step}")
    return global_bs // per_step


def torchrun_cmd(env: dict, script: str) -> list[str]:
    """Launcher command line from the environment. Multi-node needs every rendezvous variable; a partial set is refused."""
    gpus = int(env.get("COMPUTECOIN_GPU_COUNT", "1") or "1")
    if not 1 <= gpus <= MAX_GPUS:
        raise kitrt.KitError("invalid_param", "unsupported GPU count")
    nnodes = int(env.get("NNODES", "1") or "1")
    cmd = [sys.executable, "-m", "torch.distributed.run", f"--nproc_per_node={gpus}"]
    if nnodes > 1:
        for k in ("MASTER_ADDR", "MASTER_PORT", "NODE_RANK"):
            if not env.get(k):
                raise kitrt.KitError("rendezvous_missing", k)
        cmd += [f"--nnodes={nnodes}", f"--node_rank={int(env['NODE_RANK'])}",
                f"--master_addr={env['MASTER_ADDR']}", f"--master_port={int(env['MASTER_PORT'])}"]
    else:
        cmd += ["--standalone"]
    return cmd + [script]


def launcher(params: dict) -> dict:
    kitrt.verify_manifest(WEIGHTS, MANIFEST)
    load_examples(kitrt.input_file("dataset"))  # fail early, before any GPU work
    gpus = int(os.environ.get("COMPUTECOIN_GPU_COUNT", "1") or "1")
    nnodes = int(os.environ.get("NNODES", "1") or "1")
    plan_accumulation(params["global_batch_size"], params["micro_batch_size"], gpus * nnodes)
    cmd = torchrun_cmd(dict(os.environ), os.path.abspath(__file__))
    rc = subprocess.run(cmd, check=False, env={**os.environ, "COMPUTECOIN_ROLE": "worker"}).returncode
    if rc != 0:
        raise kitrt.KitError("training_failed", f"torchrun exited with {rc}")
    if int(os.environ.get("NODE_RANK", "0") or "0") != 0:
        return {"kit": "llm-finetune-lora", "rank_node": int(os.environ["NODE_RANK"]), "note": "outputs are written by node 0"}
    with open(os.path.join(kitrt.OUT_DIR, "metrics.json"), "r", encoding="utf-8") as fh:
        metrics = json.load(fh)
    return {"kit": "llm-finetune-lora", "world_size": gpus * nnodes, **metrics}


def worker(params: dict) -> None:  # pragma: no cover - needs GPUs, torch, peft
    import torch
    import torch.distributed as dist
    from peft import LoraConfig, get_peft_model
    from torch.nn.parallel import DistributedDataParallel as DDP
    from transformers import AutoModelForCausalLM, AutoTokenizer

    rank, world, local = int(os.environ["RANK"]), int(os.environ["WORLD_SIZE"]), int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(local)
    dist.init_process_group("nccl")
    torch.manual_seed(params["seed"] + rank)
    torch.use_deterministic_algorithms(True, warn_only=True)
    accum = plan_accumulation(params["global_batch_size"], params["micro_batch_size"], world)
    texts = load_examples(kitrt.input_file("dataset"))
    base = os.path.join(WEIGHTS, "model")
    tok = AutoTokenizer.from_pretrained(base, local_files_only=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(base, local_files_only=True, torch_dtype=torch.bfloat16).to(local)
    cfg = LoraConfig(r=params["lora_r"], lora_alpha=params["lora_alpha"], lora_dropout=params["lora_dropout"],
                     target_modules="all-linear", task_type="CAUSAL_LM")
    model = get_peft_model(model, cfg)
    ddp = DDP(model, device_ids=[local])
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=params["learning_rate"])

    ckpt_dir = kitrt.CKPT_DIR
    adapter_dir = os.path.join(kitrt.OUT_DIR, "adapter")
    start_epoch, losses = 0, []
    done = kitrt.committed_files() if rank == 0 else {}
    state_rel = "train_state.pt"
    if kitrt.RESUME and state_rel in kitrt.committed_files():  # every rank reads the same trusted file
        st = torch.load(kitrt.committed_files()[state_rel], map_location="cpu", weights_only=True)
        if st["world_size"] != world:
            raise kitrt.KitError("invalid_param", "cannot resume with a different world size")
        start_epoch, losses = st["epoch"], st["losses"]
        model.load_state_dict(st["adapter"], strict=False)
        opt.load_state_dict(st["opt"])

    n = len(texts)
    per_rank = n // world  # drop the remainder so every rank steps the same number of times
    steps_per_epoch = max(1, per_rank // (params["micro_batch_size"] * accum))
    for epoch in range(start_epoch, params["epochs"]):
        g = torch.Generator().manual_seed(params["seed"] + epoch)  # same permutation on every rank
        order = torch.randperm(n, generator=g).tolist()[rank::world][:per_rank]
        ddp.train()
        for step in range(steps_per_epoch):
            tot = 0.0
            for a in range(accum):
                lo = (step * accum + a) * params["micro_batch_size"]
                idx = order[lo:lo + params["micro_batch_size"]] or order[:params["micro_batch_size"]]
                enc = tok([texts[i] for i in idx], padding=True, truncation=True, max_length=params["max_seq_len"],
                          return_tensors="pt").to(local)
                labels = enc["input_ids"].masked_fill(enc["attention_mask"] == 0, -100)
                loss = ddp(**enc, labels=labels).loss / accum
                loss.backward()
                tot += float(loss.detach())
            opt.step()
            opt.zero_grad(set_to_none=True)
            t = torch.tensor([tot], device=local)
            dist.all_reduce(t)
            losses.append(float(t.item()) / world)
        if rank == 0:
            os.makedirs(ckpt_dir, exist_ok=True)
            p = os.path.join(ckpt_dir, state_rel)
            torch.save({"epoch": epoch + 1, "losses": losses, "world_size": world, "opt": opt.state_dict(),
                        "adapter": {k: v for k, v in model.state_dict().items() if "lora_" in k}}, p + ".part")
            os.replace(p + ".part", p)
            kitrt.commit([p], epoch + 1)
        dist.barrier()
    if rank == 0:
        model.save_pretrained(adapter_dir)
        finite = all(math.isfinite(x) for x in losses)
        with open(os.path.join(kitrt.OUT_DIR, "metrics.json"), "w", encoding="utf-8") as fh:
            json.dump({"steps": len(losses), "first_loss": losses[0] if losses else None,
                       "final_loss": losses[-1] if losses else None, "finite": finite, "epochs": params["epochs"],
                       "accumulation": accum, "adapter_files": sorted(os.listdir(adapter_dir))}, fh, sort_keys=True)
    dist.destroy_process_group()


if __name__ == "__main__":
    if os.environ.get("COMPUTECOIN_ROLE") == "worker" and "RANK" in os.environ:
        _params = kitrt.load_params(SCHEMA)
        try:
            worker(_params)
        except kitrt.KitError as exc:
            print(f"kit error {exc.code}: {exc}", file=sys.stderr)
            sys.exit(2)
    else:
        kitrt.run_main(lambda: launcher(kitrt.load_params(SCHEMA)))
