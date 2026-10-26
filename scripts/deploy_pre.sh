#!/usr/bin/env bash
set -euo pipefail
: "${DEPLOY_PATH:?}" "${DEPLOY_SERVICE:?}" "${IMAGE_REGISTRY:?}" "${IMAGE_NAMESPACE:?}" "${IMAGE_NAME:?}" "${IMAGE_TAG:?}" "${ELE2API_ORIGIN:?}"
mkdir -p "$DEPLOY_PATH"
test -f "$DEPLOY_PATH/.env"
env_checksum="$(sha256sum "$DEPLOY_PATH/.env" | awk '{print $1}')"
if [ ! -f "$DEPLOY_PATH/docker-compose.yml" ]; then
  cp docker-compose.yml "$DEPLOY_PATH/docker-compose.yml"
fi
compose_checksum="$(sha256sum "$DEPLOY_PATH/docker-compose.yml" | awk '{print $1}')"
docker network inspect my-shared-net >/dev/null 2>&1 || docker network create my-shared-net
(
  cd "$DEPLOY_PATH"
  pulled=0
  for attempt in 1 2 3; do
    if IMAGE_REGISTRY="$IMAGE_REGISTRY" IMAGE_NAMESPACE="$IMAGE_NAMESPACE" IMAGE_NAME="$IMAGE_NAME" IMAGE_TAG="$IMAGE_TAG" docker compose pull "$DEPLOY_SERVICE"; then pulled=1; break; fi
    sleep 5
  done
  test "$pulled" -eq 1
)
if [ "$(docker inspect -f '{{.State.Running}}' "$DEPLOY_SERVICE" 2>/dev/null || true)" = true ]; then
  active_commands() {
    docker exec -i "$DEPLOY_SERVICE" python - <<'PY'
import os, sqlite3
db = sqlite3.connect('file:' + os.environ.get('ELE_DATA_DIR', '/app/data') + '/ele2api.db?mode=ro', uri=True, timeout=10)
print(db.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('queued','preparing','submitting','submitted','running')").fetchone()[0]
      + db.execute("SELECT COUNT(*) FROM accounts WHERE status='logging_in'").fetchone()[0])
PY
  }
  deadline=$((SECONDS + 1200))
  while true; do
    active="$(active_commands)"
    echo "ELE2API active tasks or logins: $active"
    if [ "$active" -eq 0 ]; then
      sleep 3
      if [ "$(active_commands)" -eq 0 ]; then break; fi
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "Refusing to interrupt tasks or online login"
      exit 1
    fi
    sleep 10
  done
fi
cd "$DEPLOY_PATH"
IMAGE_REGISTRY="$IMAGE_REGISTRY" IMAGE_NAMESPACE="$IMAGE_NAMESPACE" IMAGE_NAME="$IMAGE_NAME" IMAGE_TAG="$IMAGE_TAG" docker compose up -d --no-deps --no-build --pull never "$DEPLOY_SERVICE"
test "$env_checksum" = "$(sha256sum .env | awk '{print $1}')"
test "$compose_checksum" = "$(sha256sum docker-compose.yml | awk '{print $1}')"
for attempt in $(seq 1 30); do
  if curl -fsS --max-time 5 "$ELE2API_ORIGIN/health" >/dev/null; then exit 0; fi
  sleep 5
done
echo "ELE2API did not become healthy; existing data and configuration were preserved."
exit 1
