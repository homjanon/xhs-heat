# -*- coding: utf-8 -*-
"""
xhs-heat · common.py
共享底座：路径、配置、原子读写、HTTP、时间、日志。
纯标准库，零第三方依赖 —— CI 与本地 claw 环境都能直接跑。
"""

import gzip
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DOCS = os.path.join(ROOT, "docs")
DATA_DIR = os.path.join(DOCS, "data")
DAILY_DIR = os.path.join(DOCS, "daily")
REPORT_DIR = os.path.join(DATA_DIR, "reports")
AGG_DIR = os.path.join(DATA_DIR, "agg")
INBOX_DIR = os.path.join(ROOT, "inbox")

CN_TZ = timezone(timedelta(hours=8))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def log(msg=""):
    print(msg, flush=True)


def step(title):
    log("")
    log("=" * 62)
    log(title)
    log("=" * 62)


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------
def now_cn():
    return datetime.now(CN_TZ)


def today_str():
    return now_cn().strftime("%Y-%m-%d")


def parse_ts(value):
    """把各种时间写法解析成带时区的 datetime；解析不了返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # 秒 / 毫秒
        v = float(value)
        if v > 1e12:
            v /= 1000.0
        return datetime.fromtimestamp(v, CN_TZ)
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return parse_ts(int(s))
    s = s.replace("Z", "+00:00")
    fmts = (
        "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d",
    )
    for f in fmts:
        try:
            dt = datetime.strptime(s, f)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CN_TZ)
            return dt
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CN_TZ)
        return dt
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def load_secrets():
    """本地 .env.local（JSON：ENV_NAME -> key）→ os.environ。绝不入仓。"""
    path = os.path.join(ROOT, ".env.local")
    if not os.path.exists(path):
        return
    obj = read_json(path, default={}) or {}
    for k, v in obj.items():
        if isinstance(v, str) and v and not os.environ.get(k):
            os.environ[k] = v


def load_config(path=None):
    path = path or os.path.join(HERE, "sources.json")
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 文件读写（一律 LF、UTF-8、原子替换）
# ---------------------------------------------------------------------------
def _ensure_dir(path):
    if path and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError) as e:
        log("  ⚠️ 读取失败 %s: %s" % (os.path.basename(path), e))
        return default


def write_json(path, obj, indent=None):
    _ensure_dir(os.path.dirname(path))
    data = json.dumps(obj, ensure_ascii=False, indent=indent, sort_keys=False)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(data)
        f.write("\n")
    os.replace(tmp, path)
    return len(data.encode("utf-8"))


def write_text(path, text):
    _ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def read_jsonl(path):
    """支持 .jsonl 与 .jsonl.gz，也支持整份 JSON 数组。"""
    if not os.path.exists(path):
        return []
    opener = gzip.open if path.endswith(".gz") else io.open
    raw = opener(path, "rt", encoding="utf-8")
    with raw as f:
        content = f.read()
    content = content.strip()
    if not content:
        return []
    if content[0] == "[":
        try:
            return json.loads(content)
        except ValueError:
            return []
    out = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def write_jsonl_gz(path, rows):
    _ensure_dir(os.path.dirname(path))
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        for r in rows:
            gz.write((json.dumps(r, ensure_ascii=False) + "\n").encode("utf-8"))
    with open(path, "wb") as f:
        f.write(buf.getvalue())
    return len(buf.getvalue())


# ---------------------------------------------------------------------------
# 哈希与文本
# ---------------------------------------------------------------------------
def sha1_short(text, n=12):
    if not text:
        return ""
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:n]


_FULL2HALF = str.maketrans(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")


def norm_text(text):
    """全角转半角、去所有空白、小写。用于词典匹配。"""
    if not text:
        return ""
    return re.sub(r"\s+", "", str(text).translate(_FULL2HALF)).lower()


_PUNCT = re.compile(r"[\s\u3000!-/:-@\[-`{-~，。！？、；：“”‘’（）《》【】…—·~·]+")


def clean_text(text):
    """去标点空白，保留中英文数字。用于相似度与去重。"""
    if not text:
        return ""
    return _PUNCT.sub("", str(text).translate(_FULL2HALF)).lower()


def truncate(text, n):
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------------------
# HTTP（标准库，带 UA / 超时 / 可选代理绕过）
# ---------------------------------------------------------------------------
def _opener(no_proxy=True):
    if no_proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def http_get(url, timeout=15, headers=None, no_proxy=True, retries=1):
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with _opener(no_proxy).open(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read()
            except Exception:
                pass
            last = (e.code, body)
            if e.code in (403, 404, 410):
                return last
        except Exception as e:  # 网络层
            last = (0, str(e).encode("utf-8"))
        if attempt < retries:
            time.sleep(1.5)
    return last if last else (0, b"")


def http_post_json(url, payload, timeout=20, headers=None, no_proxy=True):
    hdrs = {"User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with _opener(no_proxy).open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        return e.code, body
    except Exception as e:
        return 0, str(e).encode("utf-8")


def decode(body):
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, AttributeError):
            continue
    return ""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def pct(x, digits=1):
    return round(float(x) * 100, digits)


def die(msg, code=1):
    log("❌ " + msg)
    sys.exit(code)
