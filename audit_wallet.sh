#!/usr/bin/env bash
set -e

WALLET="$1"
CHAIN="${2:-sol}"

if [ -z "$WALLET" ]; then
  echo "Usage: ./audit_wallet.sh <WALLET_ADDRESS> [CHAIN]"
  echo "Example: ./audit_wallet.sh Eu1KU118rGQEAMnV5uXojfdx6nhozSgNr4Fhxi2suxHB sol"
  exit 1
fi

echo "Auditing wallet: $WALLET on chain: $CHAIN ..."
curl -s -X POST http://127.0.0.1:8000/api/wallet \
  -H "Content-Type: application/json" \
  -d "{\"chain\":\"$CHAIN\",\"address\":\"$WALLET\"}" | jq .
