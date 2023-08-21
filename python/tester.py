import config
from controller_server import routing_pb2
from controller_server.routing_pb2 import GetArrivalInfoResponse, GetInvocationRouteResponse, EmptyRequest
import time
from pprint import pprint
import environment


class Test:
    def __init__(self, cluster):
        self.cluster: environment.Cluster = cluster

    def test_routing_rpc(self):
        # Testing: routing request handling and routing info update
        print('------------------------Test routing------------------------------')
        for i in range(3):
            routing_res: GetInvocationRouteResponse = self.cluster.routing_stub.GetInvocationRoute(
                routing_pb2.GetInvocationRouteRequest(actionName="helloPython", activationId=f'id{i}'))
            print(f'routing result for invocation {i}:', routing_res.invokerInstanceId)
            time.sleep(0.5)
        arrival_info: GetArrivalInfoResponse = self.cluster.routing_stub.GetArrivalInfo(EmptyRequest())
        pprint(f'ArrivalInfo:\n{arrival_info}')
        self.cluster.update_activation_record()
        pprint(self.cluster.func_2_invocation2Arrival)
        print('--------------------Test routing end------------------------------')

    def test_generate_workload_routing_state_update_get_obs(self):
        self.cluster.reset()
        while 1:
            time.sleep(5)
            self.cluster._check_healthy_on_each_step()




if __name__ == '__main__':
    ...
