import getopt
import sys
from concurrent import futures

import grpc
import controller_pb2 as controller_types
import controller_pb2_grpc as controller_service

state = {}

class ControllerService(controller_service.ControllerServiceServicer):
    def GetInvocationRoute(self, request, context):
        return controller_types.GetInvocationRouteResponse(invokerHost="panic-cloud-xs-01.cs.rutgers.edu:50051")

def main():
    opts, args = getopt.getopt(sys.argv[1:], 'p:')
    for opt, arg in opts:
        if opt == '-p':
            state['port'] = arg

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    controller_service.add_ControllerServiceServicer_to_server(ControllerService(), server)
    server.add_insecure_port(f"0.0.0.0:{state['port']}")
    server.start()

if __name__ == "__main__":
    main()