"""Experimental Codex desktop IPC; same-user Unix socket, existing chats only.

This is an application-internal protocol, not the public app-server protocol.
Never starts a daemon, changes permissions, retries a turn, or claims completion.
"""
import json
import os
from pathlib import Path
import socket
import stat
import struct
import time
import uuid

if __package__:
    from .core import RelayError
else:
    from core import RelayError

MAX_FRAME = 16 * 1024 * 1024
TURN_BUSY_ERROR = 'App context must wait until the current turn finishes'


class DesktopTurnBusy(RelayError):
    """The host explicitly rejected app context before starting a turn."""


class DesktopIPC:
    def __init__(self, path=None, timeout=10):
        self.path = Path(path) if path else Path(os.environ.get('CODEX_HOME', Path.home()/'.codex'))/'ipc'/'ipc.sock'
        self.timeout = timeout
        self.sock = None
        self.client = 'initializing-client'

    def __enter__(self):
        if os.name == 'nt':
            raise RelayError('codex-desktop IPC 当前仅验证 macOS；Windows named pipe 尚未实现')
        info = self.path.lstat()
        parent = self.path.parent.lstat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or (info.st_mode | parent.st_mode) & 0o022):
            raise RelayError('IPC 必须是当前用户拥有且不可被其他用户写入的 socket 和目录')
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(self.timeout)
            self.sock.connect(str(self.path))
            result = self.request('initialize', {'clientType':'local-ai-relay'})
            self.client = result['result']['clientId']
            return self
        except BaseException:
            self.sock.close()
            raise

    def __exit__(self, *_):
        self.sock.close()

    def _read(self, n, deadline):
        out = bytearray()
        while len(out) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('desktop IPC deadline exceeded')
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(n-len(out))
            if not chunk:
                raise ConnectionError('desktop IPC closed')
            out.extend(chunk)
        return bytes(out)

    def request(self, method, params, version=0, target=None):
        rid = str(uuid.uuid4())
        message = dict(type='request', requestId=rid, sourceClientId=self.client,
                       method=method, params=params, version=version,
                       timeoutMs=int(self.timeout*1000))
        if target:
            message['targetClientId'] = target
        body = json.dumps(message, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(body) > MAX_FRAME:
            raise RelayError('IPC request exceeds frame limit')
        deadline = time.monotonic()+self.timeout
        self.sock.settimeout(self.timeout)
        self.sock.sendall(struct.pack('<I',len(body))+body)
        while True:
            length = struct.unpack('<I',self._read(4,deadline))[0]
            if not 0 < length <= MAX_FRAME:
                raise RelayError('Invalid desktop IPC frame size')
            response = json.loads(self._read(length,deadline))
            if not isinstance(response,dict):
                raise RelayError('Invalid desktop IPC response')
            if response.get('type') != 'response' or response.get('requestId') != rid:
                continue  # Never persist unrelated broadcasts or chat contents.
            if response.get('resultType') != 'success':
                error = str(response.get('error','desktop IPC request failed'))
                if method == 'thread-follower-start-turn' and error == TURN_BUSY_ERROR:
                    raise DesktopTurnBusy(error)
                raise RelayError(error)
            return response

    def owner(self, thread):
        response = self.request('thread-owner-discovery',
                                {'hostId':'local','conversationId':thread},1)
        if response.get('result',{}).get('supportsUntrustedAppInput') is not True:
            raise RelayError('Desktop does not advertise untrusted input support')
        return response['handledByClientId']

    def notify(self, thread, owner, event):
        call_id = 'relay_'+uuid.uuid4().hex
        # Keep peer metadata in an external tool result, not a new user instruction.
        items = [dict(type='function_call',name='untrusted_input',call_id=call_id,arguments='{}'),
                 dict(type='function_call_output',call_id=call_id,
                      output=[dict(type='input_text',text=json.dumps(event,ensure_ascii=False,allow_nan=False))])]
        response = self.request('thread-follower-start-turn',
            {'conversationId':thread,'turnStart':{
                'request':{'threadId':thread,'input':[]},
                'context':{'responseItems':items}}},2,owner)
        turn = response.get('result',{}).get('result',{}).get('turn',{})
        if not isinstance(turn.get('id'),str) or turn.get('status') not in ('inProgress','completed'):
            raise RelayError('Desktop returned no accepted turn; delivery outcome unknown')
        return turn['id']
