import getopt
import logging
import sys
from concurrent import futures

import grpc
import controller_pb2 as controller_types
import controller_pb2_grpc as controller_service
from grpc_reflection.v1alpha import reflection

state = {}

class ControllerService(controller_service.ControllerServiceServicer):
    def GetInvocationRoute(self, request, context):
        return controller_types.GetInvocationRouteResponse(invokerInstanceId=0)

def main():
    opts, args = getopt.getopt(sys.argv[1:], 'p:')
    for opt, arg in opts:
        if opt == '-p':
            state['port'] = arg

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    controller_service.add_ControllerServiceServicer_to_server(ControllerService(), server)
    reflection.enable_server_reflection((controller_types.DESCRIPTOR.services_by_name["ControllerService"].full_name, reflection.SERVICE_NAME), server)
    server.add_insecure_port(f"[::]:{state['port']}")
    server.start()
    server.wait_for_termination()

if __name__ == "__main__":
    logging.basicConfig()
    main()