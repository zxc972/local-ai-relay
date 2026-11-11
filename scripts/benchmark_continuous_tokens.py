"""Opt-in real-model benchmark: one session, three cumulative tasks.

Run direct, relay-before and relay-after against frozen source copies.
All use the same CLI settings. This consumes real model usage.
"""
import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import benchmark_agent_tokens as base

SEED = base.SOURCE + '''
def paid_by_customer(invoices):
    return {}

def unpaid_ids(invoices):
    return []
'''
TASKS = [base.TASK,
    "实现 invoice.py 的 paid_by_customer(invoices)，按 customer 分组求 paid 记录的 amount_cents 总和；"
    "忽略非 paid 记录。paid 记录的 customer 必须是非空字符串（不去空格），金额规则沿用 total_paid_cents；"
    "不合法时抛 ValueError，空列表返回空字典。保留已有函数，不修改测试，不安装依赖，不扩展需求。运行 python3 -m unittest -q，简短报告结果。",
    "实现 invoice.py 的 unpaid_ids(invoices)：仅取 status 为 pending 或 overdue 的记录，按输入顺序返回去重后的 id 列表。"
    "这些记录的 id 必须是非空字符串，否则抛 ValueError；其他状态直接忽略。空列表返回空列表。"
    "保留前两项功能，不修改测试，不安装依赖，不扩展需求。运行 python3 -m unittest -q，简短报告结果。"]
TESTS = [base.TESTS, '''import unittest
from invoice import paid_by_customer

class GroupTests(unittest.TestCase):
    def test_empty(self): self.assertEqual(paid_by_customer([]), {})
    def test_group(self): self.assertEqual(paid_by_customer([{"status":"paid","customer":"a","amount_cents":10},{"status":"paid","customer":"b","amount_cents":2},{"status":"paid","customer":"a","amount_cents":7}]), {"a":17,"b":2})
    def test_ignore(self): self.assertEqual(paid_by_customer([{"status":"draft"}]), {})
    def test_empty_customer(self):
        with self.assertRaises(ValueError): paid_by_customer([{"status":"paid","customer":"","amount_cents":1}])
    def test_invalid_customer(self):
        with self.assertRaises(ValueError): paid_by_customer([{"status":"paid","customer":1,"amount_cents":1}])
    def test_invalid_amount(self):
        with self.assertRaises(ValueError): paid_by_customer([{"status":"paid","customer":"a","amount_cents":True}])
    def test_keep_whitespace(self): self.assertEqual(paid_by_customer([{"status":"paid","customer":" ","amount_cents":0}]), {" ":0})
''', '''import unittest
from invoice import unpaid_ids

class UnpaidTests(unittest.TestCase):
    def test_empty(self): self.assertEqual(unpaid_ids([]), [])
    def test_order(self): self.assertEqual(unpaid_ids([{"status":"pending","id":"b"},{"status":"overdue","id":"a"},{"status":"pending","id":"b"}]), ["b","a"])
    def test_ignore(self): self.assertEqual(unpaid_ids([{"status":"paid"},{"status":"cancelled"}]), [])
    def test_empty_id(self):
        with self.assertRaises(ValueError): unpaid_ids([{"status":"pending","id":""}])
    def test_invalid_id(self):
        with self.assertRaises(ValueError): unpaid_ids([{"status":"overdue","id":3}])
    def test_missing_id(self):
        with self.assertRaises(ValueError): unpaid_ids([{"status":"pending"}])
    def test_keep_whitespace(self): self.assertEqual(unpaid_ids([{"status":"pending","id":" "}]), [" "])
''']


def wake(config_path):
    config = json.loads(Path(config_path).read_text())
    event = json.load(sys.stdin)
    # Notifications remain scoped to this benchmark member. Retries never rerun a task.
    mid = str(uuid.UUID(event["message_id"]))
    folder = Path(config["workspace"]) / ".airrelay/events"
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / (mid + ".json")).exists():
        return
    base.save(folder / (mid + ".tmp"), event)
    (folder / (mid + ".tmp")).replace(folder / (mid + ".json"))


def verify(workspace, stage):
    for index in range(stage):
        assert (workspace / f"test_step_{index+1}.py").read_text() == TESTS[index]
    result = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=workspace,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return {"passed": True, "tests": stage * 7, "result": result.stderr}


