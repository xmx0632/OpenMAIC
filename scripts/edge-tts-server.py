#!/usr/bin/env python3
"""edge-tts + MiMo + VibeVoice 三引擎的 OpenAI 兼容包装服务。

把语音合成暴露成 POST /v1/audio/speech（OpenAI TTS 格式），
供 OpenMAIC 的 TTS_OPENAI 通道使用。三个引擎按 voice 名自动路由：

  Edge 引擎（默认，免费秒出）：
    alloy/nova/shimmer/... 见下方 VOICE_MAP（中文神经语音）

  MiMo 引擎（小米 MiMo 在线 API，音质好；API Key 已内置默认值，可用 MIMO_API_KEY 覆盖）：
    mimo-冰糖 / mimo-茉莉 / mimo-苏打 / mimo-白桦        预置中文音色
    mimo-Mia / mimo-Chloe / mimo-Milo / mimo-Dean        预置英文音色
    mimo-clone-<样音名> → demo/voices/<样音名>.wav        音色克隆（参考音频 5-10s
                   清晰人声，wav/mp3/m4a/flac 均可；样音库为项目内相对目录
                   demo/voices，与 vv-<自定义名> 克隆音色共用，可用 CLONE_VOICE_DIR 覆盖）

  VibeVoice 引擎（本地大模型，质量高，RTF 约 2x，worker 冷加载约 30-40s）：
    vv-xinran   → zh-Xinran_woman  中文女声，清晰自然（推荐）
    vv-bowen    → zh-Bowen_man     中文男声，沉稳
    vv-anchen   → zh-Anchen_man_bgm 中文男声，带 BGM 氛围
    vv-<自定义名> → demo/voices/<自定义名>.wav（语音克隆：放 5-10s 清晰人声
                   样音即可作为新音色，详见 VibeVoice 仓库 VOICES-ZH.md）

  ZOO-833 子进程化（2026-09-22）：生成在独立 worker 进程（vv_worker.py）中进行，
  模型常驻 ~6.8GB 的问题随之根治——worker 空闲自动退出，进程退出即全额归还内存；
  本服务进程只做路由，不再 import torch，常驻 footprint ~80MB。

用法（Windows 推荐用本目录独立 venv 启动，三引擎共用）：
  scripts/venv/Scripts/python.exe scripts/edge-tts-server.py   # Windows，监听 8500
  /path/to/VibeVoice/venv/bin/python edge-tts-server.py        # Mac/Linux 旧方式
  PORT=9000 ...                                                 # 自定义端口

环境变量（可写入脚本同目录 .env，真实环境变量优先）：
  MIMO_API_KEY     MiMo API Key（不设则用代码内置默认 Key，零配置可用）
  MIMO_BASE_URL    默认 https://api.xiaomimimo.com/v1
  CLONE_VOICE_DIR  克隆样音库目录（默认 <项目根>/demo/voices，vv- 与 mimo-clone- 共用）
  VIBEVOICE_DIR   VibeVoice 代码目录（macOS 沿用原固定路径 /Volumes/macext/code/demo/VibeVoice，
                  其他平台默认 <项目根>/../VibeVoice；仅启用 vv 引擎时需要）
  VIBEVOICE_MODEL 1.5B 权重目录（macOS 沿用原固定路径 /Volumes/macext/llm-model/models/VibeVoice-1.5B，
                  其他平台默认 <VIBEVOICE_DIR>/VibeVoice-1.5B）
  VIBEVOICE_DISABLED=1  禁用 VibeVoice 引擎（纯 edge+mimo 模式，依赖更少）
  VV_IDLE_UNLOAD_SEC=900   vv worker 空闲多少秒自动退出（默认 15 分钟）
  VV_REQUEST_TIMEOUT_SEC=1200  单次 vv 生成的父进程侧超时（防 worker 卡死）

注意：MiMo / VibeVoice 引擎忽略 speed 参数（Edge 引擎保持 1.25x 固定倍率）。
"""
import base64
import io
import json
import os
import subprocess
import sys
import threading
import time

import edge_tts
import uvicorn
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


