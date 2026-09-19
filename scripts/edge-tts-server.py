#!/usr/bin/env python3
"""edge-tts + VibeVoice 双引擎的 OpenAI 兼容包装服务。

把语音合成暴露成 POST /v1/audio/speech（OpenAI TTS 格式），
供 OpenMAIC 的 TTS_OPENAI 通道使用。两个引擎按 voice 名自动路由：

  Edge 引擎（默认，免费秒出）：
    alloy/nova/shimmer/... 见下方 VOICE_MAP（中文神经语音）

  VibeVoice 引擎（本地大模型，质量高，RTF 约 2x，首次加载约 40s）：
    vv-xinran   → zh-Xinran_woman  中文女声，清晰自然（推荐）
    vv-bowen    → zh-Bowen_man     中文男声，沉稳
    vv-anchen   → zh-Anchen_man_bgm 中文男声，带 BGM 氛围
    vv-<自定义名> → demo/voices/<自定义名>.wav（语音克隆：放 5-10s 清晰人声
                   样音即可作为新音色，详见 VibeVoice 仓库 VOICES-ZH.md）

用法（必须用 VibeVoice 的 venv 启动，两引擎共用）：
  /path/to/VibeVoice/venv/bin/python edge-tts-server.py     # 监听 8500
  PORT=9000 ...                                              # 自定义端口

环境变量：
  VIBEVOICE_DIR   VibeVoice 代码目录（默认本机路径）
  VIBEVOICE_MODEL 1.5B 权重目录（默认本机路径）
  VIBEVOICE_DISABLED=1  禁用 VibeVoice 引擎（纯 edge 模式，用系统 python 即可）

注意：VibeVoice 引擎忽略 speed 参数（Edge 引擎保持 1.25x 固定倍率）。
"""
import io
import os

import edge_tts
import uvicorn
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="edge-tts OpenAI-compatible server")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# OpenAI 音色目录 → Edge 中文神经语音 的完整映射
# （edge-tts 免费通道当前仅 8 个中文音色：晓晓/晓伊/云健/云希/云夏/云扬 + 东北晓北/陕西晓妮）
VOICE_MAP = {
    # 女声
    "alloy": "zh-CN-XiaoxiaoNeural",      # 晓晓·温暖女声（默认）
    "nova": "zh-CN-YunxiaNeural",         # 云夏·清亮女声
    "shimmer": "zh-CN-liaoning-XiaobeiNeural",  # 晓北·东北口音女声
    "coral": "zh-CN-shaanxi-XiaoniNeural",      # 晓妮·陕西口音女声
    "sage": "zh-CN-XiaoyiNeural",         # 晓伊·少女音
    "marin": "zh-CN-XiaoxiaoNeural",      # （并入晓晓）
    # 男声
    "onyx": "zh-CN-YunxiNeural",          # 云希·阳光男声
    "ash": "zh-CN-YunjianNeural",         # 云健·浑厚男声
    "echo": "zh-CN-YunyangNeural",        # 云扬·新闻播音男声
    "fable": "zh-CN-YunxiNeural",         # （并入云希）
    "ballad": "zh-CN-YunyangNeural",      # （并入云扬）
    "cedar": "zh-CN-YunjianNeural",       # （并入云健）
    # 语义别名（兼容直接传语义名）
    "female": "zh-CN-XiaoxiaoNeural",
    "male": "zh-CN-YunxiNeural",
    "child": "zh-CN-XiaoyiNeural",        # 晓伊·童声
    "tongtong": "zh-CN-XiaoxiaoNeural",
}
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


class SpeechRequest(BaseModel):
    model: str = "edge-tts"
    input: str
    voice: str = "alloy"
    response_format: str = "mp3"
    speed: float = 1.0


def resolve_voice(name: str) -> str:
    n = (name or "").lower().strip()
    if n in VOICE_MAP:
        return VOICE_MAP[n]
    # 已经是 Edge 语音名就直接用
    if n.startswith("zh-") or n.startswith("en-"):
        return name
    # 未知名字按语义兜底
    if "male" in n or "yun" in n:
        return VOICE_MAP["male"]
    if "child" in n:
        return VOICE_MAP["child"]
    return DEFAULT_VOICE


