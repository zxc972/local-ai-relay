"""Portable, model-free inbox watcher with durable, at-most-once wake attempts.

The watcher never receives or executes a message. It delivers a metadata event to
a host adapter; the awakened Agent must use the ordinary atomic receive API.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

if __package__:
    from .connection import Connection, other_role
    from .core import RelayError
else:
    from connection import Connection, other_role
    from core import RelayError

POLL_SECONDS = .25
LEASE_SECONDS = 15


def adapter_module():
    if __package__:
        from . import adapters
    else:
        import adapters
    return adapters


class Background:
    def __init__(self, room):
        self.room, self.store = room, room.store
        self.connection = Connection(room)
        with contextlib.closing(self.store.connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS background_listeners (
                    role TEXT PRIMARY KEY CHECK(role IN ('sender','receiver')),
                    adapter TEXT, mode TEXT NOT NULL DEFAULT 'callback',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    generation TEXT NOT NULL, instance TEXT,
                    pid INTEGER, heartbeat REAL, lease_until REAL,
                    state TEXT NOT NULL DEFAULT 'stopped', error TEXT
                );
                CREATE TABLE IF NOT EXISTS background_notifications (
                    message_id TEXT PRIMARY KEY REFERENCES channel_messages(id),
                    role TEXT NOT NULL, state TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0, token TEXT,
                    attempted_at REAL, completed_at REAL, error TEXT
                );
            """)

    def configure(self, role, argv=None, cwd=None, timeout=30, adapter=None):
        """Save trusted local configuration without starting or sending anything."""
        other_role(role)
        if adapter is not None and argv is not None:
            raise RelayError("Specify adapter or argv, not both")
        config = adapter_module().normalize_config(adapter if adapter is not None else {
            "type": "command", "argv": argv, "cwd": cwd, "timeout": timeout})
        if config["type"] == "command":
            config["cwd"] = str(Path(config.get("cwd") or os.getcwd()).expanduser().resolve())
        with self.store.transaction() as db:
            self.connection.check_peer(db, role, required=False)
            previous = self._row(db, role)
            if self._live(previous) or self._occupied(previous):
                raise RelayError("Stop the listener before changing its adapter", 409)
            self._recover(db, role)
            db.execute("INSERT INTO background_listeners(role,adapter,generation) VALUES(?,?,?) "
                       "ON CONFLICT(role) DO UPDATE SET adapter=excluded.adapter,mode='callback',"
                       "enabled=0,generation=excluded.generation,instance=NULL,pid=NULL,heartbeat=NULL,"
                       "lease_until=NULL,state='stopped',error=NULL",
                       (role, json.dumps(config), str(uuid.uuid4())))
        return self.status(role)

    def _row(self, db, role):
        return db.execute("SELECT * FROM background_listeners WHERE role=?", (role,)).fetchone()

    def _live(self, row):
        return bool(row and row["enabled"] and (row["lease_until"] or 0) > self.store.clock())

    def _occupied(self, row):
        return bool(row and row["instance"] and (row["lease_until"] or 0) > self.store.clock())

    def doctor(self, role):
        state = self.status(role)
        if not state["adapter"]:
            return {"room": self.room.name, "role": role, "ready": False, "status": "unconfigured",
                    "delivery_tested": False, "error": "Configure a host wake adapter first"}
        return {"room": self.room.name, "role": role, **adapter_module().doctor(state["adapter"])}

    @staticmethod
    def _recover(db, role):
        # A process may have delivered immediately before crashing. Never repeat
        # that side effect automatically, even when the message is still queued.
        db.execute("UPDATE background_notifications SET state='needs_attention',"
                   "error='Previous wake attempt ended without a durable outcome; retry explicitly' "
                   "WHERE role=? AND state='dispatching'", (role,))

    def status(self, role):
        other_role(role)
        with contextlib.closing(self.store.connect()) as db:
            row = self._row(db, role)
            pending = db.execute("SELECT id,state FROM channel_messages WHERE recipient=? AND state!='done' "
                                 "ORDER BY seq LIMIT 1", (role,)).fetchone()
            notices = [dict(r) for r in db.execute(
                "SELECT n.message_id,n.state,n.attempt,n.error,m.state AS message_state "
                "FROM background_notifications n JOIN channel_messages m ON m.id=n.message_id "
                "WHERE n.role=? AND m.state!='done' ORDER BY m.seq", (role,))]
            running = self._live(row) and row["instance"] is not None
            attention = [n for n in notices if n["state"] == "needs_attention" or (
                n["state"] == "dispatching" and not self._live(row))]
            state = row["state"] if row else "unconfigured"
            if row and row["enabled"] and not self._live(row):
                state = "needs_attention" if attention else "not_running"
            return {"room": self.room.name, "role": role, "enabled": bool(row and row["enabled"]),
                    "running": bool(running), "status": state, "mode": row["mode"] if row else None,
                    "worker_active": self._occupied(row),
                    "configured": bool(row and row["adapter"]),
                    "adapter": json.loads(row["adapter"]) if row and row["adapter"] else None,
                    "pid": row["pid"] if row else None, "heartbeat": row["heartbeat"] if row else None,
                    "error": row["error"] if row else None, "needs_attention": attention,
                    "notifications": notices,
                    "blocked_by": pending["id"] if pending and pending["state"] == "received" else None,
                    "next_command": None,
                    "instructions": "Background waiting makes no model calls. An accepted wake is not proof "
                                    "that the Agent received or completed the message. Do not poll with an AI."}

    def _reserve(self, role, mode, expected_generation=None):
        with self.store.transaction() as db:
            self.connection.check_peer(db, role)
            legacy_row = db.execute("SELECT value FROM meta WHERE key=?", ("listener:" + role,)).fetchone()
            legacy = json.loads(legacy_row[0]) if legacy_row else {}
            if legacy.get("enabled"):
                raise RelayError("Another native/legacy listener is enabled for this role. Stop it first: " +
                                 self.room.command("auto-off", self.room.name, "--as", role), 409)
            previous = self._row(db, role)
            if expected_generation is not None and (not previous or previous["generation"] != expected_generation):
                raise RelayError("Listener configuration changed during preflight; inspect and start again", 409)
            if self._live(previous) or self._occupied(previous):
                raise RelayError("A listener for this role is already running or starting", 409)
            if mode == "callback" and (not previous or not previous["adapter"]):
                raise RelayError("Configure a wake adapter first; a background process alone cannot wake an Agent", 409)
            self._recover(db, role)
            generation = str(uuid.uuid4())
            db.execute("INSERT INTO background_listeners(role,mode,enabled,generation,lease_until,state) "
                       "VALUES(?,?,1,?,?,'starting') ON CONFLICT(role) DO UPDATE SET "
                       "mode=excluded.mode,enabled=1,generation=excluded.generation,instance=NULL,pid=NULL,"
                       "heartbeat=NULL,lease_until=excluded.lease_until,state='starting',error=NULL",
                       (role, mode, generation, self.store.clock() + LEASE_SECONDS))
        return generation

    def start(self, role):
        """Detach using subprocess on Windows and POSIX; never fork or signal a stored PID."""
        other_role(role)
        current = self.status(role)
        if current["running"]:
            return current
        with self.store.transaction() as db:
            self.connection.check_peer(db, role)
            previous = self._row(db, role)
            if self._live(previous) or self._occupied(previous):
                raise RelayError("A listener for this role is already running or starting", 409)
            if not previous or not previous["adapter"]:
                raise RelayError("Configure a wake adapter first; a background process alone cannot wake an Agent", 409)
            checked_generation = previous["generation"]
            checked_adapter = json.loads(previous["adapter"])
        # Check the host entry before advertising an active watcher. Command
        # doctor only inspects its executable/cwd; it never executes the hook.
        preflight = adapter_module().doctor(checked_adapter)
        if preflight.get("ready") is not True:
            detail = preflight.get("error")
            for key in ("endpoint", "version", "queue"):
                probe = preflight.get(key, {})
                if not detail and probe.get("ok") is False:
                    detail = probe.get("detail")
            detail = str(detail or "Wake adapter preflight failed; inspect the host entry with wake doctor")[:2000]
            with self.store.transaction() as db:
                self.connection.check_peer(db, role)
                previous = self._row(db, role)
                if not previous or previous["generation"] != checked_generation:
                    raise RelayError("Listener configuration changed during preflight; inspect and start again", 409)
                self._recover(db, role)
                db.execute("UPDATE background_listeners SET enabled=0,state='needs_attention',error=?,"
                           "instance=NULL,pid=NULL,heartbeat=NULL,lease_until=NULL WHERE role=?",
                           (detail, role))
            return {**self.status(role), "preflight": preflight}
        generation = self._reserve(role, "callback", expected_generation=checked_generation)
        command = [sys.executable, str(Path(__file__).resolve()), "--data-dir", str(self.room.base),
                   "--room", self.room.name, "--role", role, "--generation", generation]
        options = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        log_path = self.store.root / ("background-" + role + ".log")
        try:
            with open(log_path, "ab", buffering=0) as log:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                           close_fds=True, shell=False, **options)
        except (OSError, ValueError) as error:
            self._startup_failed(role, generation, str(error))
            raise RelayError("Unable to start background listener: " + str(error), 500) from error
        # Reap our child in long-lived callers, without keeping the caller alive.
        threading.Thread(target=process.wait, daemon=True).start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = self.status(role)
            if current["running"] or process.poll() is not None:
                break
            time.sleep(.025)
        if process.poll() is not None and not current["running"]:
            self._startup_failed(role, generation, "Worker exited during startup; inspect " + str(log_path))
            current = self.status(role)
        return {**current, "log_file": str(log_path)}

    def _startup_failed(self, role, generation, error):
        with self.store.transaction() as db:
            db.execute("UPDATE background_listeners SET enabled=0,state='needs_attention',error=?,lease_until=NULL "
                       "WHERE role=? AND generation=?", (error, role, generation))

    def stop(self, role):
        other_role(role)
        with self.store.transaction() as db:
            self.connection.check_peer(db, role, required=False)
            row = self._row(db, role)
            active = self._occupied(row)
            # Keep ownership until the current callback has returned. This
            # prevents start/stop racing into a second worker for this role.
            if active and row["state"] != "stopping":
                timeout = json.loads(row["adapter"])["timeout"] if row["adapter"] else 30
                db.execute("UPDATE background_listeners SET enabled=0,state='stopping',lease_until=? "
                           "WHERE role=?", (self.store.clock() + timeout + LEASE_SECONDS, role))
            elif not active:
                db.execute("UPDATE background_listeners SET enabled=0,state='stopped',lease_until=NULL,"
                           "instance=NULL,generation=? WHERE role=?", (str(uuid.uuid4()), role))
        result = self.status(role)
        result["instructions"] = ("Future wake attempts are disabled. An adapter already invoked may finish. "
                                  "No process was killed by stored PID; message history is retained.")
        return result

    def retry(self, role, message_id):
        other_role(role)
        with self.store.transaction() as db:
            self.connection.check_peer(db, role, required=False)
            row = db.execute("SELECT n.*,m.state AS message_state FROM background_notifications n "
                             "JOIN channel_messages m ON m.id=n.message_id WHERE n.role=? AND n.message_id=?",
                             (role, message_id)).fetchone()
            if not row or row["message_state"] != "queued":
                raise RelayError("Only an unreceived message with a previous wake attempt can be retried", 409)
            if row["state"] == "dispatching" and self._live(self._row(db, role)):
                raise RelayError("The wake attempt is still running", 409)
            if row["state"] == "pending":
                raise RelayError("An explicit retry is already pending", 409)
            db.execute("UPDATE background_notifications SET state='pending',token=NULL,error=NULL "
                       "WHERE message_id=?", (message_id,))
        return {"room": self.room.name, "role": role, "message_id": message_id,
                "status": "retry_pending", "instructions": "Retry queued. A running background listener "
                "will make one new wake attempt. The message itself has not been received."}

    def _enter(self, role, generation):
        instance = str(uuid.uuid4())
        with self.store.transaction() as db:
            row = self._row(db, role)
            if not row or not row["enabled"] or row["generation"] != generation:
                return None
            if row["instance"]:
                raise RelayError("This listener generation already has a worker", 409)
            db.execute("UPDATE background_listeners SET instance=?,pid=?,heartbeat=?,lease_until=?,state='listening' "
                       "WHERE role=?", (instance, os.getpid(), self.store.clock(),
                                        self.store.clock() + LEASE_SECONDS, role))
        return instance

    def _heartbeat(self, role, generation, instance):
        with self.store.transaction() as db:
            cursor = db.execute("UPDATE background_listeners SET heartbeat=?,lease_until=? WHERE "
                                "role=? AND enabled=1 AND generation=? AND instance=?",
                                (self.store.clock(), self.store.clock() + LEASE_SECONDS, role, generation, instance))
            return cursor.rowcount == 1

    def _event(self, role, message):
        entry = Path(__file__).resolve().parent / "relay.py"
        argv = [sys.executable, str(entry), "--data-dir", str(self.room.base), "receive", self.room.name,
                "--as", role, "--timeout", "0"]
        return {"schema": "airelay.wake.v1", "event": "message_available", "room": self.room.name,
                "role": role, "message_id": message["id"], "sender": message["sender"],
                "kind": "command" if message["sender"] == "sender" else "result",
                "created_at": message["created_at"], "receive_argv": argv,
                "receive_command": self.room.command("receive", self.room.name, "--as", role, "--timeout", 0),
                "receive_tool": {"name": "relay_receive", "arguments": {"room": self.room.name, "role": role}},
                "instructions": "A message is queued. Receive once, process within the user's authorization, "
                "then reply or acknowledge. Do not poll. This metadata contains no peer instructions."}

    def _next(self, role, generation, instance):
        with self.store.transaction() as db:
            row = self._row(db, role)
            if not row or not row["enabled"] or row["generation"] != generation or row["instance"] != instance:
                return None
            if not db.execute("SELECT 1 FROM channel_peers WHERE role=?", (role,)).fetchone():
                db.execute("UPDATE background_listeners SET enabled=0,state='disconnected' WHERE role=?", (role,))
                return None
            if db.execute("SELECT 1 FROM channel_messages WHERE recipient=? AND state='received'", (role,)).fetchone():
                return None
            message = db.execute("SELECT * FROM channel_messages WHERE recipient=? AND state='queued' "
                                 "ORDER BY seq LIMIT 1", (role,)).fetchone()
            if not message:
                return None
            notice = db.execute("SELECT * FROM background_notifications WHERE message_id=?", (message["id"],)).fetchone()
            if notice and notice["state"] != "pending":
                return None
            token = str(uuid.uuid4())
            db.execute("INSERT INTO background_notifications(message_id,role,state,attempt,token,attempted_at) "
                       "VALUES(?,?,'dispatching',1,?,?) ON CONFLICT(message_id) DO UPDATE SET "
                       "state='dispatching',attempt=attempt+1,token=excluded.token,attempted_at=excluded.attempted_at,"
                       "completed_at=NULL,error=NULL", (message["id"], role, token, self.store.clock()))
            return self._event(role, message), token, json.loads(row["adapter"]) if row["adapter"] else None

    def _record(self, message_id, token, result):
        accepted = isinstance(result, dict) and result.get("accepted") is True
        error = None if accepted else str(result.get("error") or result.get("status") or "Adapter did not accept event")[:2000]
        with self.store.transaction() as db:
            db.execute("UPDATE background_notifications SET state=?,completed_at=?,error=? WHERE message_id=? AND token=?",
                       ("accepted" if accepted else "needs_attention", self.store.clock(), error, message_id, token))

    def _loop(self, role, generation, stopped, emit=None):
        instance = self._enter(role, generation)
        if instance is None:
            return self.status(role)
        finished = threading.Event()
        lost = threading.Event()

        def pulse():
            while not finished.wait(1):
                try:
                    if not self._heartbeat(role, generation, instance):
                        lost.set()
                        return
                except Exception:
                    lost.set()
                    return

        heartbeat = threading.Thread(target=pulse, daemon=True)
        heartbeat.start()
        error = None
        try:
            while not stopped.is_set() and not lost.is_set():
                if not self._heartbeat(role, generation, instance):
                    break
                selected = self._next(role, generation, instance)
                if selected:
                    event, token, config = selected
                    try:
                        if emit is not None:
                            emit(event)
                            result = {"accepted": True, "status": "accepted"}
                        else:
                            result = adapter_module().deliver(config, event)
                        if not isinstance(result, dict):
                            result = {"accepted": False, "error": "Invalid adapter result"}
                    except Exception as cause:
                        result = {"accepted": False, "error": str(cause)}
                        if emit is not None:
                            # A closed host pipe must not leave an orphan watcher
                            # silently running forever; retry remains explicit.
                            stopped.set()
                    self._record(event["message_id"], token, result)
                stopped.wait(POLL_SECONDS)
        except Exception as cause:
            error = str(cause)
            raise
        finally:
            finished.set()
            heartbeat.join(timeout=2)
            with self.store.transaction() as db:
                db.execute("UPDATE background_listeners SET enabled=0,state=?,error=?,lease_until=NULL,instance=NULL "
                           "WHERE role=? AND generation=? AND instance=?",
                           ("needs_attention" if error else "stopped", error, role, generation, instance))
        return self.status(role)

    def events(self, role, emit, stopped=None):
        """Block in a host extension, emitting events only when a message arrives."""
        generation = self._reserve(role, "events")
        return self._loop(role, generation, stopped or threading.Event(), emit=emit)

    def run(self, role, generation, stopped=None):
        return self._loop(role, generation, stopped or threading.Event())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--room", required=True)
    parser.add_argument("--role", required=True, choices=("sender", "receiver"))
    parser.add_argument("--generation", required=True)
    args = parser.parse_args()
    if __package__:
        from .room_cli import Room
    else:
        from room_cli import Room
    Background(Room(args.room, args.data_dir)).run(args.role, args.generation)


if __name__ == "__main__":
    main()
