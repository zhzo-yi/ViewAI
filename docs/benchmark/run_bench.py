#!/usr/bin/env python3
"""DoVideoAI 解析链路基准脚本。

对同一批素材做多次「冷路径」分析（每次先清空该 media 的检查点与缓存），
记录端到端耗时、分阶段耗时、token 估算与调用计数，逐条写入 JSONL。

不改产品代码；只调用公开 HTTP 接口 + 一个 reset 脚本。
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("API_BASE", "http://localhost:9090")
USER = os.environ.get("BENCH_USER", "demo_user")
PASSWORD = os.environ.get("BENCH_PASSWORD", "Demo@12345")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "results")
ROOT = os.environ.get("REPO_ROOT", os.path.abspath(os.path.join(HERE, "..", "..")))
MEDIA_DIR = os.environ.get("MEDIA_DIR", os.path.join(ROOT, "testmedia"))
MODE = "LEARNING"
GOAL = "总结这段素材讲解的知识点，给出三种遍历的定义与示例推演，并列出核心结论"
# Windows 上 `bash` 常指向 WSL，可用 BASH_BIN 指向 Git Bash
BASH = os.environ.get("BASH_BIN", "bash")

# (tier, 文件名, 计划运行次数)
TIERS = [
    ("short", "lesson_speech.mp4", int(os.environ.get("BENCH_SHORT", "20"))),
    ("mid", "lesson_5min.mp4", int(os.environ.get("BENCH_MID", "5"))),
    ("long", "lesson_15min.mp4", int(os.environ.get("BENCH_LONG", "3"))),
]

TERMINAL = {"COMPLETED", "FAILED"}


def http_json(path, token=None, method="GET", params=None, timeout=120):
    url = BASE + path
    if params:
        url += ("&" if "?" in path else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def login():
    payload = json.dumps({"username": USER, "password": PASSWORD}).encode()
    req = urllib.request.Request(BASE + "/user/login", data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())["data"]["token"]


def list_media(token):
    data = http_json("/media/list", token=token, timeout=30).get("data") or []
    return {m["filename"]: m["id"] for m in data}


def upload(token, filename):
    """用 curl 上传（Python 手写 multipart 容易出错，curl 已验证可用）。"""
    src = os.path.join(MEDIA_DIR, filename)
    # curl 读本地文件比在 Python 里手拼 multipart 稳；Windows 上需传原生路径
    proc = subprocess.run(
        ["curl", "-sS", "-X", "POST", BASE + "/media/upload",
         "-H", "Authorization: Bearer " + token,
         "-F", f"file=@{os.path.abspath(src)};type=video/mp4"],
        capture_output=True, text=True, timeout=900)
    payload = json.loads(proc.stdout)
    if payload.get("code") != 0:
        raise RuntimeError(f"upload failed: {payload}")
    return payload["data"]["id"]


def reset(media_id):
    reset_script = os.path.join(HERE, "reset-media.sh")
    subprocess.run([BASH, reset_script, str(media_id)],
                   check=True, capture_output=True, timeout=120)


def submit(token, media_id):
    return http_json("/analysis/ai", token=token, method="POST",
                     params={"id": media_id, "goal": GOAL, "mode": MODE}, timeout=120)


def poll(token, media_id, deadline_s):
    started = time.time()
    last = None
    while time.time() - started < deadline_s:
        try:
            data = http_json("/analysis/analysis-status", token=token,
                             params={"id": media_id, "goal": GOAL, "mode": MODE}, timeout=60)
            last = data.get("data") or {}
            if last.get("state") in TERMINAL:
                return last
        except Exception as exc:  # 网络抖动不应中断整轮
            print(f"  poll error: {exc}", file=sys.stderr)
        time.sleep(3)
    return last or {"state": "TIMEOUT"}


def trace(token, media_id):
    try:
        return http_json("/analysis/agent-trace", token=token,
                         params={"id": media_id, "goal": GOAL, "mode": MODE}, timeout=60).get("data") or {}
    except Exception as exc:
        print(f"  trace error: {exc}", file=sys.stderr)
        return {}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, "runs.jsonl")
    token = login()
    existing = list_media(token)
    print("existing media:", existing)

    media_ids = {}
    for tier, filename, _ in TIERS:
        if filename in existing:
            media_ids[tier] = existing[filename]
            print(f"[{tier}] reuse media id={media_ids[tier]}")
        else:
            media_ids[tier] = upload(token, filename)
            print(f"[{tier}] uploaded media id={media_ids[tier]}")

    with open(out_path, "a", encoding="utf-8") as sink:
        for tier, filename, runs in TIERS:
            media_id = media_ids[tier]
            for idx in range(1, runs + 1):
                print(f"=== {tier} run {idx}/{runs} (media {media_id}) ===")
                reset(media_id)
                started = time.time()
                try:
                    submit_resp = submit(token, media_id)
                    submit_ok = True
                except Exception as exc:
                    print(f"  submit failed: {exc}", file=sys.stderr)
                    submit_ok = False
                if not submit_ok:
                    sink.write(json.dumps({"tier": tier, "run": idx, "mediaId": media_id,
                                           "submit": "error"}, ensure_ascii=False) + "\n")
                    sink.flush()
                    continue

                deadline = 3600 if tier == "long" else 1800
                status = poll(token, media_id, deadline)
                wall_ms = int((time.time() - started) * 1000)

                snap = trace(token, media_id)
                counters = snap.get("counters") or {}
                stages = snap.get("stageDurationMs") or {}
                result_text = status.get("result") or ""

                record = {
                    "tier": tier,
                    "run": idx,
                    "mediaId": media_id,
                    "state": status.get("state"),
                    "message": status.get("message") or "",
                    "wallMs": wall_ms,
                    "stageDurationMs": stages,
                    "counters": counters,
                    "traceId": snap.get("traceId"),
                    "resultChars": len(result_text) if isinstance(result_text, str) else 0,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                sink.write(json.dumps(record, ensure_ascii=False) + "\n")
                sink.flush()
                print(f"  state={record['state']} wall={wall_ms/1000:.0f}s "
                      f"tokens_in={counters.get('inputTokensEstimated')} "
                      f"tokens_out={counters.get('outputTokensEstimated')}")


if __name__ == "__main__":
    main()