# ZOO-474：owner 确认视频配音固定为正常语速的 1.25 倍。
# 为零侵入 OpenMAIC 应用源代码（改应用需重新 build 并重启主服务），
# 语速在本服务统一固定：忽略请求中的 speed，一律按 TTS_SPEED 倍率合成，
# 换算 rate = (TTS_SPEED - 1) × 100%（1.25 → "+25%"）。
# 历史遗留：原换算误把倍率当偏移量（speed=1.0 被换成 "+100%" 即 2 倍速），
# 即最初"配音语速过快"的根因，本换算已修正。
# 微调语速：改下方默认值或设 TTS_SPEED 环境变量，重启本进程即可（秒级）。
TTS_SPEED = float(os.environ.get("TTS_SPEED", "1.25"))


def speed_to_rate() -> str:
    offset = max(int(round((TTS_SPEED - 1.0) * 100)), -100)
    return f"{offset:+d}%"


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest):
    # VibeVoice 引擎路由（vv-* 或 zh-<Name> 格式；慢但质量高，RTF 约 2x）
    if not VIBEVOICE_DISABLED:
        vv_voice = vv_resolve_voice(req.voice)
        if vv_voice is not None:
            from starlette.concurrency import run_in_threadpool

            wav = await run_in_threadpool(vv_generate, req.input, vv_voice)
            if req.response_format == "wav":
                return Response(content=wav, media_type="audio/wav")
            return Response(content=_wav_to_mp3(wav), media_type="audio/mpeg")

    # Edge 引擎（默认）
    voice = resolve_voice(req.voice)
    rate = speed_to_rate()
    buf = io.BytesIO()
    communicate = edge_tts.Communicate(req.input, voice, rate=rate)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buf.write(chunk["data"])
    buf.seek(0)
    return Response(content=buf.read(), media_type="audio/mpeg")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "provider": "edge-tts + vibevoice",
        "vibevoice": {"enabled": not VIBEVOICE_DISABLED, "loaded": _VV["model"] is not None},
        "vibevoice_voices": ["vv-xinran", "vv-bowen", "vv-anchen", "vv-<自定义克隆音色>"],
    }


# ---------------------------------------------------------------------------
# VibeVoice 引擎（本地 1.5B 大模型 TTS + 零样本克隆）
# 懒加载：首个 vv-* 请求才初始化（约 40s，常驻约 5GB 内存）
# ---------------------------------------------------------------------------
VIBEVOICE_DISABLED = os.environ.get("VIBEVOICE_DISABLED") == "1"
VIBEVOICE_DIR = os.environ.get("VIBEVOICE_DIR", "/Volumes/macext/code/demo/VibeVoice")
VIBEVOICE_MODEL = os.environ.get("VIBEVOICE_MODEL", "/Volumes/macext/llm-model/models/VibeVoice-1.5B")

# OpenAI 音色名 → VibeVoice 内置音色
VV_VOICE_MAP = {
    "vv-xinran": "zh-Xinran_woman",
    "vv-bowen": "zh-Bowen_man",
    "vv-anchen": "zh-Anchen_man_bgm",
}
_VV = {"model": None, "processor": None, "lock": __import__("threading").Lock()}
# 生成串行锁（2026-09-20 根治并发崩溃）：全局同一时刻只允许一个 VibeVoice 生成。
# 并发叠跑曾在双课堂重叠时打爆内存（abort 134），排队等待的请求只占线程栈内存，安全。
# 前提：openMAIC 侧需 TTS_REQUEST_TIMEOUT_MS 调大（排队等待也计入其请求超时）。
_VV_GEN_LOCK = __import__("threading").Lock()

# VibeVoice 官方建议：中文用英文标点，服务端自动归一化
_PUNCT_MAP = str.maketrans({
    "，": ",", "。": ".", "！": "!", "？": "?", "；": ";", "：": " ",
    "（": " ", "）": " ", "「": " ", "」": " ", "“": " ", "”": " ",
})

# 数字归一化（2026-09-20）：VibeVoice 训练/推理均不做 text normalization（官方 FAQ Q3），
# 阿拉伯数字与 % 会被念成英文（1977 → "nineteen seventy-seven"，77% → 英文）。
# 中文旁白场景统一在服务端转汉字；Edge 引擎自带归一化，不走这段。
import re as _re

_DIGITS_CN = "零一二三四五六七八九"


def _cn_num(num_str: str) -> str:
    """权位读法（77→七十七，3.5→三点五），cn2an 失败时原样返回。"""
    try:
        from cn2an import an2cn

        return an2cn(num_str)
    except Exception:
        return num_str


