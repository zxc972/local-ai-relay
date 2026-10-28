"""Real Docker + authenticated HTTP attack simulation, using disposable data.

No model calls, credentials, real personal files or mock execution backend.
Fails rather than skipping if Docker is unavailable.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from chat_executor import Executor, DockerRunner, job_content, encode, JOB_TYPE
from chat_remote import RemoteChat, request
from chat_server import create_server
from chat_safety import protect_network
from core import RelayError


def verify(context, image, output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    p = {"version": 1, "room": "test", "name": "executor", "server": None, "docker_context": context,
         "image": "sha256:" + "0" * 64, "timeout_seconds": 10, "memory_mb": 128, "work_mb": 4, "jobs_per_hour": 120}
    p["image"] = json.loads(DockerRunner(p).command(["image", "inspect", image]))[0]["Id"]
    runner = DockerRunner(p)
    preflight = runner.preflight()
    checks = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="airelay-executor-attacks-") as folder:
        root = Path(folder).resolve()
        sentinel = root / "host-private.txt"
        secret = "HOST-CANARY-" + uuid.uuid4().hex
        sentinel.write_text(secret)
        before = hashlib.sha256(sentinel.read_bytes()).hexdigest()
        protect_network(root / "server")
        server = create_server(root / "server", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        worker_thread, stopped, failures = None, threading.Event(), []
        try:
            url = server.service.public_url
            invitation = request(url, "/api/rooms", server.service.admin_token, {"action": "create", "room": "test"})["invite"]
            peers = {name: RemoteChat(root / name, url, "test", name) for name in ("alice", "bob")}
            for peer in peers.values():
                peer.join(invitation, client="manual", protocol_version=2)
            worker = Executor(root / "worker", {**p, "server": url})
            worker.connect(invitation)
            def listen():
                try:
                    worker.run(invitation, stopped)
                except BaseException as error:
                    failures.append(error)
            worker_thread = threading.Thread(target=listen, daemon=True)
            worker_thread.start()

            def case(label, code=None, files=None, who="alice", expected="completed", raw=None):
                content = raw if raw is not None else job_content(["python3", "-c", code], files)
                sent = peers[who].send(content, to="executor", key=uuid.uuid4().hex)
                # A real model-free background worker receives and executes.
                # The harness polls only for verification, never running a model.
                deadline = time.monotonic() + 45
                while True:
                    if failures:
                        raise failures[0]
                    packet = peers[who].call("receive")
                    if packet.get("messages"):
                        break
                    assert time.monotonic() < deadline, (label, "no result")
                    time.sleep(.1)
                message = worker_message(peers[who], packet, sent["message_id"])
                outcome = json.loads(message["content"])
                assert outcome["status"] == expected, (label, outcome)
                peers[who].call("ack", {"delivery_id": packet["delivery_id"], "message_ids": [message["id"]]})
                assert sentinel.read_text() == secret, label
                assert secret not in encode(outcome), label
                assert not runner.command(["ps", "-a", "--filter", "name=^/airelay-job-", "--format", "{{.Names}}"]).strip(), label
                checks.append({"case": label, "passed": True, "status": outcome["status"], "cleaned": outcome.get("cleaned", True)})
                print(json.dumps(checks[-1], ensure_ascii=False), flush=True)
                return outcome

            # Positive control: permissions allow useful work without approval.
            result = case("normal_text_job", "from pathlib import Path; print(Path('input.txt').read_text().upper())", {"input.txt": "hello"})
            assert result["stdout"] == "HELLO\n"
            case("host_file_read_write_delete", f"""from pathlib import Path
p=Path({str(sentinel)!r})
for op in (lambda:p.read_text(),lambda:p.write_text('damaged'),lambda:p.unlink()):
    try: op()
    except (FileNotFoundError,PermissionError,OSError): pass
    else: raise AssertionError('host file accessible')
print('host access denied')""")
            case("symlink_escape_and_docker_socket", f"""import os
from pathlib import Path
Path('escape').symlink_to({str(sentinel)!r})
try: Path('escape').read_text()
except OSError: pass
else: raise AssertionError('symlink escaped')
assert not Path('/var/run/docker.sock').exists()
for credential in ('/root/.ssh/id_ed25519','/work/.codex/auth.json'):
    try: Path(credential).read_bytes()
    except OSError: pass
    else: raise AssertionError('credential accessible')
print('no host credentials or socket')""")
            os.environ["AIRELAY_TEST_PRIVATE_SECRET"] = "host-environment-secret"
            try:
                case("host_environment_not_inherited", "import os; assert 'AIRELAY_TEST_PRIVATE_SECRET' not in os.environ; print('environment isolated')")
            finally:
                os.environ.pop("AIRELAY_TEST_PRIVATE_SECRET", None)
            case("network_egress_denied", """import socket
for address in ('1.1.1.1','192.168.5.2','127.0.0.1'):
    try: socket.create_connection((address,443),timeout=.5)
    except OSError: pass
    else: raise AssertionError('network access')
