#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""命令行 worker：接收 JSON 参数、输出 JSON 结果（供 Node MCP server 调用）。

为什么拆开：dsh 的 StdioClientTransport 与「Python 常驻进程」之间存在
握手时序问题（实测 4 次启动、0 条消息收到），而与 Node 常驻进程交互正常
（argo 已验证）。因此让 Node 负责常驻与协议，Python 只在真正干活时被拉起。

用法：
    python worker.py '{"fn":"video_search","args":{"query":"..."}}'
输出（stdout，UTF-8 JSON）：
    {"ok": true, "text": "..."}  或  {"ok": false, "error": "..."}
"""
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from server import _video_search, _cache_stats  # noqa: E402

FNS = {"video_search": _video_search, "video_cache_stats": _cache_stats}


def main():
    if len(sys.argv) < 2:
        sys.stdout.write(json.dumps({"ok": False, "error": "missing args"},
                                    ensure_ascii=False))
        return
    try:
        req = json.loads(sys.argv[1])
    except Exception as e:
        sys.stdout.write(json.dumps({"ok": False, "error": "bad json: %s" % e},
                                    ensure_ascii=False))
        return
    fn = FNS.get(req.get("fn", ""))
    if not fn:
        sys.stdout.write(json.dumps({"ok": False, "error": "unknown fn: %s" % req.get("fn")},
                                    ensure_ascii=False))
        return
    try:
        text = fn(req.get("args") or {})
        sys.stdout.write(json.dumps({"ok": True, "text": text}, ensure_ascii=False))
    except Exception as e:
        sys.stderr.write("[worker] %s\n" % traceback.format_exc())
        sys.stdout.write(json.dumps({"ok": False, "error": "%s" % e}, ensure_ascii=False))


if __name__ == "__main__":
    main()
