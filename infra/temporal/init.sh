#!/bin/sh
# One-shot (temporal-init service): wait for the server, then register the namespace.
set -eu
until temporal operator cluster health --address "$TEMPORAL_ADDRESS" 2>/dev/null | grep -q SERVING; do
  echo "waiting for temporal at $TEMPORAL_ADDRESS"; sleep 2
done
if temporal operator namespace describe -n default --address "$TEMPORAL_ADDRESS" >/dev/null 2>&1; then
  echo "namespace default exists"
else
  temporal operator namespace create -n default --retention 72h --address "$TEMPORAL_ADDRESS"
fi
