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

| 指标 | low-memory（基线，2026-09-21） | standard（本次） | 变化 |
|---|---|---|---|
| 截帧模式 | screenshot（SwiftShader） | **beginframe** | — |
| 截帧耗时 | 575.8s（20.0 fps） | **220.8s（52.1 fps）** | **2.6×** |
| 编码 | 与截帧流水重叠 | 71.5s（>240s 时长不再流式，改为盘帧后单独编码） | — |
| 整任务 | 584.5s | **305.8s** | **1.91×** |
| 容器内存峰值 | ~1GiB（4g 上限） | 1.4GiB（8g 上限） | 余量充足 |

## 注意事项

- Docker VM 常驻 10GiB：16GB 宿主机上若同时跑 VibeVoice TTS（常驻 ~5GB）+ 渲染，内存会紧张；生成与导出尽量错峰。
- 产物验证：h264 1080p30 + AAC，时长 383.33s 与课堂一致。
- 后续可选（未做）：渲染进程原生跑宿主机（Metal + VideoToolbox，预估再 ~1.7×），需改 `resource-profile.ts` / `render-executor.ts`，见 ZOO-809 issue 评论。
