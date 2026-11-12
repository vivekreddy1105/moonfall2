#!/usr/bin/env python3
"""security_demo.py -- runs Moonfall 2's security tests and explains each one.

Run it from the game folder (no game needs to be running):
    cd /opt/moonfall2 && python3 security_demo.py           # all tests
    python3 security_demo.py 3                              # only test 3
It starts its own temporary servers on ports 7900-7930 (the real game uses 7777/7778),
attacks them, and prints PASS/FAIL.  Test 2 asks for your sudo password.

    1  Can an eavesdropper on the network read the game?       (plain TCP vs TLS)
    2  Can one player steal another player's identity file?     (file permissions)
    3  Can someone forge a certificate that says "alice"?       (CA signature check)
    4  Can the SERVER's certificate be used to log in as a player?
    5  Does the client refuse a private key other users can read?
    6  Can a fake server trick a player into connecting?        (server verification)
    7  Can a player mess up other players' screens?             (escape-code injection)
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from netchaos import ChaosProxy          # noqa: E402  (test tool: a wiretap on the path)
from wolfnet import Client, Refused      # noqa: E402

PKI = os.environ.get("PKI", os.path.expanduser("~/.moonfall/pki"))
SECRET = "psst, erin is the wolf"
B, G, R, Y, D, X = "\033[1m", "\033[1;32m", "\033[1;31m", "\033[33m", "\033[2m", "\033[0m"
results = []


def header(n, title, what):
    print(f"\n{B}━━━━ TEST {n}: {title} ━━━━{X}")
    print(f"{D}{what}{X}")


def verdict(n, ok, text):
    results.append((n, ok))
    print(f"{G}PASS{X} {text}" if ok else f"{R}FAIL{X} {text}")


def show(label, value):
    print(f"  {Y}{label:<28}{X} {value}")


def wire(data, limit=90):
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data[:limit])


class TempServer:
    """A throw-away moonfall_server.py process whose log we can read."""

    def __init__(self, *args):
        self.log = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)
        self.p = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "moonfall_server.py"), *args],
                                  stdout=self.log, stderr=subprocess.STDOUT, cwd=HERE)

    async def ready(self, port):
        for _ in range(50):
            try:
                _r, w = await asyncio.open_connection("127.0.0.1", port)
                w.transport.abort()
                await asyncio.sleep(0.1)
                return
            except OSError:
                await asyncio.sleep(0.1)
        raise SystemExit(f"temporary server on port {port} did not start:\n{self.lines()}")

    def lines(self, grep=""):
        self.log.flush()
        with open(self.log.name) as f:
            return [l.rstrip() for l in f if grep in l]

    def stop(self):
        self.p.terminate()
        self.p.wait()
        os.unlink(self.log.name)


def player(name):
    return dict(cert=f"{PKI}/players/{name}.crt", key=f"{PKI}/players/{name}.key", ca=f"{PKI}/ca.crt")


async def try_join(addr, **tls):
    """Returns (name or None, error text)."""
    try:
        c = Client(addr, reconnect=False, **tls)
        name = await c.connect()
        await c.close()
        return name, ""
    except (OSError, ConnectionError, Refused) as e:
        return None, str(e) or type(e).__name__


# ------------------------------------------------------------------ tests
async def test1(dev, real):
    header(1, "Can an eavesdropper on the network read the game?",
           f'A player sends "{SECRET}". A wiretap (netchaos) sits on the network path and\n'
           "records every byte, like someone running Wireshark on the same Wi-Fi.")
    captures = {}
    for label, port, tport, tls in (("plain TCP", 7901, 7900, None), ("TLS 1.3", 7911, 7910, player("alice"))):
        tap = await ChaosProxy(("127.0.0.1", port), ("127.0.0.1", tport), capture=True, log=lambda s: None).start()
        scheme = "tls" if tls else "tcp"
        c = Client(f"{scheme}://127.0.0.1:{port}", reconnect=False, **(tls or {}))
        await c.connect("spy")
        await c.send("chat", text=SECRET)
        await asyncio.sleep(0.5)
        await c.close()
        await tap.close()
        captures[label] = bytes(tap.capture)
        i = captures[label].find(SECRET.encode())
        show(f"{label}: wiretap saw", wire(captures[label][max(0, i - 25):] if i >= 0 else captures[label][-90:]))
    plain_leak = SECRET.encode() in captures["plain TCP"]
    tls_leak = SECRET.encode() in captures["TLS 1.3"]
    show("secret visible in plain TCP?", "YES" if plain_leak else "no")
    show("secret visible in TLS?", "YES" if tls_leak else "no (only random-looking bytes)")
    verdict(1, plain_leak and not tls_leak,
            "Plain TCP exposes the wolf chat to anyone on the path; TLS hides it.")


def test2():
    header(2, "Can one player steal another player's identity file?",
           "Over TLS, holding alice's private key = being alice. The key lives in\n"
           "/home/alice/.moonfall/player.key. We try to read it as the Linux user dave.")
    if not shutil.which("sudo"):
        return verdict(2, False, "sudo is not available")
    ls = subprocess.run(["sudo", "ls", "-la", "/home/alice/.moonfall/"], capture_output=True, text=True)
    for line in ls.stdout.splitlines()[1:]:
        show("", line)
    r = subprocess.run(["sudo", "-u", "dave", "cat", "/home/alice/.moonfall/player.key"],
                       capture_output=True, text=True)
    show("as dave: cat alice's key", (r.stderr or r.stdout).strip().splitlines()[0] if (r.stderr or r.stdout)
         else "(empty)")
    verdict(2, r.returncode != 0 and "Permission denied" in r.stderr,
            "The kernel refuses: the key is mode 0600 and owned by alice.")


async def test3(real):
    header(3, 'Can someone forge a certificate that says "alice"?',
           "An attacker makes their OWN certificate with the name alice (self-signed,\n"
           "not signed by the game's CA) and tries to join.")
    tmp = tempfile.mkdtemp()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                    "-nodes", "-keyout", f"{tmp}/forged.key", "-out", f"{tmp}/forged.crt",
                    "-subj", "/CN=alice", "-days", "1"], capture_output=True, check=True)
    os.chmod(f"{tmp}/forged.key", 0o600)
    name, err = await try_join("tls://127.0.0.1:7920", cert=f"{tmp}/forged.crt",
                               key=f"{tmp}/forged.key", ca=f"{PKI}/ca.crt")
    await asyncio.sleep(0.3)
    show("attacker's client says", err or f"joined as {name}!")
    show("server log says", (real.lines("rejected") or ["(nothing)"])[-1])
    good, _ = await try_join("tls://127.0.0.1:7920", **player("alice"))
    show("control: real alice cert", f"joined as {good}" if good else "failed")
    shutil.rmtree(tmp)
    verdict(3, name is None and good == "alice",
            "The forged certificate is rejected during the TLS handshake; the real one works.")
    return tmp


async def test4(real):
    header(4, "Can the SERVER's certificate be used to log in as a player?",
           "The server certificate is also signed by the game CA. Someone who copies it\n"
           "tries to use it as a player certificate.")
    name, err = await try_join("tls://127.0.0.1:7920", cert=f"{PKI}/server.crt",
                               key=f"{PKI}/server.key", ca=f"{PKI}/ca.crt")
    await asyncio.sleep(0.3)
    show("client says", err or f"joined as {name}!")
    show("server log says", (real.lines("rejected") or ["(nothing)"])[-1])
    verdict(4, name is None, "Rejected: the server certificate is marked 'serverAuth' only "
                             "(extendedKeyUsage), so it can't act as a player.")


async def test5():
    header(5, "Does the client refuse a private key other users can read?",
           "Like ssh, the client checks the key's file permissions before using it.")
    tmp = tempfile.mkdtemp()
    shutil.copy(f"{PKI}/players/alice.key", f"{tmp}/player.key")
    os.chmod(f"{tmp}/player.key", 0o644)
    name, err = await try_join("tls://127.0.0.1:7920", cert=f"{PKI}/players/alice.crt",
                               key=f"{tmp}/player.key", ca=f"{PKI}/ca.crt")
    show("key permissions", "-rw-r--r-- (0644)")
    show("client says", err.replace(tmp, "…") or f"joined as {name}!")
    shutil.rmtree(tmp)
    verdict(5, name is None and "readable by other users" in err,
            "The client refuses to use a key that other users could have copied.")


async def test6():
    header(6, "Can a fake server trick a player into connecting?",
           "An attacker runs their own server (with a certificate from a DIFFERENT CA)\n"
           "and a player connects to it, e.g. after a DNS or Wi-Fi trick.")
    tmp = tempfile.mkdtemp()
    pki = [sys.executable, os.path.join(HERE, "moonfall_pki.py"), "--pki", f"{tmp}/pki"]
    subprocess.run(pki + ["init"], capture_output=True, check=True)
    subprocess.run(pki + ["server"], capture_output=True, check=True)
    fake = TempServer("--listen", "tls://127.0.0.1:7930", "--pki", f"{tmp}/pki")
    try:
        await fake.ready(7930)
        name, err = await try_join("tls://127.0.0.1:7930", **player("alice"))
        show("player's client says", err or f"joined the FAKE server as {name}!")
    finally:
        fake.stop()
        shutil.rmtree(tmp)
    verdict(6, name is None and "certificate verify failed" in err,
            "The client only trusts servers signed by the game CA, so the fake server is refused\n"
            "     (and alice's key is never used to prove anything to it).")


async def test7(dev):
    header(7, "Can a player mess up other players' screens?",
           "A player sends chat containing terminal escape codes: ESC[2J (clear the screen)\n"
           "and ESC[31m (red). Moonfall 1 relayed these raw.")
    attack = "\x1b[2J\x1b[31mHACKED your screen\x07"
    victim, evil = Client("tcp://127.0.0.1:7900", reconnect=False), Client("tcp://127.0.0.1:7900", reconnect=False)
    await victim.connect("victim")
    await evil.connect("evil")
    await evil.send("chat", text=attack)
    got = None
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        m = await asyncio.wait_for(victim.recv(), 3)
        if m and m.get("kind") == "chat" and m.get("sender") == "evil":
            got = m["text"]
            break
    await evil.close()
    await victim.close()
    show("attacker sent", repr(attack))
    show("victim received", repr(got))
    verdict(7, got is not None and "\x1b" not in got and "\x07" not in got,
            "The server strips control characters before relaying, so the text is harmless.")


async def main():
    only = set(sys.argv[1:])
    if not os.path.exists(f"{PKI}/server.crt"):
        sys.exit(f"No certificates in {PKI}. Run first: sudo ./setup_users.sh")
    print(f"{B}Moonfall 2 security tests{X}  (temporary servers on ports 7900-7930; PKI: {PKI})")
    dev = TempServer("--dev", "--listen", "tcp://127.0.0.1:7900", "--listen", "tls://127.0.0.1:7910",
                     "--pki", PKI)
    real = TempServer("--listen", "tls://127.0.0.1:7920", "--pki", PKI)
    try:
        await dev.ready(7900)
        await real.ready(7920)
        for n, fn in ((1, lambda: test1(dev, real)), (2, None), (3, lambda: test3(real)),
                      (4, lambda: test4(real)), (5, test5), (6, test6), (7, lambda: test7(dev))):
            if only and str(n) not in only:
                continue
            if n == 2:
                test2()
            else:
                await fn()
    finally:
        dev.stop()
        real.stop()
    ok = sum(1 for _, k in results if k)
    print(f"\n{B}Summary: {ok}/{len(results)} passed{X}  " +
          " ".join(f"{G if k else R}T{n}{X}" for n, k in results))


if __name__ == "__main__":
    asyncio.run(main())
