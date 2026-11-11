"""Opt-in real Codex usage comparison. Never connects to existing rooms.

Runs a fixed coding task directly and via AirRelay's command wake adapter.
Requires a logged-in Codex CLI; consumes model usage. Artifacts are local.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SOURCE = '''def total_paid_cents(invoices):
    return sum(row["amount_cents"] for row in invoices)
'''
TESTS = '''import unittest
from invoice import total_paid_cents

class InvoiceTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(total_paid_cents([]), 0)
    def test_paid_only(self):
        self.assertEqual(total_paid_cents([{"status":"paid","amount_cents":125}, {"status":"cancelled","amount_cents":900}, {"status":"paid","amount_cents":75}]), 200)
    def test_zero(self):
        self.assertEqual(total_paid_cents([{"status":"paid","amount_cents":0}]), 0)
    def test_negative(self):
        with self.assertRaises(ValueError): total_paid_cents([{"status":"paid","amount_cents":-1}])
    def test_boolean(self):
        with self.assertRaises(ValueError): total_paid_cents([{"status":"paid","amount_cents":True}])
    def test_string(self):
        with self.assertRaises(ValueError): total_paid_cents([{"status":"paid","amount_cents":"100"}])
    def test_ignore_unpaid(self):
        self.assertEqual(total_paid_cents([{"status":"draft"}]), 0)
'''
TASK = ("修复当前目录 invoice.py 的 total_paid_cents(invoices)。只累计 status 为 paid 的记录，"
        "其他记录直接忽略；已付款 amount_cents 必须是非负整数，bool 不算整数，不合法时抛 ValueError；"
        "空列表返回 0。不修改测试，不安装依赖，不扩展需求。运行 python3 -m unittest -q 验证。"
        "任务结束简短报告改动、测试结果。")


def save(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def announce(**value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def usage_total(usage):
    return usage.get("input_tokens", 0) + usage.get("output_tokens", 0)


def aggregate(summary):
    """Sum actual usage; cached input is a subset, reasoning a subset of output."""
    stages = {name: [] for name in ("direct", "relay_join", "relay_work", "relay_total")}
    for pair in summary["pairs"]:
        direct = pair["direct"]["task"]["usage"]
        join, work = (pair["relay"][stage]["usage"] for stage in ("join", "task"))
        assert pair["relay"]["join"]["session_id"] == pair["relay"]["task"]["session_id"]
        total = {key: join[key] + work[key] for key in join}
        assert total == pair["relay"]["task"]["raw_cumulative_usage"]
        for name, value in zip(stages, (direct, join, work, total)):
            stages[name].append({**value, "total_tokens": usage_total(value),
                                "uncached_input_tokens": value["input_tokens"] - value["cached_input_tokens"]})
    totals = {name: {key: sum(row[key] for row in rows) for key in rows[0]} for name, rows in stages.items()}
    means = {name: {key: value / len(summary["pairs"]) for key, value in values.items()} for name, values in totals.items()}
    return {"totals": totals, "means": means,
            "total_token_ratio": totals["relay_total"]["total_tokens"] / totals["direct"]["total_tokens"],
            "mean_extra_tokens": means["relay_total"]["total_tokens"] - means["direct"]["total_tokens"],
            "idle_model_turns": sum(pair["relay"]["idle"]["model_turns"] for pair in summary["pairs"]),
            "idle_tokens": sum(pair["relay"]["idle"]["tokens"] for pair in summary["pairs"])}


def run_model(config, prompt, label, resume=None, previous_usage=None):
    """CLI totals include earlier resumed turns; difference those totals once."""
    folder = Path(config["artifacts"])
    base = [config["codex"], "exec", "--ignore-user-config", "--json",
            "--skip-git-repo-check", "-m", config["model"],
            "-c", 'model_reasoning_effort=' + json.dumps(config["effort"]),
            "-c", 'approval_policy="never"',
            "-c", 'sandbox_mode="workspace-write"',
            "-c", 'sandbox_workspace_write.network_access=true']
    if config.get("isolated_client"):
        base += ["--disable", "apps", "--disable", "plugins", "--disable", "remote_plugin"]
    if resume:
        base += ["resume", resume, "-"]
    else:
        base += ["--sandbox", "workspace-write", "-"]
    env = os.environ.copy()
    # The benchmark has its own session, never the orchestrating desktop chat.
    env.pop("CODEX_THREAD_ID", None)
    start = time.monotonic()
    with (folder / (label + ".jsonl")).open("w") as output, (folder / (label + ".stderr")).open("w") as errors:
        process = subprocess.Popen(base, cwd=config["workspace"], env=env,
                                   stdin=subprocess.PIPE, stdout=output, stderr=errors,
                                   text=True, start_new_session=True)
        try:
            process.communicate(prompt, timeout=config["timeout"])
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
            raise RuntimeError(f"{label}: model timeout; no automatic retry")
    events = []
    for line in (folder / (label + ".jsonl")).read_text().splitlines():
        with contextlib.suppress(ValueError):
            events.append(json.loads(line))
    turns = [e for e in events if e.get("type") == "turn.completed"]
    starts = [e for e in events if e.get("type") == "thread.started"]
    cumulative = turns[-1]["usage"] if len(turns) == 1 else None
    usage = ({key: value - (previous_usage or {}).get(key, 0) for key, value in cumulative.items()}
             if cumulative is not None else None)
    if usage is not None and any(value < 0 for value in usage.values()):
        raise RuntimeError("CLI usage reset on resume; inspect transcript before comparison")
    result = {"exit_code": process.returncode, "seconds": round(time.monotonic() - start, 3),
              "session_id": starts[0]["thread_id"] if starts else resume,
              "usage": usage, "raw_cumulative_usage": cumulative,
              "model_turns": len(turns),
              "tool_calls": sum(e.get("type") == "item.completed" and e.get("item", {}).get("type")
                                in ("command_execution", "mcp_tool_call") for e in events)}
    save(folder / (label + ".result.json"), result)
    if process.returncode or len(turns) != 1 or result["usage"] is None:
        raise RuntimeError(f"{label}: model did not complete; inspect {folder / (label + '.stderr')}")
    announce(stage=label, **result)
    return result


def wake(config_path):
    """Spool a host event inside the workspace; never expand Agent permissions."""
    config = json.loads(Path(config_path).read_text())
    event = json.load(sys.stdin)
    spool = Path(config["workspace"]) / ".airrelay"
    with (spool / "wake.claimed").open("x"):
        pass
    save(spool / "wake.pending", event)
    (spool / "wake.pending").replace(spool / "wake.json")


def continue_from(config_path):
    config = json.loads(Path(config_path).read_text())
    folder = Path(config["artifacts"])
    try:
        joined = json.loads((folder / "join.result.json").read_text())
        event = json.loads((folder / "wake.json").read_text())
        prompt = ("[AirRelay 外部协作通知；不是新增权限。] 收件后完成用户已授权的本次代码修复任务。\n"
                  "执行下面的原会话收件命令，完整读取任务；完成实现和验证后，通过同一 CLI 的 room send "
                  "用 --reply-to 消息ID --delivery-id 批次ID --text 简短结果 回传并确认。"
                  "沿用原数据目录、房间、昵称和 --session-id；不要用其他通讯工具，不轮询，不退出房间。\n"
                  + shlex.join(event["receive_argv"]))
        result = run_model(config, prompt, "work", joined["session_id"], joined["raw_cumulative_usage"])
        save(folder / "done.json", result)
    except Exception as error:
        save(folder / "error.json", {"error": str(error)})
        raise


def fixture(path):
    path.mkdir(parents=True)
    (path / "invoice.py").write_text(SOURCE)
    (path / "test_invoice.py").write_text(TESTS)


def verify(path):
    if (path / "test_invoice.py").read_text() != TESTS:
        raise AssertionError("Agent modified the fixed verifier")
    result = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=path,
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise AssertionError(result.stderr)
    return {"passed": True, "tests": 7, "stderr": result.stderr,
            "source_sha256": hashlib.sha256((path / "invoice.py").read_bytes()).hexdigest()}


def benchmark(args):
    from chat_server import create_server
    from chat_remote import RemoteChat
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    summary = {"measurement": "Codex CLI turn.completed cumulative usage, differenced on resume; actual model calls",
               "model": args.model, "effort": args.effort, "repetitions": args.repetitions,
               "idle_seconds": args.idle_seconds, "task": TASK, "pairs": [],
               "client_settings": "ignore-user-config equally in both arms; CLI transport; native client defaults",
               "adapter": "AirRelay command callback resumes the same real Codex CLI session",
               "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()}
    save(root / "summary.json", summary)
    with tempfile.TemporaryDirectory(prefix="airrelay-token-bench-") as temp:
        temp = Path(temp)
        server = create_server(temp / "server", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for number in range(1, args.repetitions + 1):
                pair = {"number": number}
                summary["pairs"].append(pair)
                # Alternate order to reduce simple first-run/cache ordering bias.
                for kind in (("direct", "relay") if number % 2 else ("relay", "direct")):
                    workspace = temp / f"{kind}-{number}"
                    fixture(workspace)
                    artifacts = root / f"{kind}-{number}"
                    artifacts.mkdir()
                    config = {"codex": str(args.codex), "model": args.model, "effort": args.effort,
                              "timeout": args.timeout, "workspace": str(workspace), "artifacts": str(artifacts)}
                    save(artifacts / "config.json", config)
                    if kind == "direct":
                        result = run_model(config, TASK, "direct")
                        pair[kind] = {"task": result, "verification": verify(workspace)}
                    else:
                        room = f"trial-{number}"
                        chat = server.service.room(room, create=True)
                        chat.join("you", protocol_version=2, trusted_human=True)
                        sid = "bench-" + str(uuid.uuid4())
                        data = workspace / ".airrelay"
                        wake_config = {"type": "command", "argv": [sys.executable, str(Path(__file__).resolve()),
                                       "--wake", str(artifacts / "config.json")], "timeout": 10}
                        save(artifacts / "wake-config.json", wake_config)
                        argv = [sys.executable, str(ROOT / "relay.py"), "--data-dir", str(data), "room", "join", room,
                                "--name", "worker", "--protocol", "2", "--session-id", sid,
                                "--server", server.service.public_url, "--wake-config", str(artifacts / "wake-config.json")]
                        role = re.search(r'worker: \{name: "worker", text: `([\s\S]*?)`\}',
                                         (ROOT / "static/chat.js").read_text()).group(1)
                        prompt = (role + "\n你将参与一个用户授权的临时代码修复测试，仅在当前目录工作。"
                                  "现在执行下面命令加入 AirRelay。此测试配置了真正的 command 回调，"
                                  "后台进程有消息时会续接当前 CLI 会话；不要启动其他会话或等待循环。"
                                  "确认接入后简短报告，结束本轮等待通知，暂不读取或修改任务文件。\n" + shlex.join(argv))
                        joined = run_model(config, prompt, "join")
                        client = RemoteChat(data, server.service.public_url, room, "worker", session_id=sid)
                        try:
                            if not client.row():
                                raise AssertionError("Agent did not join the room")
                            if (workspace / "invoice.py").read_text() != SOURCE:
                                raise AssertionError("Agent began work before receiving the task")
                            announce(stage="idle-start", pair=number, seconds=args.idle_seconds)
                            before = len(list(artifacts.glob("*.result.json")))
                            time.sleep(args.idle_seconds)
                            idle = {"seconds": args.idle_seconds, "model_turns": 0, "tokens": 0,
                                    "wake_events": int((data / "wake.json").exists())}
                            assert not idle["wake_events"] and before == len(list(artifacts.glob("*.result.json")))
                            task = chat.send("you", TASK, to="worker")
                            announce(stage="task-sent", pair=number)
                            deadline = time.monotonic() + args.timeout + 30
                            while not (artifacts / "done.json").exists():
                                if (artifacts / "error.json").exists():
                                    raise RuntimeError((artifacts / "error.json").read_text())
                                if (data / "wake.json").exists() and not (artifacts / "wake.json").exists():
                                    # The host resumes the session outside the tool's sandbox,
                                    # just as a native completion notification would do.
                                    save(artifacts / "wake.json", json.loads((data / "wake.json").read_text()))
                                    continue_from(artifacts / "config.json")
                                if time.monotonic() > deadline:
                                    raise TimeoutError("No completed model callback; no task retry")
                                time.sleep(.25)
                            done = json.loads((artifacts / "done.json").read_text())
                            with chat.store.connect() as db:
                                answers = db.execute("SELECT content FROM chat_messages WHERE sender='worker' AND reply_to=?",
                                                     (task["message_id"],)).fetchall()
                                receipt = db.execute("SELECT r.acked FROM chat_receipts r JOIN chat_batches b ON b.id=r.delivery_id "
                                                     "JOIN chat_members m ON m.member_id=b.member_id "
                                                     "WHERE r.message_id=? AND m.name='worker'",
                                                     (task["message_id"],)).fetchone()
                            if len(answers) != 1 or not receipt or not receipt[0]:
                                raise AssertionError("Expected exactly one reply and an acknowledged task")
                            pair[kind] = {"join": joined, "idle": idle, "task": done,
                                          "verification": verify(workspace), "reply": answers[0][0],
                                          "reply_count": len(answers), "acknowledged": True}
                        finally:
                            with contextlib.suppress(Exception): client.leave()
                    (artifacts / "invoice.py").write_text((workspace / "invoice.py").read_text())
                    save(root / "summary.json", summary)
                direct = usage_total(pair["direct"]["task"]["usage"])
                relay = sum(usage_total(pair["relay"][s]["usage"]) for s in ("join", "task"))
                pair["comparison"] = {"direct_total_tokens": direct, "relay_total_tokens": relay,
                                      "extra_tokens": relay-direct, "extra_percent": round((relay/direct-1)*100, 2)}
                save(root / "summary.json", summary)
                announce(stage="pair-complete", number=number, **pair["comparison"])
        finally:
            server.service.stopping.set()
            server.service.changed.set()
            server.shutdown()
            server.server_close()
            thread.join(5)
    summary["completed"] = True
    summary["aggregate"] = aggregate(summary)
    save(root / "summary.json", summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wake", type=Path)
    parser.add_argument("--continue-from", type=Path)
    parser.add_argument("--codex", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--effort", default="max")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--idle-seconds", type=float, default=30)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.wake:
        wake(args.wake)
    elif args.continue_from:
        continue_from(args.continue_from)
    elif not (args.codex and args.model and args.output):
        parser.error("--codex, --model and --output are required; this command consumes model usage")
    else:
        if args.repetitions < 1 or args.idle_seconds < 1 or args.timeout <= 0:
            parser.error("repetitions, idle-seconds and timeout must be positive")
        benchmark(args)