def _load_env_file(path: str) -> None:
    """极简 .env 加载：KEY=VALUE（# 注释、可带引号）；不覆盖已存在的真实环境变量。"""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

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

    # MiMo 引擎路由（mimo-* 前缀；在线 API，音质好，需 MIMO_API_KEY）
    mimo_spec = mimo_resolve_voice(req.voice)
    if mimo_spec is not None:
        from starlette.concurrency import run_in_threadpool

        wav = await run_in_threadpool(mimo_generate, req.input, mimo_spec)
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
    p = _VV_WORKER["proc"]
    alive = p is not None and p.poll() is None
    idle = int(time.time() - _VV_WORKER["last_active"]) if alive else None
    return {
        "status": "ok",
        "provider": "edge-tts + mimo + vibevoice",
        "mimo": {
            "enabled": bool(MIMO_API_KEY),
            "key_source": "env" if os.environ.get("MIMO_API_KEY", "").strip() else "default",
            "voices": ["mimo-冰糖", "mimo-茉莉", "mimo-苏打", "mimo-白桦",
                        "mimo-Mia", "mimo-Chloe", "mimo-Milo", "mimo-Dean",
                        "mimo-clone-<样音名>"],
        },
        "vibevoice": {
            "enabled": not VIBEVOICE_DISABLED,
            "mode": "subprocess",
            "loaded": alive,  # 兼容旧字段名：worker 存活即"模型可用"
            "worker_running": alive,
            "worker_pid": p.pid if alive else None,
            "worker_idle_sec": idle,
        },
        "vibevoice_voices": ["vv-xinran", "vv-bowen", "vv-anchen", "vv-<自定义克隆音色>"],
    }


# ---------------------------------------------------------------------------
# 引擎公共配置（环境变量，.env 已在文件头加载）
# 目录一律用项目相对路径（基于 scripts/ 的上一级），os.path 拼接兼容 macOS/Windows；
# 机器相关的真实路径需要覆盖时写进 .env 或环境变量
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIBEVOICE_DISABLED = os.environ.get("VIBEVOICE_DISABLED") == "1"
# VibeVoice 目录/模型默认值按平台区分：macOS 沿用原部署的固定路径（原环境不配
# .env 也能直接跑）；其他平台（如 Windows）退回项目相对路径。均可用环境变量/.env 覆盖。
if sys.platform == "darwin":
    _VV_DIR_DEFAULT = "/Volumes/macext/code/demo/VibeVoice"
    _VV_MODEL_DEFAULT = "/Volumes/macext/llm-model/models/VibeVoice-1.5B"
else:
    _VV_DIR_DEFAULT = os.path.join(PROJECT_ROOT, os.pardir, "VibeVoice")
    _VV_MODEL_DEFAULT = os.path.join(_VV_DIR_DEFAULT, "VibeVoice-1.5B")
VIBEVOICE_DIR = os.environ.get("VIBEVOICE_DIR", _VV_DIR_DEFAULT)
VIBEVOICE_MODEL = os.environ.get("VIBEVOICE_MODEL", _VV_MODEL_DEFAULT)
# 克隆样音库（项目内相对目录）：vv-<名字> 与 mimo-clone-<名字> 共用同一套样音
CLONE_VOICE_DIR = os.environ.get(
    "CLONE_VOICE_DIR", os.path.join(PROJECT_ROOT, "demo", "voices")
)
VV_IDLE_UNLOAD_SEC = int(os.environ.get("VV_IDLE_UNLOAD_SEC", "900"))
VV_REQUEST_TIMEOUT_SEC = float(os.environ.get("VV_REQUEST_TIMEOUT_SEC", "1200"))


# ---------------------------------------------------------------------------
# MiMo 引擎（小米 MiMo 在线 API：预置音色 + 音色克隆）
# 按需创建 OpenAI 兼容客户端（线程安全单例）；未配 Key 时仅 mimo-* 请求报 503，
# 不影响 Edge / VibeVoice 引擎。文档:
# https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/audio/speech-synthesis-v2.5
# ---------------------------------------------------------------------------
# MiMo API Key：内置主人自有 Key 作为默认值（macOS/新环境零配置可用 mimo-*）；
# 需要换 Key 时用环境变量 MIMO_API_KEY 或 scripts/.env 覆盖
_MIMO_KEY_DEFAULT = "sk-c548dcue6vvym2aoq8obmbsbs9j0r3ui0hn5ogx3ge46wjda"
MIMO_API_KEY = (os.environ.get("MIMO_API_KEY") or _MIMO_KEY_DEFAULT).strip()
MIMO_BASE_URL = os.environ.get("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1").strip()
MIMO_TTS_MODEL = os.environ.get("MIMO_TTS_MODEL", "mimo-v2.5-tts").strip()
MIMO_CLONE_MODEL = os.environ.get("MIMO_CLONE_MODEL", "mimo-v2.5-tts-voiceclone").strip()

