import getopt
import logging
import sys
from concurrent import futures
from datetime import datetime

import grpc
import routing_pb2 as routing_types
import clusterstate_pb2 as clusterstate_types
import routing_pb2_grpc as routing_service
from grpc_reflection.v1alpha import reflection

state = {}

class RoutingService(routing_service.RoutingServiceServicer):
    def GetInvocationRoute(self, request, context):
        return routing_types.GetInvocationRouteResponse(invokerInstanceId=0)

    def RoutingUpdateClusterState(self, request, context):
        print(f"[{datetime.now().strftime('%H:%M:%S')}]" + request)
        return clusterstate_types.UpdateClusterStateResponse()

def main():
    opts, args = getopt.getopt(sys.argv[1:], 'p:')
    for opt, arg in opts:
        if opt == '-p':
            state['port'] = arg

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    routing_service.add_RoutingServiceServicer_to_server(RoutingService(), server)
    reflection.enable_server_reflection((routing_types.DESCRIPTOR.services_by_name["RoutingService"].full_name, reflection.SERVICE_NAME), server)
    server.add_insecure_port(f"[::]:{state['port']}")
    server.start()
    server.wait_for_termination()

if __name__ == "__main__":
    logging.basicConfig()
    main()