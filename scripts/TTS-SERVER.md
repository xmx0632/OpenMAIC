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

## 运行时切换音色（免重启）

生成请求体加 `ttsVoice` 字段即可，优先级：请求参数 > 环境变量 TTS_VOICE > 提供商默认（alloy）。

```bash
curl -X POST /api/generate-classroom -d '{...,"ttsVoice":"vv-xinran"}'
# openmaic-video-gen 脚本：--flags '{"ttsVoice":"vv-bowen"}'
```

可用值：alloy 等 Edge 音色；vv-xinran / vv-bowen / vv-anchen（VibeVoice）；vv-<自定义克隆音色>。

## 故障分析实录（2026-09-20，含崩溃现场还原）

### Anchen 音色的 BGM 从哪来

BGM 不是功能，是烧在样音文件里的：`zh-Anchen_man_bgm.wav` 本身就是混好背景音乐的人声样音（官方「氛围音色」产品线，另有 en-Alice_woman_bgm 同款）。零样本克隆会把样音完整声学场景（含音乐）一起学走。想要无 BGM：用 vv-xinran / vv-bowen（样音干净），或拿干净人声样音做克隆音色。

### 崩溃根因：多个 VibeVoice 任务并发重叠（2026-09-20 实锤）

现场日志：任务A（vv-xinran 32/34 收尾）与任务B（vv-anchen 1/34 开场）重叠约 5 分钟后服务崩溃（05:04），随后 4 次重启尝试 exit 1（内存未回收），05:08 重启成功。

机制：本服务生成路径**没有串行保护**（锁只在模型初始化）——uvicorn 线程池允许多个 vv_generate 并发叠跑；5GB 常驻模型 × N 份并发生成激活内存 + MPS 并发前向 → abort（exit 134/1）。单条生成本身健康（小片段 6-15s/条，RTF 正常）。

### 相邻隐患（同日代码分析，未触发但存在）

- openMAIC 对每个 TTS 请求默认只等 **30 秒**（`tts-providers.ts` DEFAULT_TTS_REQUEST_TIMEOUT_MS=30000）。长片段（>30s 音频）会超时报错。对策（免改代码）：openMAIC `.env.local` 加 `TTS_REQUEST_TIMEOUT_MS=600000`
- `TTS_MAX_TEXT_LENGTH` 映射表没有 openai-tts 条目 → 长口播不切分整段下发

### 运行纪律（防再崩）

1. **同一时间只跑一个 VibeVoice 音色的课程**；提交前 `tail` 服务日志确认无进行中的生成
2. 崩溃后重启失败（exit 1）= 内存未回收，等 1-2 分钟再试
3. 本服务无自动重启机制（无 launchd/crontab），崩了要手动拉起：
   `cd openmaic/scripts && nohup /path/to/VibeVoice/venv/bin/python edge-tts-server.py > /tmp/tts-server.log 2>&1 &`
4. ~~长期根治（生成串行锁）~~ **已于 2026-09-20 实施**：`_VV_GEN_LOCK` 串行锁包住 编码+生成+解码 全程，全局同一时刻仅一个 VibeVoice 生成，其余 FIFO 排队（排队只占线程栈内存，安全）。实测 3 并发请求 27.9/38.6/48.6s 交错完成、全部成功。**配套**：openMAIC `.env.local` 已加 `TTS_REQUEST_TIMEOUT_MS=600000`（排队等待计入请求超时，30s 默认值会误杀排队片段）。运行纪律第 1 条（同时只跑一个 vv 课程）仍建议遵守——串行锁保不崩，但两个课会互相拖慢
