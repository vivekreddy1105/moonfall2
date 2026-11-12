#!/bin/bash
# Creates the 'moonfall' group, the shared socket directory, the player accounts,
# and each player's TLS identity (certificate + private key in ~player/.moonfall,
# mode 0600, owned by that player).  The CA lives in YOUR home: ~/.moonfall/pki.
# Usage: sudo ./setup_users.sh [player names...]   (default: alice bob carol dave erin)
set -e
[ "$(id -u)" = 0 ] || { echo "Run with sudo: sudo $0 $*"; exit 1; }
OP=${SUDO_USER:?run this with sudo from your normal account}
PLAYERS=${@:-alice bob carol dave erin}
DIR=$(cd "$(dirname "$0")" && pwd)
PKI=$(getent passwd "$OP" | cut -d: -f6)/.moonfall/pki

groupadd -f moonfall
usermod -aG moonfall "$OP"
for p in $PLAYERS; do
  id "$p" &>/dev/null || useradd -m -s /bin/bash "$p"
  usermod -aG moonfall "$p"
done
# setgid dir: socket inherits group 'moonfall'; sticky bit: users can't delete others' files
install -d -m 3770 -g moonfall /tmp/moonfall
chmod -R o+rX "$DIR"   # players must be able to read the code

# Certificates (Project #2/#3): the CA and keys are created as YOU, not root.
pki() { sudo -u "$OP" python3 "$DIR/moonfall_pki.py" --pki "$PKI" "$@"; }
[ -f "$PKI/ca.key" ] || pki init
pki server                       # valid for localhost and this machine's current IPs
pki player $PLAYERS
python3 "$DIR/moonfall_pki.py" --pki "$PKI" install $PLAYERS    # root: copy into each home
echo "Done. Log out/in (or run 'newgrp moonfall') so your group membership applies."