print('network denied')""")
            case("nonroot_readonly_seccomp", """import os
from pathlib import Path
assert os.getuid()==65534
s=Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\t1' in s and 'Seccomp:\t2' in s
assert 'CapEff:\t0000000000000000' in s
try: Path('/etc/airelay-attack').write_text('bad')
except OSError: pass
else: raise AssertionError('root writable')
print('kernel controls active')""")
            alice_data = "ALICE-" + uuid.uuid4().hex
            result = case("requester_a_workspace", "from pathlib import Path; x=Path('input.txt').read_text(); Path('leftover.txt').write_text(x); Path('/tmp/leftover.txt').write_text(x); print(x)", {"input.txt": alice_data})
            assert alice_data in result["stdout"]
            result = case("requester_b_cannot_read_a", "from pathlib import Path; assert not Path('leftover.txt').exists(); assert not Path('/tmp/leftover.txt').exists(); print(Path('input.txt').read_text())", {"input.txt": "BOB-DATA"}, who="bob")
            assert result["stdout"] == "BOB-DATA\n" and alice_data not in encode(result)
            history = peers["bob"].call("history", {"after": 0})
            assert alice_data not in encode(history)
            # A requester cannot retrieve another identity's private job or result.
            with worker.store.connect() as db:
                alice_mid = db.execute("SELECT message_id FROM jobs ORDER BY created_at LIMIT 1").fetchone()[0]
            try:
                peers["bob"].call("context", {"message_id": alice_mid})
            except RelayError as error:
                assert error.status == 404
            else:
                raise AssertionError("other requester's message leaked")
            checks.append({"case": "cross_requester_history_and_idor", "passed": True})
            case("path_traversal_rejected_before_execution", raw=encode({"type": JOB_TYPE, "argv": ["true"], "files": {"../private": "bad"}}), expected="rejected")
            case("permission_escalation_fields_rejected", raw=encode({"type": JOB_TYPE, "argv": ["true"], "files": {}, "network": "host", "mounts": ["/"]}), expected="rejected")
            case("workspace_disk_limit", """from pathlib import Path
try:
    with open('huge','wb') as f:
        for _ in range(32): f.write(b'x'*1048576)
except OSError: print('workspace quota enforced')
else: raise AssertionError('workspace not bounded')""")
            case("output_flood_stopped", "import sys; sys.stdout.write('x'*1000000); sys.stdout.flush()", expected="output_limit")
            worker.runner.policy["timeout_seconds"] = 2
            case("timeout_terminates_container", "import time; time.sleep(60)", expected="timeout")
            worker.runner.policy["timeout_seconds"] = 10
            case("memory_exhaustion_contained", "x=bytearray(1024*1024*1024); print(len(x))", expected="memory_limit")
            case("process_limit", """import subprocess
children=[]
try:
    for _ in range(70): children.append(subprocess.Popen(['sleep','2']))
except OSError: print('process quota enforced')
else: raise AssertionError('process limit missing')
finally:
    for child in children: child.terminate()
    for child in children: child.wait()""")
            case("service_still_works_after_attacks", "print('healthy')", who="bob")
            after = hashlib.sha256(sentinel.read_bytes()).hexdigest()
            assert before == after
            with worker.store.connect() as db:
                states = [dict(row) for row in db.execute("SELECT status,COUNT(*) AS count FROM jobs GROUP BY status")]
            report = {"passed": True, "backend": preflight, "checks": checks, "host_file_hash_unchanged": before == after,
                      "jobs": states, "model_calls": 0, "manual_approvals": 0,
                      "background_worker": True,
                      "elapsed_seconds": round(time.monotonic() - started, 2),
                      "limits": {"cpu": 1, "memory_mb": 128, "work_mb": 4, "pids": 32, "output_bytes": 8192},
                      "scope": "scripted agents; real Docker execution and HTTP routing, not a native Codex/Claude session sandbox"}
            (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            return report
        finally:
            stopped.set()
            if worker_thread:
                worker_thread.join(20)
                assert not worker_thread.is_alive(), "worker did not stop"
            server.service.stopping.set()
            server.shutdown()
            server.server_close()
            thread.join(5)


def worker_message(peer, packet, reply_to):
    matches = [m for m in packet["messages"] if m["reply_to"] == reply_to]
    assert len(matches) == 1, packet
    item = matches[0]
    content = item["content"]
    while not item["complete"]:
        item = peer.call("context", {"message_id": item["id"], "delivery_id": packet["delivery_id"],
                                     "offset": item["next_offset"], "budget": 6000})["messages"][0]
        content += item["content"]
    return {**item, "content": content}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-context", default="default")
    parser.add_argument("--image", default="python:3.12-slim")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.docker_context, args.image, args.output)
    print(json.dumps({"passed": report["passed"], "checks": len(report["checks"]), "elapsed_seconds": report["elapsed_seconds"]}))
