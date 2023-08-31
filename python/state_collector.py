# will run in the training agent process
import pprint
from collections import defaultdict
import logging
import time
from controller_server import clusterstate_pb2_grpc, routing_pb2
from controller_server.clusterstate_pb2 import UpdateClusterStateRequest, UpdateClusterStateResponse, \
    GetRoutingColdStartRequest, \
    GetRoutingColdStartResponse

from threading import Lock
import environment as env


class WskClusterInfoCollector(clusterstate_pb2_grpc.ClusterStateServiceServicer):
    def __init__(self, cluster: env.Cluster):
        self.cluster: env.Cluster = cluster
        self.stats_update_lock = Lock()
        self.warming_dummy_id = 0

    def _clear_stale_info(self, touch_func_invoker_set: set[tuple[str, int]]):
        # get rid of stale record in the information dict
        for func in self.cluster.strId_2_funcs.keys():
            for invoker in self.cluster.id_2_invoker.values():
                if (func, invoker.id) in touch_func_invoker_set:
                    continue
                else:
                    self.cluster.func_2_warminfo[func][invoker] = frozenset()
                    self.cluster.func_2_busyinfo[func][invoker] = frozenset()
                    self.cluster.func_2_warminginfo[func][invoker] = frozenset()

    def UpdateClusterState(self, request: UpdateClusterStateRequest, context):
        # container set get entirely updated on each call (override)
        # NOTE, must make sure every related data structure is updated properly, must make sure every [func][invoker] pair is updated ???!!!
        # NOTE, each invoker will have an entry, but not all the function registered will have an entry
        # ({funcStr:ActionState}, freeMem(MB))
        logging.info(f"Received state update RPC from OW controller, request: {request}")
        touched_func_invoker_set: set[tuple[str, int]] = set()  # contain the func that has been touched by the update
        with self.cluster.cluster_state_lock:
            assert list(request.clusterState.actionStatePerInvoker.keys()).sort() == list(
                self.cluster.id_2_invoker).sort(), "Invoker list from rpc update does not match record"
            for invoker_id, info in request.clusterState.actionStatePerInvoker.items():
                invoker = self.cluster.id_2_invoker[invoker_id]
                invoker.free_mem = info.freeMemoryMB  # actually atomic operation, no need of lock
                invoker.reset_core_pinning_count()  # reset core pinning info
                for func_id_str, action_state in info.actionStates.items():
                    touched_func_invoker_set.add((func_id_str, invoker_id))
                    # busy, warm,      [...str...]
                    for container_status, container_lst in action_state.stateLists.items():
                        core_pinned_list_list = []
                        for container in container_lst.containers:
                            try:
                                pin_lst = [invoker.id_2_core[int(i)] for i in container.core_pin.split(",")]
                            except ValueError:
                                pin_lst = []
                                logging.error(f"No pinning information for container {container.id}")
                            finally:
                                core_pinned_list_list.append(pin_lst)
                        containers = [
                            env.Container(container.id, pin_lst_, invoker) for container, pin_lst_ in
                            # NOTE, id might be empty str, but its okay as agent won't delete a warming container
                            zip(container_lst.containers, core_pinned_list_list)]
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
                            case "warming":  # warming containers all have the same "empty" id
                                self.cluster.func_2_warminginfo[func_id_str][invoker] = frozenset(containers)
                            case _:
                                assert False
            self._clear_stale_info(touched_func_invoker_set)  # clear stale entries
            self.cluster.update_invoker_state()
        # prepare to update the routing server, NOTE, actually you need a lock if update freq is high considering concurrent state update
        function_set = set().union(self.cluster.func_2_busyinfo.keys(), self.cluster.func_2_warminfo.keys(),
                                   self.cluster.func_2_warminfo.keys())

        func_2_ContainerCounter = routing_pb2.NotifyClusterInfoRequest()
        for func_id_str in function_set:
            container_counter = routing_pb2.ContainerCounter()
            for invk_id, invoker in self.cluster.id_2_invoker.items():
                total = len(self.cluster.func_2_busyinfo[func_id_str][invoker]) + len(
                    self.cluster.func_2_warminfo[func_id_str][invoker]) + len(
                    self.cluster.func_2_warminginfo[func_id_str][
                        invoker])  # it's okay the set of warming container all have empty id
                if total > 0:  # zero container, skip
                    container_counter.count.append(total)
                    container_counter.invokerId.append(invk_id)
            try:
                # https://stackoverflow.com/questions/52583468/protobuf3-python-how-to-set-element-to-mapstring-othermessage-dict
                func_2_ContainerCounter.func_2_ContainerCounter[func_id_str].CopyFrom(
                    container_counter)  # bugfix here, any other similar
            except ValueError as e:
                logging.error(f"Exception {e}")
        self.cluster.last_cluster_staste_update_time = time.time()
        resp = self.cluster.routing_stub.NotifyClusterInfo(func_2_ContainerCounter)
        logging.info(f"NotifyClusterInfo to routing process response:{resp.result_code}")
        self.cluster.first_update_arrival_event.set()  # mark there is at least one update
        return UpdateClusterStateResponse()

    # NOTE, cold start is realized by routing to a specific invoker, which might not lead to a Real cold-start
    def GetRoutingColdStart(self, request: GetRoutingColdStartRequest, context):
        # get all invokers whose memory is enough and the type match default type, if exist find a proper one
        # get all invoker whose memory is enough, if exist find a proper one
        # No invoker has enough memory
        logging.info("Get routing cold start RPC request")
        func_str = request.func_str
        self.cluster.stats.increase_cold_start_count(func_str)
        try:
            mem_req = self.cluster.strId_2_funcs[func_str].mem_req  # in MB
        except KeyError:  # the function is not registered, should be a invokerHealthTestAction0
            logging.warning(f"The function is not registered, {func_str}")
            return GetRoutingColdStartResponse(invoker_selected=0)  # TODO, handle system action function
        with self.cluster.cluster_state_lock:
            invoker_meet_mem = [invoker for invoker in self.cluster.id_2_invoker.values() if invoker.free_mem > mem_req]
            if invoker_meet_mem:
                invoker_of_default_type = [invoker for invoker in invoker_meet_mem if
                                           invoker.type == self.cluster.DEFAULT_SERVER_TYPE]
                if invoker_of_default_type:
                    res_invoker = self.cluster.find_proper_invoker_to_place_container(invoker_of_default_type).id
                else:
                    res_invoker = self.cluster.find_proper_invoker_to_place_container(invoker_meet_mem).id
                logging.info(f"Decided to cold start on invoker {res_invoker}")
            else:
                # TODO, how to handle, how the invoker handle cold start when there is not enough resource
                res_invoker = 0
                logging.info(f"Not enough resource, decided to cold start on invoker {res_invoker}")
        return GetRoutingColdStartResponse(invoker_selected=res_invoker)
