"""
wolfnet.py -- the ONE-FILE communications library for Moonfall 2 (distributed edition).

Project #1 used one primitive: UNIX domain stream sockets + asyncio, which only
work between processes on the same machine.  Project #2/#3 keeps that transport
and adds a second one so players can join from OTHER machines:

  "unix:/tmp/moonfall/game.sock"  UNIX stream socket.  Identity = SO_PEERCRED
                                  (the kernel tells us the peer's UID).
  "tls://host:7777"               TCP + TLS 1.3 with MUTUAL authentication.
                                  Identity = the CommonName of the client
                                  certificate, signed by the game's own CA.
  "tcp://host:7777"               Plain TCP, nickname identity.  Only allowed
                                  with dev=True; it exists to demonstrate why
                                  TLS is needed (anyone on the path can read it).

A server can listen on several addresses at once (e.g. local players on the
UNIX socket, remote players over TLS); the game code cannot tell the difference.

Wire format (unchanged from Project #1): 4-byte big-endian length + UTF-8 JSON
object with a string "type".  Frames over MAX_FRAME (64 KB) are rejected.

Distributed-systems features (new):
  * Sessions outlive connections.  A dropped player keeps their seat for
    `grace` seconds; the client library reconnects automatically with
    exponential backoff and resumes the session with a secret token.
  * Exactly-once, in-order delivery across reconnects.  Every server->client
    frame carries a per-session "seq"; the server keeps the last REPLAY_MAX
    frames and replays what the client missed.  Every client->server frame
    carries "cseq"; the client keeps unacknowledged frames and resends them
    after a reconnect, and the server drops duplicates.
  * Heartbeats detect dead links.  The server pings every `heartbeat` seconds
    (pings carry the cumulative ack and are used to measure RTT).  A side that
    hears nothing for DEAD_AFTER heartbeats declares the link dead.  This
    catches "half-open" TCP connections (cable pulled, laptop asleep, NAT
    timeout) that never produce an error by themselves.

Security / robustness (Project #1 features kept, plus TLS):
  * Identity is never taken from the client's words: SO_PEERCRED (UNIX) or a
    CA-verified certificate (TLS).  The server stamps "sender" on every message.
  * TLS 1.3 only, client certificates REQUIRED, the client verifies the
    server's certificate and hostname, and refuses a private key file that
    other users can read.
  * Hello / TLS-handshake timeouts, a cap on simultaneous connections and on
    not-yet-authenticated connections per IP address (so a flood of idle TCP
    connections can't lock real players out), per-session token-bucket rate
    limiting, bounded queues everywhere.

Server API
    srv = Server(listen, on_join, on_message, on_leave, *, dev=False,
                 tls=(cert, key, ca), grace=30, heartbeat=3,
                 on_away=None, on_back=None)
        listen: an address string or a list of them (see above)
    await srv.start() / await srv.close()
    srv.send(name, msg)                    # unicast
    srv.multicast(group, msg, exclude=())  # to all members of a named group
    srv.broadcast(msg, exclude=())         # to every player that has a session
    srv.join_group(name, group) / srv.leave_group(name, group) / srv.members(group)
    srv.kick(name, reason)
    srv.info(name) -> {"state", "transport", "addr", "rtt_ms", "away_s"}
  Callbacks (plain functions or coroutines):
    on_join(name)                 a NEW session started
    on_message(name, msg_dict)    an application message ("sender" already set)
    on_leave(name)                the session ENDED (bye, kick, or grace expired)
    on_away(name)                 the connection dropped; the seat is held
    on_back(name, info)           the player reconnected; info = {"mode", "replayed", "gap"}

Client API
    c = Client(addr, *, cert=None, key=None, ca=None, reconnect=True, on_link=None)
    name = await c.connect(nick=None)      # returns the server-assigned name
    await c.send(type, **fields)           # never lost: resent after a reconnect
    msg = await c.recv()                   # dict, or None when the session ends
    await c.close()                        # polite leave (no grace period)
    c.rtt_ms                               # last round-trip time measured by the server
  on_link(state, info) is called with state "down", "up" or "lost".
"""
import asyncio
import collections
import json
import os
import pwd
import re
import secrets
import socket
import ssl
import struct
import time

