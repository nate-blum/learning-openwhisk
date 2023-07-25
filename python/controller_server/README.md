IMPORTANT:
must copy latest up to date controller.proto from /core/.../controller/protobuf into this folder
must install requirements.txt
must run:
python3 -m grpc_tools.protoc --proto_path=. --python_out=. --grpc_python_out=. *.proto
to generate python proto files

python3 server.py -p 3100