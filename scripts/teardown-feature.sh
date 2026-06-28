#!/bin/bash
set -euo pipefail

NAME=$1
docker rm -f "$NAME" 2>/dev/null || true
echo "Removed container: ${NAME}"