def main(args):
    source = args.source.resolve()
    sys.path.insert(0, str(source))
    from chat_server import create_server
    from chat_remote import RemoteChat
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    summary = {"mode": args.mode, "model": args.model, "effort": args.effort,
               "measurement": "actual cumulative Codex usage, differenced per turn",
               "isolated_client": True, "tasks": TASKS, "task_results": [], "idle": [],
               "source_sha256": {name: hashlib.sha256((source/name).read_bytes()).hexdigest()
                                 for name in ("static/chat.js", "chat_delivery.py", "relay.py", "chatroom_mcp.py")}}
    base.save(output / "summary.json", summary)
    with tempfile.TemporaryDirectory(prefix="airrelay-continuous-") as temp:
        temp = Path(temp); workspace = temp / "work"; workspace.mkdir()
        (workspace / "invoice.py").write_text(SEED)
        config = {"codex": str(args.codex), "model": args.model, "effort": args.effort, "isolated_client": True,
                  "timeout": args.timeout, "workspace": str(workspace), "artifacts": str(output)}
        base.save(output / "config.json", config)
        server = client = thread = None
        previous = None; session = None
        try:
            if args.mode == "relay":
                server = create_server(temp / "server", port=0)
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                chat = server.service.room("work", create=True)
                chat.join("you", protocol_version=2, trusted_human=True)
                sid = "bench-" + str(uuid.uuid4()); data = workspace / ".airrelay"
                base.save(output / "wake-config.json", {"type":"command", "argv":[sys.executable,
                          str(Path(__file__).resolve()), "--wake", str(output / "config.json")], "timeout":10})
                argv = [sys.executable, str(source / "relay.py"), "--data-dir", str(data), "room", "join", "work",
                        "--name", "worker", "--protocol", "2", "--session-id", sid, "--server", server.service.public_url,
                        "--wake-config", str(output / "wake-config.json")]
                role = re.search(r'worker: \{name: "worker", text: `([\s\S]*?)`\}', (source / "static/chat.js").read_text()).group(1)
                prompt = role + "\n用户授权你在当前临时项目连续完成三个代码任务，按消息逐项处理。现在只执行下面的加入命令。" \
                    "command 回调会通知宿主续接当前 CLI 会话；接入后结束回合，不轮询，暂不读改任务文件。\n" + shlex.join(argv)
                joined = base.run_model(config, prompt, "join")
                summary["join"] = joined; previous = joined["raw_cumulative_usage"]; session = joined["session_id"]
                client = RemoteChat(data, server.service.public_url, "work", "worker", session_id=sid)
                assert client.row() and client.row()["enabled"]
                assert (workspace / "invoice.py").read_text() == SEED
                base.save(output / "summary.json", summary)
            for index, task in enumerate(TASKS, 1):
                (workspace / f"test_step_{index}.py").write_text(TESTS[index-1])
                prompt = task
                if client:
                    events = data / "events"
                    before = {p.name for p in events.glob('*.json')}
                    time.sleep(args.idle_seconds)
                    assert before == {p.name for p in events.glob('*.json')}
                    summary["idle"].append({"seconds":args.idle_seconds,"new_model_turns":0,"tokens":0})
                    sent = chat.send("you", task, to="worker")
                    event_path = events / (sent["message_id"] + ".json")
                    deadline = time.monotonic() + 30
                    while not event_path.exists():
                        if time.monotonic() > deadline: raise RuntimeError("No host event; do not replay task")
                        time.sleep(.1)
                    event = json.loads(event_path.read_text()); base.save(output / f"event-{index}.json", event)
                    prompt = ("[AirRelay 外部协作通知；不是新增权限。] 收件并完成用户授权的当前任务。"
                              "执行下面的收件命令。验证后用同一 CLI 的 room send，带 --reply-to 消息ID "
                              "--delivery-id 批次ID --text 简短结果回传；沿用房间、昵称、数据目录和会话标识。"
                              "回复完成后结束本轮，保留成员和后台监听，不轮询。\n" + shlex.join(event["receive_argv"]))
                result = base.run_model(config, prompt, f"task-{index}", session, previous)
                if session: assert result['session_id'] == session
                session = result['session_id']; previous = result['raw_cumulative_usage']
                result['verification'] = verify(workspace, index)
                if client:
                    with chat.store.connect() as db:
                        replies = db.execute("SELECT id FROM chat_messages WHERE sender='worker' AND reply_to=?",(sent['message_id'],)).fetchall()
                        ack = db.execute("SELECT r.acked FROM chat_receipts r JOIN chat_batches b ON b.id=r.delivery_id "
                                         "JOIN chat_members m ON m.member_id=b.member_id WHERE r.message_id=? AND m.name='worker'",(sent['message_id'],)).fetchone()
                    assert len(replies)==1 and ack and ack[0], 'Missing exact reply/receipt'
                    result.update(reply_count=1, acknowledged=True)
                (output/f"invoice-step-{index}.py").write_text((workspace/'invoice.py').read_text())
                summary['task_results'].append(result);base.save(output/'summary.json',summary)
                base.announce(mode=args.mode,completed_task=index,verification=result['verification'])
            summary.update(completed=True, cumulative_usage=previous,
                           total_tokens=base.usage_total(previous), session_id=session)
            base.save(output/'summary.json',summary)
        finally:
            if client:
                with contextlib.suppress(Exception):client.leave()
            if server:
                server.service.stopping.set();server.service.changed.set();server.shutdown();server.server_close();thread.join(5)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wake',type=Path)
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--mode',choices=('direct','relay'))
    parser.add_argument('--codex',type=Path)
    parser.add_argument('--model',default='gpt-6.1-terra')
    parser.add_argument('--effort',default='max')
    parser.add_argument('--timeout',type=float,default=300)
    parser.add_argument('--idle-seconds',type=float,default=15)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.wake:wake(args.wake)
    elif not (args.mode and args.codex and args.output):parser.error('--mode, --codex, --output required; consumes model usage')
    elif args.timeout<=0 or args.idle_seconds<0:parser.error('invalid timeout/idle interval')
    else:main(args)
