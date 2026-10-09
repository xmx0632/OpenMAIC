> 本文件是 `~/.claude/skills/openmaic-video-gen/SKILL.md` 的仓库存档副本（版本同步用）；实际生效以本机 skills 目录为准，配套脚本在 skill 目录的 scripts/ 下。

---
name: openmaic-video-gen
description: "用本机 openMAIC 服务制作视频教程并导出 MP4/教学资源包的端到端流水线：提交生成→轮询→场景类型校验（全 slides、无 quiz/PBL）→封面结尾截图→导出教学资源包(内含PPTX+交互页,2026-09-21起不再单独导出PPTX)→渲染 MP4→落盘 /Volumes/macext/downloads（移动硬盘专用目录,未挂载时回退 ~/Downloads）。Triggers: 用 openMAIC 做视频教程、生成课堂、导出 mp4/教学资源包/pptx、制作课程视频、openmaic。"
license: MIT
---

# openMAIC 视频教程端到端制作与导出

## 触发场景

- 「用 openMAIC 做〈主题〉的视频教程」「生成一个课堂」「把这节课导出成 MP4 / 教学资源包」（PPTX 在教学资源包内，不单独导出）
- 已有课堂地址，只需重新导出或补导出文件

## 服务与环境（已验证事实，直接用）

- 本机服务 `http://localhost:3000`（OpenMAIC v1.0.1，`/Volumes/macext/code/demo/OpenMAIC`），默认模型 GLM `glm-5.3-flash`（超时已修复，ZOO-456），健康检查 `GET /api/health`
- 生成 API：`POST /api/generate-classroom`，body `{requirement, enableTTS, enableImageGeneration, enableVideoGeneration, enableWebSearch}` → 202 返回 `{jobId, pollUrl}`；轮询 pollUrl 30~60s 一次，`done:true`（或 `status:succeeded`）时 `result.url` 为课堂地址。**默认 `enableImageGeneration: true`**（ZOO-511 起接入硅基流动 Kwai-Kolors/Kolors，服务端已限流 2 张/分钟；关闭用 `--flags '{"enableImageGeneration":false}'`）。**配图由绘图codex 外部提供的流程（ZOO-515 默认）提交时必须显式传该 flags**——脚本默认 true 会混入内置生成图，F28（ZOO-756）曾因此占位图残留成片、被大类验收退回
- **出图单并发生成（ZOO-972 系列 2026-09-26 用户确认）**：给绘图codex 的出图单 requirement 必须写明「多张互不依赖的配图并发生成：同一脚本单元并发提交多个生图调用，每张仍是独立调用、独立核验，不要逐张串行」。背景：绘图codex 的调度并发（4）按「单」计，单内串行只是它的工作习惯、非平台限制（ZOO-1002 实测 2 张串行约 35 分钟，其中约 25 分钟是思考间隙）；并发可把关键路径压近单图耗时。首单先 2 并发试跑、稳定再加；不拆一图一单占多个调度位
- 耗时基线（2026-09-26 起生产启用场景并发 `PARALLEL_SCENE_CONCURRENCY=3`，ZOO-986 实测后改写）：大纲约 1.5 分钟 + 场景阶段（7 场景 G 规格）约 17~19 分钟（c=1 串行口径为 47 分钟）+ TTS/媒体/落盘，**6~7 场景课堂全程约 20~25 分钟**（旧口径「单场景 4~8 分钟、全程约 30 分钟」作废）；场景完成顺序可能乱序（日志 Scene k/N 行乱序属正常），入库与成片顺序仍严格按大纲序；**开配图时媒体阶段每张间隔 ≥33 秒串行生成（2 IPM 限流），10 页课程约多 5~6 分钟，属正常不要当成卡死**；**单 job 上限 45 分钟；严格串行，一次只跑一个 job**（并发是 job 内场景级，全局「一次一个 job」不变）
- MP4 渲染：`GET /api/export-video/capability` 返回 `enabled:true` 时可用（渲染走课堂 ZIP 上传 `/api/export-video/render`，由课堂页自动完成）；渲染时长≈课堂时长
- **PPTX 无服务端端点**（pptxgenjs 浏览器端组装），教学资源包与 MP4 的导出 ZIP 也在浏览器端组装——所以导出一律用 playwright 驱动课堂页点按钮，不要试图复刻前端请求
- 课堂页翻页用底部 "Next scene" 按钮（`button[aria-label="Next scene"]`），ArrowRight 无效
- 请求体一律由脚本 `JSON.stringify` 生成（等价 python3 json.dumps），中文内嵌引号手拼易坏 JSON
- API key / `.env.local` 内容绝不写入任何文档或评论
- **TTS 双引擎**（2026-09-19 起，`scripts/edge-tts-server.py`，文档 `openmaic/scripts/TTS-SERVER.md`）：Edge 秒出（alloy 晓晓女声等）+ VibeVoice 本地大模型（`vv-xinran` 欣然女声 / `vv-bowen` 博文男声 / `vv-anchen` 安辰带BGM / `vv-1`、`vv-2` 自定义克隆音色 / `vv-<自定义克隆音色>`）。服务须用 `/Volumes/macext/code/demo/VibeVoice/venv/bin/python` 启动；VibeVoice 首次请求约 40s 加载，常驻后 RTF~2x，适合高质量配音场景
- **运行时切音色（免重启）**：生成请求加 `ttsVoice` 字段（`--flags '{"ttsVoice":"vv-xinran"}'`），优先级：请求参数 > TTS_VOICE 环境变量 > 默认 alloy。实测 vv-xinran 全链路生效

