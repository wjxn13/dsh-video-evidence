// dsh-video-evidence —— Node MCP server（常驻）+ Python worker（按需）。
//
// 为什么 Node 常驻：dsh 的 StdioClientTransport spawn 后立即发 initialize，
// 与 Python 常驻进程握手不稳定（实测 4 次启动 0 条消息）；Node 冷启动足够快，
// 且 argo（同为 Node 形态）已验证与 dsh 交互正常。
// 真正的检索/ASR 仍在 Python 侧（worker.py），只在工具被调用时拉起。
const { spawn } = require("child_process");
const path = require("path");

const DIR = __dirname;
const PY = path.join(DIR, ".venv", "Scripts", "python.exe");
const WORKER = path.join(DIR, "worker.py");
const WORKER_TIMEOUT = Number(process.env.VDB_WORKER_TIMEOUT || 220000);

function log(...a) {
  process.stderr.write("[vdb-node] " + a.join(" ") + "\n");
}

const TOOLS = [
  {
    name: "video_search",
    description:
      "按问题检索 B 站视频，并定位到真正讲到该内容的片段（带时间戳、可直接回跳）。" +
      "适用于想看某个知识点的视频讲解、教程片段、课程/会议里的某段发言。" +
      "不适用于找网页资料（用 argo_search）或论文（用学术源）。" +
      "返回结果附有关键帧总览图的本地路径（画面证据）：具备视觉能力的模型应按顺序读取这些图片、" +
      "核对画面内容，与转录时间轴对齐后综合回答；无视觉能力的模型忽略图片路径，只使用转录文本即可。" +
      "首次查询某视频需拉流+本地转写，耗时与音频长度相关；同一视频再次提问命中缓存，几乎瞬时返回。",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "要检索的问题或关键词" },
        max_videos: { type: "integer", default: 3, description: "最多处理几个候选视频（1-5）" },
        max_clips: { type: "integer", default: 5, description: "返回几个最相关片段（1-10）" },
        audio_seconds: { type: "integer", default: 300, description: "每个视频最多转写前多少秒（60-1800）" },
        budget_seconds: { type: "integer", default: 170, description: "总时间预算，超时返回已得结果" },
      },
      required: ["query"],
    },
  },
  {
    name: "video_cache_stats",
    description: "查看已缓存转写的视频数量（缓存按 BV 号，换问题问同一视频可直接命中）。",
    inputSchema: { type: "object", properties: {} },
  },
];

function callWorker(fn, args) {
  return new Promise((resolve) => {
    let out = "";
    let err = "";
    let child;
    try {
      child = spawn(PY, [WORKER, JSON.stringify({ fn, args })], {
        cwd: DIR,
        windowsHide: true,
        env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
      });
    } catch (e) {
      resolve({ ok: false, error: "spawn failed: " + e.message });
      return;
    }
    const timer = setTimeout(() => {
      try { child.kill(); } catch (_) {}
      resolve({ ok: false, error: "worker 超时（>" + WORKER_TIMEOUT + "ms）" });
    }, WORKER_TIMEOUT);
    child.stdout.on("data", (d) => { out += d.toString("utf8"); });
    child.stderr.on("data", (d) => { err += d.toString("utf8"); });
    child.on("error", (e) => {
      clearTimeout(timer);
      resolve({ ok: false, error: "worker error: " + e.message });
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      if (err.trim()) log("worker stderr:", err.trim().slice(0, 300));
      try {
        resolve(JSON.parse(out));
      } catch (e) {
        resolve({ ok: false,
          error: "worker 输出无法解析（exit " + code + "）: " + out.slice(0, 200) });
      }
    });
  });
}

// 与 client 对称的发送格式：null=未定, true=Content-Length 帧, false=NDJSON。
// dsh 的 dsh-mcp-client 实测发的是 NDJSON（裸 JSON + \n，无 Content-Length 头），
// 与 argo/memorix 用的标准 SDK 帧格式不同；响应必须对称，否则 client 解析不到。
let clientFraming = null;

function send(obj) {
  const s = JSON.stringify(obj);
  if (clientFraming === false) {
    process.stdout.write(s + "\n");
  } else {
    process.stdout.write(
      "Content-Length: " + Buffer.byteLength(s, "utf8") + "\r\n\r\n" + s
    );
  }
}

