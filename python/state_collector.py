# will run in the training agent process

from controller_server import clusterstate_pb2
from controller_server import clusterstate_pb2_grpc, routing_pb2
from controller_server.clusterstate_pb2 import UpdateClusterStateRequest
from environment import Cluster, Container


class WskClusterInfoCollector(clusterstate_pb2_grpc.ClusterStateServiceServicer):
    def __init__(self, cluster: Cluster):
        self.cluster: Cluster = cluster

    def UpdateClusterState(self, request: UpdateClusterStateRequest, context):
        #NOTE, must make sure every related data structure is updated properly
            # ({funcStr:ActionState}, freeMem(MB))
        self.cluster.cluster_state_lock.acquire()
        for invoker_id, info in request.clusterState.actionStatePerInvoker.items():
            invoker = self.cluster.id_2_invoker[invoker_id]
            invoker.free_mem = info.freeMemoryMB  # atomic operation, no need of lock
            invoker.reset_core_pinning_count() # reset core pinning info
            for func_id_str, action_state in info.actionStates.items():
                # busy, warm,      [...str...]
                for container_status, container_lst in action_state.stateLists.items():

                    containers = [
                        Container(container.id, [invoker.id_2_core[int(i)] for i in container.core_pin.split(",")],invoker) for
                        container in container_lst.containers]
                    # update core pinning count
                    for c in containers:
                        for core in c.pinned_core:
                            core.num_pinned_container+=1

                    match container_status:
                        case "busy":
                            self.cluster.func_2_busyinfo[func_id_str][invoker]=frozenset(containers) # update entirely instead of modification
                        case "free":
                            self.cluster.func_2_warminfo[func_id_str][invoker]=frozenset(containers)
                        case "warming":
                            self.cluster.func_2_warminginfo[func_id_str][invoker]=frozenset(containers)
                        case _:
                            assert False
        self.cluster._update_invoker_state()
        self.cluster.cluster_state_lock.release()
        # prepare to update the routing server
        function_set = set().union(self.cluster.func_2_busyinfo.keys(), self.cluster.func_2_warminfo.keys(),
                                   self.cluster.func_2_warminfo.keys())

        func_2_ContainerCounter = routing_pb2.NotifyClusterInfoRequest()
        for func_id_str in function_set:
            container_counter = routing_pb2.ContainerCounter()
            for invk_id in self.cluster.id_2_invoker.keys():
                invoker = self.cluster.id_2_invoker[invk_id]
                total = len(self.cluster.func_2_busyinfo[func_id_str][invoker]) + len(
                    self.cluster.func_2_warminfo[func_id_str][invoker]) + len(
                    self.cluster.func_2_warminginfo[func_id_str][invoker])
                if total > 0:  # zero container, skip
                    container_counter.count.append(total)
                    container_counter.invokerId.append(invk_id)
            func_2_ContainerCounter.func_2_ContainerCounter[func_id_str] = container_counter
        self.cluster.routing_stub.NotifyClusterInfo(func_2_ContainerCounter)