## TTS 音色设置（2026-09-19 新增）

生成课程时可指定配音音色，三种方式按优先级生效：**请求参数 `ttsVoice` > 环境变量 `TTS_VOICE` > 默认 `alloy`**（晓晓女声）。全程无需重启服务。

```bash
# skill 脚本方式（推荐）
node openmaic-video.mjs all --requirement-file req.txt --flags '{"ttsVoice":"vv-bowen"}'

# API 直调方式
curl -X POST http://localhost:3000/api/generate-classroom \
  -H "Content-Type: application/json" \
  -d '{"requirement":"...","enableTTS":true,"ttsVoice":"vv-xinran", ...}'

# 全局默认方式（改 openmaic/.env.local 后重启 openMAIC 生效）
TTS_VOICE=vv-xinran
```

可用音色（走 8500 双引擎服务，详见 `openmaic/scripts/TTS-SERVER.md`）：

| 音色值 | 引擎 | 说明 |
|--------|------|------|
| `alloy` / `nova` / `onyx` 等 | Edge | 免费秒出（默认 alloy=晓晓女声） |
| `vv-xinran` | VibeVoice | 欣然女声，清晰自然，高质量推荐 |
| `vv-bowen` | VibeVoice | 博文男声，沉稳 |
| `vv-anchen` | VibeVoice | 安辰男声，带 BGM 氛围 |
| `vv-1` | VibeVoice | 自定义克隆音色①（3s 样本，2026-09-20 登记；私人声音仅限本人使用） |
| `vv-2` | VibeVoice | 自定义克隆音色②（10s 样本，登记同上；ZOO-785 已实测 ttsVoice 全链路生效，含 tts-verify） |
| `vv-<自定义名>` | VibeVoice | 克隆音色：5-10s 人声样音放 `VibeVoice/demo/voices/<名>.wav` 即用 |

注意：VibeVoice 音色首次请求约 40s 模型加载（之后常驻约 5GB 内存），单条生成约为音频时长 2 倍；整课 TTS 阶段会比 Edge 慢数分钟，属正常。Edge 音色适合快速迭代，VibeVoice 适合最终成片。

## 版本同步（改本 skill 后必做）

本 skill 另有一份仓库存档副本：`/Volumes/macext/code/demo/OpenMAIC/skills/openmaic-video-gen.md`（自述头两行 + 正文镜像）。**每次修改 SKILL.md 或 scripts 后，把正文同步过去**（保留存档说明头），避免两边漂移。

## 一句话用法

