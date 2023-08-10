# will run in the training agent process

import logging
from controller_server import clusterstate_pb2
from controller_server import clusterstate_pb2_grpc, routing_pb2
from controller_server.clusterstate_pb2 import UpdateClusterStateRequest, GetRoutingColdStartRequest, \
    GetRoutingColdStartResponse
from environment import Cluster, Container


class WskClusterInfoCollector(clusterstate_pb2_grpc.ClusterStateServiceServicer):
    def __init__(self, cluster: Cluster):
        self.cluster: Cluster = cluster

    def UpdateClusterState(self, request: UpdateClusterStateRequest, context):
        # container set get entirely updated on each call (override)
        # NOTE, must make sure every related data structure is updated properly
        # ({funcStr:ActionState}, freeMem(MB))
        logging.info("Received state update RPC from OW controller")
        self.cluster.cluster_state_lock.acquire()
        assert list(request.clusterState.actionStatePerInvoker.keys()).sort() == list(
            self.cluster.id_2_invoker).sort(), "Invoker list from rpc update does not match record"
        for invoker_id, info in request.clusterState.actionStatePerInvoker.items():
            invoker = self.cluster.id_2_invoker[invoker_id]
            invoker.free_mem = info.freeMemoryMB  # atomic operation, no need of lock
            invoker.reset_core_pinning_count()  # reset core pinning info
            for func_id_str, action_state in info.actionStates.items():
                # busy, warm,      [...str...]
                for container_status, container_lst in action_state.stateLists.items():
                    containers = [
                        Container(container.id, [invoker.id_2_core[int(i)] for i in container.core_pin.split(",")],
                                  invoker) for
                        container in container_lst.containers]
                    # update core pinning count
                    for c in containers:
                        for core in c.pinned_core:
                            core.num_pinned_container += 1

                    match container_status:
                        case "busy":
                            self.cluster.func_2_busyinfo[func_id_str][invoker] = frozenset(
                                containers)  # update entirely instead of modification
                        case "free":
                            self.cluster.func_2_warminfo[func_id_str][invoker] = frozenset(containers)
                        case "warming": # warming containers all have the same "empty" id
                            self.cluster.func_2_warminginfo[func_id_str][invoker] = frozenset(containers)
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
            for invk_id, invoker in self.cluster.id_2_invoker.keys():
                total = len(self.cluster.func_2_busyinfo[func_id_str][invoker]) + len(
                    self.cluster.func_2_warminfo[func_id_str][invoker]) + len(
                    self.cluster.func_2_warminginfo[func_id_str][invoker]) # it's okay the set of warming container all have empty id
                if total > 0:  # zero container, skip
                    container_counter.count.append(total)
                    container_counter.invokerId.append(invk_id)
            func_2_ContainerCounter.func_2_ContainerCounter[func_id_str] = container_counter
        self.cluster.routing_stub.NotifyClusterInfo(func_2_ContainerCounter)

    def GetRoutingColdStart(self, request: GetRoutingColdStartRequest, context):
        # get all invokers whose memory is enough and the type match default type, if exist find a proper one
        # get all invoker whose memory is enough, if exist find a proper one
        # No invoker has enough memory
        logging.info("Get routing cold start RPC request")
        func_str = request.func_str
        mem_req = self.cluster.strId_2_funcs[func_str].mem_req  # in MB
        with self.cluster.cluster_state_lock:
            invoker_meet_mem = [invoker for invoker in self.cluster.id_2_invoker.values() if invoker.free_mem > mem_req]
            if invoker_meet_mem:
                invoker_of_default_type = [invoker for invoker in invoker_meet_mem if
                                           invoker.type == self.cluster.DEFAULT_SERVER_TYPE]
                if invoker_of_default_type:
                    res_invoker = self.cluster.find_proper_invoker_to_place_container(invoker_of_default_type).id
                else:
                    res_invoker = self.cluster.find_proper_invoker_to_place_container(invoker_meet_mem).id
            else:
                # TODO, how to handle, how the invoker handle cold start when there is not enough resource
                res_invoker = 0
        return GetRoutingColdStartResponse(invoker_selected=res_invoker)
