# will run in the training agent process

from controller_server import clusterstate_pb2
from controller_server import clusterstate_pb2_grpc
from controller_server.clusterstate_pb2 import UpdateClusterStateRequest
from environment import Cluster


class WskClusterInfoCollector(clusterstate_pb2_grpc.ClusterStateServiceServicer):
    def __init__(self, cluster: Cluster):
        self.cluster: Cluster = cluster

    def UpdateClusterState(self, request: UpdateClusterStateRequest, context):
        # ({funcStr:ActionState}, freeMem(MB))
        self.cluster.cluster_state_lock.acquire()
        for invoker_id, info in request.clusterState.actionStatePerInvoker.items():
            invoker = self.cluster.id_2_invoker[invoker_id]
            self.cluster.invoker_2_freemem[invoker_id] = info.freeMemoryMB  # atomic operation, no need of lock
            for func_id_str, action_state in info.actionStates.items():
                # busy, warm,      [...str...]
                for container_status, container_lst in action_state.stateLists.items():
                    containers_str = [container.id for container in container_lst.containers]
                    match container_status:
                        case "busy":
                            self.cluster.func_2_busyinfo[func_id_str][invoker].update(containers_str)
                        case "free":
                            self.cluster.func_2_warminfo[func_id_str][invoker].update(containers_str)
                        case "warming":
                            self.cluster.func_2_warminginfo[func_id_str][invoker].update(containers_str)
                        case _:
                            assert False
        self.cluster.cluster_state_lock.release()
