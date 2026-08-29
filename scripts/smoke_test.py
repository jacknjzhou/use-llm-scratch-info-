"""端到端冒烟测试：health → 预置模板 → 上传 → 发起任务 → 轮询结果。

用法：python scripts/smoke_test.py [base_url]
默认 base_url = http://localhost:8008
"""
import json
import sys
import time
import urllib.request
import urllib.error

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8008"


def req(method, path, data=None, raw=False):
    url = BASE + path
    body = None
    headers = {}
    if data is not None:
        if isinstance(data, (dict, list)):
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        else:
            body = data
    r = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            content = resp.read()
            return resp.status, (content if raw else json.loads(content))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def wait_task(task_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, data = req("GET", f"/api/tasks/{task_id}")
        if data["status"] in ("succeeded", "failed", "partially_succeeded"):
            return data
        time.sleep(2)
    raise TimeoutError("任务超时未结束")


def main():
    ok = []

    # 1. health
    s, _ = req("GET", "/api/health")
    ok.append(("GET /api/health", s == 200))

    # 2. 预置模板
    s, schemas = req("GET", "/api/schemas")
    presets = [x for x in schemas if x["is_preset"]]
    ok.append(("预置模板=8", s == 200 and len(presets) == 8))

    # 3. 上传样例 txt
    sample = ("采购合同\n甲方：华信科技有限公司\n乙方：远大贸易有限公司\n"
              "合同编号：HT-2026-0001\n签订日期：2026年8月1日\n"
              "合同金额：人民币120万元，含税\n付款方式：签订后预付30%，验收合格后结清尾款\n"
              "合同期限：2026年9月1日至2027年8月31日\n").encode("utf-8")
    boundary = "----smokeboundary"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; "
            f"filename=\"contract.txt\"\r\nContent-Type: text/plain\r\n\r\n").encode() + sample + \
           f"\r\n--{boundary}--\r\n".encode()
    s, files = req("POST", "/api/files", body, raw=True) if False else _upload(boundary, body)
    ok.append(("文件上传", s == 200 and isinstance(files, list) and len(files) == 1))
    if not ok[-1][1]:
        print("上传失败:", files)
        sys.exit(1)
    file_id = files[0]["file_id"]

    # 4. 配置一个 dummy LLM（默认），验证提取链路走到 LLM 调用边界
    s, cfg = req("POST", "/api/llm-configs", {
        "name": "smoke-dummy", "base_url": "http://127.0.0.1:9/v1",
        "api_key": "sk-smoke", "model": "dummy", "is_default": True})
    ok.append(("LLM 配置创建", s == 200))

    # 5. 发起指定模板任务
    schema = presets[0]
    s, task = req("POST", "/api/extract", {
        "file_ids": [file_id], "schema_id": schema["id"], "priority": 9,
        "selected_fields": ["party_a", "contract_amount", "sign_date", "contract_no"]})
    ok.append(("发起任务(指定模板+加急)", s == 200 and task.get("match_mode") == "assigned"))
    task_id = task["task_id"]

    # 6. 轮询到结束
    data = wait_task(task_id)
    ok.append(("任务进入终态", data["status"] in ("succeeded", "partially_succeeded", "failed")))
    print("  任务终态:", data["status"], "| progress:", data["progress"])
    print("  任务 error:", (data.get("error") or "")[:200])
    for f in data.get("files", []):
        print(f"  文件[{f['status']}] {f['filename']} 覆盖率={f.get('coverage')} error={(f.get('error') or '')[:120]}")

    # 7. 智能匹配模式（auto，不传模板）也应创建成功
    s, task2 = req("POST", "/api/extract", {"file_ids": [file_id], "priority": 1})
    ok.append(("发起任务(智能匹配+低优先)", s == 200 and task2.get("match_mode") == "auto"))
    if s == 200:
        data2 = wait_task(task2["task_id"])
        print("  auto 任务终态:", data2["status"])

    # 8. 模板导出（共享）可用
    s, exp = req("GET", f"/api/schemas/{schema['id']}/export")
    ok.append(("模板导出", s == 200 and "fields" in exp))

    # 9. 任务 JSON 导出
    s, _ = req("GET", f"/api/tasks/{task_id}/export?format=json")
    ok.append(("JSON 导出", s == 200))

    # 10. Excel 导出（HTTP 层返回 200 + 非空）
    s, raw = req("GET", f"/api/tasks/{task_id}/export?format=xlsx", raw=True)
    ok.append(("Excel 导出", s == 200 and len(raw) > 1000))

    print("\n========== 结果 ==========")
    failed = 0
    for name, passed in ok:
        print(("  PASS  " if passed else "  FAIL  ") + name)
        failed += 0 if passed else 1
    print("==========================")
    print(f"{len(ok) - failed}/{len(ok)} 通过")
    sys.exit(1 if failed else 0)


def _upload(boundary, body):
    url = BASE + "/api/files"
    r = urllib.request.Request(url, data=body, method="POST")
    r.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


if __name__ == "__main__":
    main()