```bash
cd .claude/skills/openmaic-video-gen/scripts
echo '制作一节 5 分钟以内的中文课程：《……》。受众……控制在 6 个场景以内（含封面与结尾页）……' > /tmp/req.txt
node openmaic-video.mjs all --requirement-file /tmp/req.txt
```

`scripts/example-requirement.txt` 是一份完整可跑的需求正文示例（5 分钟科普课，已含"封面/结尾页/全 slides"的措辞），可直接 `--requirement-file example-requirement.txt` 试跑。

产出：`/Volumes/macext/downloads/<课程主题名>-<YYYYMMDD>.mp4` + `<课程主题名>-<YYYYMMDD>-教学资源包.zip`（**2026-09-21 用户指定：成片只导 MP4 + 教学资源包，不再单独导出 PPTX——资源包内已含 PPTX 与交互式页面**；命名规范：课程主题名 + 日期，主题名自动取课堂标题并清洗非法字符，如 `二维码为什么是三个定位角-20260912.mp4`；`--topic-id` 可显式覆盖前缀，仅 A/B 对照等需要短代号时用），截图与 `manifest.json` 在 `./openmaic-out/`。

## 端到端流程（脚本已固化，勿手工跳步）

1. **预检**：`/api/health` 服务在线；`/api/export-video/capability` 查渲染可用性（不可用自动降级为教学资源包+课堂 ZIP）
2. **提交生成**：requirement = 强制规范块（脚本自动拼接）+ 需求正文
3. **轮询**：30s 间隔，超 45 分钟判超时
4. **场景类型校验**（合规红线）：`GET /api/classroom?id=<id>` 读 `scenes[].type`，**必须全部为 `slide`**；发现 `quiz`/`interactive`/`pbl` → 脚本退出码 3，视为不合规需重生成（见下）。同时报告配图覆盖（X/Y 页有插图）；开启配图却为 0 张时打警告（多为硅基流动日额度耗尽，课程仍可用）
5. **聚光灯 soften（ZOO-551 起强制）**：脚本在校验后、任何页面访问/导出前，自动给全部 spotlight 动作写入 `dimOpacity=0.15`（幂等，双存储同步）。背景：默认 0.5 的暗幕在知识点高亮时画面大面积过暗，用户明确要求全线路按 0.15。openMAIC 侧默认值（引擎/描述符/store）已于 ZOO-551 一并改为 0.15 并重建，此处是数据级保险带；**不要用 `--no-constraints` 之类的方式绕过，也不要手工改回高暗度**
6. **TTS 读音校验（2026-09-20 起强制；仅 vv-\* VibeVoice 音色需要，Edge 音色跳过）**：本集口播用 vv-\* 时，导出前跑 `./scripts/tts-verify.py --classroom <id> --fix --voice <本集音色>`（whisper-medium 转写 + 拼音音节比对，坏 take 自动重录换掉，详见下文专节）；校验不过不导出，换过 take 必须重跑 export。Edge 音色（alloy/晓晓等）为确定性合成、无随机坏 take 模式，**不需要本步骤**
7. **截图质检**：第 1 页（封面）+ 最后一页（结尾）viewport 截图，供人工确认封面/结尾页质量
8. **导出教学资源包**：课堂页右上角导出菜单「导出教学资源包」（PPTX + 交互式页面，浏览器端组装 ZIP；**2026-09-21 用户指定不再单独导出 PPTX**，资源包内已含），命名 `<主题名>-<日期>-教学资源包.zip`，捕获浏览器下载
9. **导出 MP4**：菜单「导出视频」→ 弹窗「渲染 MP4」（默认 1080p/30fps/标准），等渲染完成捕获下载
10. **落盘回报**：文件复制到 `/Volumes/macext/downloads`（命名 `<课程主题名>-<YYYYMMDD>.<ext>`），输出结果清单（路径/大小/场景清单/截图）

分阶段续跑（生成与导出可拆开，例如生成后先人工看截图）：

```bash
node openmaic-video.mjs generate --requirement-file req.txt   # 只生成+校验
node openmaic-video.mjs export --url http://localhost:3000/classroom/<id>  # 只导出
```

## 强制制作规范（不可省略，脚本自动注入）

