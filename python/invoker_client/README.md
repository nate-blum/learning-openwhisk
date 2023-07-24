# IMPORTANT:
- must copy latest up to date invoker.proto from /core/.../invoker/protobuf into this folder
- must install requirements.txt
- must run:
python3 -m grpc_tools.protoc --proto_path=. --python_out=. --grpc_python_out=. *.proto
to generate python proto files

- python3 client.py -h panic-cloud-xs-01.cs.rutgers.edu:50051