async function handle(msg) {
  const id = msg.id;
  const method = msg.method || "";
  // 全量日志：dsh 曾静默断连（表现为反复拉起本进程 + stdin closed），
  // 只能靠记录它实际发了哪些 method 来定位。
  log("recv:", method, "id=" + id);

  // MCP 心跳：必须回空 result。回 error 会让客户端判定连接不健康 → 断连重连
  // → 极端情况下工具注册不进去。注意要排在 notifications 分支之前。
  if (method === "ping") {
    send({ jsonrpc: "2.0", id, result: {} });
    return;
  }
  // 能力探测：返回空列表而非 method not found，避免客户端因报错断开。
  if (method === "resources/list") {
    send({ jsonrpc: "2.0", id, result: { resources: [] } });
    return;
  }
  if (method === "resources/templates/list") {
    send({ jsonrpc: "2.0", id, result: { resourceTemplates: [] } });
    return;
  }
  if (method === "prompts/list") {
    send({ jsonrpc: "2.0", id, result: { prompts: [] } });
    return;
  }
  if (method === "initialize") {
    const params = msg.params || {};
    const clientVer = params.protocolVersion || "";
    log("initialize: client protocolVersion =", clientVer || "(none)",
        "clientInfo =", JSON.stringify(params.clientInfo || {}));
    // 关键：必须回显 client 发来的 protocolVersion，不能硬编码新版。
    // MCP SDK 会校验 server 返回的版本是否在自身支持列表内；硬编码 "2025-06-18"
    // 会让较老的 SDK 判定协商失败并直接关闭 stdin —— 表现为日志里
    // "server ready → stdin closed → 再次 spawn" 循环，工具永远注册不上。
    send({
      jsonrpc: "2.0", id,
      result: {
        protocolVersion: clientVer || "2025-06-18",
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: "video-evidence", version: "0.2.0" },
        instructions: "按问题检索 B 站视频并定位到具体片段（时间戳可回跳）。",
      },
    });
    return;
  }
  if (method === "tools/list") {
    send({ jsonrpc: "2.0", id, result: { tools: TOOLS } });
    return;
  }
  if (method === "tools/call") {
    const p = msg.params || {};
    const name = p.name;
    const args = p.arguments || {};
    if (name !== "video_search" && name !== "video_cache_stats") {
      send({ jsonrpc: "2.0", id, result: {
        content: [{ type: "text", text: "未知工具: " + name }], isError: true } });
      return;
    }
    log("call", name, JSON.stringify(args).slice(0, 200));
    const res = await callWorker(name, args);
    if (res && res.ok) {
      send({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text: res.text }] } });
    } else {
      send({ jsonrpc: "2.0", id, result: {
        content: [{ type: "text", text: "执行失败：" + ((res && res.error) || "未知错误") }],
        isError: true } });
    }
    return;
  }
  if (method.startsWith("notifications/")) return;
  if (id !== undefined) {
    send({ jsonrpc: "2.0", id, error: { code: -32601, message: "method not found: " + method } });
  }
}

let buf = Buffer.alloc(0);
let gotAny = false;
// 从缓冲取一条完整消息：优先 Content-Length 帧（标准 MCP / argo），
// 回退 NDJSON（dsh 的 dsh-mcp-client 实测用这种：裸 JSON + \n，无头部）。
// 返回：string=消息体，""=跳过继续，null=数据不全等更多。
function nextMessage() {
  const idx = buf.indexOf("\r\n\r\n");
  if (idx >= 0) {
    const header = buf.slice(0, idx).toString("utf8");
    const m = /content-length:\s*(\d+)/i.exec(header);
    if (m) {
      const len = parseInt(m[1], 10);
      if (buf.length < idx + 4 + len) return null; // 收全了再解析
      const body = buf.slice(idx + 4, idx + 4 + len).toString("utf8");
      buf = buf.slice(idx + 4 + len);
      clientFraming = true;
      return body;
    }
    buf = buf.slice(idx + 4); // 有空行却无 Content-Length，丢弃头部继续
    return "";
  }
  const nl = buf.indexOf("\n");
  if (nl >= 0) {
    const line = buf.slice(0, nl).toString("utf8").trim();
    buf = buf.slice(nl + 1);
    if (!line) return "";
    clientFraming = false;
    return line;
  }
  return null;
}

process.stdin.on("data", (d) => {
  if (!gotAny) {
    gotAny = true;
    log("FIRST stdin data: bytes=" + d.length +
        " raw=" + JSON.stringify(d.toString("utf8").slice(0, 400)));
  }
  buf = Buffer.concat([buf, d]);
  for (;;) {
    const body = nextMessage();
    if (body === null) break;
    if (body === "") continue;
    let msg;
    try {
      msg = JSON.parse(body);
    } catch (e) {
      log("bad msg:", body.slice(0, 200));
      continue;
    }
    handle(msg).catch((e) => log("handle error:", e && e.message));
  }
});
process.stdin.on("end", () => log("stdin END (gotAnyData=" + gotAny + ")"));
process.stdin.on("close", () => log("stdin CLOSE (gotAnyData=" + gotAny + ")"));
process.stdin.on("error", (e) => log("stdin ERROR:", (e && e.message) || e));
process.stdout.on("error", (e) => log("stdout ERROR:", (e && e.message) || e));
process.on("exit", (c) => log("process exit code=" + c + " gotAnyData=" + gotAny));
log("server ready");
