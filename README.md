# dsh-video-evidence

**给 [DeepSeek Harness (dsh)](https://github.com/deepseek-ai) 的 MCP 插件：按问题检索 B 站视频，并定位到真正讲到该内容的片段（带时间戳、可直接回跳）。**

不是"搜到视频"就结束，而是回答"这个问题的答案在视频的第几分钟"。

```
用户：Spring Boot 项目怎么创建？

→ 1. https://www.bilibili.com/video/BV17m411d7Ny?t=0
     环境准备、JDK 版本选择 …
→ 2. https://www.bilibili.com/video/BV17m411d7Ny?t=60
     打开 IDEA，新建 Project，选择 Spring Initializr …
→ 3. https://www.bilibili.com/video/BV17m411d7Ny?t=90
     填写 Group / Artifact，勾选依赖，Finish …
```

---

## 为什么做这个

市面上的"跨模态检索 / 智能搜索"插件，绝大多数做的是**网页和文本**这一层：给一段查询，返回一堆链接和摘要。这一层已经很挤了，重复造轮子没有意义。

真正还没有被好好解决的，是**视频内部的片段级定位**：

- 搜索引擎只能告诉你"这个视频可能相关"，不会告诉你相关的是哪 30 秒；
- 视频平台自带的字幕搜索依赖 UP 主上传字幕，覆盖率和质量都靠运气；
- 对教程、课程录像、会议发言这类内容，用户真正要的从来不是"整片"，而是"讲到 X 的那一小段"。

本插件补的就是这一段：**公开视频 → 本地转写 → 片段级检索 → 可回跳的时间戳证据**。全部本地完成，不依赖任何付费 API 或第三方检索服务。

---

## 工作流程

```
        ┌──────────────────────── dsh (MCP client) ────────────────────────┐
        │  mcp__video_evidence__video_search  /  __video_cache_stats       │
        └───────────────────────────────┬──────────────────────────────────┘
                                        │ stdio / JSON-RPC
        ┌───────────────────────────────▼──────────────────────────────────┐
        │  server.js   （Node 常驻进程，只做协议转发）                       │
        │   · 双分帧解析：NDJSON ⇄ Content-Length                           │
        │   · 收到 tools/call 才 spawn Python worker                        │
        └───────────────────────────────┬──────────────────────────────────┘
                                        │ spawn（按需，用完即退）
        ┌───────────────────────────────▼──────────────────────────────────┐
        │  worker.py → server.py                                           │
        │   1. bili.py      B 站 wbi 签名搜索 → 取 cid → 取 360P 流地址      │
        │   2. asr.py       ffmpeg 拉流转 16k 单声道 wav                     │
        │                   faster-whisper (base/int8, CPU) 转写            │
        │   3. cache.py     SQLite 按 BV 号缓存转写结果（TTL 30 天）          │
        │   4. evidence.py  中文 BM25（单字 + 二元组）打分                    │
        │                   45s 滑窗 / 30s 步长切块 → 排序取 top-k           │
        └──────────────────────────────────────────────────────────────────┘
```

**为什么是 Node 常驻 + Python worker：** dsh 的 `dsh-mcp-client` spawn 之后会立刻发 `initialize`，跟 Python 常驻进程握手不稳定（实测 4 次启动、0 条消息收到）。Node 冷启动足够快，而真正的检索/ASR 留在 Python 侧，只在工具被真正调用时才拉起，空闲时不占资源。

---

## 安装

### 前置依赖

| 依赖 | 说明 |
| --- | --- |
| Node.js | 18+，只用到内置模块，无需 `npm install` |
| Python | 3.9+ |
| ffmpeg | 必须在 PATH 中，或用 `FFMPEG_BIN` 指定绝对路径 |
| faster-whisper | `pip install -r requirements.txt` |

```bash
git clone https://github.com/wjxn13/dsh-video-evidence.git
cd dsh-video-evidence

# Windows
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# macOS / Linux
python3 -m venv .venv
# 注意：server.js 里写死了 .venv/Scripts/python.exe（Windows 布局）。
# 非 Windows 需要把 server.js 中 PY 的路径改成 .venv/bin/python。
.venv/bin/pip install -r requirements.txt
```

首次调用 `video_search` 时，faster-whisper 会自动下载 base 模型到 `models/base/`（约 145MB）。也可以手动放置，或用 `ASR_MODEL_DIR` 指向已有模型目录。

### 接入 dsh

在用户级 `~/.dsh/cordis.patch.yml` 里追加（路径改成你自己的）：

```yaml
- insert:
    - id: mcp-video-evidence
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: video_evidence        # ← 决定工具名前缀：mcp__video_evidence__*
        transport: stdio
        command: "C:/path/to/node.exe"
        args:
          - "C:/path/to/dsh-video-evidence/server.js"
        cwd: "C:/path/to/dsh-video-evidence"
        env:
          PYTHONIOENCODING: "utf-8"
          PYTHONUNBUFFERED: "1"
        toolCallTimeoutMs: 240000
```

几个关键点：

- **`name` 要填 `@deepseek-ai/dsh-mcp-client`**，这是 dsh 的 MCP loader 包名，不是随便起的名字。
- **`serverName` 决定工具名前缀**：`mcp__<serverName>__<toolName>`，所以这里是 `mcp__video_evidence__video_search`。
- **`command` 用 node 的绝对路径**。dsh 会清理子进程环境，不能依赖 PATH。
- **`cwd` 要指向插件目录**，`server.js` 依赖它定位 `.venv` 和 `worker.py`。
- **`toolCallTimeoutMs` 给足**（这里 240 秒）。首次查询要拉流 + 转写，默认的短超时会让工具在结果出来前被判失败。

然后**重启 dsh**——配置只在启动时加载。用 `dsh --dump-config --profile web` 可以确认条目有没有被正确合并进去。

> **⚠️ `id` 不能和已有条目重复。** cordis loader 不允许同 id 覆盖，重复会直接让 dsh 启动崩溃（报 duplicate loader entry）。改配置前先备份 `cordis.patch.yml`。

---

## 工具

### `video_search`

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `query` | string | *必填* | 要检索的问题或关键词 |
| `max_videos` | integer | 3 | 最多处理几个候选视频（1–5） |
| `max_clips` | integer | 5 | 返回几个最相关片段（1–10） |
| `audio_seconds` | integer | 300 | 每个视频最多转写前多少秒（60–1800） |
| `budget_seconds` | integer | 170 | 总时间预算，超时就返回已经拿到的结果 |

### `video_cache_stats`

无参数。返回已缓存转写的视频数量。

---

## 性能实测

本机环境：Windows 11 / Ryzen 7 6800H / RTX 3050 Ti（**纯 CPU 推理，不用显卡**），faster-whisper `base` + `int8`：

| 项目 | 实测 |
| --- | --- |
| 转写速度 | 45 秒音频 ≈ 2.3 秒（约 20 倍实时） |
| 10 分钟视频 | ≈ 30 秒 |
| 命中缓存后 | 几乎瞬时（只查 SQLite + BM25） |

也就是说，一次交互式提问（转写前 90–180 秒）通常在几秒内返回。

---

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FFMPEG_BIN` | PATH 里的 `ffmpeg` | ffmpeg 可执行文件路径。不设则自动从 PATH 查找，找不到才需要显式指定 |
| `ASR_MODEL_DIR` | `<插件目录>/models/base` | faster-whisper 模型目录 |
| `BILI_SESSDATA` | 空 | B 站 SESSDATA Cookie。不填也能用（搜索/取流正常），填了可以尝试走官方字幕通道 |
| `VDB_CACHE` | `<插件目录>/cache.sqlite3` | 转写缓存数据库路径 |
| `VDB_CACHE_TTL` | `2592000`（30 天） | 缓存有效期（秒） |
| `VDB_AUDIO_SEC` | `300` | 默认单视频转写时长上限 |
| `VDB_BUDGET_SEC` | `170` | 默认总时间预算 |
| `VDB_MAX_VIDEOS` | `3` | 默认候选视频数 |
| `VDB_WINDOW` / `VDB_STRIDE` | `45` / `30` | 滑窗切块的窗口长度与步长（秒） |
| `VDB_WORKER_TIMEOUT` | `220000` | Python worker 超时（毫秒） |

---

## 已知限制

- **只覆盖 B 站公开视频**，且只转写前 `audio_seconds` 秒（默认 5 分钟）。长视频靠后的内容需要调大该参数，耗时线性增长。
- **匿名状态下拿不到官方字幕**（`subtitles` 接口返回 0 条），所以一律走本地 ASR，不依赖 UP 主有没有上传字幕。
- **BM25 是词面匹配**，不做语义向量检索。换个说法提问可能命中不到，这是有意的取舍——本地 CPU 上跑 embedding 模型会显著拖慢交互式响应。
- **`server.js` 里 Python 路径写死为 `.venv/Scripts/python.exe`**（Windows 布局），非 Windows 需要手动改一处。

---

## 排错

### 工具注册不上去（dsh 里看不到 `mcp__video_evidence__*`）

这是最坑的一类问题，现象是：配置明明正确（`dsh --dump-config --profile web` 能看到条目），重启也没用，日志里反复刷 `server ready → stdin closed → server ready`。

**根因：同一个 dsh 实例里存在两套 MCP 分帧格式。**

- dsh 自带的 `dsh-mcp-client` 发的是 **NDJSON**（裸 JSON + `\n`，**没有 `Content-Length` 头**）
- argo / memorix 这类用标准 MCP SDK 的走的是 `Content-Length: N\r\n\r\n{json}`

只认 `Content-Length` 的解析器收 NDJSON 时永远凑不出一条完整消息 → 不回 `initialize` → dsh 超时关 stdin → 重拉，死循环。

本插件的 `server.js` 已经用 `clientFraming` 闩锁 + `nextMessage()` 做了双格式兼容，响应按客户端格式对称回写。如果你要 fork 改写协议层，**这两段不要动**。

配套的两个必要细节：

1. **`initialize` 必须回显客户端发来的 `protocolVersion`**，不能硬编码。MCP SDK 会校验服务端返回的版本是否在自身支持列表内，硬编码新版会让较老 SDK 判定协商失败并直接关闭 stdin。
2. **`ping` / `resources/list` / `prompts/list` 要返回空结果而不是 `-32601`。** 客户端探测能力时收到 method not found 会判定连接不健康，进而断连重连。

**怎么定位：** 在 stdin 的 `data` 回调里打第一条原始数据：

```js
if (!gotAny) {
  gotAny = true;
  log("FIRST stdin data: raw=" + JSON.stringify(d.toString("utf8").slice(0, 400)));
}
```

看到 `{"method":"initialize",...}` 后面直接跟 `\n`、没有 `Content-Length`，就确认是 NDJSON。

### B 站搜索返回空（HTTP 200、`code:0`、但结果为空）

wbi 签名的坑：`urllib.parse.urlencode` 默认用 `quote_plus`，空格编码成 `+`；B 站服务端按 `%20` 重算签名，校验不通过就静默拒绝。

```python
# 必须用 quote（空格 -> %20），不能用默认的 quote_plus（空格 -> +）
q = urllib.parse.urlencode(p, quote_via=urllib.parse.quote)
```

`bili.py` 里已经修好，并且加了非 wbi 的 legacy 接口作为兜底。

---

## 目录结构

```
dsh-video-evidence/
├── server.js      # Node 常驻 MCP server（协议层：双分帧 + 方法分发）
├── worker.py      # Python worker 入口（CLI，读 JSON 参数、打 JSON 结果）
├── server.py      # 业务编排：搜索 → 取流 → 转写 → 检索
├── bili.py        # B 站 API 客户端（wbi 签名、搜索、cid、playurl）
├── asr.py         # ffmpeg 拉流 + faster-whisper 转写
├── evidence.py    # 中文 BM25 打分 + 滑窗切块
├── cache.py       # SQLite 转写缓存（按 BV 号，TTL 30 天）
└── requirements.txt
```

---

## License

MIT
