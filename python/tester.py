import logging

import numpy as np

import config
from controller_server import routing_pb2
from controller_server.routing_pb2 import GetArrivalInfoResponse, GetInvocationRouteResponse, EmptyRequest
import time
from pprint import pprint
import environment
import data_structure


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

    def test_create_container_issue_requests(self):
        # <----------test deletion of container ---------->
        # for function, dict_ in self.cluster.func_2_warminfo.items():
        #     for invoker, st in dict_.items():
        #         for container in st:
        #             invoker.rpc_delete_container(container_id=container.id, func_name=function)
        #             logging.info(f"Deleted one container {container.id} on invoker {invoker.id}")
        # <---------Test reset---------------------------->
        # for invoker in self.cluster.id_2_invoker.values():
        #     invoker.rpc_reset_invoker()
        # <---------------Test start container--------------->
        # self.cluster.take_action({0: data_structure.Action(container_delta=1,type='xs')})
        # self.cluster.take_action({1: data_structure.Action(container_delta=1,type='xe')})
        # while True:
        #    time.sleep(10)
        #self.cluster.update_activation_record()

        self.cluster.reset()
        for i in range(15):
            print(f"[==============================================Step {i}=======================================================")
            self.cluster.step(np.array([0, -1, 0, -1]))
        # # time.sleep(6) # after the invocation is done
        # n = 0
        # while n < 20:
        #     logging.info(f"------------------------------Step {n}---------------------------------------------------")
        #     self.cluster.step(np.array([0, -1, 0, -1]))
        #     n += 1
        # self.cluster.get_avg_busy_container_utilization_per_type(['func1', 'func2'])


if __name__ == '__main__':
    ...
