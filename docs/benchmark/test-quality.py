#!/usr/bin/env python3
"""一次性质量实测：断点续传、重复分片幂等、并发 complete、并发提交幂等。

基准跑完后执行，避免与基准争抢 CPU/网络。
"""
import hashlib
import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid

BASE = os.environ.get("API_BASE", "http://localhost:9090")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("REPO_ROOT", os.path.abspath(os.path.join(HERE, "..", "..")))
SRC = os.environ.get("BENCH_MEDIA", os.path.join(ROOT, "testmedia", "lesson_15min.mp4"))
CHUNK_SIZE = 1024 * 1024
OUT = os.path.join(HERE, "results", "quality-tests.json")
BASH = os.environ.get("BASH_BIN", "bash")
BENCH_USER = os.environ.get("BENCH_USER", "demo_user")
BENCH_PASSWORD = os.environ.get("BENCH_PASSWORD", "Demo@12345")

results = {}


def login():
    payload = json.dumps({"username": BENCH_USER, "password": BENCH_PASSWORD}).encode()
    req = urllib.request.Request(BASE + "/user/login", data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())["data"]["token"]


def call(path, token, params=None, method="GET", body=None, ctype=None, timeout=120):
    url = BASE + path
    if params:
        url += ("&" if "?" in path else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method, data=body)
    req.add_header("Authorization", "Bearer " + token)
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def multipart(fields, file_field, filename, data):
    boundary = "----bench" + uuid.uuid4().hex
    body = bytearray()
    for key, value in fields.items():
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
             f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
    body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def env_value(key, default=""):
    """从仓库根目录 .env 读取变量，避免把本机密码写进脚本。"""
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    return default


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def mysql(query):
    # 直接调用 mysql 客户端，不经 sh -c，避免 SQL 里的引号被 shell 吃掉
    proc = subprocess.run(
        [BASH, "-c",
         f"cd {ROOT} && docker compose --env-file .env exec -T mysql "
         f"mysql -uroot -p{env_value('MYSQL_ROOT_PASSWORD')} media_db -N -e \"{query}\""],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        print("mysql error:", proc.stderr.strip()[:200])
    return proc.stdout.strip()


def minio_objects(uuid_prefix):
    proc = subprocess.run(
        [BASH, "-c",
         f"cd {ROOT} && docker compose --env-file .env exec -T minio sh -c "
         f"'ls /data/media | grep -c {uuid_prefix}'"],
        capture_output=True, text=True, timeout=120)
    return proc.stdout.strip()


def start_upload(token, filename, total):
    _, body = call("/media/init-upload", token, method="POST",
                   params={"filename": filename, "totalChunks": total})
    return body["data"]


def put_chunk(token, upload_id, index, total, payload, filename):
    raw, ctype = multipart({}, "file", filename, payload)
    return call(f"/media/upload-chunk", token, method="POST",
                params={"uploadId": upload_id, "chunkIndex": index, "totalChunks": total},
                body=raw, ctype=ctype, timeout=300)


def status(token, upload_id):
    _, body = call("/media/upload-status", token, params={"uploadId": upload_id})
    return sorted(body["data"])


def complete(token, upload_id):
    return call("/media/complete-upload", token, method="POST", params={"uploadId": upload_id}, timeout=600)


def test_resume(token, data, filename):
    total = (len(data) + CHUNK_SIZE - 1) // CHUNK_SIZE
    upload_id = start_upload(token, filename, total)
    got = [upload_id]

    first = total * 3 // 5  # 传 60% 后“断线”
    for i in range(first):
        put_chunk(token, upload_id, i, total, data[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE], filename)
    after_interrupt = status(token, upload_id)

    # 重复上传同一个分片（模拟响应丢失后的客户端重试）
    put_chunk(token, upload_id, 2, total, data[2 * CHUNK_SIZE:3 * CHUNK_SIZE], filename)
    dup_status = status(token, upload_id)

    for i in range(first, total):
        put_chunk(token, upload_id, i, total, data[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE], filename)
    resumed = status(token, upload_id)

    code, body = complete(token, upload_id)
    media_id = (body.get("data") or {}).get("id")
    expected_md5 = md5_file(SRC)
    stored = mysql(f"select content_hash, file_path from media_files where id={media_id};") if media_id else ""
    parts = stored.split()
    actual_md5 = parts[0] if parts else ""
    object_uuid = ""
    if len(parts) > 1:
        object_uuid = parts[1].rsplit("/", 1)[-1].split(".")[0]
    rows = mysql(f"select count(*) from media_files where filename='{filename}';")

    return {
        "totalChunks": total,
        "uploadId": upload_id,
        "chunksUploadedBeforeInterrupt": len(after_interrupt),
        "statusAfterInterrupt": after_interrupt,
        "statusAfterDuplicateChunk": dup_status,
        "duplicateChunkDidNotDoubleCount": len(dup_status) == len(after_interrupt),
        "statusAfterResume": len(resumed),
        "completeHttp": code,
        "mediaId": media_id,
        "md5Expected": expected_md5,
        "md5Stored": actual_md5,
        "md5Match": expected_md5 == actual_md5,
        "rowsWithSameFilename": rows,
        "minioObjectsForThisUpload": minio_objects(object_uuid) if object_uuid else "",
    }


def test_concurrent_complete(token, data, filename):
    total = (len(data) + CHUNK_SIZE - 1) // CHUNK_SIZE
    upload_id = start_upload(token, filename, total)
    for i in range(total):
        put_chunk(token, upload_id, i, total, data[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE], filename)

    out = []
    lock = threading.Lock()

    def fire():
        code, body = complete(token, upload_id)
        with lock:
            out.append({"http": code, "code": body.get("code"), "mediaId": (body.get("data") or {}).get("id"),
                        "message": body.get("message")})

    threads = [threading.Thread(target=fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = mysql(f"select count(*) from media_files where filename='{filename}';")
    return {"results": out, "rowsWithSameFilename": rows,
            "onlyOneRecord": rows.strip() == "1"}


def test_concurrent_analysis(token, media_id, tag):
    go = f"并发幂等实测 {tag}：总结素材讲解的知识点"
    log_path = r"D:\dsh-pro-agent\DOVideo-AI-main\backend.log"
    # 先清空该 media 的检查点，确保两次提交都是冷路径，而不是命中已有结果
    subprocess.run([BASH, os.path.join(HERE, "reset-media.sh"), str(media_id)],
                   check=True, capture_output=True, timeout=120)
    before = 0
    try:
        with open(log_path, encoding="utf-8", errors="ignore") as fh:
            before = sum(1 for line in fh if "stage=START" in line and f"taskId={media_id}" in line)
    except FileNotFoundError:
        pass

    out = []
    lock = threading.Lock()

    def fire():
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    BASE + "/analysis/ai?" + urllib.parse.urlencode(
                        {"id": media_id, "goal": go, "mode": "LEARNING"}),
                    method="POST",
                    headers={"Authorization": "Bearer " + token}), timeout=120) as resp:
                code = resp.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        with lock:
            out.append(code)

    threads = [threading.Thread(target=fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    deadline = time.time() + 900
    state = None
    while time.time() < deadline:
        _, body = call("/analysis/analysis-status", token,
                       params={"id": media_id, "goal": go, "mode": "LEARNING"})
        state = (body.get("data") or {}).get("state")
        if state in ("COMPLETED", "FAILED"):
            break
        time.sleep(3)

    time.sleep(3)
    after = 0
    with open(log_path, encoding="utf-8", errors="ignore") as fh:
        after = sum(1 for line in fh if "stage=START" in line and f"taskId={media_id}" in line)

    _, trace_body = call("/analysis/agent-trace", token,
                         params={"id": media_id, "goal": go, "mode": "LEARNING"})
    trace = trace_body.get("data") or {}
    counters = trace.get("counters") or {}
    return {
        "httpCodes": out,
        "finalState": state,
        "startEventsDelta": after - before,
        "modelCalls": counters.get("modelCalls"),
        "tokensIn": counters.get("inputTokensEstimated"),
        "tokensOut": counters.get("outputTokensEstimated"),
        "note": "one START event + 单份 token 视为幂等生效",
    }


def main():
    token = login()
    if os.path.exists(OUT):  # 支持分段重跑，不覆盖已有结果
        try:
            with open(OUT, encoding="utf-8") as fh:
                results.update(json.load(fh))
        except Exception:
            pass 
    with open(SRC, "rb") as fh:
        data = fh.read()
    tag = int(time.time())
    filename = f"resume_bench_{tag}.mp4"

    print("A. 断点续传 ...")
    only = os.environ.get("ONLY", "").upper()
    if only in ("", "A"):
        results["resume"] = test_resume(token, data, filename)
        print(json.dumps(results["resume"], ensure_ascii=False, indent=1))

    print("B. 并发 complete ...")
    if only in ("", "B"):
        results["concurrentComplete"] = test_concurrent_complete(token, data, f"concurrent_bench_{tag}.mp4")
        print(json.dumps(results["concurrentComplete"], ensure_ascii=False, indent=1))

    print("C. 并发提交分析 ...")
    if only in ("", "C"):
        results["concurrentAnalysis"] = test_concurrent_analysis(token, 3, tag)
        print(json.dumps(results["concurrentAnalysis"], ensure_ascii=False, indent=1))

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    print("written ->", OUT)


if __name__ == "__main__":
    main()