PROTO = 2
MAX_FRAME = 64 * 1024
HELLO_TIMEOUT = 10.0
SEND_QUEUE_MAX = 256
REPLAY_MAX = 512          # frames kept per session for replay after a reconnect
UNACKED_MAX = 1024        # client frames kept until the server acknowledges them
DEAD_AFTER = 3            # missed heartbeats before a link is declared dead
MAX_CONNS = 64            # simultaneous connections, total
MAX_PENDING_PER_IP = 8    # connections from one IP still in TLS handshake / hello
_HDR = struct.Struct("!I")
_NICK_RE = re.compile(r"[A-Za-z0-9_-]{1,16}")
_USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")   # what useradd accepts


class ProtocolError(Exception):
    pass


class Refused(ConnectionError):
    """The server answered the hello with an error (e.g. 'already connected')."""


# ---------------------------------------------------------------- framing
def encode(msg: dict) -> bytes:
    data = json.dumps(msg, separators=(",", ":")).encode()
    if len(data) > MAX_FRAME:
        raise ProtocolError("message too large")
    return _HDR.pack(len(data)) + data


async def read_frame(reader: asyncio.StreamReader) -> dict:
    (n,) = _HDR.unpack(await reader.readexactly(_HDR.size))
    if n == 0 or n > MAX_FRAME:
        raise ProtocolError(f"bad frame length {n}")
    raw = await reader.readexactly(n)
    try:
        msg = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProtocolError("invalid JSON") from e
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ProtocolError("message must be an object with a 'type'")
    return msg


# ---------------------------------------------------------------- addresses & identity
def parse_addr(addr: str):
    """'unix:/p' | '/p' -> ('unix', '/p', None);  'tls://h:p' -> ('tls', h, p)."""
    if addr.startswith("unix:"):
        return "unix", addr[5:], None
    if "://" in addr:
        scheme, rest = addr.split("://", 1)
        if scheme not in ("tls", "tcp"):
            raise ValueError(f"unknown scheme {scheme!r}")
        host, _, port = rest.rpartition(":")
        return scheme, host.strip("[]") or "0.0.0.0", int(port)
    return "unix", addr, None


def peer_credentials(sock: socket.socket):
    """Ask the kernel who is on the other end of a UNIX socket: (pid, uid, gid)."""
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)


def cert_common_name(cert: dict):
    for rdn in (cert or {}).get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return None


def server_tls_context(cert, key, ca) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(cert, key)
    ctx.load_verify_locations(ca)        # trust ONLY the game's CA, not the system store
    ctx.verify_mode = ssl.CERT_REQUIRED  # no client certificate, no connection
    return ctx


def client_tls_context(cert, key, ca) -> ssl.SSLContext:
    if os.stat(key).st_mode & 0o077:
        raise PermissionError(f"{key} is readable by other users; run: chmod 600 {key}")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)   # verifies server cert + hostname
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_verify_locations(ca)
    ctx.load_cert_chain(cert, key)
    return ctx


async def _maybe_await(fn, *args):
    if fn is None:
        return
    res = fn(*args)
    if asyncio.iscoroutine(res):
        await res


# ---------------------------------------------------------------- server internals
class _Conn:
    """One live connection: a bounded outgoing queue drained by its own writer task."""

    def __init__(self, writer, transport, addr):
        self.writer, self.transport, self.addr = writer, transport, addr
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=SEND_QUEUE_MAX)
        self.last_rx = time.monotonic()
        self.closed = self.said_bye = False
        self.task = asyncio.create_task(self._writer_loop())

    async def _writer_loop(self):
        try:
            while True:
                data = await self.queue.get()
                if data is None:
                    break
                self.writer.write(data)
                await self.writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.close()

    def push(self, data: bytes):
        if self.closed:
            return
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:   # not reading: drop the link; replay covers the rest
            self.close(abort=True)

    def close(self, abort=False):
        if not self.closed:
            self.closed = True
            if abort:
                self.writer.transport.abort()
            else:
                self.writer.close()


