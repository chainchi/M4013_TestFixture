#!/bin/bash
# Runs a command in the Pi's shared tmux session (visible to anyone attached),
# waits for it to finish and prints its output.
#   tools/pirun.sh 'i2cdetect -y 1'          [timeout seconds, default 60]
# Open the shared window with:  ssh -t m4013pi@<pi> tmux new -A -s shared
H=${PI_HOST:-m4013pi@192.168.10.2}
cmd=$1
timeout=${2:-60}
id=$RANDOM$RANDOM
ssh_pi() { ssh -o BatchMode=yes -o ConnectTimeout=5 "$H" "$@"; }

# one { ... } block over two lines: bash reads both before running anything, so
# a trailing comment in cmd can't swallow the marker and sudo can't discard it
ssh_pi "tmux has-session -t shared 2>/dev/null || tmux new-session -d -s shared -x 160 -y 45
tmux send-keys -t shared -l -- $(printf %q "{ $cmd")
tmux send-keys -t shared Enter
tmux send-keys -t shared -l -- $(printf %q "}; echo \"#done-$id rc=\$?\"")
tmux send-keys -t shared Enter" || exit 255

for ((i = 0; i < timeout * 2; i++)); do
    out=$(ssh_pi "tmux capture-pane -p -J -S -5000 -t shared")
    if grep -q "^#done-$id" <<<"$out"; then
        # output = lines after the typed block, up to the marker
        CMD1=$(head -n1 <<<"$cmd") awk -v id="done-$id" '
            index($0, "$ { " ENVIRON["CMD1"]) { buf = ""; next }      # last time cmd was typed
            /^#done-/ && index($0, id) { printf "%s%s\n", buf, $0; exit }
            !index($0, id) { buf = buf $0 "\n" }' <<<"$out"
        exit 0
    fi
    sleep 0.5
done
echo "pirun: still running after ${timeout}s (left running in the shared terminal)" >&2
exit 124