requirement 头部固定拼接以下约束块（`--no-constraints` 仅限调试，正式任务禁用）：

1. **全部场景使用演示文稿（slides）形式**——不得出现测验（quiz）、项目练习（PBL）、互动组件（interactive）；openMAIC 异步 API 没有大纲编辑中间点，这是唯一的前置控制手段，生成后必须再校验（脚本已做，双重保险）
2. **第 1 页必须是封面页**：标题 + 副标题 + 贴合主题的视觉版式，不允许白页/空白开场
3. **最后一页必须是结尾页**：总结 3~5 个要点 + 一句行动指引，不允许戛然而止
4. **配图规范**（ZOO-511）：封面页必须有 1 张贴合主题的插画，正文每页最多 1 张；统一扁平矢量插画风格、色调贴合主题；插画内不出现文字、不用照片风；**外部配图嵌入前统一展平为不透明 RGB**——视频渲染对 RGBA 透明通道 PNG 合成异常（错位变形，ZOO-791 曾重渲染 3 次定位），出图单注明「不透明背景」或预处理 `ffmpeg -i in.png -vf format=rgb24 out.png`
5. **口播开场规范**（用户 2026-09-16 指定，ZOO-548）：任何页的口播开头都不说「大家好」等问候语；整个视频开头不用「这节课我们……」课堂式说法，改用「今天我们来看看……」「我们来了解一下……」等自然表述；后续各页用过渡句直接进入内容
6. **口播字数硬约束**（ZOO-612 教训，2026-09-17）：每页口播 ≤150 字、全片合计 ≤900 字——F14 首次生成因无字数约束产出 9 分 52 秒超长片、付费重生成一次，此后作为注入块硬约束

需求正文写法见 `templates/requirement-template.md`（场景数上限要写"含封面与结尾页"）。

## TTS 读音校验（tts-verify，2026-09-20 起新增）

```bash
./scripts/tts-verify.py --classroom <id> [--fix] [--voice vv-xinran] [--takes 3] [--limit N] [--model medium]
# 共享 venv（/Volumes/macext/code/demo/tools/asr-venv）缺失时脚本自动引导安装（约 1 分钟，仅首次）
```

- **为什么**：VibeVoice 是扩散式生成 TTS，同一文本多次合成结果随机，偶发整段发糊/音节粘连/句尾杂音的坏 take（文本管线正确也会发生；F29 实测同句文字出现过 3.1s 与 5.0s 两个版本、4.8s 与 13s 失控版）。符号变音与数字念英文属文本管线问题，已在 8500 服务修复；采样坏 take 只能合成后校验兜住
- **怎么做**：whisper-medium 逐条转写课堂口播 mp3 → 与口播文本做**拼音音节级**比对（同音字转写差异不算错）→ 差异率超 15% 判坏；`--fix` 时对坏条目重录 `--takes` 个 take、逐个 ASR 复验，自动用零/最低差异 take 覆盖课堂音频文件（内含时长/字数 >0.55s/字 的失控看门狗；2026-09-20 G52 教训：**选 take 优先「rate 达标且语速在 0.17–0.33s/字 正常带」的理想 take 直接采用**——ASR 内容全对但语速拖到 0.39–0.45s/字 的慢速 take 会漏过 0.55 看门狗，人耳一听就是毛病）
- **纪律**：只有 vv-\*（VibeVoice）音色需要本步骤，出片前必跑 `--fix`；换过 take 的课堂必须重新 export 才进成片。Edge 音色（alloy/晓晓等）确定性合成、无坏 take 模式，直接跳过 tts-verify。**issue 规范强制要求 Edge 课堂也跑时，必须显式 `--voice alloy`**——脚本 `--voice` 默认 `vv-xinran`，不传会把修复 take 换成异音色混进 alloy 成片（DY08，2026-10-02）；另注意 whisper+vv 合成争抢 CPU 偶发 300s 合成超时崩溃（未写入任何文件，重跑即可）
- **数字假阳性（DY08 实证，2026-10-02）**：含年份/年龄的口播会被 whisper 时而转写成阿拉伯数字（「一百七十」→「170」）、时而写成汉字（「550」→「五百五十」），与参考文本书写口径不一致即超阈值——音频本身读音正确。判别法：对超阈值条目把两侧数字归一成中文读数后重算拼音音节差（DY08 三条 0.20/0.24/0.64 → 0.00/0.06/0.00 全过），且 Edge 的 3 个重合成 take 与原音频逐字节一致即为确定性佐证；证据落 `*-artifact-check.txt` 随交付引用
- **局限（如实）**：ASR 非金标准，声调级细微出入判不了；「校验通过」= 把坏 take 概率压到极低（坏 take × ASR 漏检的交集），最终验收仍靠人耳抽查
- 报告落 `tts-verify-report.json`（每条 rate/asr/fixed），随交付证据引用
- **多音字（G95 教训，2026-10-07）**：Edge/alloy 也会念错多音字——「藏在」念成 zàng。引擎不会按语境猜对，须改词重合成：藏在→隐藏在（强制 cáng），成片后 ASR 抽该窗口验证转写含「隐」；课堂音频/文本/持久化三层同步（样板 `g95v5-ttsfix.mjs` 模式）后必须重新 export。写口播稿时主动避开裸多音字（藏/传/降/差点/行李）

