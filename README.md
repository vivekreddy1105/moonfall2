# 🌕 Moonfall 2: a *distributed* Werewolves-like game

Moonfall 2 is the Project #2/#3 version of my Project #1 game, [Moonfall](https://github.com/vivekreddy1105/moonfall).
Project 1 ran on **one machine** over UNIX domain sockets. Moonfall 2 is a **distributed
system**: players can now join from **other machines** over **TCP + mutual TLS 1.3**, and the
game survives network failures (dropped connections, half-open links, crashed clients)
without losing or duplicating a single message.

> **Communications library:** **`wolfnet.py`**, a single file at the top level of this repo.
> Every byte the server, clients and bots exchange goes through it.
> See [its API](#-the-communications-library-wolfnetpy).
>
> **Classmates: please try to break it!** See [Try to break it](#-try-to-break-it-feedback-wanted).

![Game 1: local and remote players in one game, with a network cut](screenshots/game1_night1_and_network_cut.png)
*Seven tmux panes: the server, the "bad network" simulator, three **local** players (UNIX socket),
and two **remote** players (TLS). Every player is a different Linux user.*

---

## 🆕 What changed since Project 1

| | Project 1 | Moonfall 2 (Projects 2/3) |
|---|---|---|
| Where players can be | same machine only | same machine **or any other machine** |
| Transport | UNIX domain stream socket | UNIX socket **and** TCP + **TLS 1.3**, at the same time |
| Who you are | kernel `SO_PEERCRED` (your UID) | `SO_PEERCRED` locally; a **client certificate** signed by the game's own CA remotely |
| Eavesdropping | not possible (local socket) | network traffic is encrypted (TLS) |
| Connection drops | player "fled" immediately | seat held for 30 s; the client **reconnects automatically** |
| Lost messages on reconnect | n/a | **exactly-once, in-order** delivery both ways (sequence numbers, replay, resend) |
| Dead link with no error (half-open TCP) | n/a | **heartbeats** detect it in 3 missed beats (≈9 s) |
| Client crash | player gone | restart within 30 s and you get your seat **and your role** back |
| Terminal escape codes in chat | relayed raw (bug!) | stripped by the server (and by the client) |

The game rules and the library API are unchanged, so `moonfall_server.py` can't tell whether a
player is local or remote.

---

## 🎭 The game

A moderator server secretly deals roles to 4 or more players:

| Role | Goal | Night action |
|---|---|---|
| 🐺 **Werewolf** (1 per 4 players) | Eliminate villagers until wolves equal or outnumber them | `/kill <name>`, chosen with the pack in a private wolf chat |
| 🔮 **Seer** | Find the wolves | `/see <name>`, once per night, to learn if that player is a werewolf |
| 💉 **Doctor** (5+ players) | Keep people alive | `/save <name>`, but not the same person two nights in a row |
| 🧑 **Villager** | Find the wolves | None: talk, reason, and vote |

**🌙 Night:** only wolves can speak, in their pack channel. The seer and doctor act secretly.

**☀ Day:** everyone talks, can `/w` whisper, and votes with `/vote`.
- The tally is public and live.
- A tie banishes nobody.
- Everyone sees *that* you whispered, but not *what* you said.

**💀 Dead players** watch, and can talk to each other in a private ghosts channel.

**Win conditions:** the village wins when every wolf is dead. The wolves win when they equal or
outnumber the rest.

**Phase length:** a phase ends as soon as everyone has acted, or after 5 minutes (night) or
10 minutes (day).

---

## 🚀 Quick start

**Requirements:** Ubuntu (tested on 26.04), **Python 3.11+**, `tmux` and `openssl`. There are no
pip packages and nothing to compile.

### Option A: the full demo (multi-user, local + remote players)
```bash
git clone https://github.com/vivekreddy1105/moonfall2.git
sudo cp -r moonfall2 /opt/moonfall2 && sudo chmod -R a+rX /opt/moonfall2   # home dirs are private on Ubuntu
cd /opt/moonfall2
sudo ./setup_users.sh       # users alice..erin, the 'moonfall' group, and a TLS certificate for each
./play_tmux.sh              # maximize your terminal first: 7 panes
```
Once everyone has joined, type `/start` in any player pane.

| Pane | What it is |
|---|---|
| **SERVER** | the moderator: UNIX socket `/tmp/moonfall/game.sock` **and** TLS port 7777 |
| **NETCHAOS** | a "bad network" between the remote players and the server. Type `cut`, `freeze`, `thaw`, `delay 150` or `dump on` |
| **alice, bob, carol** | **local** players, each a separate Linux user, on the UNIX socket |
| **dave, erin** | **remote** players, each a separate Linux user, over **TLS** through NETCHAOS, exactly like a player on another machine |

**Useful variations:**
- **Fixed roles for a scripted demo (test mode):** `ROLES=dave=werewolf,alice=seer,erin=doctor ./play_tmux.sh`
- **Choose who is local and who is remote:** `LOCAL="alice bob" REMOTE="carol dave erin" ./play_tmux.sh`
- **Restart:** `tmux kill-session -t moonfall && ./play_tmux.sh`

**Things to try during a game:**
- In NETCHAOS, type `cut`. The remote players reconnect, and everyone sees "lost connection… reconnected".
- Type `freeze`, then quickly `/vote` as dave. After about 9 s the dead link is detected, dave
  reconnects, and his vote arrives exactly once.
- Type `delay 150`, wait a few seconds, then type `/who` in any player pane to see the remote players' RTT.
- Type `dump on`, then chat as dave or erin. The traffic is unreadable.
- Kill a player's client with `sudo pkill -KILL -u erin -f moonfall_client.py`. Pressing Enter in
  erin's pane within 30 s gives her seat and role back. Otherwise she "flees the village".

### Option B: quick single-user test (no sudo)
```bash
python3 moonfall_pki.py init && python3 moonfall_pki.py server && python3 moonfall_pki.py player alice bob carol dave erin
DEV=1 ./play_tmux.sh        # one Linux user, nicknames instead of real identities
```

### Option C: join from a REAL second machine
1. **On the server machine:**
   ```bash
   python3 moonfall_pki.py player frank && python3 moonfall_pki.py bundle frank
   ```
2. **Copy** `frank-moonfall.tgz`, `wolfnet.py` and `moonfall_client.py` to the other machine.
3. **On the other machine:**
   ```bash
   tar xzf frank-moonfall.tgz -C ~ && chmod 700 ~/.moonfall
   python3 moonfall_client.py --server tls://SERVER_IP:7777
   ```

The server certificate is valid for the server's current IP addresses. If the IP changes,
rerun `python3 moonfall_pki.py server`.

---

## ⌨️ Commands

| Command | When | What it does |
|---|---|---|
| `<text>` | any time | Chat: day = everyone, night = wolf pack only, dead = ghosts only |
| `/w <name> <text>` | day | Private whisper |
| `/vote <name>` | day | Vote to banish (you can change your vote) |
| `/kill <name>` | night, werewolf | Choose the victim |
| `/see <name>` | night, seer | Learn whether a player is a werewolf |
| `/save <name>` | night, doctor | Protect a player |
| `/who` | any time | Alive/dead players **plus every player's connection**: `unix`/`tls`, address, round-trip time, or `AWAY` |
| `/net` | any time | Your own link statistics: RTT, reconnects, replayed, resent, duplicates |
| `/start` | lobby | Start the game (4+ players) |
| `/help`, `/quit` | any time | Help; leave politely (no grace period) |

---

## 📡 The communications library: `wolfnet.py`

**Location:** `wolfnet.py` at the top level of this repo. It's a single file using only the
Python standard library (`asyncio`, `ssl`, `socket`). The server, client and bot import it, and
**none of them open a socket themselves**.

### Addresses: one API, three transports
| Address | Transport | Identity of the peer |
|---|---|---|
| `unix:/tmp/moonfall/game.sock` | UNIX domain stream socket (Project 1) | kernel `SO_PEERCRED` → Linux username |
| `tls://host:7777` | TCP + TLS 1.3, **client certificate required** | certificate Common Name, verified against the game CA |
| `tcp://host:port` | plain TCP, **only with `--dev`** | none (a nickname). It exists only to show why TLS is needed |

A server can listen on several at once: `Server(["unix:/tmp/moonfall/game.sock", "tls://0.0.0.0:7777"], ...)`.

### Server API
```python
srv = Server(listen, on_join, on_message, on_leave, *, dev=False,
             tls=(cert, key, ca), grace=30, heartbeat=3, on_away=None, on_back=None)
await srv.start();  await srv.close()
srv.send(name, msg)                    # unicast
srv.multicast(group, msg, exclude=())  # e.g. "wolves", "ghosts"
srv.broadcast(msg, exclude=())         # everyone
srv.join_group(name, group);  srv.leave_group(name, group);  srv.members(group)
srv.kick(name, reason)
srv.info(name)   # {"state": "online"/"away", "transport", "addr", "rtt_ms", "away_s"}
# callbacks: on_join(name)  on_message(name, msg)  on_leave(name)
#            on_away(name)  on_back(name, {"mode", "replayed", "gap"})     <- new
```

### Client API
```python
c = Client("tls://host:7777", cert=..., key=..., ca=..., reconnect=True, on_link=callback)
name = await c.connect(nick=None)   # the name the SERVER assigned
await c.send("chat", text="hello")  # never lost: resent after a reconnect if needed
msg = await c.recv()                # a dict, or None when the session ends
await c.close()                     # polite leave
c.rtt_ms, c.stats                   # measured RTT; reconnects/replayed/resent/duplicates
# on_link(state, info): state is "down", "up" or "lost"
```

### Wire format
Unchanged from Project 1: a 4-byte big-endian length, then a UTF-8 JSON object with a `"type"`.
The maximum frame size is 64 KB. New fields and messages:

| Field / type | Direction | Purpose |
|---|---|---|
| `hello {proto, nick, resume, last_seq}` | client → server | `resume` = secret session token, `last_seq` = last message received |
| `welcome {name, session, mode, base, ack, replayed, gap, heartbeat}` | server → client | `mode` is `new`, `resume` or `rejoin` |
| `seq` on every message | server → client | per-session sequence number. The client drops duplicates; the server keeps the last 512 for replay |
| `cseq` on every message | client → server | client sequence number. The server drops duplicates; the client keeps unacknowledged messages and resends them |
| `ping {t, ack, rtt}` / `pong {t}` | both ways | heartbeat every 3 s, cumulative ack, RTT measurement |
| `bye` | both ways | polite close: the session ends now, with no grace period |

### How a reconnect works
1. The link dies. Either an error (cut) or **silence for 3 heartbeats** (a frozen, half-open link).
2. The server marks the player **AWAY**, holds the seat for 30 s, and keeps queuing their messages.
3. The client reconnects (backoff 0.25 s → 4 s) and sends `hello` with its **token** and **last_seq**.
4. The server checks the identity (same certificate or same UID) **and** the token, then replays
   every message after `last_seq`, and tells the client the last `cseq` it processed.
5. The client resends everything the server didn't get. Duplicates are dropped on both sides,
   so delivery is exactly once and in order.
6. If nobody comes back within 30 s, the player "flees the village" (Project 1 behaviour).
   A *new* client process with the same identity within 30 s gets the seat back (`rejoin`), and
   the game re-sends its role.

### Where each communication pattern is used
| Pattern | Used for |
|---|---|
| **Unicast** | roles, seer visions, doctor confirmations, whispers, errors, `/who` |
| **Multicast** | the wolf pack and the ghosts. Members can be on **different transports** (a local wolf and a remote wolf share one channel) |
| **Broadcast** | day chat, live votes, deaths, phase changes, connection notices, game over |

### Security and robustness design
- **Unforgeable identity on both transports.**
  - **UNIX:** `SO_PEERCRED`.
  - **TLS:** only certificates signed by the game CA are accepted, and the name *is* the
    certificate's CN.
  - **Either way:** the server stamps `sender` on every message.
- **Keys are bound to Linux users.** Each player's private key is `~player/.moonfall/player.key`
  (mode 0600). The client refuses a key that other users can read.
- **No impersonating the server.**
  - Clients verify the server's certificate and hostname.
  - Player certificates are `clientAuth`-only and the server's is `serverAuth`-only, so neither
    can pose as the other.
- **Resume needs two things.** A stolen session token alone is useless: the identity must match.
- **An open TCP port is a new attack surface.** Limits added:
  - a TLS handshake and hello timeout (10 s);
  - at most 64 connections in total, and 8 unauthenticated connections per IP;
  - per-player rate limiting (10 messages/s, burst 20; repeat offenders are kicked);
  - bounded queues everywhere.
- **Escape-code injection fixed.** Control characters are stripped from all player text.

---

## 🧪 Testing

| Tool | What it does |
|---|---|
| `netchaos.py` | A TCP relay that simulates a bad network **without root**: `cut` (Wi-Fi drop), `freeze` (half-open link), `delay N` (latency) and `dump on` (wiretap). It is a test tool, **not** part of the game's communications |
| `security_demo.py` | Runs 7 attacks against temporary servers and prints PASS/FAIL. Run it with `python3 security_demo.py` |

**Results** (from the screenshots and test runs):

| Test | Result |
|---|---|
| Network cut | the remote players reconnected in **0.01–0.03 s** and missed messages were replayed |
| Frozen (half-open) link | detected after **9.0 s** (3 × 3 s heartbeats). Then reconnected, and messages typed during the freeze were resent and delivered **once** |
| 100 messages each way across one freeze and one cut | **100/100 delivered in order, 0 duplicates**, both directions |
| `delay 150` | RTT **307 ms** for the TLS players vs **1.4 ms** for local UNIX players, and the game stayed playable |
| Client killed with `kill -9`, restarted within 30 s | seat and role restored. Not restarted: "fled" after 30 s |
| `security_demo.py` | **7/7 passed**: wiretap, key theft, forged certificate, server certificate as a player, world-readable key, fake server, escape-code injection |

---

## 🖼️ Screenshots

| | |
|---|---|
| ![](screenshots/game1_night1_and_network_cut.png) | **Game 1, night 1 and day 1.** The seer sees dave is a wolf, the doctor saves bob, and alice whispers to erin. A `cut` and a `freeze` in NETCHAOS: the remote players reconnect, and the server logs the dead links |
| ![](screenshots/game1_latency_who_and_doctor_rule.png) | **`/who` with `delay 150`:** TLS players at 307 ms, local players at 1.4 ms. The doctor can't save the same person twice |
| ![](screenshots/game1_village_wins.png) | **Game 1 ends:** the seer's claim convinces the village, and the werewolf dave is banished |
| ![](screenshots/game2_crash_and_rejoin.png) | **Game 2:** the seer is killed on night 1, and her ghost message reaches only the dead. erin's client is killed (`kill -9`), and pressing Enter in her pane rejoins: "seat restored" and her role is re-sent |
| ![](screenshots/game2_tie_vote.png) | **Game 2, the tie:** 2–2, so nobody is banished |
| ![](screenshots/game2_werewolves_win.png) | **Game 2 ends:** the wolf wins after the doctor is banished |
| ![](screenshots/game3_wolf_pack_across_transports_tls_dump.png) | **Game 3:** one wolf is local (carol) and one is remote over TLS (erin), sharing one pack channel. NETCHAOS `dump on` shows only encrypted bytes |
| ![](screenshots/game3_player_fled_wolves_win.png) | **Game 3:** bob's client is killed and he never returns. After 30 s he "fled the village". The wolves win |
| ![](screenshots/security_demo_7_of_7.png) | **`security_demo.py`:** all 7 attacks fail |

---

## 📁 Files

| File | Purpose |
|---|---|
| **`wolfnet.py`** | **The communications library (single file).** UNIX + TLS transports, sessions, replay, heartbeats |
| `moonfall_server.py` | Moderator and game logic (`--listen`, `--grace`, `--heartbeat`, `--roles` for test mode) |
| `moonfall_client.py` | Interactive player terminal (`--server unix:… / tls://…`) |
| `moonfall_bot.py` | A bot that makes random legal moves (works over UNIX or TLS) |
| `moonfall_pki.py` | The game's tiny certificate authority: `init`, `server`, `player`, `install`, `bundle`, `list` |
| `setup_users.sh` | Creates the users, the `moonfall` group, `/tmp/moonfall`, and each player's certificate |
| `play_tmux.sh` | Launches the server, NETCHAOS, and the local and remote player panes |
| `netchaos.py` | Bad-network simulator (test tool) |
| `security_demo.py` | Automated security tests |
| `screenshots/` | Gameplay and test screenshots |

---

## 🔍 Try to break it (feedback wanted!)

Moonfall 2 now has an **open TCP port**, which is a very different attack surface from
Project 1's `0660` socket file. Please spend 10–15 minutes on any of these and tell me what
happened, **even if nothing broke**. What held up is just as useful to me.

1. **Impersonation over the network:** can you join as someone else?
   - Forge or modify a certificate.
   - Reuse a session token you sniffed.
   - Connect twice with the same identity.
2. **Denial of service on port 7777:**
   - Open many TCP connections and never finish the TLS handshake (slowloris).
   - Send a valid hello and then nothing.
   - Flood messages.

   Does the game stay playable for everyone else?
3. **Breaking exactly-once:**
   - Send frames with a huge, negative, or repeated `cseq`.
   - Resume with a wrong `last_seq`.
   - Reconnect very fast in a loop.

   Can you make a vote count twice, or make someone miss a message?
4. **Abusing the grace period:** can disconnecting and reconnecting at the right moment let
   you stall the game, dodge a vote, or learn something?
5. **Information leaks:** what can someone on the network learn without decrypting, from
   message sizes and timing (for example, who is a wolf, based on traffic at night)?
6. **Crashing the server or client:** garbage bytes before or after the TLS handshake, a huge
   length header, invalid JSON, half a frame, terminal escape codes in names or chat.
7. **General:** bugs, confusing messages, or setup problems.

**How to report:** open a [GitHub Issue](../../issues) or reply to my Canvas post. The exact
command you ran or a screenshot helps a lot.

Raw TLS starting point (make yourself a player certificate first, with Option B above):
```python
import socket, ssl, struct, json
ctx = ssl.create_default_context(cafile="ca.crt"); ctx.load_cert_chain("players/alice.crt", "players/alice.key")
s = ctx.wrap_socket(socket.create_connection(("127.0.0.1", 7777)), server_hostname="127.0.0.1")
send = lambda m: s.sendall(struct.pack("!I", len(d := json.dumps(m).encode())) + d)
send({"type": "hello", "proto": 2}); print(s.recv(4096))
# now try: send({"type": "chat", "text": "hi", "cseq": -5}) ... or never send anything at all
```
(Run it from `~/.moonfall/pki`, where Option B puts the certificates.)

### Known limitations (so you don't have to report these)
- **No certificate revocation:** a stolen player key works until it expires (1 year).
  The fix is to reissue the CA.
- **One server:** it's a single point of failure, and game state isn't saved to disk.
  If the server dies, the game ends.
- **NETCHAOS simulates connection-level failures** (cut, freeze, delay), not packet loss.
  TCP already hides packet loss.
- **Hostname verification:** clients check the server certificate's IP/hostname, so they must
  connect using an address listed in it.
