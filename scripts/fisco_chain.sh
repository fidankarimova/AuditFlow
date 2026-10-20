#!/usr/bin/env bash
# Local FISCO BCOS 3.x chain: 4 nodes, PBFT consensus, Web3-compatible RPC on node0 (127.0.0.1:8545, chain id 20200).
#
#   scripts/fisco_chain.sh up      # download (first run), build and start the chain
#   scripts/fisco_chain.sh stop    # stop all 4 nodes
#   scripts/fisco_chain.sh status  # latest block per node
#
# Files live outside the repo in $FISCO_DIR (default ~/fisco). Linux x86_64 / aarch64.
set -euo pipefail

VERSION="${FISCO_VERSION:-v3.17.1}"
DIR="${FISCO_DIR:-$HOME/fisco}"
NODES="$DIR/nodes/127.0.0.1"
ARCH="$(uname -m)"

up() {
  mkdir -p "$DIR" && cd "$DIR"
  if [ ! -x fisco-bcos ]; then
    base="https://github.com/FISCO-BCOS/FISCO-BCOS/releases/download/$VERSION"
    curl -sSLO "$base/build_chain.sh"
    curl -sSLO "$base/fisco-bcos-linux-$ARCH.tar.gz"
    tar xzf "fisco-bcos-linux-$ARCH.tar.gz"
  fi
  if [ ! -d "$NODES" ]; then
    bash build_chain.sh -l 127.0.0.1:4 -p 30300,20200 -e ./fisco-bcos -o nodes
    # Web3 RPC: only node0, only on localhost (all nodes would otherwise bind the same port 8545).
    sed -i '/^\[web3_rpc\]/,/^\[/{s/^\(\s*\)enable=.*/\1enable=false/;s/listen_ip=0.0.0.0/listen_ip=127.0.0.1/}' "$NODES"/node*/config.ini
    sed -i '/^\[web3_rpc\]/,/^\[/{s/^\(\s*\)enable=false/\1enable=true/}' "$NODES/node0/config.ini"
  fi
  bash "$NODES/start_all.sh"
}

case "${1:-up}" in
  up) up ;;
  stop) bash "$NODES/stop_all.sh" ;;
  status)
    for n in 0 1 2 3; do
      echo "node$n: $(grep -ahoE 'number=[0-9]+' "$NODES/node$n/log/"*.log 2>/dev/null | tail -1)"
    done ;;
  *) echo "usage: $0 up|stop|status" >&2; exit 2 ;;
esac
