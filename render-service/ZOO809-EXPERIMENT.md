# ZOO-809 实验记录：渲染服务切换 standard profile（beginFrame）

日期：2026-09-22（UTC 2026-09-21 晚）。本分支（`zoo809/render-standard-profile`）记录本次实验的运维变更与结果。

## 变更内容（本实验无代码改动，仅部署配置）

| 项 | 原值 | 新值 | 位置 |
|---|---|---|---|
| 渲染资源档 | `RENDER_RESOURCE_PROFILE=low-memory`（强制 screenshot 截帧） | `standard`（prefer-beginframe） | `OpenMAIC/.env`（gitignore，不入库） |
| 容器内存上限 | `RENDER_SERVICE_MEMORY_LIMIT=4g` | `8g` | 同上 |
| Docker Desktop VM 内存 | 6144 MiB | 10240 MiB | Docker Desktop settings-store.json |

回滚：git tag `zoo809-backup-20260922`（main @ 8256674）；`.env` 原文备份在实验 workdir `openmaic-env.backup`；Docker settings 备份 `docker-settings.backup.json`。

## A/B 结果（同一课堂 bJJC_mzszh，compositionHash e98a7c86b48e1342，11500 帧 / 383.3s / 1080p30）

| 指标 | low-memory（基线，2026-09-21） | standard（容器，2026-09-21） | 原生 apple-silicon（2026-09-22） |
|---|---|---|---|
| 截帧模式 | screenshot（SwiftShader） | **beginframe**（SwiftShader） | beginframe（ANGLE Metal） |
| 截帧耗时 | 575.8s（20.0 fps） | **220.8s（52.1 fps）** | 453.2s（25.4 fps） |
| 编码 | 与截帧流水重叠 | 71.5s（libx264） | 49.5s（h264_videotoolbox） |
| 整任务 | 584.5s | **305.8s（1.91×）** ✅ | 516.0s（0.89×，更慢）❌ |
| 产物大小 | — | 25.5 MB | 94.8 MB（VT 质量映射码率更高） |

## 终稿（2026-09-22 晚，应用户内存约束）：Docker 总内存 <= 8G，速度保持 1.92×

用户约束：postgres 压小、渲染容器放大、Docker 总内存 <= 8GiB、VibeVoice 留够内存、保持 ~2× 速度。

- Docker VM：8192 MiB（MemTotal 实测 7.75GiB）
- standard 档内存门槛：代码下调 8GiB -> **6GiB**（实测峰值 1.4~1.8GiB，>3x 余量；镜像已重建）
- 渲染容器 mem_limit：**6g**；postgres 加 **512m** 硬上限（实测占用 ~28MiB）
- 第 4 次同课堂 A/B 验证：beginframe、截帧 222.3s（51.7fps）、整任务 **304.0s（1.92×）**、
  峰值内存 1.70GiB/6g、产物 25.5MB —— 与 10GiB VM 时速度持平（305.8s）。
- 主机侧为 VibeVoice 留出 ~8GiB（16 - VM 8）；ZOO-833 已将 VibeVoice 改为空闲退出子进程。

## 实验 2 结论：原生 Metal 路径更慢，已回退

- 原因分析：幻灯片类简单 2D 内容下，beginFrame+Metal 每帧要走 GPU 回读，开销高于
  SwiftShader 直接在内存里栅格化；编码虽快（VideoToolbox 232fps）但编码本就不是瓶颈，
  且 `-q:v` 质量映射产物大 3.7×。原生路径内存表现好（渲染峰值约 1.5GiB，主机余量 ≥4GiB），
  但速度不达标，按用户判定标准回退。
- 回退后定稿状态（2026-09-22）：容器 render-service + `standard` profile + 8g 上限，
  Docker VM 9216 MiB（standard 档最低可行值，为 VibeVoice 留内存：VM 按需占用，
  空闲时实测仅 ~1.8GiB）。
- 实验 2 代码保留在本分支（`apple-silicon` profile + `useGpu` + `RENDER_HOST`），
  镜像未重建，容器不加载这些改动；如需复跑原生：见下节命令。
- 复跑原生（可选）：见 git 历史本文件更早版本与 ZOO-809 issue 评论；要点是
  `RENDER_RESOURCE_PROFILE=apple-silicon` + headless-shell 路径 + `npm start`。

## 注意事项

- Docker VM 设 9216 MiB 是 standard 档的最低可行值；VM 内存按需占用（空闲实测 ~1.8GiB），
  但若观察到与 VibeVoice TTS（常驻 ~5GB）争内存：先错峰（生成与导出本就应串行），
  必要时一键回退低内存档——`.env` 改回 `low-memory/4g` + Docker VM 调回 6144 MiB 并重启。
- 产物验证：h264 1080p30 + AAC，时长 383.33s 与课堂一致。
- 回滚锚点：git tag `zoo809-backup-20260922`（main @ 8256674）；`.env` 原始备份与 Docker
  settings 备份在 ZOO-809 workdir。
