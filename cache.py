"""转写结果缓存。

按 BV 号缓存（不是按 query）——同一个视频换问题问第二遍直接命中，
这是本工具最主要的成本节省点（省掉拉流 + ASR）。
"""
import json
import os
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("VDB_CACHE", os.path.join(HERE, "cache.sqlite3"))
TTL = int(os.environ.get("VDB_CACHE_TTL", str(30 * 24 * 3600)))


def _conn():
    c = sqlite3.connect(DB, timeout=15)
    c.execute("""CREATE TABLE IF NOT EXISTS transcripts (
        bvid TEXT PRIMARY KEY,
        cid INTEGER,
        title TEXT,
        author TEXT,
        duration INTEGER,
        lang TEXT,
        source TEXT,
        segments TEXT,
        created_at REAL
    )""")
    return c


def get(bvid: str):
    try:
        c = _conn()
        row = c.execute("SELECT cid,title,author,duration,lang,source,segments,created_at"
                        " FROM transcripts WHERE bvid=?", (bvid,)).fetchone()
        c.close()
    except Exception:
        return None
    if not row:
        return None
    if time.time() - (row[7] or 0) > TTL:
        return None
    return {"bvid": bvid, "cid": row[0], "title": row[1], "author": row[2],
            "duration": row[3], "lang": row[4], "source": row[5],
            "segments": json.loads(row[6] or "[]")}


def put(bvid, cid, title, author, duration, segments, lang, source):
    try:
        c = _conn()
        c.execute("INSERT OR REPLACE INTO transcripts VALUES (?,?,?,?,?,?,?,?,?)",
                  (bvid, cid, title, author, duration, lang, source,
                   json.dumps(segments, ensure_ascii=False), time.time()))
        c.commit()
        c.close()
        return True
    except Exception:
        return False


def stats():
    try:
        c = _conn()
        n = c.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
        c.close()
        return n
    except Exception:
        return -1


def clear():
    try:
        c = _conn()
        c.execute("DELETE FROM transcripts")
        c.commit()
        c.close()
        return True
    except Exception:
        return False
