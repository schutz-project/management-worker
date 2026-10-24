#!/usr/bin/env bash
set -Eeuo pipefail
TAG="${1:?image tag required}"
SERVICE="${2:?service required}"
[[ "$TAG" =~ ^sha-[a-f0-9]{40}-[0-9]+-[0-9]+$ ]] || {
  echo INVALID_TAG
  exit 2
}
case "$SERVICE" in
  management-web) PEER=management-worker ;;
  management-worker) PEER=management-web ;;
  *) echo INVALID_SERVICE; exit 2 ;;
esac

cd /opt/schutz-monitoring
umask 077
exec 9>.management-release.lock
flock -w 120 9 || {
  echo MANAGEMENT_RELEASE_LOCK_TIMEOUT
  exit 1
}

project=schutz-monitoring
container="schutz-monitoring-${SERVICE}-1"
peer="schutz-monitoring-${PEER}-1"
db=schutz-monitoring-management-db-1
web=schutz-monitoring-management-web-1
worker=schutz-monitoring-management-worker-1

# The running Web container records the complete ordered deployment stack.
config_files=$(docker inspect schutz-monitoring-management-web-1 \
  --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}')
validated_files=$(python3 - "$config_files" <<'COMPOSE_FILES_PY'
import sys
from pathlib import Path

root = "/opt/schutz-monitoring/"
required = {
    root + "compose.management-ec2.yaml",
    root + "updates/incident-drafts/compose.incident-drafts.yaml",
    root + "compose.loki-only.yaml",
    root + "compose.otel-only.yaml",
    root + "compose.prometheus-only.yaml",
    root + "compose.blackbox-only.yaml",
    root + "compose.grafana-only.yaml",
    root + "compose.ecr.yaml",
}
https = root + "compose.management-https.yaml"
files = sys.argv[1].split(",")
if len(files) != len(set(files)):
    raise SystemExit("COMPOSE_FILES_DUPLICATED")
if not required <= set(files) or set(files) - required - {https}:
    raise SystemExit("COMPOSE_FILE_SET_MISMATCH")
if https in files and files[-1] != https:
    raise SystemExit("HTTPS_OVERRIDE_MUST_BE_LAST")
if not all(Path(name).is_file() for name in files):
    raise SystemExit("COMPOSE_FILE_MISSING")
print("\n".join(files))
COMPOSE_FILES_PY
)
mapfile -t files <<< "$validated_files"

args=()
has_override=0
for f in "${files[@]}"; do
  [[ -f "$f" ]] || { echo COMPOSE_FILE_MISSING; exit 1; }
  [[ $(realpath "$f") != /opt/schutz-monitoring/compose.ecr.yaml ]] ||
    has_override=1
  args+=(-f "$f")
done
[[ "$has_override" == 1 ]] || {
  echo ECR_OVERRIDE_NOT_ACTIVE
  exit 1
}

dc() {
  docker compose -p "$project" "${args[@]}" "$@"
}

old_image=$(docker inspect "$container" --format '{{.Config.Image}}')
old_image_id=$(docker inspect "$container" --format '{{.Image}}')
old_container_id=$(docker inspect "$container" --format '{{.Id}}')
peer_id=$(docker inspect "$peer" --format '{{.Id}}')
db_id=$(docker inspect "$db" --format '{{.Id}}')

[[ $(docker image inspect "$old_image" --format '{{.Id}}') == "$old_image_id" ]]

new_image="527673188566.dkr.ecr.ap-northeast-2.amazonaws.com/schutz/${SERVICE}:${TAG}"
expected_image="$old_image"
expected_id="$old_image_id"

check_health() {
  [[ $(docker inspect "$container" --format '{{.State.Running}}' 2>/dev/null) == true &&
     $(docker inspect "$container" --format '{{.RestartCount}}' 2>/dev/null) == 0 &&
     $(docker inspect "$container" --format '{{.Config.Image}}' 2>/dev/null) == "$expected_image" &&
     $(docker inspect "$container" --format '{{.Image}}' 2>/dev/null) == "$expected_id" &&
     $(docker inspect "$peer" --format '{{.Id}}' 2>/dev/null) == "$peer_id" &&
     $(docker inspect "$peer" --format '{{.State.Running}}' 2>/dev/null) == true &&
     $(docker inspect "$peer" --format '{{.RestartCount}}' 2>/dev/null) == 0 &&
     $(docker inspect "$db" --format '{{.Id}}' 2>/dev/null) == "$db_id" &&
     $(docker inspect "$db" --format '{{.State.Health.Status}}' 2>/dev/null) == healthy ]] || return 1

  docker exec "$web" python -c 'import urllib.request; assert urllib.request.urlopen("http://127.0.0.1:8000/health",timeout=5).status==200' >/dev/null 2>&1 || return 1

  local logs
  logs=$(docker logs --since 180s "$worker" 2>&1) || return 1
  [[ "$logs" == *'probe collection complete'* &&
     "$logs" == *'login event collection complete'* &&
     "$logs" != *'unavailable'* ]]
}