class _Session:
    """A player's seat.  It survives reconnects; only bye/kick/grace expiry ends it."""

    def __init__(self, name, rate, burst):
        self.name = name
        self.token = secrets.token_hex(16)
        self.seq = 0                                        # last seq sent to this player
        self.replay = collections.deque(maxlen=REPLAY_MAX)  # (seq, frame bytes)
        self.last_cseq = 0                                  # last client seq processed
        self.conn: _Conn | None = None
        self.away_since = None
        self.expiry: asyncio.Task | None = None
        self.rtt = None
        self.kicked = False
        self.rate, self.burst, self.tokens = rate, burst, float(burst)
        self.last_fill, self.strikes = time.monotonic(), 0

    def allow(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.last_fill) * self.rate)
        self.last_fill = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class Server:
    def __init__(self, listen, on_join=None, on_message=None, on_leave=None, *,
                 dev=False, tls=None, grace=30.0, heartbeat=3.0, on_away=None,
                 on_back=None, socket_mode=0o660, rate=10.0, burst=20, max_conns=MAX_CONNS):
        self.addrs = [listen] if isinstance(listen, str) else list(listen)
        self.dev, self.tls, self.socket_mode = dev, tls, socket_mode
        self.on_join, self.on_message, self.on_leave = on_join, on_message, on_leave
        self.on_away, self.on_back = on_away, on_back
        self.grace, self.heartbeat = grace, heartbeat
        self.rate, self.burst, self.max_conns = rate, burst, max_conns
        self.sessions: dict[str, _Session] = {}
        self.groups: dict[str, set] = {}
        self._listeners, self._unix_paths, self._nconns = [], [], 0
        self._pending: dict[str, int] = {}
        self._tls_ctx = None
        self._hb_task = None
        self._closing = False

    @property
    def path(self):   # Project #1 compatibility: first listening address
        return ", ".join(self.addrs)

    # ---------- lifecycle ----------
    async def start(self):
        for addr in self.addrs:
            kind, host, port = parse_addr(addr)
            if kind == "unix":
                self._listeners.append(await self._start_unix(host))
            elif kind == "tls":
                if self._tls_ctx is None:
                    if not self.tls:
                        raise ValueError("tls:// needs tls=(cert, key, ca)")
                    self._tls_ctx = server_tls_context(*self.tls)
                # Accept plain TCP and upgrade inside _handle, so the per-IP
                # limit applies BEFORE the (expensive) TLS handshake.
                self._listeners.append(await asyncio.start_server(
                    lambda r, w: self._handle(r, w, "tls"), host, port))
            else:
                if not self.dev:
                    raise ValueError("plain tcp:// is insecure and only allowed with dev=True")
                self._listeners.append(await asyncio.start_server(
                    lambda r, w: self._handle(r, w, "tcp"), host, port))
        self._hb_task = asyncio.create_task(self._heartbeat_loop())

    async def _start_unix(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.exists(path):            # stale socket from a crash?
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(path)
                probe.close()
                raise RuntimeError(f"a server is already running on {path}")
            except (ConnectionRefusedError, FileNotFoundError):
                os.unlink(path)
        old = os.umask(0o117)
        try:
            srv = await asyncio.start_unix_server(lambda r, w: self._handle(r, w, "unix"), path=path)
        finally:
            os.umask(old)
        os.chmod(path, self.socket_mode)
        self._unix_paths.append(path)
        return srv

    async def close(self):
        self._closing = True
        if self._hb_task:
            self._hb_task.cancel()
        for s in list(self.sessions.values()):
            if s.expiry:
                s.expiry.cancel()
            if s.conn:
                s.conn.push(encode({"type": "bye"}))
                s.conn.push(None)
        await asyncio.sleep(0.3)
        for lst in self._listeners:
            lst.close()
        for s in list(self.sessions.values()):
            if s.conn:
                s.conn.close()
        for p in self._unix_paths:
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass

    # ---------- routing primitives ----------
    def send(self, name, msg):
        s = self.sessions.get(name)
        if not s:
            return
        s.seq += 1
        data = encode({**msg, "seq": s.seq})
        s.replay.append((s.seq, data))       # kept even if the player is away
        if s.conn:
            s.conn.push(data)

    def multicast(self, group, msg, exclude=()):
        for n in list(self.groups.get(group, ())):
            if n not in exclude:
                self.send(n, msg)

    def broadcast(self, msg, exclude=()):
        for n in list(self.sessions):
            if n not in exclude:
                self.send(n, msg)

    def join_group(self, name, group):
        if name in self.sessions:
            self.groups.setdefault(group, set()).add(name)

    def leave_group(self, name, group):
        self.groups.get(group, set()).discard(name)

    def members(self, group):
        return set(self.groups.get(group, ()))

    def kick(self, name, reason="kicked"):
        s = self.sessions.get(name)
        if not s:
            return
        s.kicked = True
        if s.conn:
            s.conn.push(encode({"type": "error", "text": reason}))
            s.conn.push(None)                # writer flushes, then closes -> session ends
        else:
            asyncio.create_task(self._end(s))

    def info(self, name):
        s = self.sessions.get(name)
        if not s:
            return None
        c = s.conn
        return {"state": "online" if c else "away",
                "transport": c.transport if c else None,
                "addr": c.addr if c else None,
                "rtt_ms": None if s.rtt is None else round(s.rtt * 1000, 2),
                "away_s": None if c else round(time.monotonic() - s.away_since, 1)}

    # ---------- heartbeats ----------
    async def _heartbeat_loop(self):
        while True:
            await asyncio.sleep(self.heartbeat)
            now = time.monotonic()
            for s in list(self.sessions.values()):
                c = s.conn
                if not c:
                    continue
                if now - c.last_rx > self.heartbeat * DEAD_AFTER:
                    print(f"[wolfnet] {s.name}: no traffic for {now - c.last_rx:.1f}s, link is dead", flush=True)
                    c.close(abort=True)      # the handler's read fails -> session goes "away"
                else:
                    c.push(encode({"type": "ping", "t": now, "ack": s.last_cseq,
                                   "rtt": s.rtt}))

    # ---------- per-connection task ----------
    async def _handle(self, reader, writer, transport):
        peer = writer.get_extra_info("peername")
        ip = peer[0] if isinstance(peer, tuple) else "local"
        if self._nconns >= self.max_conns or self._pending.get(ip, 0) >= MAX_PENDING_PER_IP:
            writer.transport.abort()
            return
        self._nconns += 1
        self._pending[ip] = self._pending.get(ip, 0) + 1
        authed = False

        def authenticated():           # called once the hello is accepted
            nonlocal authed
            if not authed:
                authed = True
                self._pending[ip] -= 1
        try:
            if transport == "tls":
                try:
                    await writer.start_tls(self._tls_ctx, ssl_handshake_timeout=HELLO_TIMEOUT)
                except (ssl.SSLError, OSError, asyncio.TimeoutError, ConnectionError) as e:
                    print(f"[wolfnet] TLS handshake from {ip} rejected: {getattr(e, 'verify_message', None) or getattr(e, 'reason', None) or e!r}", flush=True)
                    writer.transport.abort()
                    return
            await self._serve(reader, writer, transport, authenticated)
        finally:
            self._nconns -= 1
            if not authed:
                self._pending[ip] -= 1
            if self._pending.get(ip) == 0:
                del self._pending[ip]

    def _identity(self, writer, transport):
        if transport == "unix":
            _pid, uid, _gid = peer_credentials(writer.get_extra_info("socket"))
            try:
                return pwd.getpwuid(uid).pw_name
            except KeyError:
                return f"uid{uid}"
        if transport == "tls":
            cn = cert_common_name(writer.get_extra_info("peercert"))
            return cn if cn and _USER_RE.fullmatch(cn) else None
        return None                              # plain tcp: no identity at all

    async def _refuse(self, writer, text):
        try:
            writer.write(encode({"type": "error", "text": text}))
            await asyncio.wait_for(writer.drain(), 2)
        except Exception:
            pass
        writer.close()

    async def _serve(self, reader, writer, transport, authenticated):
        try:
            ident = self._identity(writer, transport)
            hello = await asyncio.wait_for(read_frame(reader), HELLO_TIMEOUT)
            if hello["type"] != "hello":
                raise ProtocolError("expected hello")
        except Exception:
            writer.transport.abort()
            return
        nick = hello.get("nick")
        name = nick if (self.dev and isinstance(nick, str) and _NICK_RE.fullmatch(nick)) else ident
        if name is None:
            return await self._refuse(writer, "no verified identity (certificate CN or UNIX user)")

        s = self.sessions.get(name)
        token = hello.get("resume")
        if s is None:
            if isinstance(token, str):
                return await self._refuse(writer, "session expired")
            s, mode = _Session(name, self.rate, self.burst), "new"
            self.sessions[name] = s
        elif isinstance(token, str) and secrets.compare_digest(token, s.token):
            mode = "resume"                      # same client process, came back
        elif s.conn is None:
            mode = "rejoin"                      # new process (e.g. client restarted)
            s.token, s.last_cseq = secrets.token_hex(16), 0
        else:
            return await self._refuse(writer, f"'{name}' is already connected")

        peer = writer.get_extra_info("peername")
        addr = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) else "local"
        conn = _Conn(writer, transport, addr)
        authenticated()
        if s.conn:                               # takeover of a link not yet known dead
            s.conn.close(abort=True)
        if s.expiry:
            s.expiry.cancel()
            s.expiry = None
        was_away = s.away_since is not None
        s.away_since = None

        # Welcome + replay are queued with no 'await' in between, so no other
        # message can slip in out of order.
        last_seq = hello.get("last_seq") if mode == "resume" else s.seq
        last_seq = last_seq if isinstance(last_seq, int) else 0
        missed = [d for q, d in s.replay if q > last_seq]
        oldest = s.replay[0][0] if s.replay else s.seq + 1
        gap = mode == "resume" and last_seq + 1 < oldest
        conn.push(encode({"type": "welcome", "name": name, "session": s.token, "proto": PROTO,
                          "mode": mode, "base": s.seq, "ack": s.last_cseq,
                          "replayed": len(missed), "gap": gap, "heartbeat": self.heartbeat}))
        for d in missed:
            conn.push(d)
        s.conn = conn
        if mode == "new":
            await _maybe_await(self.on_join, name)
        elif was_away or mode == "rejoin":
            await _maybe_await(self.on_back, name, {"mode": mode, "replayed": len(missed), "gap": gap})

        try:
            while not conn.closed:
                msg = await read_frame(reader)
                conn.last_rx = time.monotonic()
                t = msg["type"]
                if t == "pong":
                    if isinstance(msg.get("t"), (int, float)):
                        s.rtt = time.monotonic() - msg["t"]
                    continue
                if t == "bye":
                    conn.said_bye = True
                    break
                cseq = msg.pop("cseq", None)
                if isinstance(cseq, int):
                    if cseq <= s.last_cseq:      # a resent frame we already processed
                        continue
                    s.last_cseq = cseq
                if not s.allow():
                    s.strikes += 1
                    if s.strikes > 30:
                        self.kick(name, "rate limit exceeded")
                        break
                    self.send(name, {"type": "error", "text": "slow down (rate limited)"})
                    continue
                msg["sender"] = name             # authoritative; overrides anything client sent
                try:
                    await _maybe_await(self.on_message, name, msg)
                except Exception as e:           # a game bug must not kill the connection loop
                    print(f"[wolfnet] handler error for {name}: {e!r}", flush=True)
        except (asyncio.IncompleteReadError, ConnectionError, ProtocolError, ssl.SSLError, OSError):
            pass
        finally:
            conn.close()
            conn.task.cancel()
            if s.conn is conn and self.sessions.get(name) is s and not self._closing:
                s.conn = None
                if conn.said_bye or s.kicked or self.grace <= 0:
                    await self._end(s)
                else:                            # hold the seat
                    s.away_since = time.monotonic()
                    s.expiry = asyncio.create_task(self._expire(s))
                    await _maybe_await(self.on_away, name)

    async def _expire(self, s):
        await asyncio.sleep(self.grace)
        if s.conn is None:
            s.expiry = None
            await self._end(s)

    async def _end(self, s):
        if self.sessions.get(s.name) is not s:
            return
        del self.sessions[s.name]
        if s.expiry and s.expiry is not asyncio.current_task():
            s.expiry.cancel()
        for g in self.groups.values():
            g.discard(s.name)
        await _maybe_await(self.on_leave, s.name)


