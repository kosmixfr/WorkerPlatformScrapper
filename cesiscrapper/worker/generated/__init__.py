"""Stubs gRPC générés depuis `proto/platform.proto` et `proto/kv.proto`.

Régénération (depuis la racine du projet) :

    python -m grpc_tools.protoc -I proto ^
        --python_out=worker/generated ^
        --grpc_python_out=worker/generated ^
        proto/platform.proto proto/kv.proto

Puis remplacer dans les fichiers `*_pb2_grpc.py` les imports absolus par des
imports relatifs (`from . import platform_pb2 as platform__pb2`).
"""
