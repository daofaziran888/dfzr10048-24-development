# 拾影工坊 · 音视频采集与转写结构化平台

面向音视频媒体源接入、分片切分、转码作业、转写任务、语音分段、说话人标注、字幕对齐、时间轴标注、术语抽取、转写版本、置信复核、纠错回路、字幕包交付、分片水位与失败重投的采集结构化平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8005/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5178/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8005`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 媒体源 | `media_source` | 媒体源 | 媒体源编号、来源渠道、媒体标题 |
| 采集通道 | `capture_channel` | 通道配置 | 通道编号、关联媒体源、并发上限 |
| 媒体分片 | `media_shard` | 分片记录 | 分片编号、关联通道、起始秒 |
| 转码作业 | `transcode_job` | 转码作业 | 作业编号、关联分片、目标编码 |
| 转写任务 | `transcribe_task` | 转写任务 | 任务编号、关联分片、转写引擎 |
| 语音分段 | `speech_segment` | 语音段 | 分段编号、关联任务、起始毫秒 |
| 说话人标注 | `speaker_label` | 标注条目 | 标注编号、关联分段、说话人标识 |
| 字幕对齐 | `subtitle_align` | 对齐结果 | 对齐编号、关联分段、字幕行号 |
| 时间轴标注 | `timeline_mark` | 标注点 | 标注点编号、关联媒体源、标注毫秒 |
| 术语抽取 | `term_extract` | 术语条目 | 术语编号、关联任务、术语文本 |
| 转写版本 | `transcript_version` | 版本记录 | 版本编号、关联任务、版本号 |
| 置信复核 | `confidence_review` | 复核条目 | 复核编号、关联分段、段置信度 |
| 纠错回路 | `correction_loop` | 纠错单 | 纠错编号、关联版本、纠错范围毫秒 |
| 字幕包 | `subtitle_pack` | 字幕包 | 字幕包编号、关联媒体源、绑定版本 |
| 媒体索引 | `media_index` | 索引条目 | 索引编号、关联媒体源、索引词 |
| 分片水位 | `shard_watermark` | 水位读数 | 水位编号、关联媒体源、已处理分片序号 |
| 失败重投 | `fail_redeliver` | 重投单 | 重投编号、关联分片、失败阶段 |
| 转写配额 | `transcribe_quota` | 配额条目 | 配额编号、来源渠道、转写引擎 |
| 抽样质检 | `sample_qc` | 质检样本 | 样本编号、关联任务、抽样比例 |
| 交付回执 | `delivery_receipt` | 回执记录 | 回执编号、关联字幕包、接收方标识 |
| 归档架位 | `archive_shelf` | 归档条目 | 归档编号、关联媒体源、架位标识 |
| 采集审计 | `media_audit` | 审计条目 | 审计编号、关联对象、操作类型 |
| 说话人改标事件 | `relabel_event` | 改标事件 | 事件编号、关联分段、说话人标签、回执编号、是否采纳、改标次数 |
| 转写装配稿 | `assembly_draft` | 装配稿 | 装配稿编号、绑定版本、装配行数、关联字幕包 |

## 说话人改标可追溯（字幕署名争议）

处置字幕署名争议时，通用列表只能看到最新说话人标签，缺少「当时是谁、把什么改成什么」的人工凭据。平台因此新增一条持久化追溯链：

- **改标事件台账**（`app/pipeline/relabel_event_ledger.py`）：每次机器回执与人工更正都落盘到 `backend/runtime_data/relabel_events.json`，重启可读回。
  - 人工改标即时生效并确认，并使该分段此前待确认的机器领取失效；
  - 机器回执按 `receipt_id` 幂等，重送不新增事件、不增加改标次数；
  - 人工结论形成后，迟到的机器结果只留痕（`applied=false`），不能更改人工结论；
  - 改标次数只统计已采纳的人工改标。
- **转写版本装配**（`app/pipeline/transcript_assembly.py`）：为每个转写版本冻结不可变装配稿（`assembly_drafts.json`），逐行（字幕行号/语音段）绑定实际采用的标签、写入人、原标签与来源事件，凭据跟随实际正文；重装一份生成一份新稿，旧稿保留。
- **字幕包关联实际装配稿**：字幕包通过 `draft_id` 关联到当时的装配稿，可沿行号还原写入人和原标签，并支持与已发字幕逐行对照。
- **写入人隔离**：可按 `operator` 查询，编辑 B 只读自己的改标来源。

### 追溯接口（前缀 `/api/traceability`）

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/relabel/machine` | 登记机器回执（重送幂等、人工结论后迟到拒收） |
| POST | `/relabel/manual` | 登记人工改标（即时生效） |
| POST | `/relabel/confirm` | 确认机器回执 |
| GET | `/relabel/events` | 按分段/写入人查询改标事件 |
| POST | `/assemble` | 为转写版本装配一份新稿 |
| POST | `/packs/link` | 字幕包关联实际装配稿 |
| GET | `/packs/{pack_id}/draft` | 取字幕包实际装配稿 |
| GET | `/packs/{pack_id}/lines/{line_no}` | 逐行还原写入人与原标签 |
| POST | `/packs/{pack_id}/compare` | 与已发字幕逐行对照 |

持久化目录由 `app/config.py` 的 `runtime_dir` 控制（默认 `backend/runtime_data/`，已加入 `.gitignore`）；容器部署时在 `docker-compose.yml` 挂载命名卷 `traceability_data`。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