# 预置音色（官方文档，大小写不敏感匹配）
MIMO_VOICES = ["冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean"]
# 样音查找顺序 = 字典序：同名 mp3 优先于 wav（mp3 上传体积约 1/3~1/6，克隆质量 128k 足够），
# wav 保留作为 VibeVoice vv-<名> 的样音与 mimo 兜底；预转换命令：
#   ffmpeg -i <名>.wav -b:a 128k <名>.mp3
_MIMO_MIME = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".flac": "audio/flac"}

_MIMO = {"client": None, "lock": threading.Lock()}


def _mimo_client():
    if _MIMO["client"] is None:
        with _MIMO["lock"]:
            if _MIMO["client"] is None:
                try:
                    from openai import OpenAI
                except ImportError:
                    raise HTTPException(
                        status_code=503,
                        detail="openai 库未安装：请用 scripts/venv 中的 python 启动本服务",
                    )
                _MIMO["client"] = OpenAI(api_key=MIMO_API_KEY, base_url=MIMO_BASE_URL)
    return _MIMO["client"]


def _mimo_find_sample(name: str) -> str | None:
    """在 CLONE_VOICE_DIR 里按常见扩展名找样音文件。"""
    base = os.path.join(CLONE_VOICE_DIR, name)
    for ext in _MIMO_MIME:
        if os.path.isfile(base + ext):
            return base + ext
    return None


def mimo_resolve_voice(name: str) -> tuple[str, object] | None:
    """解析 mimo-* 音色，返回 ("preset", 音色名) 或 ("clone", 样音路径)；非 mimo-* 返回 None。"""
    n = (name or "").strip()
    if not n.lower().startswith("mimo-"):
        return None
    rest = n[5:]
    if n.lower().startswith("mimo-clone-"):
        sample = _mimo_find_sample(rest[6:])
        if sample is None:
            raise HTTPException(
                status_code=404,
                detail=f"MiMo 克隆样音不存在: {rest}（请在 {CLONE_VOICE_DIR} 放入 "
                        f"{rest}.wav，5-10s 清晰人声，或用 CLONE_VOICE_DIR 指定样音目录）",
            )
        return ("clone", sample)
    for v in MIMO_VOICES:
        if rest.lower() == v.lower():
            return ("preset", v)
    raise HTTPException(
        status_code=400,
        detail="未知 MiMo 音色: "
        + rest
        + "，可用: "
        + ", ".join("mimo-" + v for v in MIMO_VOICES)
        + " 或 mimo-clone-<样音名>",
    )


def mimo_generate(text: str, spec: tuple[str, object]) -> bytes:
    """同步调用 MiMo 合成，返回 wav 字节（24kHz）。spec 见 mimo_resolve_voice。"""
    if not MIMO_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="MiMo 未配置：请在 scripts/.env（或环境变量）设置 MIMO_API_KEY",
        )
    kind, val = spec
    if kind == "clone":
        model = MIMO_CLONE_MODEL
        mime = _MIMO_MIME.get(os.path.splitext(val)[1].lower(), "audio/wav")
        with open(val, "rb") as f:
            voice_param = f"data:{mime};base64,{base64.b64encode(f.read()).decode('utf-8')}"
    else:
        model = MIMO_TTS_MODEL
        voice_param = val
    completion = _mimo_client().chat.completions.create(
        model=model,
        messages=[
            {"role": "user", "content": ""},  # 风格指令，留空贴合音色默认风格
            {"role": "assistant", "content": text.strip()},  # 待合成文本（官方约定）
        ],
        audio={"format": "wav", "voice": voice_param},
    )
    return base64.b64decode(completion.choices[0].message.audio.data)


# ---------------------------------------------------------------------------
# VibeVoice 引擎（本地 1.5B 大模型 TTS + 零样本克隆）
# ZOO-833（2026-09-22）子进程化：生成在 vv_worker.py 独立进程中进行，
# 本进程不 import torch（常驻 ~80MB）；worker 空闲自动退出，内存随进程全额归还。
# 文本预处理（标点兼容 + 数字归一化）随模型一并迁入 worker。
# ---------------------------------------------------------------------------

# OpenAI 音色名 → VibeVoice 内置音色
VV_VOICE_MAP = {
    "vv-xinran": "zh-Xinran_woman",
    "vv-bowen": "zh-Bowen_man",
    "vv-anchen": "zh-Anchen_man_bgm",
}
# 生成串行锁（2026-09-20 根治并发崩溃，2026-09-22 子进程化后语义保留）：
# 全局同一时刻只允许一个 VibeVoice 生成 / 一个 worker 存活——
# 并发 N 个 worker = N×6.8GB，16GB 机器必炸（即 abort 134 现场的内存基数）。
# 排队等待的请求只占线程栈内存，安全；openMAIC 侧需 TTS_REQUEST_TIMEOUT_MS 调大（排队计入超时）。
_VV_GEN_LOCK = threading.Lock()
_VV_WORKER = {"proc": None, "busy": False, "last_active": time.time(), "seq": 0}

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


