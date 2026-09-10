# OpenMAIC 接入本地 edge-tts 语音合成服务

本文介绍如何让 OpenMAIC 使用微软 Edge 神经网络语音（免费、无需 API Key）作为语音合成（TTS）后端。实现方式是跑一个**本地转发服务**，把 Edge 语音包装成 OpenAI 兼容的 `/v1/audio/speech` 接口，再让 OpenMAIC 走内置的 `TTS_OPENAI` 通道调用它。

```
OpenMAIC (Docker 容器)
    │  POST http://host.docker.internal:8500/v1/audio/speech
    ▼
edge-tts 转发服务 (宿主机, scripts/edge-tts-server.py)
    │  WebSocket
    ▼
微软 Edge 神经网络语音服务（免费）
```

## 前置条件

- Python 3.10+（Windows/Linux/macOS 均可）
- OpenMAIC 已用 Docker Compose 部署（`docker compose up -d --build` 可正常访问）
- 宿主机能访问微软服务（edge-tts 走公网 WebSocket）

## 第一步：部署 edge-tts 转发服务

脚本已在仓库中：[`scripts/edge-tts-server.py`](../scripts/edge-tts-server.py)。

```bash
# 1. 安装依赖
pip install edge-tts fastapi "uvicorn[standard]"

# 2. 启动服务（默认监听 127.0.0.1:8500）
python scripts/edge-tts-server.py

# 自定义端口：
PORT=9000 python scripts/edge-tts-server.py
```

验证服务可用：

```bash
curl http://127.0.0.1:8500/health
# {"status":"ok","provider":"edge-tts"}
```

> 注意：服务默认只监听 `127.0.0.1`。Docker 容器通过 `host.docker.internal` 访问宿主机时，Docker Desktop 的网关进程会代转到宿主机回环地址，因此**不需要**改成 `0.0.0.0`。如果是 Linux 原生 Docker（非 Desktop），容器访问宿主机需改用 `--add-host=host.docker.internal:host-gateway` 并让服务监听 `0.0.0.0`（自行评估暴露风险）。

### 常驻运行（可选）

转发服务是普通 Python 进程，建议注册为系统服务或计划任务保证开机自启。例如 Windows 计划任务：

```powershell
schtasks /Create /TN "edge-tts-server" /SC ONSTART /RU SYSTEM ^
  /TR "python D:\path\to\OpenMAIC\scripts\edge-tts-server.py"
```

Linux 可用 systemd unit 或 `pm2`/`supervisor` 托管。

## 第二步：配置 OpenMAIC

编辑项目根目录的 `.env.local`（没有就从 `.env.example` 复制一份）：

```ini
# --- TTS：走本地 edge-tts 转发服务 ---
TTS_OPENAI_API_KEY=edge-tts-local
TTS_OPENAI_BASE_URL=http://host.docker.internal:8500/v1

# --- 放行本地/内网地址（SSRF 防护默认拦截私网 URL） ---
ALLOW_LOCAL_NETWORKS=true
```

三项都**必须**配置，缺一不可：

| 变量 | 说明 |
|------|------|
| `TTS_OPENAI_API_KEY` | 转发服务不校验 key，但 **必须填一个非空占位值**。OpenMAIC 只把 `voxcpm-tts`、`lemonade-tts` 列为免 key 通道，`openai-tts` 通道 key 为空时 provider 不会激活，设置页里看不到它。 |
| `TTS_OPENAI_BASE_URL` | 必须带 `/v1` 后缀（客户端代码拼 `${baseUrl}/audio/speech`）。容器内部访问宿主机用 `host.docker.internal`，**不能写 `localhost`**（那指向容器自己）。 |
| `ALLOW_LOCAL_NETWORKS=true` | OpenMAIC 有 SSRF 防护，默认拒绝一切私网/回环地址的出站请求。此开关只应在本机或可信内网部署时开启，**公网部署不要开**。 |

应用配置（Docker Compose 部署）：

```bash
docker compose up -d
```

这三个变量都是运行时变量，重建容器即可生效，**无需重新构建镜像**。

> 如果你是非 Docker 方式跑 OpenMAIC（本地 `pnpm dev`），`TTS_OPENAI_BASE_URL` 直接写 `http://127.0.0.1:8500/v1` 即可，`host.docker.internal` 仅用于容器访问宿主机的场景。

