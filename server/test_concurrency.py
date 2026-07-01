"""Concurrency tests for the synapse_v2 HTTP server.

Covers the 2026-07-01 freeze: a silent keep-alive client parked the lone
HTTPServer thread in a blocking read; the accept backlog (default 5) filled
and every new connection timed out — the whole network read as "synapse
down" while the container showed Up.

The teeth are in LATENCY: on a threaded server /ping answers immediately
while a client is parked; on a single-threaded one it stalls for the whole
handler timeout (60s in production — i.e. forever, every time a poller
reconnects). A plain reachability check would pass on both.

Run: python test_concurrency.py
"""

import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request

os.environ["SYNAPSE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "synapse_test.db")
os.environ["SYNAPSE_PORT"] = "13004"

import synapse_v2 as s  # noqa: E402

PASS = []
FAIL = []

# The single-threaded server can serve a queued request once the parked
# handler times out — but only after the FULL handler timeout. Anything
# near-instant proves another thread served it.
MAX_PING_S = 1.0


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))


def timed_ping():
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{s.PORT}/ping", timeout=10) as r:
            ok = r.status == 200
    except Exception:
        ok = False
    return ok, time.monotonic() - t0


# --- 0. production code must ship a finite keep-alive timeout ---------------
prod_timeout = s.V2Handler.timeout
check(
    "production handler has a finite keep-alive timeout (<=120s)",
    isinstance(prod_timeout, (int, float)) and 0 < prod_timeout <= 120,
    f"V2Handler.timeout={prod_timeout!r}",
)

s.init_db()
# Shrink the timeout so the reaper test doesn't wait the production value.
s.V2Handler.timeout = 3
server = s.make_server()
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.3)

ok, dt = timed_ping()
check("baseline: /ping answers fast", ok and dt < MAX_PING_S, f"ok={ok} dt={dt:.2f}s")

# --- 1. connect-and-go-silent client must not stall others ------------------
silent = socket.create_connection(("127.0.0.1", s.PORT))
time.sleep(0.3)  # let the server pick the connection up and park in read
ok, dt = timed_ping()
check("ping fast while silent client parked", ok and dt < MAX_PING_S, f"ok={ok} dt={dt:.2f}s")

# --- 2. keep-alive client that stops talking after one request --------------
ka = socket.create_connection(("127.0.0.1", s.PORT))
ka.sendall(b"GET /ping HTTP/1.1\r\nHost: x\r\n\r\n")
time.sleep(0.3)  # response served; handler now blocks awaiting request #2
ok, dt = timed_ping()
check("ping fast while idle keep-alive client parked", ok and dt < MAX_PING_S, f"ok={ok} dt={dt:.2f}s")

# --- 3. handler timeout reaps the parked connection --------------------------
ka.settimeout(8)
try:
    ka.recv(65536)  # response to the one real request
    reaped = "still-open"
    while True:
        chunk = ka.recv(65536)
        if not chunk:
            reaped = "closed"
            break
except (ConnectionResetError, ConnectionAbortedError):
    reaped = "closed"
except socket.timeout:
    reaped = "still-open"
check("idle keep-alive connection is closed by handler timeout", reaped == "closed", reaped)

# --- 4. burst of parallel requests all answer fast ---------------------------
results = []


def hit():
    results.append(timed_ping())


threads = [threading.Thread(target=hit) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check(
    "8 parallel requests all answered fast",
    len(results) == 8 and all(ok and dt < MAX_PING_S * 3 for ok, dt in results),
    str([(ok, round(dt, 2)) for ok, dt in results]),
)

silent.close()
server.shutdown()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