def _vv_worker_proc() -> subprocess.Popen:
    """取存活 worker；没有则拉起（模型冷加载发生在 worker 内首个请求，约 30-40s）。"""
    p = _VV_WORKER["proc"]
    if p is not None and p.poll() is None:
        return p
    worker_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vv_worker.py")
    p = subprocess.Popen(
        [sys.executable, worker_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,  # 继承父进程 stderr → 模型加载日志进服务日志
        env={**os.environ, "VIBEVOICE_DIR": VIBEVOICE_DIR, "VIBEVOICE_MODEL": VIBEVOICE_MODEL,
             "CLONE_VOICE_DIR": CLONE_VOICE_DIR,
             "VV_IDLE_UNLOAD_SEC": str(VV_IDLE_UNLOAD_SEC)},
    )
    _VV_WORKER["proc"] = p
    print(f"[vv-worker] spawned pid={p.pid}", flush=True)
    return p


def _vv_request(payload: dict, timeout: float) -> dict:
    """发一行 JSON 请求，带截止时间读一行回复；超时/死亡时杀掉 worker 并抛错。

    worker 死亡（含被超时杀掉）不致命：下一个请求会自动重拉新 worker。
    """
    p = _vv_worker_proc()
    timed_out = {"flag": False}

    def _kill_on_timeout():
        timed_out["flag"] = True
        p.kill()

    killer = threading.Timer(timeout, _kill_on_timeout)
    try:
        killer.start()
        p.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        p.stdin.flush()
        line = p.stdout.readline().decode("utf-8")
    finally:
        killer.cancel()
    if not line:
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
        raise RuntimeError(
            f"vv-worker 无响应（pid={p.pid}, exit={p.poll()}, timeout={timed_out['flag']}）"
        )
    return json.loads(line)


def vv_generate(text: str, voice: str) -> bytes:
    """同步生成，返回 wav 字节（24kHz int16 单声道）。串行锁内完成整个派发。"""
    with _VV_GEN_LOCK:
        _VV_WORKER["busy"] = True
        _VV_WORKER["last_active"] = time.time()
        try:
            _VV_WORKER["seq"] += 1
            reply = _vv_request(
                {"id": _VV_WORKER["seq"], "text": text, "voice": voice},
                VV_REQUEST_TIMEOUT_SEC,
            )
            if not reply.get("ok"):
                raise RuntimeError(f"vv-worker: {reply.get('error')}")
            with open(reply["wav"], "rb") as f:
                wav = f.read()
            os.unlink(reply["wav"])
            return wav
        finally:
            _VV_WORKER["busy"] = False
            _VV_WORKER["last_active"] = time.time()


def _vv_idle_watchdog():
    """父进程侧空闲看门狗（主退出路径；worker 自身的空闲退出是兜底）。"""
    while True:
        time.sleep(15)
        p = _VV_WORKER["proc"]
        if p is None or p.poll() is not None or _VV_WORKER["busy"]:
            continue
        if time.time() - _VV_WORKER["last_active"] < VV_IDLE_UNLOAD_SEC:
            continue
        if not _VV_GEN_LOCK.acquire(blocking=False):
            continue  # 正有请求在跑/在排队，下轮再看
        try:
            print(f"[vv-worker] idle {VV_IDLE_UNLOAD_SEC}s, exiting pid={p.pid}（内存随进程归还）", flush=True)
            try:
                p.stdin.write(b'{"cmd": "quit"}\n')
                p.stdin.flush()
                p.wait(10)
            except Exception:
                p.kill()
                p.wait()
            _VV_WORKER["proc"] = None
        finally:
            _VV_GEN_LOCK.release()


threading.Thread(target=_vv_idle_watchdog, daemon=True).start()


def _tts_tmp_dir() -> str:
    """固定临时目录（2026-10-09，ZOO-2091 用户指定）：不再跟随 TMPDIR——
    服务从 Multica 任务会话启动会继承任务级 TMPDIR，任务结束后该目录被平台
    清理，此处写 wav/mkv 即 500。默认脚本旁 tts-tmp/（每次调用自愈重建），
    TTS_TMP_DIR 环境变量可覆盖，极端不可写时回退 /tmp。"""
    d = os.environ.get("TTS_TMP_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "tts-tmp")
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        return "/tmp"


def _wav_to_mp3(wav_bytes: bytes) -> bytes:
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".wav", dir=_tts_tmp_dir(), delete=False) as f:
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