## 第三步：验证

命令行端到端验证（在仓库目录下）：

```bash
# 容器内访问转发服务
docker compose exec openmaic wget -qO- http://host.docker.internal:8500/health

# 合成一段中文
printf '{"input":"你好世界","voice":"female"}' > /tmp/tts-req.json
curl -s -o /tmp/tts.mp3 -w "HTTP %{http_code}, %{size_download} bytes\n" \
  -X POST http://127.0.0.1:8500/v1/audio/speech \
  -H "Content-Type: application/json" --data-binary @/tmp/tts-req.json
# 预期：HTTP 200, 数千字节
```

然后打开 OpenMAIC → 设置 → 语音合成，选择 `openai-tts` 通道，点"测试 TTS"。

### 语音映射

请求中的 `voice` 会被宽松匹配到 Edge 中文语音（见脚本内 `VOICE_MAP`）：

| 请求 voice | 实际语音 |
|-----------|---------|
| `female` / 空 / `alloy` / `tongtong` | zh-CN-XiaoxiaoNeural（晓晓·女声，默认） |
| 含 `male` / `yun` / `guy` | zh-CN-YunxiNeural（云希·男声） |
| 含 `child` / `yi` | zh-CN-XiaoyiNeural（晓伊·童声） |
| `zh-` / `en-` 开头的合法 Edge 语音名 | 直接使用 |

`model` 参数被忽略，输出固定为 mp3（`audio/mpeg`）。

## 可靠性增强：NoAudioReceived 重试

edge-tts 偶发抛出 `NoAudioReceived`（微软服务端抖动，时序令牌按 5 分钟窗口校验，时钟偏差会加剧）。当前仓库版本的脚本已内置 3 次自动重试（`scripts/edge-tts-server.py` 的 `speech` 处理器）。如果你用的是旧版脚本，可按下面方式改造：

```python
import asyncio

@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest):
    voice = resolve_voice(req.voice)
    rate = f"{int((req.speed or 1.0) * 100):+d}%"
    last_exc = None
    for _ in range(3):                      # 偶发失败自动重试
        try:
            buf = io.BytesIO()
            communicate = edge_tts.Communicate(req.input, voice, rate=rate)
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    buf.write(chunk["data"])
            buf.seek(0)
            return Response(content=buf.read(), media_type="audio/mpeg")
        except edge_tts.exceptions.NoAudioReceived as exc:
            last_exc = exc
            await asyncio.sleep(0.5)
    raise last_exc
```

另外保持 edge-tts 为新版（`pip install -U edge-tts`），旧版本在微软调整鉴权后会整体失效。

## 常见问题

**测试 TTS 报 `Local/private network URLs are not allowed`**
`ALLOW_LOCAL_NETWORKS=true` 没有生效。确认写进了 `.env.local` 后执行 `docker compose up -d` 重建容器；可用 `docker compose exec openmaic printenv ALLOW_LOCAL_NETWORKS` 确认容器内取值。

**设置页里看不到 openai-tts 通道**
`TTS_OPENAI_API_KEY` 是空的。填任意非空占位值（如 `edge-tts-local`）后 `docker compose up -d`。

**报 `NoAudioReceived: No audio was received`**
微软服务端偶发抖动或本机时钟偏差过大。使用带重试的脚本版本；仍频繁出现时校准系统时间（`w32tm /resync`）。

**命令行 curl 测试中文返回 400**
Windows 控制台默认 GBK 编码，会把命令行里的中文参数搞成非法 UTF-8。把请求体写成 UTF-8 文件用 `--data-binary @file.json` 发送（见上文验证步骤），这是测试方式问题，不是服务问题。

**端口被占用 / 想换端口**
启动时 `PORT=9000 python scripts/edge-tts-server.py`，同步修改 `.env.local` 里的 `TTS_OPENAI_BASE_URL`。

## 安全提醒

- `ALLOW_LOCAL_NETWORKS=true` 会放开 SSRF 防护对私网地址的限制，**仅限本机/可信内网使用**，公网可访问的部署不要开启。
- 转发服务无鉴权且允许任意合成请求，只绑定 `127.0.0.1` 即可避免被局域网滥用；不要为了方便改绑 `0.0.0.0` 暴露到局域网。
