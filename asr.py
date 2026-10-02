"""音频提取 + 本地 ASR。

实测：CPU / base / int8 下，45 秒音频转写约 2.3 秒（约为实时的 20 倍速），
10 分钟视频约 30 秒，可用于交互式检索。
"""
import os
import shutil
import subprocess

# 找 ffmpeg：FFMPEG_BIN → 本机已知绝对路径 → PATH。
# 为什么不能只信 shutil.which：dsh 会清理子进程环境，PATH 里那个
# WinGet shim（AppData\...\WinGet\Links\ffmpeg.exe）实测是断链
# （指向已被清掉的 Packages 目录），which 能返回它、exit 却是
# WinError 3「系统找不到指定的路径」——所以每个候选都要真跑一次 -version。
FFMPEG_CANDIDATES = [
    r"C:\Program Files\SVC Fusion\ffmpeg.exe",
    r"C:\Program Files\SVC Fusion\project\ffmpeg\bin\ffmpeg.exe",
]


def _find_ffmpeg() -> str:
    cands = [os.environ.get("FFMPEG_BIN")] + FFMPEG_CANDIDATES + [shutil.which("ffmpeg")]
    for c in cands:
        if not c or not os.path.exists(c):
            continue
        try:
            p = subprocess.run([c, "-version"], capture_output=True, timeout=15)
            if p.returncode == 0:
                return c
        except Exception:
            continue
    return shutil.which("ffmpeg") or "ffmpeg"


FFMPEG = _find_ffmpeg()
HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.environ.get(
    "ASR_MODEL_DIR", os.path.join(HERE, "models", "base"))

_model = None


def get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(MODEL_DIR, device="cpu", compute_type="int8",
                              cpu_threads=os.cpu_count() or 4)
    return _model


def extract_audio(stream_url: str, seconds: int, out_wav: str,
                  start: int = 0) -> tuple:
    """拉流并转成 16k 单声道 wav。返回 (ok, 错误信息)。"""
    cmd = [FFMPEG, "-y", "-loglevel", "error",
           "-headers", "Referer: https://www.bilibili.com/\r\n",
           "-user_agent", ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/131.0.0.0 Safari/537.36")]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", stream_url, "-vn", "-ac", "1", "-ar", "16000",
            "-t", str(seconds), out_wav]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception as e:
        return False, str(e)
    if p.returncode != 0 or not os.path.exists(out_wav):
        return False, (p.stderr or "")[:300]
    return True, ""


def transcribe(wav: str, language=None) -> tuple:
    """返回 (segments, 识别语言)。segments = [{start, end, text}]"""
    segs, info = get_model().transcribe(wav, beam_size=1, language=language,
                                        vad_filter=True)
    return ([{"start": round(float(s.start), 2),
              "end": round(float(s.end), 2),
              "text": (s.text or "").strip()} for s in segs],
            getattr(info, "language", "") or "")
