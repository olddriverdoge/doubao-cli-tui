#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试用的假 doubao CLI：复刻 headless 会话的审批与输出行为。

真正的 CLI 会：
1) 在 stderr（必须是 TTY）打印 `[local runtime: ...] ...? [y/N] `，用 readline 读一行；
2) 结束时在 stdout 打印 JSON。
本脚本按同样协议工作，用来离线验证 TUI。
"""

import json
import os
import sys
import time

CID = "38450000000000001"
RID = "63060000000000001"


def err(text: str) -> None:
    sys.stderr.write(text)
    sys.stderr.flush()


def ask(prompt: str) -> bool:
    err(prompt)
    line = sys.stdin.readline()
    return line.strip().lower() in ("y", "yes")


def write_receipt(answer: str) -> None:
    conf = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    directory = os.path.join(conf, "doubao-cli", "turns", "mock")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{CID}-{RID}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"conversationId": CID, "runId": RID, "answer": answer, "status": "running"}, handle, ensure_ascii=False)


def main(argv: list) -> int:
    workspace = "/tmp"
    for index, item in enumerate(argv):
        if item == "--workspace" and index + 1 < len(argv):
            workspace = argv[index + 1]

    # 只读查询类命令：直接返回结构化结果，不进入任务流程
    if "sessions" in argv and "list" in argv:
        sys.stdout.write(json.dumps({
            "sessions": [
                {"id": CID, "title": "接管审计（mock）"},
                {"id": "38450000000000002", "title": "脚手架冒烟"},
            ],
            "currentId": CID,
        }, ensure_ascii=False))
        return 0
    if "models" in argv:
        sys.stdout.write(json.dumps({
            "models": [
                {"name": "doubao-seed-2-0-pro", "reasoning": "high"},
                {"name": "doubao-seed-2-0-lite", "reasoning": "off"},
            ]
        }, ensure_ascii=False))
        return 0
    if "model" in argv and "select" in argv:
        pos = argv.index("select")
        name = argv[pos + 1] if pos + 1 < len(argv) else "unknown"
        sys.stdout.write(json.dumps({"model": name, "changed": True}, ensure_ascii=False))
        return 0
    if "status" in argv:
        sys.stdout.write(json.dumps({"loggedIn": True, "accountId": "1342919234290264"}, ensure_ascii=False))
        return 0
    if "runtimes" in argv:
        sys.stdout.write(json.dumps({
            "runtimes": [{"name": "local", "available": True, "engine": "node-http-sse", "hostPlatform": "android"}]
        }, ensure_ascii=False))
        return 0

    message = ""
    if "create" in argv:
        pos = argv.index("create")
        message = argv[pos + 1] if pos + 1 < len(argv) else ""
    elif "send" in argv:
        pos = argv.index("send")
        message = argv[pos + 2] if pos + 2 < len(argv) else ""

    err("[mock] accepted, provisioning local runtime\n")
    time.sleep(0.05)

    if not ask(f"\n[local runtime: AskOnRisk; approval-required] Approve local.workspace.apply: ensure_dir (docs); cwd {workspace}? [y/N] "):
        sys.stdout.write(json.dumps({"conversationId": CID, "runId": RID, "status": "failed", "error": "denied"}, ensure_ascii=False))
        return 1
    err("[mock] ensured docs/\n")

    write_receipt("正在读取项目结构…")
    time.sleep(0.2)

    if not ask(f"\n[local runtime: AskOnRisk; approval-required] Approve local.workspace.apply: write_file (TAKEOVER.md); cwd {workspace}? [y/N] "):
        sys.stdout.write(json.dumps({"conversationId": CID, "runId": RID, "status": "failed", "error": "denied"}, ensure_ascii=False))
        return 1
    write_receipt("正在写入 docs/TAKEOVER.md…")
    time.sleep(0.2)

    if not ask(f"\n[local runtime: AskOnRisk; approval-required] Approve Bash: command: ls -la src; cwd {workspace}? [y/N] "):
        sys.stdout.write(json.dumps({"conversationId": CID, "runId": RID, "status": "failed", "error": "denied"}, ensure_ascii=False))
        return 1

    write_receipt("正在汇总审计结论…")
    time.sleep(0.2)
    answer = (
        f"接管审计完成（mock）。\n收到消息：{message[:40]}\n"
        "已确认仓库根是项目目录，docs/TAKEOVER.md 已生成。"
    )
    payload = {
        "platform": "headless",
        "mode": "work",
        "conversationId": CID,
        "runId": RID,
        "status": "completed",
        "accepted": True,
        "reply": {"text": answer},
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
