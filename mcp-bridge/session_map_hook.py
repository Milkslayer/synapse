#!/usr/bin/env python3
"""SessionStart hook: map this Claude Code session to its Synapse identity.

Claude Code invokes SessionStart hooks with a JSON payload on stdin
({session_id, transcript_path, cwd, source, ...}). The Synapse bridge and
channel plugin pair with each other via their parent PID (the Claude Code
process) — but hooks run through a shell, so OUR parent is the shell, not
Claude. Rather than guess which ancestor is Claude by name, we write the
session mapping for EVERY ancestor PID: exactly one of them is the Claude
process, and the bridge only ever reads session-ppid-{its own PPID}.json.
The rest are shells whose files nothing reads (swept after 7 days).

Wired in ~/.claude/settings.json under hooks.SessionStart. Must never break
session start: all failures are swallowed, exit code is always 0, stdout
stays empty.
"""

import json
import os
import sys
import time

LEASE_DIR = os.environ.get(
    "SYNAPSE_LEASE_DIR",
    os.path.join(os.path.expanduser("~"), ".claude", "synapse-v2"),
)
MAX_DEPTH = 16          # ancestor-walk bound
SWEEP_AFTER_S = 7 * 86400


def _ancestors_windows():
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x2

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),   # ULONG_PTR
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (0, -1):
        return []
    ppid_of = {}
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if k32.Process32First(snap, ctypes.byref(entry)):
            while True:
                ppid_of[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if not k32.Process32Next(snap, ctypes.byref(entry)):
                    break
    finally:
        k32.CloseHandle(snap)
    chain, pid = [], os.getpid()
    for _ in range(MAX_DEPTH):
        pid = ppid_of.get(pid)
        if not pid or pid in chain:
            break
        chain.append(pid)
    return chain


def _ancestors_posix():
    chain, pid = [], os.getppid()
    for _ in range(MAX_DEPTH):
        if pid <= 1 or pid in chain:
            break
        chain.append(pid)
        try:
            with open(f"/proc/{pid}/stat") as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except Exception:
            break
    return chain


def ancestors():
    try:
        return _ancestors_windows() if os.name == "nt" else _ancestors_posix()
    except Exception:
        return [os.getppid()]  # degraded: at least the shell's parent chain root


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    session_id = data.get("session_id")
    if not session_id:
        return
    os.makedirs(LEASE_DIR, exist_ok=True)
    payload = json.dumps({
        "session_id": session_id,
        "transcript_path": data.get("transcript_path"),
        "cwd": data.get("cwd"),
        "source": data.get("source"),
        "written_at": time.time(),
        "hook_pid": os.getpid(),
    }, indent=2)
    for pid in ancestors():
        path = os.path.join(LEASE_DIR, f"session-ppid-{pid}.json")
        tmp = f"{path}.tmp{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, path)
        except OSError:
            pass
    # Sweep old session maps: their PIDs may be reused by future processes.
    cutoff = time.time() - SWEEP_AFTER_S
    try:
        for fn in os.listdir(LEASE_DIR):
            if fn.startswith("session-ppid-") and fn.endswith(".json"):
                p = os.path.join(LEASE_DIR, fn)
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.remove(p)
                except OSError:
                    pass
    except OSError:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
