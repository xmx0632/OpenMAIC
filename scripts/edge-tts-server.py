#!/usr/bin/env python3
"""edge-tts 的 OpenAI 兼容包装服务。

把免费的微软 Edge 神经网络语音暴露成 POST /v1/audio/speech
（OpenAI TTS 格式），供 OpenMAIC 的 TTS_OPENAI 通道使用。

用法：
  python3 edge-tts-server.py            # 监听 8500
  PORT=9000 python3 edge-tts-server.py

voice 映射：请求里的 voice 名会被宽松匹配到 Edge 中文语音，
默认女声 Xiaoxiao，含 "male"/"yun" 映射男声 Yunxi。
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


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest):
    voice = resolve_voice(req.voice)
    rate = f"{int((req.speed or 1.0) * 100):+d}%"
    buf = io.BytesIO()
    communicate = edge_tts.Communicate(req.input, voice, rate=rate)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buf.write(chunk["data"])
    buf.seek(0)
    return Response(content=buf.read(), media_type="audio/mpeg")


@app.get("/health")
async def health():
    return {"status": "ok", "provider": "edge-tts"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8500)))
