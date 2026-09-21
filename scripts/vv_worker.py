#!/usr/bin/env python3
"""VibeVoice 生成子进程（ZOO-833 方案二：模型常驻内存的根治）。

父进程 edge-tts-server.py 通过 stdin/stdout 的 JSON 行协议派发生成请求，
音频经临时文件原子交付（避免半截输出被当成正常音频）。

为什么是子进程：实测模型加载后常驻 ~6.8GB（MPS），且原实现无任何卸载路径。
进程退出是唯一 100% 归还内存的方式，本 worker 由此获得两条退出路径：
  1. 空闲自退出：VV_IDLE_UNLOAD_SEC（默认 900s）无请求 → 退出（父进程看门狗为主，此处兜底）
  2. 孤儿自退出：父进程死亡（ppid 变化，macOS 上被 reparent 给 launchd）→ 立即退出，
     杜绝"服务重启后 6.8GB 孤儿进程残留"

协议（每行一个 JSON）：
  请求：{"id": N, "text": "...", "voice": "..."} 或 {"cmd": "quit"}
  回复：{"id": N, "ok": true, "wav": "/tmp/xxx.wav"} 或 {"id": N, "ok": false, "error": "..."}

模型在 worker 内懒加载（首个请求约 30-40s），文本预处理（标点兼容 + 数字归一化）
也在本进程完成，父进程保持纯路由（不 import torch）。
"""
import json
import os
import sys
import tempfile
import threading
import time

IDLE_EXIT_SEC = int(os.environ.get("VV_IDLE_UNLOAD_SEC", "900"))
POLL_SEC = 5
VIBEVOICE_DIR = os.environ.get("VIBEVOICE_DIR", "/Volumes/macext/code/demo/VibeVoice")
VIBEVOICE_MODEL = os.environ.get("VIBEVOICE_MODEL", "/Volumes/macext/llm-model/models/VibeVoice-1.5B")

# busy 期间绝不自退出（生成中途退出 = 半截输出）；last_active 由主循环维护
_STATE = {"busy": False, "last_active": time.time()}


def _watchdog():
    """双看门狗：父进程死亡 / 空闲超时。os._exit 保证即时终止（无需清理）。"""
    origin_ppid = os.getppid()
    while True:
        time.sleep(POLL_SEC)
        if os.getppid() != origin_ppid:
            print("[vv-worker] parent gone, self-exit", file=sys.stderr, flush=True)
            os._exit(0)
        if not _STATE["busy"] and time.time() - _STATE["last_active"] > IDLE_EXIT_SEC:
            print(f"[vv-worker] idle {IDLE_EXIT_SEC}s, self-exit", file=sys.stderr, flush=True)
            os._exit(0)


# ---------------------------------------------------------------------------
# 文本预处理（自 edge-tts-server.py 迁移，逻辑不变：VibeVoice 专用，Edge 通道不走）
# ---------------------------------------------------------------------------
import re as _re

_PUNCT_MAP = str.maketrans({
    "，": ",", "。": ".", "！": "!", "？": "?", "；": ";", "：": ",",
    "（": ",", "）": ",", "「": "", "」": "", "『": "", "』": "",
    "“": "", "”": "", "‘": "", "’": "",
    "…": ",", "‥": ",", "—": ",", "～": ",", "〜": ",",
    "、": ",", "｜": ",", "|": ",", "·": "", "‧": "", "•": "",
})

_VV_ALLOWED = _re.compile(r"[^一-鿿A-Za-z0-9,.:;!?%()'\- ]")


def sanitize_punct(text: str) -> str:
    t = text.translate(_PUNCT_MAP)
    t = _VV_ALLOWED.sub("", t)
    t = _re.sub(r",\s*,+", ",", t)
    t = _re.sub(r"\s+", " ", t).strip()
    t = _re.sub(r"(?<=[一-鿿]) +(?=[一-鿿A-Za-z0-9,.])", "", t)
    t = _re.sub(r"(?<=[A-Za-z0-9,.]) +(?=[一-鿿])", "", t)
    t = _re.sub(r"^,+|,+$", "", t).strip()
    return t


_DIGITS_CN = "零一二三四五六七八九"


def _cn_num(num_str: str) -> str:
    try:
        from cn2an import an2cn

        return an2cn(num_str)
    except Exception:
        return num_str


