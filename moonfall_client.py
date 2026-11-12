#!/usr/bin/env python3
"""moonfall_client.py -- interactive player terminal. Uses wolfnet.Client only.

Local player:   python3 moonfall_client.py                       (UNIX socket)
Remote player:  python3 moonfall_client.py --server tls://HOST:7777
                (identity = ~/.moonfall/player.crt + player.key, issued by moonfall_pki.py)"""
import argparse
import asyncio
import os
import re
import sys

from wolfnet import Client, Refused

C = {"system": "\033[36m", "chat": "\033[0m", "wolf": "\033[31m", "ghost": "\033[90m",
     "whisper": "\033[35m", "private": "\033[33m", "role": "\033[1;33m", "phase": "\033[1;34m",
     "death": "\033[1;31m", "vote": "\033[32m", "gameover": "\033[1;32m", "error": "\033[91m",
     "link": "\033[1;93m"}
RST = "\033[0m"
HELP = """Commands:
  <text>            talk (day: everyone, night: wolf pack only, dead: ghosts only)
  /w <name> <text>  whisper privately (day only; others see that you whispered)
  /vote <name>      vote to banish (day)
  /kill <name>      werewolf night attack     /see <name>  seer vision
  /save <name>      doctor protection         /who         players + connections
  /start            start the game (lobby)    /net         link statistics
  /quit             leave"""
# Defense in depth: never let received text drive the terminal (keep only newlines).
_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")


def show(kind, text):
    print(f"\r{C.get(kind, '')}{text}{RST}")
    print("> ", end="", flush=True)


def render(m):
    kind = m.get("kind", m["type"])
    sender = m.get("sender")
    prefix = {"wolf": "[wolves] ", "ghost": "[ghosts] ", "whisper": "[whisper] "}.get(kind, "")
    who = f"{sender}: " if sender and kind in ("chat", "wolf", "ghost", "whisper") else ""
    show(kind, _CONTROL.sub("", f"{prefix}{who}{m.get('text', '')}"))


def on_link(state, info):
    if state == "down":
        show("link", "⚠ Connection lost. Reconnecting... (your seat is held)")
    elif state == "up":
        show("link", f"✓ Reconnected after {info['downtime']}s ({info['attempts']} attempts): "
                     f"{info['replayed']} missed messages replayed, {info['resent']} of yours resent.")
    else:
        show("link", "✗ Could not reconnect. Giving up.")


async def main():
    home = os.path.expanduser("~/.moonfall")
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", help="unix:/path or tls://host:port (default: local UNIX socket)")
    ap.add_argument("--socket", default="/tmp/moonfall/game.sock", help="UNIX socket path")
    ap.add_argument("--cert", default=f"{home}/player.crt")
    ap.add_argument("--key", default=f"{home}/player.key")
    ap.add_argument("--ca", default=f"{home}/ca.crt")
    ap.add_argument("--nick", help="nickname (only honoured if server runs --dev)")
    a = ap.parse_args()

    addr = a.server or "unix:" + a.socket
    try:
        c = Client(addr, cert=a.cert, key=a.key, ca=a.ca, on_link=on_link)
        name = await c.connect(a.nick)
    except (OSError, ConnectionError, Refused) as e:
        sys.exit(f"Could not join {addr}: {e}")
    print(f"Joined Moonfall as {name} via {c.kind.upper()}. Type /help for commands.")

    loop = asyncio.get_running_loop()
    lines: asyncio.Queue = asyncio.Queue()
    loop.add_reader(sys.stdin.fileno(), lambda: lines.put_nowait(sys.stdin.readline()))

    async def receiver():                    # concurrent task 1: network -> screen
        while (m := await c.recv()) is not None:
            render(m)
        print("\nServer closed the connection.")

    async def sender():                      # concurrent task 2: keyboard -> network
        while True:
            line = await lines.get()
            if line == "":
                return
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=2)
            cmd = parts[0].lower()
            if cmd == "/help":
                print(HELP)
            elif cmd == "/quit":
                return
            elif cmd == "/net":
                show("link", f"{c.kind} {addr}  rtt={c.rtt_ms} ms  stats={c.stats}")
            elif cmd in ("/start", "/who"):
                await c.send(cmd[1:])
            elif cmd in ("/vote", "/kill", "/see", "/save") and len(parts) >= 2:
                await c.send("act", verb=cmd[1:], target=parts[1])
            elif cmd == "/w" and len(parts) == 3:
                await c.send("whisper", to=parts[1], text=parts[2])
            elif cmd.startswith("/"):
                print("Bad command. /help for help.")
            else:
                await c.send("chat", text=line)

    tasks = [asyncio.create_task(receiver()), asyncio.create_task(sender())]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in tasks:
        t.cancel()
    loop.remove_reader(sys.stdin.fileno())
    await c.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
