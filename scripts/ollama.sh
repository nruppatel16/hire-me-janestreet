#!/usr/bin/env bash
# Project-local Ollama server for embeddings (D-023). Models live in ./.ollama-models, port 11435,
# so it never touches a personal Ollama install or its model directory.
set -euo pipefail
cd "$(dirname "$0")/.."
export OLLAMA_MODELS="$PWD/.ollama-models" OLLAMA_HOST=127.0.0.1:11435
case "${1:-start}" in
  start)
    curl -s -m 1 "http://$OLLAMA_HOST/api/version" >/dev/null && { echo "ollama already up on $OLLAMA_HOST"; exit 0; }
    nohup ollama serve > .ollama.log 2>&1 &
    for _ in $(seq 1 40); do curl -s -m 1 "http://$OLLAMA_HOST/api/version" >/dev/null && break; sleep 0.25; done
    ollama list | grep -q qwen3-embedding:0.6b || ollama pull qwen3-embedding:0.6b
    echo "ollama up on $OLLAMA_HOST" ;;
  stop)  # only the project server on this port, never a personal Ollama
    pid=$(lsof -ti "tcp:${OLLAMA_HOST##*:}" -sTCP:LISTEN || true)
    if [ -n "$pid" ]; then kill -TERM $pid && echo "stopped ollama on $OLLAMA_HOST"; else echo "not running"; fi ;;
  *) echo "usage: $0 start|stop"; exit 1 ;;
esac