## 网络素材搜集与授权（拼接前置，G95 口径固化）

用户要求"搜网上视频/图片素材做拼接"时，先按本节完成素材入库，再走下一节拼接纪律：

1. **来源优先级**：① 官方渠道（nobelprize / NASA / NSF / 各领域官网及官方频道、NASA Images、Internet Archive）→ ② 明确 CC / 公共领域库（Wikimedia Commons 等，记清 CC BY / CC BY-SA 版本与署名人）→ ③ 版权不明确或不可下载的**不硬拼**，降级为官方公布图片（角标来源）或原创示意图/动画，并在交付说明注明降级原因
2. **逐段记录来源台账**：素材名 + 来源 URL + 授权口径（CC x.x / PD / 官方新闻素材）+ 下载日期，交付时附《素材来源授权清单》；任何一段来源说不清就不用
3. **下载后入库质检（用前必做）**：`ffprobe -select_streams s` 查字幕轨（有软字幕轨的剥离或弃用）；视频素材对**计划使用的区间** 1fps 抽帧扫烧录字幕/水印（烧录中文或其他文字又去不掉的段，换素材或裁掉窗口）；图片素材检查分辨率 ≥1080p、无大面积水印
4. **图片素材拼接**：静态图先展平为不透明 RGB（RGBA 合成错位坑，同强制规范第 4 条），动态化用 Ken Burns（横移 `scale=-2:2160,crop=1920:1080:x='min((in_w-1920)/2+t*<px>,in_w-1920)':y=0`；推近 `zoompan=z='1+0.0015*on':d=<frames>':...s=1920x1080:fps=30`，样板见 scripts/g95v5 拼接命令与 g95-capbar.py）
5. 拼接执行、字幕、解说、验收一律走下一节「插入素材拼接纪律」——防中文乱码的三道闸（PIL 预渲染字幕条 / 模拟转码验收 / 源窗烧录字幕扫描）全部适用，不得因素材类型（视频/图片）省略

## 插入素材拼接纪律（G95 ZOO-1858 教训，2026-10-07 起强制）

网络素材后期插入成片（ffmpeg concat 拼接）时的红线，全部来自 G95 五轮返工实证：