wait_health() {
  local deadline=$((SECONDS + ${1:-180})) passes=0
  while (( SECONDS < deadline )); do
    if check_health; then
      passes=$((passes + 1))
      if (( passes >= 3 )); then return 0; fi
    else
      passes=0
    fi
    sleep 3
  done
  return 1
}

wait_health 30 || {
  echo BASELINE_UNHEALTHY_NO_DEPLOYMENT
  exit 1
}

export SCHUTZ_SERVICE="$SERVICE"
export SCHUTZ_OLD_IMAGE="$old_image"
export SCHUTZ_NEW_IMAGE="$new_image"

dc config --format json | python3 -c 'import json,os,sys; assert json.load(sys.stdin)["services"][os.environ["SCHUTZ_SERVICE"]]["image"]==os.environ["SCHUTZ_OLD_IMAGE"]'

if [[ "$old_image" == "$new_image" ]]; then
  echo ALREADY_DEPLOYED
  exit 0
fi

auth_dir=$(mktemp -d)
trap 'rm -rf "$auth_dir"' EXIT
aws ecr get-login-password --region ap-northeast-2 |
  docker --config "$auth_dir" login \
    --username AWS --password-stdin \
    527673188566.dkr.ecr.ap-northeast-2.amazonaws.com >/dev/null

timeout 180 docker --config "$auth_dir" pull "$new_image" >/dev/null
new_image_id=$(docker image inspect "$new_image" --format '{{.Id}}')
rm -rf "$auth_dir"
trap - EXIT

mkdir -p .release-backups
chmod 700 .release-backups
backup=$(mktemp -d .release-backups/service-cd-XXXXXXXX)
cp -p compose.ecr.yaml "$backup/compose.ecr.yaml"
printf '%s\n' "$old_image" > "$backup/previous-image.txt"

activate() {
  dc config -q &&
    dc up -d --no-deps --no-build --pull never "$SERVICE"
}

rollback() {
  local rc="${1:-$?}"
  trap - ERR INT TERM
  echo MANAGEMENT_SERVICE_ROLLBACK_START
  expected_image="$old_image"
  expected_id="$old_image_id"

  if cp -p "$backup/compose.ecr.yaml" compose.ecr.yaml &&
     activate && wait_health 180; then
    echo MANAGEMENT_SERVICE_ROLLBACK_OK
  else
    echo "MANAGEMENT_SERVICE_ROLLBACK_FAILED_BACKUP=$backup"
  fi
  exit "$rc"
}

trap rollback ERR
trap 'rollback 130' INT
trap 'rollback 143' TERM

python3 - <<'PY'
import os,re,stat,tempfile
from pathlib import Path

p=Path('compose.ecr.yaml')
lines=p.read_text().splitlines(keepends=True)
service=None
count=0

for i,line in enumerate(lines):
    m=re.match(r'^  ([A-Za-z0-9_-]+):\s*(?:#.*)?$',line)
    if m:
        service=m.group(1)
    elif line.strip() and not line.startswith('    '):
        service=None
    if service==os.environ['SCHUTZ_SERVICE'] and re.match(r'^    image:\s*',line):
        lines[i]='    image: '+os.environ['SCHUTZ_NEW_IMAGE']+'\n'
        count+=1

if count!=1:
    raise SystemExit('COMPOSE_IMAGE_REPLACEMENT_COUNT_MISMATCH')

fd,name=tempfile.mkstemp(dir='.',prefix='.service-release-')
try:
    with os.fdopen(fd,'w') as stream:
        stream.write(''.join(lines))
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(name,stat.S_IMODE(p.stat().st_mode))
    os.replace(name,p)
finally:
    if os.path.exists(name):
        os.unlink(name)
PY

expected_image="$new_image"
expected_id="$new_image_id"
activate

[[ $(docker inspect "$container" --format '{{.Id}}') != "$old_container_id" ]]
wait_health 180
trap - ERR INT TERM

echo MANAGEMENT_SERVICE_RELEASE_SUCCESS
echo "DEPLOYED_SERVICE=$SERVICE"
echo "DEPLOYED_TAG=$TAG"
echo MANAGEMENT_PEER_AND_DB_UNCHANGED
