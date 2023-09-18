import getopt
import logging
import sys
from concurrent import futures
from datetime import datetime

import grpc
import routing_pb2 as routing_types
import clusterstate_pb2 as clusterstate_types
import routing_pb2_grpc as routing_service
import clusterstate_pb2_grpc as clusterstate_service
from grpc_reflection.v1alpha import reflection
import threading

state = {
    "instance": 0
}

class RoutingService(routing_service.RoutingServiceServicer):
    def GetInvocationRoute(self, request, context):
        state["instance"] += 1
        state["instance"] %= 3
        return routing_types.GetInvocationRouteResponse(invokerInstanceId=state["instance"])

    def RoutingUpdateClusterState(self, request, context):
        print(f"[{datetime.now().strftime('%H:%M:%S')}]", request)
        return clusterstate_types.UpdateClusterStateResponse()

class ClusterStateService(clusterstate_service.ClusterStateServiceServicer):
    def UpdateClusterState(self, request, context):
        print(f"[{datetime.now().strftime('%H:%M:%S')}]", request)
        return clusterstate_types.UpdateClusterStateResponse()

def start_server(server, name):
    print("starting server " + name)
    server.start()
    server.wait_for_termination()

def main():
    # opts, args = getopt.getopt(sys.argv[1:], 'p:')
    # for opt, arg in opts:
    #     if opt == '-p':
    #         state['port'] = arg

    server1 = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    routing_service.add_RoutingServiceServicer_to_server(RoutingService(), server1)
    reflection.enable_server_reflection((routing_types.DESCRIPTOR.services_by_name["RoutingService"].full_name, reflection.SERVICE_NAME), server1)
    server1.add_insecure_port(f"[::]:50051")

    server2 = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    clusterstate_service.add_ClusterStateServiceServicer_to_server(ClusterStateService(), server2)
    reflection.enable_server_reflection((clusterstate_types.DESCRIPTOR.services_by_name["ClusterStateService"].full_name, reflection.SERVICE_NAME), server2)
    server2.add_insecure_port("[::]:50052")

    threads = [threading.Thread(target=start_server, args=server) for server in ((server2, "c"), (server1, "r"))]
    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    # server2.start()
    # server2.wait_for_termination()

if __name__ == "__main__":
    logging.basicConfig()
    main()