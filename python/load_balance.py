from collections import deque, defaultdict
from multiprocessing import Process
from controller_server.server import ControllerService
from controller_server import controller_pb2 as controller_types
from controller_server import controller_pb2_grpc as controller_service
from time import time

class WskControllerService(controller_service.ControllerServiceServicer):
    def __init__(self):
        self.func_2_arrivalQueue = defaultdict(deque)

    def GetInvocationRoute(self, request, context):
        t =time()
        func_id = 0 # TODO
        self.func_2_arrivalQueue[func_id].append(t)
        return controller_types.GetInvocationRouteResponse(invokerHost="panic-cloud-xs-01.cs.rutgers.edu:50051")


class LoadBalance:
    def __init__(self, rpc_server_port: str, max_num_thread_rpc_server: int):
        pass