def normalize_numbers(text: str) -> str:
    text = _re.sub(
        r"(?<![0-9])(\d{4})年",
        lambda m: "".join(_DIGITS_CN[int(c)] for c in m.group(1)) + "年",
        text,
    )
    text = _re.sub(r"(\d+(?:\.\d+)?)%", lambda m: "百分之" + _cn_num(m.group(1)), text)

    def _other(m):
        s, e = m.span()
        before = text[s - 1] if s > 0 else ""
        after = text[e] if e < len(text) else ""
        if (before.isascii() and before.isalpha()) or (after.isascii() and after.isalpha()):
            return m.group(0)
        return _cn_num(m.group(0))

    return _re.sub(r"\d+(?:\.\d+)?", _other, text)


# ---------------------------------------------------------------------------
# 模型（懒加载，参数与原 in-process 实现完全一致）
# ---------------------------------------------------------------------------
_MODEL = {"model": None, "processor": None}


def _ensure_model():
    if _MODEL["model"] is not None:
        return
    sys.path.insert(0, VIBEVOICE_DIR)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    import torch
    from vibevoice.modular.modeling_vibevoice_inference import (
        VibeVoiceForConditionalGenerationInference,
    )
    from vibevoice.processor.vibevoice_processor import VibeVoiceProcessor

    print(f"[vv-worker] loading model from {VIBEVOICE_MODEL} ...", file=sys.stderr, flush=True)
    t0 = time.time()
    processor = VibeVoiceProcessor.from_pretrained(VIBEVOICE_MODEL)
    model = VibeVoiceForConditionalGenerationInference.from_pretrained(
        VIBEVOICE_MODEL,
        torch_dtype=torch.bfloat16,
        device_map=None,
        attn_implementation="sdpa",
    )
    model = model.to("mps")
    model.eval()
    model.set_ddpm_inference_steps(num_steps=10)
    _MODEL["model"], _MODEL["processor"] = model, processor
    print(f"[vv-worker] model ready ({time.time() - t0:.0f}s)", file=sys.stderr, flush=True)


def _generate(text: str, voice_wav: str) -> str:
    """生成并写临时 wav，返回路径（fsync 后重命名，原子交付）。"""
    import io
    import wave

    import numpy as np
    import torch

    _ensure_model()
    inputs = _MODEL["processor"](
        text=[text],
        voice_samples=[[voice_wav]],
        padding=True,
        return_tensors="pt",
        return_attention_mask=True,
    )
    outputs = _MODEL["model"].generate(
        **inputs,
        max_new_tokens=None,
        cfg_scale=1.3,
        tokenizer=_MODEL["processor"].tokenizer,
        generation_config={"do_sample": False},
    )
    audio = outputs.speech_outputs[0]
    if isinstance(audio, torch.Tensor):
        audio = audio.detach().cpu().to(torch.float32).numpy()
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()

    fd, tmp = tempfile.mkstemp(suffix=".vv.part.wav")
    with os.fdopen(fd, "wb") as f:
        with wave.open(f, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(pcm)
        f.flush()
        os.fsync(f.fileno())
    final = tmp.replace(".part.wav", ".wav")
    os.rename(tmp, final)
    return final


def _handle(req: dict) -> dict:
    voice_wav = os.path.join(VIBEVOICE_DIR, "demo", "voices", f"{req['voice']}.wav")
    if not os.path.isfile(voice_wav):
        return {"id": req.get("id"), "ok": False,
                "error": f"FileNotFoundError: VibeVoice 音色不存在: {req['voice']}（缺 {voice_wav}）"}
    text = "Speaker 1: " + normalize_numbers(sanitize_punct(req["text"].strip()))
    wav = _generate(text, voice_wav)
    return {"id": req.get("id"), "ok": True, "wav": wav}


def main():
    threading.Thread(target=_watchdog, daemon=True).start()
    print(f"[vv-worker] ready pid={os.getpid()} idle_exit={IDLE_EXIT_SEC}s", file=sys.stderr, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stdout.write(json.dumps({"ok": False, "error": f"bad request: {e}"}) + "\n")
            sys.stdout.flush()
            continue
        if req.get("cmd") == "quit":
            print("[vv-worker] quit by parent", file=sys.stderr, flush=True)
            break
        _STATE["busy"] = True
        try:
            reply = _handle(req)
        except Exception as e:  # 任何异常都回复并继续活着（父进程决定是否重试）
            reply = {"id": req.get("id"), "ok": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            _STATE["busy"] = False
            _STATE["last_active"] = time.time()
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
