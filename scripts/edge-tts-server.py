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

# 常用 Edge 中文神经语音
VOICE_MAP = {
    "female": "zh-CN-XiaoxiaoNeural",       # 女声·晓晓（默认）
    "male": "zh-CN-YunxiNeural",            # 男声·云希
    "child": "zh-CN-XiaoyiNeural",          # 童声·晓伊",
}
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


class SpeechRequest(BaseModel):
    model: str = "edge-tts"
    input: str
    voice: str = "female"
    response_format: str = "mp3"
    speed: float = 1.0


def resolve_voice(name: str) -> str:
    n = (name or "").lower()
    if not n or n in ("female", "tongtong", "default", "alloy"):
        return VOICE_MAP["female"]
    if "male" in n or "yun" in n or "guy" in n:
        return VOICE_MAP["male"]
    if "child" in n or "yi" in n:
        return VOICE_MAP["child"]
    # 已经是 Edge 语音名就直接用
    if n.startswith("zh-") or n.startswith("en-"):
        return name
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
