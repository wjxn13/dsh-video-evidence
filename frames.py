"""画面线：关键帧总览图（video-understanding）。

与声音线（asr.py）的关系：
  声音线回答「视频里说了什么」，本模块回答「画面里是什么」。
  只对 BM25 最终命中的视频抽帧（不是所有候选），抽帧成本只花在
  真正进入答案的片段上。任何一步失败都只降级、不影响转录结果。

抽帧引擎：WorkBuddy skill「video-understanding」的 extract_keyframes.py
  （逐帧差异检测 → 关键帧 → 拼总览图，纯本地算法，不调用任何模型）。
  读图（把总览图变成理解）由调用方模型完成 —— 本模块只负责产出图。
"""
import json
import os
import shutil
import subprocess
import sys

import cache  # 复用其 DB 目录作为帧产物根目录

HERE = os.path.dirname(os.path.abspath(__file__))

# 抽帧引擎位置与本机 Python（均可被环境变量覆盖，便于换机器）
PYTHON = os.environ.get("VDB_PYTHON", r"D:\python\python.exe")
KEYFRAMES_SCRIPT = os.environ.get(
    "VDB_KEYFRAMES_SCRIPT",
    r"C:\Users\86180\.workbuddy\skills\video-understanding\scripts\extract_keyframes.py")
ENABLED = os.environ.get("VDB_FRAMES_ENABLED", "1") not in ("0", "false", "no")

# 与 video-understanding SKILL.md 对齐的固定安全档位：
# 每张 ≤12 格 / 3 列 / 宽 1600 —— 每格在模型眼里 ~380px，字幕可读
SHEET_ARGS = ["--cols", "3", "--sheet-max-cells", "12", "--sheet-width", "1600"]

FFMPEG = os.environ.get("FFMPEG_BIN") or (shutil.which("ffmpeg") or "ffmpeg")


def frames_dir(bvid: str) -> str:
    return os.path.join(os.path.dirname(cache.DB), "frames", bvid)


def frames_for(bvid: str):
    """已有帧产物则返回 (overviews, meta_path)；否则 (None, None)。"""
    d = frames_dir(bvid)
    meta_path = os.path.join(d, "keyframes.json")
    if not os.path.isdir(d) or not os.path.exists(meta_path):
        return None, None
    try:
        meta = json.load(open(meta_path, encoding="utf-8"))
    except Exception:
        return None, None
    overviews = [p for p in (meta.get("overviews") or []) if os.path.exists(p)]
    return (overviews or None), (meta_path if os.path.exists(meta_path) else None)


def download_video(stream_url: str, seconds: int, out_mp4: str,
                   start: int = 0) -> tuple:
    """拉 360P 合流 MP4（音画一体）。返回 (ok, err)。与 asr.extract_audio 同款请求头。"""
    cmd = [FFMPEG, "-y", "-loglevel", "error",
           "-headers", "Referer: https://www.bilibili.com/\r\n",
           "-user_agent", ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/131.0.0.0 Safari/537.36")]
    if start:
        cmd += ["-ss", str(start)]
    # 注意与 asr.py 的区别：这里要画面，用 copy 不转码，360P 几乎零开销
    cmd += ["-i", stream_url, "-t", str(seconds),
            "-map", "0:v:0", "-c:v", "copy", "-an", out_mp4]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception as e:
        return False, str(e)
    if p.returncode != 0 or not os.path.exists(out_mp4):
        return False, (p.stderr or "")[:300]
    return True, ""


def extract(mp4_path: str, out_dir: str, deadline=None):
    """调 video-understanding 抽帧。返回 (overviews, meta_path, err)。

    失败一律返回 err 字符串，绝不抛异常 —— 画面线是增益项，不能拖垮主链路。
    """
    if not os.path.exists(KEYFRAMES_SCRIPT):
        return None, None, "抽帧脚本不存在: %s" % KEYFRAMES_SCRIPT
    if not os.path.exists(PYTHON):
        return None, None, "Python 不存在: %s" % PYTHON
    cmd = [PYTHON, KEYFRAMES_SCRIPT, mp4_path,
           "-o", out_dir, "-q", "--json"] + SHEET_ARGS
    try:
        timeout = None
        if deadline:
            timeout = max(30, deadline - _now())
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return None, None, "抽帧超时"
    except Exception as e:
        return None, None, "抽帧异常: %s" % e
    if p.returncode != 0:
        return None, None, "抽帧失败: %s" % (p.stderr or "")[:200]
    try:
        meta = json.loads((p.stdout or "").strip().splitlines()[-1])
    except Exception:
        return None, None, "抽帧输出无法解析"
    overviews = [x for x in (meta.get("overviews") or []) if os.path.exists(x)]
    if not overviews:
        return None, None, "抽帧未产出总览图"
    meta_path = os.path.join(out_dir, "keyframes.json")
    return overviews, (meta_path if os.path.exists(meta_path) else None), ""


def _now():
    import time
    return time.time()


def ensure_for_video(client, video, seconds, deadline):
    """确保该视频有画面证据。返回 (overviews, meta_path, err)。

    已有缓存（frames/<bvid>/）直接命中；否则拉流 → 抽帧 → 删临时 mp4。
    """
    if not ENABLED:
        return None, None, "画面线未启用（VDB_FRAMES_ENABLED=0）"
    bvid = video["bvid"]
    hit_over, hit_meta = frames_for(bvid)
    if hit_over:
        return hit_over, hit_meta, ""

    import time as _t
    if deadline and _t.time() > deadline - 20:  # 给抽帧至少留 20s
        return None, None, "时间预算耗尽（抽帧前）"

    stream = client.playurl(bvid, video.get("_cid") or _cid_of(client, bvid))
    if not stream:
        return None, None, "取播放流失败"
    tmpdir = os.path.join(frames_dir_root(), "_tmp_" + bvid)
    os.makedirs(tmpdir, exist_ok=True)
    mp4 = os.path.join(tmpdir, "v.mp4")
    try:
        ok, err = download_video(stream, seconds, mp4)
        if not ok:
            return None, None, "拉流失败: %s" % err
        out = frames_dir(bvid)
        os.makedirs(out, exist_ok=True)
        overviews, meta_path, err = extract(mp4, out, deadline)
        return overviews, meta_path, err
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def frames_dir_root():
    return os.path.join(os.path.dirname(cache.DB), "frames")


def _cid_of(client, bvid: str):
    try:
        return client.view(bvid).get("cid")
    except Exception:
        return None
