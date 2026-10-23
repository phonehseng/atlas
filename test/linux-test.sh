#!/bin/sh
# Test helper (run as root inside WSL/Ubuntu or any Linux VM): starts the Linux build of the wizard for a
# throwaway install in /srv/stacktest with every port +20000, then drives steps through its API.
#   sh linux-test.sh start            start the wizard (background)
#   sh linux-test.sh run <step> [json-values]
#   sh linux-test.sh state
#   sh linux-test.sh stop
# The access key and admin password come from test-install.json next to this script (test-only values).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
BIN=${BIN:-$HERE/../dist/MediaStackSetup-linux-x64}
PORT=8599
TOKEN=$(sed -n 's/.*"token": *"\([^"]*\)".*/\1/p' "$HERE/test-install.json")
PASS=$(sed -n 's/.*"password": *"\([^"]*\)".*/\1/p' "$HERE/test-install.json")
API="http://127.0.0.1:$PORT"
H="X-Setup-Token: $TOKEN"

case "$1" in
start)
  cp "$BIN" /tmp/MediaStackSetup && chmod +x /tmp/MediaStackSetup
  nohup /tmp/MediaStackSetup --root /srv/stacktest --port-offset 20000 --name "Media Stack Test" \
    --no-browser --listen 127.0.0.1 --port $PORT --token "$TOKEN" >/tmp/mediastack-wizard.log 2>&1 &
  sleep 2; curl -fsS -H "$H" "$API/api/state" >/dev/null && echo "wizard up" ;;
state)
  curl -fsS -H "$H" "$API/api/state" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(" ".join(s["id"]+":"+s["status"] for s in d["steps"]))' ;;
run)
  step=$2; values=${3:-'{}'}
  values=$(printf '%s' "$values" | sed "s/{password}/$PASS/g")
  job=$(curl -fsS -H "$H" -H 'Content-Type: application/json' -d "{\"step\":\"$step\",\"values\":$values,\"password\":\"$PASS\"}" "$API/api/run" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["job"])')
  from=0
  while :; do
    out=$(curl -fsS -H "$H" "$API/api/job?id=$job&from=$from")
    printf '%s' "$out" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for l in d["lines"]:
    if l["kind"] != "progress" or l.get("pct", 0) >= 100:
        print(f"{l[\"kind\"]:<6} {l[\"text\"]}")
'
    from=$(printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["lines"][-1]["n"]+1 if d["lines"] else '"$from"')')
    printf '%s' "$out" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["done"] else 1)' && break
  done
  curl -fsS -H "$H" "$API/api/state" | python3 -c 'import json,sys; s=[x for x in json.load(sys.stdin)["steps"] if x["id"]=="'"$step"'"][0]; print("==", s["id"], "->", s["status"], s.get("message",""))' ;;
stop)
  curl -fsS -H "$H" -H 'Content-Type: application/json' -d '{}' "$API/api/quit" >/dev/null || true ;;
esac
