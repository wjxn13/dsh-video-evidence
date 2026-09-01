#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""dsh-video-evidence —— B 站视频片段级证据检索 MCP server。

定位（明确不做的事）：
  不做意图分类、不做多模态路由、不做 RRF 融合 —— 这些 argo 已经有了。
  只补 argo 缺的一层：从「找到视频」到「定位视频里第几秒讲了你要的东西」。

链路：搜索 → cid → 播放流 → ffmpeg 提音频 → 本地 ASR → 滑窗分块 → BM25 → 带时间戳片段
"""
import json
import os
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cache  # noqa: E402   # 仅 stdlib（sqlite3），启动快
import evidence  # noqa: E402  # 仅 stdlib，启动快
# 注意：bili（依赖 requests）必须懒加载。
# dsh 的 StdioClientTransport 在 spawn 后**立即**发送 initialize，
# 若顶层 import requests，Python 还没就绪就会握手失败 → dsh 关闭 stdin 重连，
# 表现为 server 反复重启且永远收不到消息。实测踩过。

DEFAULT_AUDIO_SEC = int(os.environ.get("VDB_AUDIO_SEC", "300"))
DEFAULT_BUDGET = int(os.environ.get("VDB_BUDGET_SEC", "170"))
MAX_VIDEOS = int(os.environ.get("VDB_MAX_VIDEOS", "3"))
WINDOW = float(os.environ.get("VDB_WINDOW", "45"))
STRIDE = float(os.environ.get("VDB_STRIDE", "30"))


def log(*a):
    """只写 stderr —— stdout 是 MCP 协议通道，绝不能污染。"""
    print(*a, file=sys.stderr, flush=True)


TOOLS = [
    {
        "name": "video_search",
        "description": (
            "按问题检索 B 站视频，并定位到真正讲到该内容的片段（带时间戳、可直接回跳）。"
            "适用于：想看某个知识点的视频讲解、教程片段、会议/课程里的某段发言。"
            "不适用于：找网页资料（用 argo_search）、找论文（用学术源）。"
            "注意：首次查询某视频需拉流+本地转写，耗时与音频长度相关；"
            "同一视频再次提问会命中缓存，几乎瞬时返回。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要检索的问题或关键词"},
                "max_videos": {"type": "integer", "default": MAX_VIDEOS,
                               "description": "最多处理几个候选视频（1-5）"},
                "max_clips": {"type": "integer", "default": 5,
                              "description": "返回几个最相关片段（1-10）"},
                "audio_seconds": {"type": "integer", "default": DEFAULT_AUDIO_SEC,
                                  "description": "每个视频最多转写前多少秒（60-1800）"},
                "budget_seconds": {"type": "integer", "default": DEFAULT_BUDGET,
                                   "description": "总时间预算，超时返回已得结果"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "video_cache_stats",
        "description": "查看已缓存转写的视频数量（缓存按 BV 号，换问题问同一视频可直接命中）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _fmt_ts(sec: float) -> str:
    sec = int(max(0, sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return "%d:%02d:%02d" % (h, m, s) if h else "%02d:%02d" % (m, s)


def _get_transcript(client, video, audio_seconds, deadline):
    """返回 (segments, source, err)。优先缓存 → 官方字幕 → 本地 ASR。"""
    bvid = video["bvid"]
    hit = cache.get(bvid)
    if hit and hit.get("segments"):
        return hit["segments"], "cache", ""

    info = client.view(bvid)
    cid = info.get("cid")
    if not cid:
        return None, "", "取 cid 失败"

    subs = client.subtitles(bvid, cid)
    if subs:
        segs = [{"start": a, "end": b, "text": c} for a, b, c in subs]
        cache.put(bvid, cid, info.get("title") or video.get("title", ""),
                  video.get("author", ""), info.get("duration") or 0, segs, "", "official_subtitle")
        return segs, "official_subtitle", ""

    if time.time() > deadline:
        return None, "", "时间预算耗尽（转写前）"

    stream = client.playurl(bvid, cid)
    if not stream:
        return None, "", "取播放流失败"

    import asr
    tmpdir = tempfile.mkdtemp(prefix="vdb_")
    wav = os.path.join(tmpdir, "a.wav")
    try:
        ok, err = asr.extract_audio(stream, audio_seconds, wav)
        if not ok:
            return None, "", "提取音频失败: %s" % err
        if time.time() > deadline:
            return None, "", "时间预算耗尽（转写前）"
        segs, lang = asr.transcribe(wav)
    except Exception as e:
        return None, "", "转写异常: %s" % e
    finally:
        try:
            if os.path.exists(wav):
                os.remove(wav)
            os.rmdir(tmpdir)
        except Exception:
            pass

    if not segs:
        return None, "", "转写结果为空"
    cache.put(bvid, cid, info.get("title") or video.get("title", ""),
              video.get("author", ""), info.get("duration") or 0, segs, lang, "asr")
    return segs, "asr", ""


def _video_search(args: dict) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "错误：query 不能为空"
    max_videos = max(1, min(5, int(args.get("max_videos") or MAX_VIDEOS)))
    max_clips = max(1, min(10, int(args.get("max_clips") or 5)))
    audio_seconds = max(60, min(1800, int(args.get("audio_seconds") or DEFAULT_AUDIO_SEC)))
    budget = max(30, min(600, int(args.get("budget_seconds") or DEFAULT_BUDGET)))
    deadline = time.time() + budget

    import bili  # 懒加载（见文件头注释：启动期不能 import requests）
    client = bili.default_client()
    candidates = client.search(query, limit=max_videos * 3)
    if not candidates:
        return "未检索到相关视频（B 站搜索无结果，可换更短更通用的关键词重试）"

    # 偏好：时长适中优先（过短信息量小，过长转写慢）
    def rank(v):
        d = v.get("duration") or 0
        if d <= 0:
            return 9999
        return abs(min(d, 1800) - 600)
    candidates.sort(key=rank)

    processed, skipped = [], []
    for v in candidates:
        if len(processed) >= max_videos or time.time() > deadline:
            break
        segs, source, err = _get_transcript(client, v, audio_seconds, deadline)
        if not segs:
            skipped.append("%s（%s）" % (v.get("bvid"), err or "无内容"))
            continue
        chunks = evidence.build_chunks(segs, WINDOW, STRIDE)
        if not chunks:
            skipped.append("%s（分块为空）" % v.get("bvid"))
            continue
        processed.append({"video": v, "chunks": chunks, "source": source})

    if not processed:
        return ("未能获得任何视频内容。跳过原因：\n- "
                + "\n- ".join(skipped[:5])
                + "\n建议：换更通用的关键词，或调大 audio_seconds / budget_seconds。")

    # 跨视频统一打分：一个片段池，避免「每个视频强行出结果」
    pool, meta = [], []
    for p in processed:
        for c in p["chunks"]:
            pool.append(c["text"])
            meta.append((p["video"], c, p["source"]))
    scores = evidence.bm25(query, [evidence.tokenize(t) for t in pool])

    ranked = sorted(zip(scores, range(len(pool))), key=lambda x: -x[0])
    picked, per_video = [], {}
    for sc, idx in ranked:
        if sc <= 0:
            continue
        v, c, src = meta[idx]
        if per_video.get(v["bvid"], 0) >= 3:  # 同一视频最多 3 段，保证多样性
            continue
        per_video[v["bvid"]] = per_video.get(v["bvid"], 0) + 1
        picked.append((sc, v, c, src))
        if len(picked) >= max_clips:
            break

    if not picked:
        return ("拿到了视频内容，但没有片段与「%s」相关。已处理：%s\n"
                "建议换更贴近视频口语表达的关键词。" %
                (query, "、".join(p["video"].get("title", "")[:20] for p in processed)))

    lines = ["查询：%s" % query,
             "已处理 %d 个视频，命中 %d 个片段（时间预算 %ds）" % (len(processed), len(picked), budget),
             ""]
    for i, (sc, v, c, src) in enumerate(picked, 1):
        t = int(c["start"])
        url = "%s?t=%d" % (v["url"], t)
        lines.append("【%d】%s" % (i, v.get("title", "")[:60]))
        lines.append("    UP %s · 时长 %s · 来源 %s" %
                     (v.get("author", "") or "-", _fmt_ts(v.get("duration") or 0), src))
        lines.append("    ⏱ %s - %s  →  %s" % (_fmt_ts(c["start"]), _fmt_ts(c["end"]), url))
        lines.append("   相关性 %.2f" % sc)
        txt = c["text"]
        lines.append("    “%s%s”" % (txt[:180], "…" if len(txt) > 180 else ""))
        lines.append("")
    if skipped:
        lines.append("（跳过：%s）" % "、".join(skipped[:3]))
    return "\n".join(lines)


def _cache_stats(_args) -> str:
    n = cache.stats()
    return "已缓存转写视频：%s 个" % ("未知" if n < 0 else n)


HANDLERS = {"video_search": _video_search, "video_cache_stats": _cache_stats}


def read_message():
    """读一帧 MCP 消息。

    必须区分三种情况，否则会被 dsh 反复重连：
      - 真正的 EOF（b""）→ 返回 None，退出
      - 前导空行（headers 还空）→ 跳过，继续读
      - 空帧（有头无体 / 坏 JSON）→ 跳过，继续读下一条
    """
    while True:
        headers = {}
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                if not headers:
                    continue
                break
            if b":" in line:
                k, v = line.decode("utf-8", "replace").split(":", 1)
                headers[k.strip().lower()] = v.strip()
        n = int(headers.get("content-length", 0) or 0)
        if n <= 0:
            continue
        raw = sys.stdin.buffer.read(n)
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            log("[vdb] 跳过无法解析的帧")
            continue


def send(obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
    sys.stdout.buffer.flush()


def main():
    log("[vdb] server starting")
    while True:
        msg = read_message()
        if msg is None:
            break
        mid = msg.get("id")
        method = msg.get("method", "")
        log("[vdb] recv method=%s id=%s" % (method, mid))
        try:
            if method == "initialize":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "video-evidence", "version": "0.1.0"},
                    "instructions": "按问题检索 B 站视频并定位到具体片段（时间戳可回跳）。"
                }})
            elif method == "tools/list":
                send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
            elif method == "tools/call":
                p = msg.get("params") or {}
                name, args = p.get("name"), p.get("arguments") or {}
                fn = HANDLERS.get(name)
                if not fn:
                    send({"jsonrpc": "2.0", "id": mid, "result": {
                        "content": [{"type": "text", "text": "未知工具: %s" % name}],
                        "isError": True}})
                else:
                    try:
                        text = fn(args)
                        send({"jsonrpc": "2.0", "id": mid, "result": {
                            "content": [{"type": "text", "text": text}]}})
                    except Exception as e:
                        log("[vdb] handler error: %s" % traceback.format_exc())
                        send({"jsonrpc": "2.0", "id": mid, "result": {
                            "content": [{"type": "text", "text": "执行失败: %s" % e}],
                            "isError": True}})
            elif method.startswith("notifications/"):
                pass
            elif mid is not None:
                send({"jsonrpc": "2.0", "id": mid, "error":
                      {"code": -32601, "message": "method not found: %s" % method}})
        except Exception:
            log("[vdb] loop error: %s" % traceback.format_exc())
    log("[vdb] stdin closed, exiting")


if __name__ == "__main__":
    main()