def normalize_numbers(text: str) -> str:
    # 1) 年份：恰好 4 位数字+「年」→ 逐位读（1977年 → 一九七七年）
    text = _re.sub(
        r"(?<![0-9])(\d{4})年",
        lambda m: "".join(_DIGITS_CN[int(c)] for c in m.group(1)) + "年",
        text,
    )
    # 2) 百分比：77% → 百分之七十七
    text = _re.sub(r"(\d+(?:\.\d+)?)%", lambda m: "百分之" + _cn_num(m.group(1)), text)
    # 3) 其余数字（含小数）→ 权位读法；紧邻英文字母的不动（GPT-4 / iPhone 类）
    def _other(m):
        s, e = m.span()
        before = text[s - 1] if s > 0 else ""
        after = text[e] if e < len(text) else ""
        if (before.isascii() and before.isalpha()) or (after.isascii() and after.isalpha()):
            return m.group(0)
        return _cn_num(m.group(0))

    return _re.sub(r"\d+(?:\.\d+)?", _other, text)


def vv_resolve_voice(name: str) -> str | None:
    """返回 VibeVoice 音色名；不是 VibeVoice 音色则 None。"""
    n = (name or "").strip()
    low = n.lower()
    if low in VV_VOICE_MAP:
        return VV_VOICE_MAP[low]
    if low.startswith("vv-"):
        return n[3:]  # vv-<自定义克隆音色名> → demo/voices/<名>.wav
    if n.startswith("zh-") and not n.startswith("zh-CN"):
        # VibeVoice 音色命名（zh-Xinran_woman）；Edge 中文语音是 zh-CN-*
        return n
    return None


def _vv_init():
    """懒加载 VibeVoice 模型（线程安全单例）。"""
    if _VV["model"] is not None:
        return
    with _VV["lock"]:
        if _VV["model"] is not None:
            return
        import sys

        sys.path.insert(0, VIBEVOICE_DIR)
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

        import torch
        from vibevoice.modular.modeling_vibevoice_inference import (
            VibeVoiceForConditionalGenerationInference,
        )
        from vibevoice.processor.vibevoice_processor import VibeVoiceProcessor

        print(f"[vibevoice] loading model from {VIBEVOICE_MODEL} ...")
        processor = VibeVoiceProcessor.from_pretrained(VIBEVOICE_MODEL)
        model = VibeVoiceForConditionalGenerationInference.from_pretrained(
            VIBEVOICE_MODEL,
            torch_dtype=torch.bfloat16,
            device_map=None,
            attn_implementation="sdpa",  # Mac 无 CUDA 注意力，用 sdpa
        )
        model = model.to("mps")
        model.eval()
        model.set_ddpm_inference_steps(num_steps=10)
        _VV["model"], _VV["processor"] = model, processor
        print("[vibevoice] model ready")


def vv_generate(text: str, voice: str) -> bytes:
    """同步生成，返回 wav 字节（24kHz int16 单声道）。"""
    import numpy as np
    import torch

    _vv_init()
    voice_wav = os.path.join(VIBEVOICE_DIR, "demo", "voices", f"{voice}.wav")
    if not os.path.isfile(voice_wav):
        raise FileNotFoundError(
            f"VibeVoice 音色不存在: {voice}（缺 {voice_wav}；克隆音色请把 5-10s 人声样音放到该目录，文件名即音色名）"
        )

    text = "Speaker 1: " + normalize_numbers(text.strip().translate(_PUNCT_MAP))
    # 串行锁包住 编码+生成+解码 全程（不只是 generate）：processor 编码也吃内存
    with _VV_GEN_LOCK:
        return _vv_generate_locked(text, voice_wav)


def _vv_generate_locked(text: str, voice_wav: str) -> bytes:
    import numpy as np
    import torch

    inputs = _VV["processor"](
        text=[text],
        voice_samples=[[voice_wav]],
        padding=True,
        return_tensors="pt",
        return_attention_mask=True,
    )
    outputs = _VV["model"].generate(
        **inputs,
        max_new_tokens=None,
        cfg_scale=1.3,
        tokenizer=_VV["processor"].tokenizer,
        generation_config={"do_sample": False},
    )
    audio = outputs.speech_outputs[0]
    if isinstance(audio, torch.Tensor):
        audio = audio.detach().cpu().to(torch.float32).numpy()  # bf16 需先转 float32
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()

    import wave

    with io.BytesIO() as buf:
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(pcm)
        return buf.getvalue()


def _wav_to_mp3(wav_bytes: bytes) -> bytes:
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        wav_path = f.name
    try:
        return subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", wav_path, "-b:a", "128k", "-f", "mp3", "pipe:1"],
            capture_output=True,
            check=True,
        ).stdout
    finally:
        os.unlink(wav_path)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8500)))
