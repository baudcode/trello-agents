#!/bin/bash
set -euo pipefail

BRANCH=$1
IMAGE=$2
PROJECT=${3:-app}
DOMAIN=${4:-feat.localhost}

HOST="${BRANCH}.${PROJECT}.${DOMAIN}"
NAME="${PROJECT}-${BRANCH}"

docker pull "$IMAGE"
docker rm -f "$NAME" 2>/dev/null || true
docker run -d \
  --name "$NAME" \
  --network web \
  --restart unless-stopped \
  --memory 512m --cpus 1.0 \
  --label "traefik.enable=true" \
  --label "traefik.http.routers.${NAME}.rule=Host(\`${HOST}\`)" \
  --label "traefik.http.routers.${NAME}.entrypoints=websecure" \
  --label "traefik.http.routers.${NAME}.tls.certresolver=le" \
  --label "traefik.http.services.${NAME}.loadbalancer.server.port=8000" \
  "$IMAGE"

echo "{\"deploy_url\": \"https://${HOST}\", \"name\": \"${NAME}\"}"
