#!/usr/bin/env bash
set -euo pipefail
: "${RESOURCE_GROUP:?set RESOURCE_GROUP}"
: "${CONTAINER_IMAGE:?set CONTAINER_IMAGE (image pinned by digest)}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

az group create -n "$RESOURCE_GROUP" -l "${LOCATION:-centralindia}" -o none
az deployment group create -g "$RESOURCE_GROUP" -n main --parameters "$here/bicep/main.bicepparam" \
  --query properties.outputs -o json

api="$(az deployment group show -g "$RESOURCE_GROUP" -n main --query properties.outputs.apiUrl.value -o tsv)"
curl -fsS --retry 5 --retry-delay 5 --max-time 60 "$api/health"; echo