1. **字幕禁止 ffmpeg drawtext**：`.ttc` 字体集选面不稳 + 小字号在平台转码后必糊（用户观感即"乱码"）。一律 **PIL 预渲染字幕条 PNG 叠图**（`overlay=(W-w)/2:H-h-64`）：粗黑体 Hiragino Sans GB W6（`/System/Library/Fonts/Hiragino Sans GB.ttc` index 2；PingFang.ttc 在当前 macOS 无此路径），主行 ≥60px、来源行 ≥36px、描边 ≥3px、底板**近实心**（alpha ≥240——半透明底在高运动/暗噪声背景段必穿底糊化）。字幕条是像素图，不存在编码环节；"乱码"排查先 `ffprobe` 查有无字幕轨、再与源 PNG 做模板匹配（NCC ≥0.89 即一致）
2. **字幕验收必须含模拟转码**：`scale=1280:720 CRF32` 与 `scale=960:540 CRF30` 两档重转码后抽字幕 crop 核可读，原生分辨率干净≠平台播放可读
3. **插入段必须有配音解说**：无声插入即缺陷；每段配同音色解说（`/api/generate/tts`，openai-tts+alloy），段长按解说时长定
4. **concat 音轨标号逐一对映**：filter 里 `[N:a]→[nx]` 与输入顺序必须人工核对（G95 v2 曾把 nc/nd/nb 绑错输入，解说错位+渐进失步）；装配后 silencedetect + whisper 逐窗验证解说对位
5. **迭代交付附件必须带版本后缀**（如 `-v5-修正版.mp4`）：同名 MP4 附件在帖内累积后，验收人极易点开旧版，把已修复的问题误报为"仍在"，反馈口径会自相矛盾（G95 v4 就这样被误报过）
6. **课堂重导出后时长会变**（音频集/垫整变化，G95 实测 248.3s→213.7s）：插入 trim 点禁止沿用旧值，必须 whisper 全片带时间戳转写后按**内容锚点**重定位，插在句间静音中点（`silencedetect -38dB` gap 的中点，口播零损失）
7. **素材源窗逐秒扫烧录字幕**：使用区间内 1fps 扫描源素材，有不可去的水印/字幕段就换素材或裁掉

## 场景不合规时的重生成策略

退出码 3 = 场景清单里有非 slide 类型。重生成前：

1. 看 `manifest.json` 的 `qc.violations`，记下违规场景的 order/title（多为"阶段自查/随堂测验"页）
2. 需求正文里若出现过"测验/练习/互动/自测"等字样，删掉或改成"以要点回顾的方式收在结尾页"
3. 在正文补一句加固："全片只有一位 AI 教师连续口播，无提问互动环节"（D 类实测有效）
4. 重跑 `generate`（**注意扣费**：一次重生成就是一次完整计费，先确认修改真的针对违规点）

## 故障排查

