"""Opt-in, model-free room worker. Untrusted commands ONLY run in Docker.

The operator configures the worker locally. Room messages cannot change its
policy, image, credentials, mounts, destination or resource limits.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

if __package__:
    from .core import RelayError, Store, integer
    from .chatroom import ChatRoom, member_name
    from .room_cli import room_name
else:
    from core import RelayError, Store, integer
    from chatroom import ChatRoom, member_name
    from room_cli import room_name

JOB_TYPE = "airelay.exec.v1"
RESULT_TYPE = "airelay.exec.result.v1"
INPUT_LIMIT = 48000
OUTPUT_LIMIT = 8192
POLICY_KEYS = {"version", "room", "name", "server", "docker_context", "image", "timeout_seconds",
               "memory_mb", "work_mb", "jobs_per_hour"}

# This runs inside the container. No host path is mounted, including /work.
# -I ignores PYTHON* and the current directory while loading this bootstrap.
BOOTSTRAP = """import json,os,pathlib,sys
job=json.load(sys.stdin)
os.umask(0o077)
for name,text in job['files'].items():
    path=pathlib.Path('/work')/name
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(text,encoding='utf-8')
os.chdir('/work')
os.execvpe(job['argv'][0],job['argv'],{'HOME':'/work','TMPDIR':'/tmp','PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8'})
"""


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def validate_job(job):
    if not isinstance(job, dict) or set(job) != {"type", "argv", "files"} or job["type"] != JOB_TYPE:
        raise RelayError("任务只接受 type、argv、files；不允许指定宿主路径、网络、权限、环境变量或回传对象", 400)
    argv, files = job["argv"], job["files"]
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 64 or
            any(not isinstance(v, str) or "\0" in v or len(v) > 24000 for v in argv) or not argv[0]):
        raise RelayError("argv 必须是 1–64 个命令参数", 400)
    if not isinstance(files, dict) or len(files) > 32:
        raise RelayError("files 最多包含 32 个文本文件", 400)
    for name, content in files.items():
        if (not isinstance(name, str) or not name or len(name) > 240 or "\\" in name or
                any(ord(c) < 32 for c in name) or PurePosixPath(name).is_absolute() or
                any(p in ("", ".", "..") for p in name.split("/")) or not isinstance(content, str)):
            raise RelayError("输入文件须为工作区内相对路径和文本；不接受路径跳转、软链接或宿主文件引用", 400)
    names = set(files)
    if any(str(parent) in names for name in names for parent in PurePosixPath(name).parents):
        raise RelayError("输入文件与目录不能重名", 400)
    try:
        size = len(encode(job).encode("utf-8"))
    except (ValueError, UnicodeError):
        raise RelayError("任务包含无效文本", 400) from None
    if size > INPUT_LIMIT:
        raise RelayError("任务及文本输入合计不能超过 48000 UTF-8 字节", 413)
    return job


def job_content(argv, files=None):
    return encode(validate_job({"type": JOB_TYPE, "argv": argv, "files": {} if files is None else files}))


def validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) != POLICY_KEYS or type(policy["version"]) is not int or policy["version"] != 1:
        raise RelayError("执行器配置字段无效；权限只能在本机配置", 400)
    room_name(policy["room"])
    member_name(policy["name"])
    if not isinstance(policy["image"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", policy["image"]):
        raise RelayError("执行镜像必须固定为本机 sha256 镜像 ID，不能使用可变标签", 400)
    if not isinstance(policy["docker_context"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", policy["docker_context"]):
        raise RelayError("Docker context 无效", 400)
    if policy["server"] is not None:
        if __package__:
            from .chat_server import server_url
        else:
            from chat_server import server_url
        server_url(policy["server"])
    integer(policy["timeout_seconds"], "timeout_seconds", 1, 300)
    integer(policy["memory_mb"], "memory_mb", 64, 1024)
    integer(policy["work_mb"], "work_mb", 1, 128)
    integer(policy["jobs_per_hour"], "jobs_per_hour", 1, 120)
    return policy


def load_policy(path):
    path = Path(path).expanduser()
    if path.is_symlink() or path.stat().st_mode & 0o022:
        raise RelayError("配置不能为软链接，也不能允许其他用户写入", 403)
    return validate_policy(json.loads(path.read_text(encoding="utf-8")))


class DockerRunner:
    def __init__(self, policy):
        self.policy = validate_policy(dict(policy))
        executable = shutil.which("docker")
        if not executable:
            raise RelayError("未找到 Docker；拒绝降级到宿主执行", 503)
        self.prefix = [executable, "--context", policy["docker_context"]]
        # Do not allow inherited DOCKER_HOST, SSH agents, tokens or config
        # overrides to redirect the configured daemon or reach the workload.
        self.env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home())}

    def command(self, args, timeout=15):
        try:
            value = subprocess.run(self.prefix + args, env=self.env, stdin=subprocess.DEVNULL,
                                   capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise RelayError("Docker 操作不可用；未改用宿主工具", 503) from None
        if value.returncode:
            raise RelayError("Docker 操作失败；检查所选 context 和运行环境", 503)
        return value.stdout

    def preflight(self):
        info = json.loads(self.command(["info", "--format", "{{json .}}" ]))
        if info.get("OSType") != "linux" or not any("seccomp" in s for s in info.get("SecurityOptions", [])):
            raise RelayError("执行器需要 Linux Docker 与 seccomp；拒绝弱化隔离", 503)
        image = json.loads(self.command(["image", "inspect", self.policy["image"]]))[0]
        if image["Config"].get("Volumes"):
            raise RelayError("执行镜像不能声明持久化卷", 403)
        return {"backend": "docker", "image": image["Id"], "network": "none", "host_mounts": False}

    def create_args(self, name):
        p = self.policy
        return ["create", "--name", name, "--interactive", "--pull", "never", "--restart", "no",
                "--read-only", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--user", "65534:65534", "--pids-limit", "32", "--cpus", "1", "--memory", f"{p['memory_mb']}m",
                "--memory-swap", f"{p['memory_mb']}m", "--ulimit", "nofile=128:128",
                "--ulimit", f"cpu={p['timeout_seconds']}:{p['timeout_seconds']}",
                "--ipc", "private", "--shm-size", "8m", "--log-driver", "none", "--no-healthcheck",
                "--tmpfs", f"/work:rw,nosuid,nodev,size={p['work_mb']}m,mode=700,uid=65534,gid=65534",
                "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=8m,mode=1777",
                "--workdir", "/work", "--env", "HOME=/work", "--entrypoint", "python3",
                p["image"], "-I", "-u", "-c", BOOTSTRAP]

    def remove(self, name):
        # Only names generated and recorded by this worker are accepted.
        if not re.fullmatch(r"airelay-job-[0-9a-f]{32}", name):
            raise RelayError("无效执行容器标识")
        self.command(["rm", "--force", "--volumes", name])

    def recover(self, name):
        # Inspect by exact generated name; absence is safe, daemon failure isn't.
        names = self.command(["ps", "-a", "--filter", "name=^/" + name + "$", "--format", "{{.Names}}"])
        if name in names.decode().splitlines():
            self.remove(name)

    def execute(self, job, name, stopped=None):
        validate_job(job)
        stopped = stopped or threading.Event()
        self.preflight()
        process = None
        result = None
        try:
            self.command(self.create_args(name))
            process = subprocess.Popen(self.prefix + ["start", "--attach", "--interactive", name],
                                       env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True)
            # Nonblocking stdin as well as output: a failed daemon must not
            # hang the worker while it is writing a large input packet.
            pending = memoryview(encode(job).encode("utf-8"))
            collected = {"stdout": bytearray(), "stderr": bytearray()}
            deadline = time.monotonic() + self.policy["timeout_seconds"]
            reason = None
            with selectors.DefaultSelector() as selector:
                for stream, kind in ((process.stdin, "input"), (process.stdout, "stdout"), (process.stderr, "stderr")):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_WRITE if kind == "input" else selectors.EVENT_READ, kind)
                while selector.get_map():
                    if stopped.is_set() or time.monotonic() >= deadline:
                        reason = "cancelled" if stopped.is_set() else "timeout"
                        break
                    for key, _ in selector.select(.1):
                        if key.data == "input":
                            try:
                                written = os.write(key.fd, pending[:4096])
                                pending = pending[written:]
                            except BrokenPipeError:
                                pending = b""
                            if not pending:
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                            continue
                        chunk = os.read(key.fd, 4096)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        room = OUTPUT_LIMIT - sum(len(x) for x in collected.values())
                        collected[key.data].extend(chunk[:max(0, room)])
                        if len(chunk) > room:
                            reason = "output_limit"
                            break
                    if reason:
                        break
            if reason:
                try:
                    self.command(["kill", name])
                except RelayError:
                    state = json.loads(self.command(["inspect", "--format", "{{json .State}}", name]))
                    if state.get("Running"):
                        raise
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                reason = reason or "transport_failure"
            state = json.loads(self.command(["inspect", "--format", "{{json .State}}", name]))
            code = state.get("ExitCode")
            reason = reason or ("memory_limit" if state.get("OOMKilled") else
                                "transport_failure" if state.get("Running") or state.get("Error") or process.returncode != code else
                                "completed" if code == 0 else "failed")
            result = {"status": reason, "exit_code": code,
                      **{k: bytes(v).decode("utf-8", "replace") for k, v in collected.items()},
                      "output_untrusted": True}
        finally:
            if process:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
            # Failure to remove stops this worker; never accept another task
            # while an earlier untrusted container might still be running.
            self.recover(name)
        return {**result, "cleaned": True}


class RoomTransport:
    def __init__(self, data_dir, policy, owner, invite=None):
        self.name, self.owner = policy["name"], owner
        self.remote = bool(policy["server"])
        if self.remote:
            if __package__:
                from .chat_remote import RemoteChat
            else:
                from chat_remote import RemoteChat
            self.chat = RemoteChat(data_dir, policy["server"], policy["room"], self.name)
            self.chat.join(invite, client="manual", protocol_version=2)
        else:
            self.chat = ChatRoom(data_dir, policy["room"])
            self.chat.join(self.name, owner=owner, protocol_version=2)

    def call(self, action, **kwargs):
        if self.remote:
            # Raw transport only: do not invoke native host wake or ActionGate.
            return self.chat.call(action, kwargs)
        if action == "context":
            return self.chat.delivery.context(self.name, self.owner, **kwargs)
        return getattr(self.chat, action)(self.name, owner=self.owner, **kwargs)

    def full_message(self, mid, delivery_id):
        offset, chunks, first = 0, [], None
        while True:
            packet = self.call("context", message_id=mid, delivery_id=delivery_id, offset=offset, budget=6000)
            if not packet.get("messages"):
                raise RelayError("任务消息不可访问", 409)
            item = packet["messages"][0]
            first = first or item
            chunks.append(item["content"])
            if item["complete"]:
                return {**first, "content": "".join(chunks)}
            if item["next_offset"] <= offset:
                raise RelayError("任务分页未前进", 409)
            offset = item["next_offset"]


class Executor:
    def __init__(self, data_dir, policy):
        self.data_dir, self.policy = Path(data_dir).resolve(), validate_policy(dict(policy))
        scope = encode([policy["server"], policy["room"], policy["name"]])
        self.store = Store(self.data_dir / "executors" / hashlib.sha256(scope.encode()).hexdigest()[:24])
        self.runner = DockerRunner(policy)
        self.transport = None
        self.state = "waiting"
        with self.store.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs(message_id TEXT PRIMARY KEY,container TEXT NOT NULL,"
                       "status TEXT NOT NULL,result TEXT,created_at REAL NOT NULL)")
            db.execute("INSERT OR IGNORE INTO meta VALUES('executor:owner',?)", ("executor:" + uuid.uuid4().hex,))
            self.owner = db.execute("SELECT value FROM meta WHERE key='executor:owner'").fetchone()[0]

    @contextlib.contextmanager
    def locked(self):
        import fcntl
        with open(self.store.root / "worker.lock", "a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RelayError("此执行器已经在运行", 409) from None
            lock.seek(0)
            lock.truncate()
            lock.write(str(os.getpid()))
            lock.flush()
            try:
                yield
            finally:
                lock.seek(0)
                lock.truncate()

    def connect(self, invite=None):
        self.runner.preflight()
        # Crash recovery never re-runs a command, even if no result was saved.
        with contextlib.closing(self.store.connect()) as db:
            pending = db.execute("SELECT * FROM jobs WHERE status='running'").fetchall()
        for row in pending:
            self.runner.recover(row["container"])
            self.save(row["message_id"], {"status": "interrupted", "executed_again": False, "cleaned": True})
        self.transport = RoomTransport(self.data_dir, self.policy, self.owner, invite)
        self.transport.call("workerstatus", state=self.state)

    def save(self, mid, result):
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET status='finished',result=? WHERE message_id=?", (encode(result), mid))

    def handle(self, message, delivery_id, stopped=None):
        mid = message["id"]
        if message["recipient"] != self.policy["name"]:
            # Public messages (including other people's results) are never jobs.
            self.transport.call("ack", delivery_id=delivery_id, message_ids=[mid])
            return {"status": "ignored_public"}
        with self.store.transaction() as db:
            old = db.execute("SELECT * FROM jobs WHERE message_id=?", (mid,)).fetchone()
            if old:
                if old["status"] == "running":
                    raise RelayError("任务执行状态未确定，先停止并重启执行器核对", 409)
                result = json.loads(old["result"])
            else:
                recent = db.execute("SELECT COUNT(*) FROM jobs WHERE created_at>?", (time.time() - 3600,)).fetchone()[0]
                if recent >= self.policy["jobs_per_hour"]:
                    # Stop consumption until the local quota window resets.
                    # Do not create an unbounded ledger of rejected requests.
                    return {"status": "rate_limited"}
                container = "airelay-job-" + uuid.uuid4().hex
                db.execute("INSERT INTO jobs VALUES(?,?,'running',NULL,?)", (mid, container, time.time()))
                result = None
        if result is None:
            try:
                # The web composer retains the visible @mention in content.
                # Routing identity above, never this text, determines ownership.
                content = re.sub(r"^@" + re.escape(self.policy["name"]) + r"(?:\s+|[，,:：！？!?]\s*)",
                                 "", message["content"].lstrip(), count=1)
                job = validate_job(json.loads(content))
            except (ValueError, RecursionError, RelayError) as error:
                result = {"status": "rejected", "reason": str(error) if isinstance(error, RelayError) else "需要结构化任务 JSON", "executed": False}
            else:
                # Runtime failures propagate and leave a durable running record.
                # No host fallback and no automatic retry of uncertain work.
                self.state = "processing"
                self.transport.call("workerstatus", state=self.state)
                try:
                    result = self.runner.execute(job, container, stopped)
                finally:
                    self.state = "waiting"
            self.save(mid, result)
        reply = {"type": RESULT_TYPE, "request_id": mid, **result}
        self.transport.call("send", content=encode(reply), to=message["sender"], reply_to=mid,
                            delivery_id=delivery_id, key="executor-result:" + mid)
        self.transport.call("workerstatus", state=self.state)
        return reply

    def step(self, stopped=None):
        packet = self.transport.call("receive")
        delivery_id = packet.get("delivery_id")
        mids = packet.get("pending_message_ids") or [m["id"] for m in packet.get("messages", [])]
        results = []
        for mid in mids:
            message = self.transport.full_message(mid, delivery_id)
            result = self.handle(message, delivery_id, stopped)
            results.append(result)
            if result["status"] == "rate_limited" or (stopped and stopped.is_set()):
                break
        return results

    def run(self, invite=None, stopped=None):
        stopped = stopped or threading.Event()
        with self.locked():
            self.connect(invite)
            def heartbeat():
                while not stopped.wait(5):
                    try:
                        self.transport.call("workerstatus", state=self.state)
                    except RelayError:
                        pass
            pulse = threading.Thread(target=heartbeat, daemon=True)
            pulse.start()
            try:
                while not stopped.is_set():
                    try:
                        results = self.step(stopped)
                    except RelayError as error:
                        # A transport error can be retried after reconnecting:
                        # saved outcomes/claims prevent command duplication.
                        if error.status != 503:
                            raise
                        stopped.wait(3)
                        self.connect()
                        continue
                    stopped.wait(5 if any(r["status"] == "rate_limited" for r in results) else 1)
            finally:
                stopped.set()
                pulse.join(1)
                try:
                    self.transport.call("leave")
                except RelayError:
                    pass


def add_commands(commands):
    root = commands.add_parser("executor", help="预配置权限的一次性容器执行器；不使用宿主 Agent 工具")
    actions = root.add_subparsers(dest="executor_action", required=True)
    init = actions.add_parser("init", help="本机一次性配置；固定镜像、断网、无宿主挂载")
    init.add_argument("--config", required=True)
    init.add_argument("--room", default="work")
    init.add_argument("--name", default="executor")
    init.add_argument("--server")
    init.add_argument("--docker-context", default="default")
    init.add_argument("--image", default="python:3.12-slim", help="已安装的可信镜像；解析后固定 ID")
    init.add_argument("--timeout", type=int, default=30)
    for action in ("doctor", "run"):
        item = actions.add_parser(action)
        item.add_argument("--config", required=True)
        if action == "run":
            item.add_argument("--invite", help="首次加入网络房间的邀请")


def run(args):
    if args.executor_action == "init":
        policy = {"version": 1, "room": args.room, "name": args.name, "server": args.server,
                  "docker_context": args.docker_context, "image": "sha256:" + "0" * 64,
                  "timeout_seconds": args.timeout, "memory_mb": 256, "work_mb": 32, "jobs_per_hour": 120}
        runner = DockerRunner(policy)
        if not isinstance(args.image, str) or args.image.startswith("-"):
            raise RelayError("镜像名称无效")
        info = json.loads(runner.command(["image", "inspect", args.image]))[0]
        policy["image"] = info["Id"]
        DockerRunner(policy).preflight()
        path = Path(args.config).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Never replace an existing operator policy implicitly.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(json.dumps(policy, ensure_ascii=False, indent=2) + "\n")
        return {"status": "configured", "config": str(path), "policy": policy,
                "permissions": "仅本任务文本输入和一次性工作区；断网，无宿主挂载，无账号凭据，无逐次批准"}
    policy = load_policy(args.config)
    if args.executor_action == "doctor":
        return {"ready": True, **DockerRunner(policy).preflight()}
    stopped = threading.Event()
    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, lambda *_: stopped.set())
    try:
        Executor(args.data_dir, policy).run(args.invite, stopped)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return {"status": "stopped"}
