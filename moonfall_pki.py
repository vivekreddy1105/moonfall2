#!/usr/bin/env python3
"""moonfall_pki.py -- creates the certificates that give remote players an identity.

Over a UNIX socket the kernel tells the server who you are (SO_PEERCRED).  Over
the network there is no kernel to ask, so Moonfall runs its own tiny
certificate authority (CA):

    ca.crt / ca.key        the game's CA (ca.key never leaves the operator)
    server.crt / .key      the game server's identity (clients check it)
    players/NAME.crt/.key  one identity per player; CN = the player's name

A player's private key is installed in ~NAME/.moonfall/ with mode 0600, owned
by that Linux user.  So "holding alice's key" == "being the Linux user alice"
(or root) on whichever machine the key lives -- the network analogue of
SO_PEERCRED.

Usage (run as the game operator; the default PKI directory is ~/.moonfall/pki):
    ./moonfall_pki.py init                      # create the CA
    ./moonfall_pki.py server [HOST_OR_IP ...]   # server cert (localhost + this host's IPs added)
    ./moonfall_pki.py player NAME [NAME ...]    # player certs
    sudo ./moonfall_pki.py install NAME ...     # copy into ~NAME/.moonfall (local players)
    ./moonfall_pki.py bundle NAME               # NAME-moonfall.tgz to copy to a remote machine
    ./moonfall_pki.py list
Uses the system `openssl` command (EC P-256 keys, SHA-256 signatures).
"""
import argparse
import io
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile

DEFAULT_PKI = os.path.join(os.path.expanduser("~" + os.environ.get("SUDO_USER", "")), ".moonfall", "pki")
NAME_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
DAYS = "365"


def ossl(*args, cwd):
    r = subprocess.run(["openssl", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"openssl {' '.join(args[:2])} failed:\n{r.stderr}")


def new_key(path, cwd):
    old = os.umask(0o077)                       # keys are born 0600
    try:
        ossl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256",
             "-out", path, cwd=cwd)
    finally:
        os.umask(old)


def sign(pki, key, crt, cn, ext_lines):
    with tempfile.NamedTemporaryFile("w", suffix=".ext", dir=pki, delete=False) as ext:
        ext.write("\n".join(["basicConstraints=critical,CA:FALSE",
                             "keyUsage=critical,digitalSignature"] + ext_lines) + "\n")
    try:
        ossl("req", "-new", "-key", key, "-subj", f"/O=Moonfall/CN={cn}", "-out", crt + ".csr", cwd=pki)
        ossl("x509", "-req", "-in", crt + ".csr", "-CA", "ca.crt", "-CAkey", "ca.key",
             "-CAcreateserial", "-days", DAYS, "-sha256", "-extfile", ext.name, "-out", crt, cwd=pki)
    finally:
        os.unlink(ext.name)
        os.unlink(os.path.join(pki, crt + ".csr"))


def cmd_init(a):
    os.makedirs(a.pki, mode=0o700, exist_ok=True)
    os.chmod(a.pki, 0o700)
    if os.path.exists(os.path.join(a.pki, "ca.key")) and not a.force:
        sys.exit(f"A CA already exists in {a.pki} (use --force to replace it).")
    new_key("ca.key", a.pki)
    ossl("req", "-x509", "-new", "-key", "ca.key", "-sha256", "-days", DAYS,
         "-subj", "/O=Moonfall/CN=Moonfall Game CA",
         "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
         "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-out", "ca.crt", cwd=a.pki)
    os.makedirs(os.path.join(a.pki, "players"), mode=0o700, exist_ok=True)
    print(f"CA created in {a.pki}")


def local_names():
    names = {"localhost", "127.0.0.1", "::1", socket.gethostname()}
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout
        names.update(out.split())
    except OSError:
        pass
    return names