| 症状 | 处置 |
|---|---|
| GLM 超时 / job failed | 已修复（传输层流式化）。若复发：看服务日志 `/Volumes/macext/code/demo/OpenMAIC/zoo456-logs/server.log` 找 `Retrying scene` / `job ... failed` 行；偶发失败隔几分钟重跑一次即可 |
| 配图为 0 张 / 日额度耗尽 | 硅基流动免费档 400 张/天。查服务日志 `Generating image` 与 `(429)` 行确认；额度耗尽当天可 `--flags '{"enableImageGeneration":false}'` 出无图版，配图缺失不影响课程其余部分 |
| 提交返回 4xx/额度告警 | 检查 `.env.local` 对应模型余额（不要把内容贴出来）；额度不足时换模型或停跑，如实上报 |
| `capability` 返回 `enabled:false` | 渲染服务未起。**降级路径**：脚本自动只导出教学资源包 + 课堂 ZIP（ZIP 可之后本地渲染），结果清单会标注 `degraded`；不要反复探测 |
| playwright 不可用 | 在 `scripts/` 下 `npm install playwright`（浏览器缓存已就绪），或 `PLAYWRIGHT_MODULE=<playwright index.mjs 路径>`；脚本也会尝试 OpenMAIC 仓库内的副本 |
| 导出按钮一直禁用 | 课堂媒体未落定（TTS/图片还在生成）。脚本报错退出时先 `/api/classroom?id=` 看场景数，等几分钟重跑 `export` |
| 下载卡住超时 | MP4 渲染上限默认 40 分钟（`--render-timeout-min` 可调）；确认渲染服务活着再重试，**勿对同一课堂并发发起多个渲染** |
| MP4 渲染完成但文件没落到 /Volumes/macext/downloads | 两种情况：① 后台/沙箱 run 对工作目录之外的写入会被虚拟化丢弃——**导出落盘一律在前台 shell 执行，或 `--out-dir` 指到 workdir 内再前台 `cp` 到 /Volumes/macext/downloads**；交付前用独立的前台 `ls` 验证文件真实存在。② 渲染产物还在服务端：从服务日志找本次 render job 的 UUID（`grep -oE "render/[a-f0-9-]{36}" zoo456-logs/server.log | tail`），直接 `curl http://localhost:3000/api/export-video/render/<UUID>/download -o /Volumes/macext/downloads/<命名>.mp4` 找回，不必重渲染 |
| 教学资源包/PPTX 丢了要重导 | 资源包与其中的 PPTX 都是浏览器端组装、服务端无存档，只能重跑：`node openmaic-video.mjs export --url <课堂地址> --skip-mp4`（约 1~3 分钟，不重新渲染 MP4） |
| tts-verify 报 `KeyError: 'audioId'` | 生成器偶发漏合成极短口播槽位（DY09 s2「先说开国。」4 字无 audioId/audioUrl，成片会静音跳读）。修复：按命名惯例补 audioId，用同音色（如 alloy）`/api/generate/tts` 合成、写 mp3、回填 audioId+audioUrl 并双存储同步（样板 `scripts/dy09-ttsfix.mjs`）；修完重跑 tts-verify |
| 重导出后 PPTX/MP4 里图片消失（无图版） | 课堂页被访问过后 persistence 文档库与文件库是双存储：改嵌图/换图 URL 后必须**同步 persistence 文档库**，否则页面按文档库旧 URL 渲染、跨域取图失败产出无图成品（ZOO-756 v2 的 f28-urlswap.mjs 已内置同步，复用该模式） |
| 提交前不确定是否有别的 job 在跑 | 先确认服务日志真身：`lsof -iTCP:3000 -sTCP:LISTEN` 找到监听进程，再看其 1w/2w 指向的文件（**2026-10-02 起实际日志是 `/private/tmp/z1000/next-start.log`，`zoo456-logs/server.log` 已停更于 09-26，别再拿旧文件判队列**；ZOO-1272 实测）。判队列口径：以最近 45 分钟窗口内 `Using server-configured model`（提交）、`Classroom persisted:`（完成）、`[ERROR] ... Classroom`（失败）计数，提交数 > 完成数+失败数 → 有 job 在跑；以 `Generated N scene outlines (… courseTitle: <名>)` 行认 job（带课程名，可区分是哪个课堂），再看其后有无该 job 的 `succeeded/failed`；最近 45 分钟内有未完结的 outlines 行 → 有 job 在跑，等它结束。**注意 `render/<uuid>` 下载轮询行是 MP4 渲染不是生成，不能当作生成活跃的证据**（ZOO-576 曾因此误判并行） |
| 整机重启后 3000 全挂（G95 实证，2026-10-07） | 按序拉起：① `open -a Docker` 等 daemon；② `cd /Volumes/macext/code/demo/OpenMAIC && docker compose --profile server-persistence --profile video-export up -d`（只起 postgres+render-service）；③ **app 必须宿主跑** `npm run start`（日志 `/private/tmp/z1000/next-start.log`，重建目录）——compose 里的 openmaic 服务挂的是空命名卷，起它会 404 所有已有 classroom（`Classroom not found`），务必 `docker compose stop openmaic`；④ TTS：`/Volumes/macext/code/demo/VibeVoice/venv/bin/python scripts/edge-tts-server.py`（8500）。TTS_OPENAI_BASE_URL 指向 127.0.0.1:8500，8500 没起时 /api/generate/tts 会失败 |

## 串行与成本纪律

- **一次只跑一个生成 job**；跨 agent 排队靠提交前查日志（上表最后一行）+ 大类 issue 的接力唤醒，不要抢跑
- 测试/验证类任务控制在 1~2 个选题（每个课堂 = GLM token + TTS 费用）
- 生成失败重试前先看日志定位原因，无脑重试烧钱

## 交付物约定

用本 skill 产出成品时，随结果回报：课堂 URL、jobId、场景数与类型清单（证明全 slides）、`/Volumes/macext/downloads` 文件清单（路径+大小）、封面/结尾截图。发布与多平台分发由人确认，agent 不擅自对外发布。
