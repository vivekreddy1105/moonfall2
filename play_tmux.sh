#!/bin/bash
# Opens a tmux session with:
#   * the game server, listening on the UNIX socket AND on TLS port 7777
#   * netchaos (port 7778 -> 7777), where you can type: cut | freeze | thaw | delay N | dump on
#   * LOCAL players: each a different Linux user on the UNIX socket (SO_PEERCRED identity)
#   * REMOTE players: each a different Linux user connecting over TLS through netchaos
#     (certificate identity), exactly as a player on another machine would.
# Usage: ./play_tmux.sh                                  (local: alice bob carol, remote: dave erin)
#        LOCAL="alice bob" REMOTE="carol dave erin" ./play_tmux.sh
#        DEV=1 ./play_tmux.sh    single-user test: one Linux user, nicknames, no sudo
#        ROLES=dave=werewolf,alice=seer,bob=doctor ./play_tmux.sh   TEST MODE: fixed roles
DIR=$(cd "$(dirname "$0")" && pwd)
LOCAL=${LOCAL-alice bob carol}
REMOTE=${REMOTE-dave erin}
PKI=${PKI:-$HOME/.moonfall/pki}
IP=$(hostname -I | awk '{print $1}')
NIGHT=${NIGHT:-300}; DAY=${DAY:-600}; GRACE=${GRACE:-30}
SOCK=/tmp/moonfall/game.sock

if [ -z "$DEV" ]; then
  sudo -v || exit 1
  [ -d /tmp/moonfall ] || sudo install -d -m 3770 -g moonfall /tmp/moonfall   # /tmp is wiped on reboot
  # Pre-flight checks (Project #1 classmate feedback: unclear errors with custom names / home dirs)
  for p in $LOCAL $REMOTE; do
    if ! id "$p" &>/dev/null; then
      echo "Error: Linux user '$p' does not exist. Run: sudo ./setup_users.sh $LOCAL $REMOTE"; exit 1
    fi
    if ! sudo -u "$p" test -r "$DIR/moonfall_client.py"; then
      echo "Error: user '$p' cannot read $DIR. Home directories are private on Ubuntu; run from /opt:"
      echo "  sudo cp -r \"$DIR\" /opt/moonfall2 && sudo chmod -R a+rX /opt/moonfall2 && cd /opt/moonfall2"
      exit 1
    fi
  done
  for p in $REMOTE; do
    if ! sudo -H -u "$p" test -r "/home/$p/.moonfall/player.key"; then
      echo "Error: '$p' has no TLS identity. Run: sudo ./setup_users.sh $LOCAL $REMOTE"; exit 1
    fi
  done
else
  SOCK=/tmp/moonfall-dev/game.sock; mkdir -p /tmp/moonfall-dev
  missing=$(for p in $REMOTE; do [ -f "$PKI/players/$p.crt" ] || echo -n "$p "; done)
  [ -z "$missing" ] || python3 "$DIR/moonfall_pki.py" --pki "$PKI" player $missing
fi
[ -f "$PKI/server.crt" ] || { echo "No server certificate in $PKI. Run: sudo ./setup_users.sh"; exit 1; }

tmux kill-session -t moonfall 2>/dev/null
tmux new-session -d -s moonfall -x 240 -y 60 \
  "python3 $DIR/moonfall_server.py ${DEV:+--dev} --listen unix:$SOCK --listen tls://0.0.0.0:7777 --pki $PKI --night $NIGHT --day $DAY --grace $GRACE ${ROLES:+--roles $ROLES}; read _"
tmux set -t moonfall pane-border-status top
tmux set -t moonfall pane-border-format " #{pane_title} "
tmux select-pane -t moonfall -T "SERVER ($(whoami)): unix:$SOCK + tls://0.0.0.0:7777"
# Wait until the server (UNIX + TLS) and netchaos are listening, so no client starts too early.
wait_up() {
  for _ in $(seq 100); do
    [ -S "$SOCK" ] && [ -n "$(ss -Hltn 'sport = :7777')" ] && [ -n "$(ss -Hltn 'sport = :7778')" ] && return 0
    sleep 0.1
  done
  echo "The server did not start; check the SERVER pane: tmux attach -t moonfall"; exit 1
}
pane() {   # pane TITLE CMD: re-tile first so there is always room for one more pane
  tmux select-layout -t moonfall tiled >/dev/null
  # The player's client runs in a loop: if it crashes or is killed, the pane stays and
  # pressing Enter rejoins in the SAME pane (the server restores the seat within the grace period).
  local loop="while true; do $2; printf '\\n\\033[1;93m[client stopped] Press Enter to rejoin, or Ctrl+C to close this pane.\\033[0m '; read _ || break; done"
  tmux split-window -t moonfall "$loop" || { echo "Could not open a pane for $1 (terminal too small?)"; exit 1; }
  tmux select-pane -t moonfall -T "$1"
}
tmux split-window -t moonfall "python3 $DIR/netchaos.py --listen 0.0.0.0:7778 --target 127.0.0.1:7777; read _"
tmux select-pane -t moonfall -T "NETCHAOS 7778->7777  (cut | freeze | thaw | delay N | dump on)"
wait_up
for p in $LOCAL; do
  if [ -n "$DEV" ]; then cmd="python3 $DIR/moonfall_client.py --socket $SOCK --nick $p"
  else cmd="sudo -H -u $p python3 $DIR/moonfall_client.py --socket $SOCK"; fi
  pane "$p  [LOCAL: UNIX socket]" "$cmd"
done
for p in $REMOTE; do
  if [ -n "$DEV" ]; then
    cmd="python3 $DIR/moonfall_client.py --server tls://$IP:7778 --nick $p --cert $PKI/players/$p.crt --key $PKI/players/$p.key --ca $PKI/ca.crt"
  else cmd="sudo -H -u $p python3 $DIR/moonfall_client.py --server tls://$IP:7778"; fi
  pane "$p  [REMOTE: TLS via netchaos $IP:7778]" "$cmd"
done
tmux select-layout -t moonfall tiled >/dev/null
tmux attach -t moonfall
