# TTS 双引擎服务说明（edge-tts + VibeVoice）

`scripts/edge-tts-server.py` 已升级为双引擎 OpenAI 兼容服务，openMAIC 的 `TTS_OPENAI` 通道（`TTS_OPENAI_BASE_URL=http://127.0.0.1:8500/v1`）零改动即可使用全部音色。

## 启动

```bash
# 必须用 VibeVoice 的 venv（两引擎依赖合一）
/Volumes/macext/code/demo/VibeVoice/venv/bin/python scripts/edge-tts-server.py
# 环境变量：PORT（默认8500）/ VIBEVOICE_DIR / VIBEVOICE_MODEL / VIBEVOICE_DISABLED=1（纯edge模式）
```

## 音色路由（按 voice 名自动分发）

### Edge 引擎（默认，免费秒出）

`alloy`（晓晓女声）/ `nova` / `onyx`（云希男声）等，完整映射见脚本内 VOICE_MAP。

### VibeVoice 引擎（本地 1.5B 大模型，质量高）

| voice 名 | 音色 | 特点 |
|----------|------|------|
| `vv-xinran` | 欣然（zh-Xinran_woman） | 中文女声，清晰自然，推荐 |
| `vv-bowen` | 博文（zh-Bowen_man） | 中文男声，沉稳 |
| `vv-anchen` | 安辰（zh-Anchen_man_bgm） | 中文男声，带 BGM 氛围 |
| `vv-<任意名>` | 自定义克隆音色 | 见下方扩展 |

性能：首次请求约 30-40s（模型加载，之后常驻约 5GB 内存），常驻后单条生成约为音频时长的 2 倍（RTF~2x）。中文标点自动转英文标点（官方建议）。忽略 speed 参数。

## 扩展：加入自己克隆的音色

1. 准备 5-10 秒清晰人声样音（单人、无 BGM），命名 `名字.wav`
2. 放入 `/Volumes/macext/code/demo/VibeVoice/demo/voices/`
3. 之后 `voice: "vv-名字"` 即可用该音色生成（零样本克隆，无需训练）

详细克隆指南：VibeVoice 仓库 `VOICES-ZH.md`（https://gitee.com/xmx0632/vibe-voice）

⚠️ 只克隆自己或获授权的声音；VibeVoice 输出自带「AI 生成」声明与水印。

## 依赖前提

- VibeVoice 代码库（含 Mac 补丁）：https://gitee.com/xmx0632/vibe-voice
- 1.5B 权重（5GB）+ Qwen2.5 分词器缓存（部署见该仓库 SETUP-ZH.md）
- ffmpeg（wav 转 mp3）
