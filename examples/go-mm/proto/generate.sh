#!/usr/bin/env bash
# Generate Go gRPC code from the proto definition.
#
# Prerequisites:
#   brew install protobuf
#   go install google.golang.org/protobuf/cmd/protoc-gen-go@latest
#   go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest
#
# Make sure $GOPATH/bin (usually ~/go/bin) is in your PATH.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROTO_SRC="$SCRIPT_DIR/../../../src/rfq_test/proto/injective_rfq_rpc.proto"
OUT_DIR="$SCRIPT_DIR/injective_rfq_rpc"

mkdir -p "$OUT_DIR"

protoc \
  --proto_path="$(dirname "$PROTO_SRC")" \
  --go_out="$OUT_DIR" \
  --go_opt=paths=source_relative \
  --go_opt=Minjective_rfq_rpc.proto=mm-scripts-go/proto/injective_rfq_rpc \
  --go-grpc_out="$OUT_DIR" \
  --go-grpc_opt=paths=source_relative \
  --go-grpc_opt=Minjective_rfq_rpc.proto=mm-scripts-go/proto/injective_rfq_rpc \
  "$(basename "$PROTO_SRC")"

echo "✅ Generated Go gRPC code in $OUT_DIR"