def cmd_server(a):
    names = sorted(local_names() | set(a.names))
    san = ",".join(("IP:" if re.fullmatch(r"[\d.]+|[0-9a-fA-F:]+:[0-9a-fA-F:]*", n) else "DNS:") + n
                   for n in names)
    new_key("server.key", a.pki)
    sign(a.pki, "server.key", "server.crt", socket.gethostname(),
         [f"subjectAltName={san}", "extendedKeyUsage=serverAuth"])
    print(f"server.crt issued for: {', '.join(names)}")


def cmd_player(a):
    for n in a.names:
        if not NAME_RE.fullmatch(n):
            sys.exit(f"{n!r} is not a valid Linux user name")
        key, crt = f"players/{n}.key", f"players/{n}.crt"
        new_key(key, a.pki)
        sign(a.pki, key, crt, n, ["extendedKeyUsage=clientAuth"])   # client-only: can't pose as the server
        print(f"issued {crt} (CN={n})")


def cmd_install(a):
    for n in a.names:
        try:
            pw = pwd.getpwnam(n)
        except KeyError:
            sys.exit(f"no Linux user {n!r}")
        d = os.path.join(pw.pw_dir, ".moonfall")
        os.makedirs(d, exist_ok=True)
        for src, dst, mode in ((f"players/{n}.key", "player.key", 0o600),
                               (f"players/{n}.crt", "player.crt", 0o644),
                               ("ca.crt", "ca.crt", 0o644)):
            path = os.path.join(d, dst)
            shutil.copyfile(os.path.join(a.pki, src), path)
            os.chown(path, pw.pw_uid, pw.pw_gid)
            os.chmod(path, mode)
        os.chown(d, pw.pw_uid, pw.pw_gid)
        os.chmod(d, 0o700)
        print(f"installed identity for {n} in {d} (key mode 0600, owner {n})")


def cmd_bundle(a):
    for n in a.names:
        out = os.path.abspath(f"{n}-moonfall.tgz")
        old = os.umask(0o077)
        with tarfile.open(out, "w:gz") as t:
            for src, dst in ((f"players/{n}.key", "player.key"), (f"players/{n}.crt", "player.crt"),
                             ("ca.crt", "ca.crt")):
                info = t.gettarinfo(os.path.join(a.pki, src), arcname=f".moonfall/{dst}")
                info.mode = 0o600 if dst.endswith(".key") else 0o644
                info.uname = info.gname = ""
                with open(os.path.join(a.pki, src), "rb") as f:
                    t.addfile(info, io.BytesIO(f.read()))
        os.umask(old)
        print(f"{out}: copy it to {n}'s account on the remote machine, then as {n}: "
              f"tar xzf {os.path.basename(out)} -C ~ && chmod 700 ~/.moonfall")


def cmd_list(a):
    for f in ["server.crt"] + sorted("players/" + p for p in os.listdir(os.path.join(a.pki, "players"))
                                     if p.endswith(".crt")):
        r = subprocess.run(["openssl", "x509", "-in", f, "-noout", "-subject", "-enddate"],
                           cwd=a.pki, capture_output=True, text=True)
        print(f"{f:24} {r.stdout.strip().replace(chr(10), '  ')}")


def main():
    ap = argparse.ArgumentParser(description="Moonfall certificate authority")
    ap.add_argument("--pki", default=DEFAULT_PKI, help=f"PKI directory (default {DEFAULT_PKI})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("--force", action="store_true"); p.set_defaults(fn=cmd_init)
    for name, fn in (("server", cmd_server), ("player", cmd_player),
                     ("install", cmd_install), ("bundle", cmd_bundle)):
        p = sub.add_parser(name)
        p.add_argument("names", nargs="*" if name == "server" else "+")
        p.set_defaults(fn=fn)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    a = ap.parse_args()
    a.pki = os.path.abspath(a.pki)
    if a.cmd != "init" and not os.path.exists(os.path.join(a.pki, "ca.key")):
        sys.exit(f"No CA in {a.pki}; run: {sys.argv[0]} init")
    a.fn(a)


if __name__ == "__main__":
    main()
