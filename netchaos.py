#!/usr/bin/env python3
"""netchaos.py -- a TCP "bad network" simulator used to TEST Moonfall 2.

It is NOT part of the game's communications (the game only uses wolfnet.py).
It is a man-in-the-middle relay that sits between clients and the server:

    client --TCP--> netchaos (--listen) --TCP--> server (--target)

and lets you break the network on purpose, without root (tc/netem needs root):

    delay N     add N ms one-way latency to every chunk, in both directions
    cut         abort every relayed connection (like a router reboot / Wi-Fi drop)
    freeze      stop forwarding on the current connections but keep them open:
                a HALF-OPEN link (cable pulled, laptop asleep).  New
                connections still work, so a client that notices can reconnect.
    thaw        resume forwarding on frozen connections
    dump on|off print every relayed chunk (shows whether an eavesdropper on the
                path can read the traffic: plain tcp:// vs tls://)
    status      show connections and counters

Usage:  python3 netchaos.py --listen 0.0.0.0:7778 --target 127.0.0.1:7777 [--delay 50] [--dump]
        then type the commands above.  Also importable (class ChaosProxy) for tests.
"""
import argparse
import asyncio
import sys
import time


def hostport(s):
    h, _, p = s.rpartition(":")
    return h or "0.0.0.0", int(p)


def printable(data: bytes, limit=96):
    shown = "".join(chr(b) if 32 <= b < 127 else "." for b in data[:limit])
    return shown + ("..." if len(data) > limit else "")


class _Link:
    def __init__(self, cid):
        self.cid = cid
        self.writers = []
        self.running = asyncio.Event()
        self.running.set()


class ChaosProxy:
    def __init__(self, listen, target, delay_ms=0, dump=False, capture=False, log=print):
        self.listen, self.target = listen, target
        self.delay_ms, self.dump, self.log = delay_ms, dump, log
        self.capture = bytearray() if capture else None
        self.links: dict[int, _Link] = {}
        self.stats = {"connections": 0, "bytes_c2s": 0, "bytes_s2c": 0, "cuts": 0}
        self._next = 0
        self._srv = None

    async def start(self):
        self._srv = await asyncio.start_server(self._accept, *self.listen)
        return self

    async def close(self):
        self.cut(quiet=True)
        if self._srv:
            self._srv.close()

    # ---------- chaos controls ----------
    def cut(self, quiet=False):
        n = len(self.links)
        for link in list(self.links.values()):
            for w in link.writers:
                w.transport.abort()
        self.stats["cuts"] += 1
        if not quiet:
            self.log(f"[chaos] CUT {n} connection(s)")

    def freeze(self):
        for link in self.links.values():
            link.running.clear()
        self.log(f"[chaos] FROZE {len(self.links)} connection(s) (half-open: no data, no FIN)")

    def thaw(self):
        for link in self.links.values():
            link.running.set()
        self.log("[chaos] thawed")

    # ---------- relay ----------
    async def _accept(self, c_reader, c_writer):
        self._next += 1
        link = _Link(self._next)
        try:
            s_reader, s_writer = await asyncio.open_connection(*self.target)
        except OSError as e:
            self.log(f"[chaos] #{link.cid}: cannot reach target: {e}")
            c_writer.transport.abort()
            return
        self.stats["connections"] += 1
        link.writers = [c_writer, s_writer]
        self.links[link.cid] = link
        peer = c_writer.get_extra_info("peername")
        self.log(f"[chaos] #{link.cid} {peer[0]}:{peer[1]} <-> {self.target[0]}:{self.target[1]}")
        await asyncio.gather(self._pump(link, c_reader, s_writer, "c->s"),
                             self._pump(link, s_reader, c_writer, "s->c"),
                             return_exceptions=True)
        self.links.pop(link.cid, None)

    async def _pump(self, link, reader, writer, direction):
        q: asyncio.Queue = asyncio.Queue()

        async def deliver():               # keeps order while adding latency
            while True:
                due, data = await q.get()
                if data is None:
                    break
                wait = due - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                await link.running.wait()  # frozen => hold the bytes forever
                writer.write(data)
                await writer.drain()

        task = asyncio.create_task(deliver())
        try:
            while data := await reader.read(65536):
                self.stats["bytes_" + direction.replace("->", "2")] += len(data)
                if self.capture is not None:
                    self.capture += data
                if self.dump:
                    self.log(f"[chaos] #{link.cid} {direction} {len(data):5d} B | {printable(data)}")
                q.put_nowait((time.monotonic() + self.delay_ms / 1000, data))
            q.put_nowait((0, None))
            await task
        except (ConnectionError, OSError):
            pass
        finally:
            task.cancel()
            for w in link.writers:
                w.transport.abort() if not link.running.is_set() else w.close()


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", default="0.0.0.0:7778")
    ap.add_argument("--target", default="127.0.0.1:7777")
    ap.add_argument("--delay", type=int, default=0, help="one-way delay in ms")
    ap.add_argument("--dump", action="store_true")
    a = ap.parse_args()
    p = await ChaosProxy(hostport(a.listen), hostport(a.target), a.delay, a.dump,
                         log=lambda s: print(s, flush=True)).start()
    print(f"[chaos] relaying {a.listen} -> {a.target}, delay {a.delay} ms. "
          f"Commands: cut | freeze | thaw | delay N | dump on|off | status", flush=True)
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue = asyncio.Queue()
    loop.add_reader(sys.stdin.fileno(), lambda: lines.put_nowait(sys.stdin.readline()))
    while (line := await lines.get()) != "":
        cmd = line.split()
        if not cmd:
            continue
        if cmd[0] == "cut":
            p.cut()
        elif cmd[0] == "freeze":
            p.freeze()
        elif cmd[0] == "thaw":
            p.thaw()
        elif cmd[0] == "delay" and len(cmd) == 2 and cmd[1].isdigit():
            p.delay_ms = int(cmd[1])
            print(f"[chaos] one-way delay is now {p.delay_ms} ms", flush=True)
        elif cmd[0] == "dump" and len(cmd) == 2:
            p.dump = cmd[1] == "on"
        elif cmd[0] == "status":
            print(f"[chaos] {len(p.links)} live connection(s), delay {p.delay_ms} ms, {p.stats}", flush=True)
        else:
            print("[chaos] commands: cut | freeze | thaw | delay N | dump on|off | status", flush=True)
    await p.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
