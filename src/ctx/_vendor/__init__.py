"""Vendored SCIP protobuf bindings (M-K4).

scip_pb2.py is generated from scip.proto (github.com/sourcegraph/scip,
committed alongside) with protoc. Regenerate after a proto bump:
    python -m grpc_tools.protoc -I. --python_out=. scip.proto
Runtime needs only the protobuf lib (the [scip] extra); absence costs
nothing (the ingester probes and degrades)."""