# ---------------------------------------------------------------- client
class Client:
    def __init__(self, addr, *, cert=None, key=None, ca=None, server_name=None,
                 reconnect=True, give_up=60.0, on_link=None):
        self.kind, self.host, self.port = parse_addr(addr)
        self.addr, self.server_name = addr, server_name
        self.ssl = client_tls_context(cert, key, ca) if self.kind == "tls" else None
        self.reconnect, self.give_up, self.on_link = reconnect, give_up, on_link
        self.reader = self.writer = None
        self.name = self.token = None
        self.heartbeat = 3.0
        self.last_seq = 0          # highest server seq delivered to the app
        self.cseq = 0              # last client seq assigned
        self.unacked = collections.deque()
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.rtt_ms = None
        self.stats = {"reconnects": 0, "replayed": 0, "resent": 0, "duplicates": 0}
        self._task = None
        self._closing = False

    async def _open(self):
        if self.kind == "unix":
            return await asyncio.open_unix_connection(self.host)
        if self.kind == "tls":
            return await asyncio.wait_for(asyncio.open_connection(
                self.host, self.port, ssl=self.ssl,
                server_hostname=self.server_name or self.host,
                ssl_handshake_timeout=HELLO_TIMEOUT), HELLO_TIMEOUT + 1)
        return await asyncio.wait_for(asyncio.open_connection(self.host, self.port), HELLO_TIMEOUT)

    async def _handshake(self, nick, resume):
        reader, writer = await self._open()
        hello = {"type": "hello", "proto": PROTO, "nick": nick}
        if resume:
            hello.update(resume=self.token, last_seq=self.last_seq)
        try:
            writer.write(encode(hello))
            await writer.drain()
            msg = await asyncio.wait_for(read_frame(reader), HELLO_TIMEOUT)
        except (asyncio.IncompleteReadError, ssl.SSLError, ProtocolError) as e:
            # With TLS 1.3 the server checks OUR certificate after the handshake has
            # "finished" on our side, so a rejection shows up here as a closed stream.
            writer.transport.abort()
            raise ConnectionError(f"server closed the connection during login "
                                  f"(certificate rejected?): {e!r}") from e
        except BaseException:
            writer.transport.abort()
            raise
        if msg["type"] != "welcome":
            writer.close()
            raise Refused(msg.get("text", "connection refused"))
        self.reader, self.writer = reader, writer
        self.name, self.token = msg["name"], msg["session"]
        self.heartbeat = float(msg.get("heartbeat", 3.0))
        if msg["mode"] != "resume":
            self.last_seq = msg["base"]
            self.unacked.clear()
            self.cseq = 0
        else:                                    # drop what the server already has, resend the rest
            ack = msg.get("ack", 0)
            while self.unacked and self.unacked[0][0] <= ack:
                self.unacked.popleft()
            for _q, data in self.unacked:
                writer.write(data)
            self.stats["resent"] += len(self.unacked)
        self.stats["replayed"] += msg.get("replayed", 0)
        return msg

    async def connect(self, nick=None):
        self._nick = nick
        await self._handshake(nick, resume=False)
        self._task = asyncio.create_task(self._run())
        return self.name

    async def _run(self):
        while True:
            try:
                if await self._read_loop():        # True = server said bye
                    break
            except (asyncio.IncompleteReadError, ConnectionError, ProtocolError,
                    ssl.SSLError, OSError, asyncio.TimeoutError):
                pass
            if self.writer:
                self.writer.transport.abort()
            if self._closing or not self.reconnect or not await self._reconnect():
                break
        self.inbox.put_nowait(None)

    async def _read_loop(self):
        while True:
            # No frame (not even a ping) for DEAD_AFTER heartbeats => the link is dead.
            msg = await asyncio.wait_for(read_frame(self.reader), self.heartbeat * DEAD_AFTER)
            t = msg["type"]
            if t == "ping":
                self.writer.write(encode({"type": "pong", "t": msg.get("t")}))
                ack = msg.get("ack", 0)
                while self.unacked and self.unacked[0][0] <= ack:
                    self.unacked.popleft()
                if msg.get("rtt") is not None:
                    self.rtt_ms = round(msg["rtt"] * 1000, 2)
                continue
            if t == "bye":
                return True
            seq = msg.pop("seq", None)
            if isinstance(seq, int):
                if seq <= self.last_seq:
                    self.stats["duplicates"] += 1
                    continue
                self.last_seq = seq
            self.inbox.put_nowait(msg)

    async def _reconnect(self):
        await _maybe_await(self.on_link, "down", {})
        down_at = time.monotonic()
        deadline, delay, attempt = down_at + self.give_up, 0.25, 0
        while time.monotonic() < deadline and not self._closing:
            attempt += 1
            try:
                w = await self._handshake(self._nick, resume=True)
            except Refused as e:
                if "expired" in str(e):
                    break
            except (OSError, ssl.SSLError, ConnectionError, ProtocolError,
                    asyncio.IncompleteReadError, asyncio.TimeoutError):
                pass
            else:
                self.stats["reconnects"] += 1
                await _maybe_await(self.on_link, "up", {
                    "attempts": attempt, "mode": w["mode"], "replayed": w.get("replayed", 0),
                    "gap": w.get("gap", False), "resent": len(self.unacked),
                    "downtime": round(time.monotonic() - down_at, 2)})
                return True
            await asyncio.sleep(delay)
            delay = min(delay * 2, 4.0)
        await _maybe_await(self.on_link, "lost", {})
        return False

    async def send(self, type, **fields):
        self.cseq += 1
        data = encode({"type": type, **fields, "cseq": self.cseq})
        if len(self.unacked) >= UNACKED_MAX:
            raise ConnectionError("too many unacknowledged messages")
        self.unacked.append((self.cseq, data))
        if self.writer is None or self.writer.is_closing():
            return                               # link is down: resent after the reconnect
        try:
            self.writer.write(data)
            await self.writer.drain()
        except (ConnectionError, OSError):
            pass

    async def recv(self):
        return await self.inbox.get()

    async def close(self):
        self._closing = True
        if self.writer:
            try:
                self.writer.write(encode({"type": "bye"}))
                await asyncio.wait_for(self.writer.drain(), 1)
            except Exception:
                pass
            self.writer.close()
        if self._task:
            self._task.cancel()
