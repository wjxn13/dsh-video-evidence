"""B 站公开接口封装：wbi 签名、搜索、cid、播放流、字幕（字幕需登录 cookie）。

实测结论（2026-08-30）：
- 匿名可用：搜索、view(cid)、playurl(360P 流)
- 匿名不可用：字幕（0 轨）、历史弹幕 —— 因此主链路走 playurl + 本地 ASR
"""
import hashlib
import os
import re
import time
import urllib.parse

import requests

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class BiliClient:
    def __init__(self, sessdata: str = "", timeout: int = 25):
        self.s = requests.Session()
        self.timeout = timeout
        self.s.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
        self._boot_cookies()
        if sessdata:
            self.s.cookies.set("SESSDATA", sessdata, domain=".bilibili.com")
        self.img_key, self.sub_key = self._wbi_keys()

    def _boot_cookies(self):
        try:
            r = self.s.get("https://api.bilibili.com/x/frontend/finger/spi",
                           timeout=self.timeout)
            d = (r.json() or {}).get("data") or {}
            if d.get("b_3"):
                self.s.cookies.set("buvid3", d["b_3"], domain=".bilibili.com")
            if d.get("b_4"):
                self.s.cookies.set("buvid4", d["b_4"], domain=".bilibili.com")
        except Exception:
            pass
        self.s.cookies.set("b_nut", str(int(time.time())), domain=".bilibili.com")

    def _wbi_keys(self):
        try:
            r = self.s.get("https://api.bilibili.com/x/web-interface/nav",
                           timeout=self.timeout)
            w = ((r.json() or {}).get("data") or {}).get("wbi_img") or {}
            img = w.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
            sub = w.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
            if img and sub:
                return img, sub
        except Exception:
            pass
        return "", ""

    def _sign(self, params: dict) -> str:
        if not self.img_key:
            return urllib.parse.urlencode(params)
        mk = "".join((self.img_key + self.sub_key)[i] for i in MIXIN_KEY_ENC_TAB)[:32]
        p = dict(params)
        p["wts"] = int(time.time())
        p = dict(sorted(p.items()))
        p = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in p.items()}
        # 注意：必须用 quote（空格->%20），不能用默认的 quote_plus（空格->+）。
        # B 站 wbi 服务端按 %20 重算签名，用 + 会导致含空格关键词签名不匹配，
        # 返回 code:0 但空结果（静默拒）。实测 "Spring Boot" 走 + 编码时为 0 条，改 %20 后正常。
        q = urllib.parse.urlencode(p, quote_via=urllib.parse.quote)
        return q + "&w_rid=" + hashlib.md5((q + mk).encode()).hexdigest()

    @staticmethod
    def _strip(s: str) -> str:
        return re.sub(r"<[^>]+>", "", s or "").strip()

    @staticmethod
    def parse_duration(d: str) -> int:
        """'2:27' / '99:25' / '443:7' -> 秒"""
        try:
            parts = [int(x) for x in (d or "0").split(":")]
            total = 0
            for v in parts:
                total = total * 60 + v
            return total
        except Exception:
            return 0

    def search(self, keyword: str, limit: int = 8) -> list:
        out = self._search_wbi(keyword, limit)
        if out:
            return out
        # wbi 搜索偶尔对含空格/特殊字符的关键词静默返空（code:0 但 result 为空），
        # 退回非 wbi 的旧接口兜底（实测该接口对 "Spring Boot" 正常返回 20 条）。
        return self._search_legacy(keyword, limit)

    def _search_wbi(self, keyword: str, limit: int) -> list:
        url = "https://api.bilibili.com/x/web-interface/wbi/search/type?" + self._sign(
            {"search_type": "video", "keyword": keyword, "page": 1})
        r = self.s.get(url, timeout=self.timeout)
        j = r.json() or {}
        if (j.get("code") not in (0, None)) or not ((j.get("data") or {}).get("result")):
            return []
        return self._pack_items((j.get("data") or {}).get("result") or [], limit)

    def _search_legacy(self, keyword: str, limit: int) -> list:
        try:
            url = ("https://api.bilibili.com/x/web-interface/search/type?"
                   + urllib.parse.urlencode({"search_type": "video", "keyword": keyword, "page": 1}))
            r = self.s.get(url, timeout=self.timeout)
            j = r.json() or {}
            if (j.get("code") not in (0, None)) or not ((j.get("data") or {}).get("result")):
                return []
            return self._pack_items((j.get("data") or {}).get("result") or [], limit)
        except Exception:
            return []

    @staticmethod
    def _pack_items(items, limit: int) -> list:
        out = []
        for it in items[:limit]:
            bvid = it.get("bvid")
            if not bvid:
                continue
            out.append({
                "bvid": bvid,
                "title": BiliClient._strip(it.get("title")),
                "author": it.get("author") or "",
                "duration": BiliClient.parse_duration(it.get("duration") or "0"),
                "url": "https://www.bilibili.com/video/%s" % bvid,
                "desc": BiliClient._strip(it.get("description") or "")[:400],
                "danmaku": it.get("video_review") or 0,
                "play": it.get("play") or 0,
            })
        return out

    def view(self, bvid: str) -> dict:
        url = "https://api.bilibili.com/x/web-interface/wbi/view?" + self._sign({"bvid": bvid})
        j = (self.s.get(url, timeout=self.timeout).json() or {})
        d = j.get("data") or {}
        return {"cid": d.get("cid"), "title": d.get("title") or "",
                "duration": d.get("duration") or 0,
                "desc": (d.get("desc") or "")[:600],
                "owner": (d.get("owner") or {}).get("name") or ""}

    def playurl(self, bvid: str, cid: int) -> str:
        url = "https://api.bilibili.com/x/player/wbi/playurl?" + self._sign(
            {"bvid": bvid, "cid": cid, "qn": 16, "fnval": 1, "fourk": 0})
        j = (self.s.get(url, timeout=self.timeout).json() or {})
        durl = ((j.get("data") or {}).get("durl") or [])
        return durl[0].get("url", "") if durl else ""

    def subtitles(self, bvid: str, cid: int) -> list:
        """返回 [(start, end, text)]；匿名恒为空，登录后才可能拿到 AI 字幕。"""
        url = "https://api.bilibili.com/x/player/wbi/v2?" + self._sign(
            {"bvid": bvid, "cid": cid})
        j = (self.s.get(url, timeout=self.timeout).json() or {})
        subs = (((j.get("data") or {}).get("subtitle") or {}).get("subtitles") or [])
        if not subs:
            return []
        # 优先中文轨
        zh = [s for s in subs if str(s.get("lan", "")).startswith("zh")] or subs
        su = zh[0].get("subtitle_url", "")
        if su.startswith("//"):
            su = "https:" + su
        try:
            body = (self.s.get(su, timeout=self.timeout).json() or {}).get("body") or []
        except Exception:
            return []
        return [(float(b.get("from", 0)), float(b.get("to", 0)), b.get("content", ""))
                for b in body]


def default_client() -> BiliClient:
    return BiliClient(sessdata=os.environ.get("BILI_SESSDATA", ""))
