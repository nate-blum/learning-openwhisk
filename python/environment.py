import logging
from typing import Dict, Tuple, List, Deque, NamedTuple, Union, Optional, Set, Final
from operator import itemgetter
import grpc
import numpy as np
from multiprocessing import Process, Queue
from concurrent import futures
from threading import Lock
from collections import deque
from itertools import chain
from bisect import bisect
from invoker_client import invoker_pb2 as invoker_types
from invoker_client import invoker_pb2_grpc as invoker_service
from controller_server import clusterstate_pb2, clusterstate_pb2_grpc, routing_pb2_grpc, routing_pb2
from data_structure import LatencyInfo, RoutingResult, Action, ActionRealizeCounter
from reward import compute_reward_using_overall_stat
from grpc_reflection.v1alpha import reflection

import training_configs  # training config
import config  # cluster environment config
from load_balance import start_rpc_routing_server_process
from state_collector import WskClusterInfoCollector


class Core:
    def __init__(self, id, max_freq, min_freq, desired_freq):
        self.id: int = id
        self.num_pinned_container: int = 0
        self.max_freq: int = max_freq
        self.min_freq: int = min_freq
        self.desired_freq: int = desired_freq


class Invoker:
    def __init__(self, id, host, type, mem_capacity, num_cores, core_spec_dict: Dict[int, Dict],
                 max_pinned_container_per_core: int) -> None:
        self.id: int = id
        self.hostname: str = host
        self.type: str = type
        self.mem_capacity: int = mem_capacity
        self.free_mem: int = mem_capacity
        self.num_cores = num_cores
        self.num_warm_container = 0
        self.num_busy_container = 0
        self.num_warming_container = 0
        self.last_utilization_record = 0  # most recent utilization record
        self.MAX_PINNED_CONTAINER_PER_CORE: Final[int] = max_pinned_container_per_core
        self.id_2_core: Dict[int, Core] = {}
        for id, spec in core_spec_dict.items():
            self.id_2_core[id] = Core(**spec)

        # -------------rpc channel-----------------
        self.channel = grpc.insecure_channel(self.hostname)
        self.stub = invoker_service.InvokerServiceStub(channel=self.channel)

    def rpc_add_container(self, action_name: str, pinned_core: List[int]) -> None:
        self.stub.NewWarmedContainer(invoker_types.NewWarmedContainerRequest(actionName=action_name, params={
            "--cpuset-cpus": "_".join([str(core) for core in pinned_core])
        }))

    def rpc_delete_container(self, container_id: str, func_name: str):
        # TODO, enable specifying the container id to delete
        self.stub.DeleteContainer(invoker_types.DeleteContainerRequest(actionName=func_name))

    def is_all_core_pinned_to_uplimit(self):
        res = True
        for core in self.id_2_core.values():
            if core.num_pinned_container < self.MAX_PINNED_CONTAINER_PER_CORE:
                res = False
                break
        return res

    def get_total_num_container(self):
        return self.num_warm_container + self.num_busy_container + self.num_warming_container

    def get_core_preference_list(self):
        core_idxs = list(range(self.num_cores))
        core_idxs.sort(key=lambda idx: self.id_2_core[idx].num_pinned_container)  # ascending order
        return core_idxs

    def reset_core_pinning_count(self):
        for core in self.id_2_core.values():
            core.num_pinned_container = 0


class Container:
    def __init__(self, str_id: str, pinned_core: List[Core], invoker: Invoker):
        self.id: str = str_id
        self.pinned_core: List[Core] = pinned_core
        self.invoker: Invoker = invoker


class Func:
    def __init__(self, id, namesp, name, mem_req, cpu_req, invoker_2_referenceExecTime):
        self.id = id
        self.namesp = namesp
        self.name = name
        self.mem_req = mem_req
        self.cpu_req = cpu_req
        self.invoker_2_referenceExecTime = invoker_2_referenceExecTime


# # Monitor collects aggregated info for agent to make decision
# class Monitor:
#     def __init__(self):
#         self.func_2_arrival_queue: Dict[int, Deque] = {}
#         self.func_2_slaLaency_for_reward: Dict[int, LatencyInfo] = {}
#         self.func_2_nColdStart = {}


class Cluster:
    SERVER_RPC_THREAD_COUNT = 4  # for routing
    SERVER_RPC_THREAD_COUNT_CLUSTER_UPDATE = 1  # 1 should be grood enough to handle, as only one rpc at a time
    RPC_SERVER_PORT = ""  # RPC server port, routing server
    RPC_SERVER_PORT_CLUSTER_UPDATE = ""  # RPC server port, cluster state update
    NUM_ACTIVE_FUNC: int = config.input_space_spec['n_func']
    ACTION_MAPPING_BOUNDARY: int = training_configs.action_mapping_boundary
    TYPE_MAPPING_BOUNDARY: List[int] = training_configs.type_mapping_boundary
    SERVER_TYPE_LIST: List[str] = list(config.cluster_spec_dict.keys())
    CONSIDER_CONTAINER_PINN_PER_CORE_LIMIT = bool(training_configs.params['consider_container_pinning_per_core_limit'])
    MOST_RECENT_KILLED_CONTAINER_SET_LIMIT = 10
    SERVER_POWER_SPECS = config.server_power_specs
    RATIO_BASED_LATENCY_FACTOR = training_configs.reward_setting['latency_factor']
    DEFAULT_SERVER_TYPE = config.default_svr_type

    def __init__(self, cluster_spec_dict, func_spec_dict: Dict[str, Dict], nn_func_input_count=2) -> None:

        self.func_id_counter = 0
        self.strId_2_funcs: Dict[str, Func] = {}  # funcid_str: func_name/action
        self.intId_2_funcStrName: Dict[int, str] = {}

        self.id_2_invoker: Dict[int, Invoker] = {}
        self.type_2_invoker: Dict[str, Set[Invoker]] = {}
        # self.id_2_container: Dict[str, Container] = {}

        self.cluster_state_lock = Lock()
        self.func_2_warminfo: Dict[
            str, Dict[Invoker, frozenset[Container]]] = {}  # {func_id: {invoker: SetOfContainer}}
        self.func_2_busyinfo: Dict[str, Dict[Invoker, frozenset[Container]]] = {}
        self.func_2_warminginfo: Dict[str, Dict[Invoker, frozenset[Container]]] = {}

        # self.func_2_warminfoSorted: Dict[int, List[Tuple[
        #     Invoker, int]]] = {}  # {func_id: [....(invoker, warmNum)...descending order...]}, updated on each heartbeat
        # self.func_2_busyinfoSorted: Dict[int, List[Tuple[Invoker, int]]] = {}
        # self.func_2_warminginfoSorted: Dict[int, List[Tuple[Invoker, int]]] = {}

        # FIFO, must use dict as container might have the same id on different invoker (docker runtime)
        self.most_recent_killed_container_cache: dict[int, Deque[str]] = None

        self.cluster_spec_dict = cluster_spec_dict
        self.server_type_lst = list(cluster_spec_dict.keys())
        self.server_types = []
        self.func_spec_dict = func_spec_dict
        self.all_func_ids = []
        self.id_2_funcname = {}
        self.funcname_2_id = {}
        self.actionRealizeCounter = ActionRealizeCounter()
        for name, spec in self.func_spec_dict.items():
            func_id = self.register_func(**spec)
            self.all_func_ids.append(func_id)
            self.id_2_funcname[func_id] = name
            self.funcname_2_id[name] = func_id
        self.active_func_ids = self.all_func_ids[:nn_func_input_count]
        self.state_info = None
        self.process_q = Queue()
        # -------------------Start Load Balancer process and the rpc server-----------------------------
        self.load_balancer_process = Process(target=start_rpc_routing_server_process,
                                             args=(self.process_q, self.RPC_SERVER_PORT, self.SERVER_RPC_THREAD_COUNT,
                                                   self.DEFAULT_SERVER_TYPE))
        self.load_balancer_process.start()
        # ---------------- Set up rpc client for query arrival info(should before cluster update rpc server, b/c the cluster
        # state update server might use the stub for sending rpc request)-----------------------------------
        self.routing_channel = grpc.insecure_channel(f'localhost:{self.RPC_SERVER_PORT}')
        self.routing_stub = routing_pb2_grpc.RoutingServiceStub(self.routing_channel)  # channel is thread safe
        # -------------------Start Cluster Update RPC server--------------------------------------------
        self.cluster_info_update_server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=self.SERVER_RPC_THREAD_COUNT_CLUSTER_UPDATE))
        clusterstate_pb2_grpc.add_ClusterStateServiceServicer_to_server(WskClusterInfoCollector(self),
                                                                        self.cluster_info_update_server)
        reflection.enable_server_reflection(
            clusterstate_pb2.DESCRIPTOR.services_by_name["WskClusterInfoCollector"].full_name,
            reflection.SERVICE_NAME, self.cluster_info_update_server)
        self.cluster_info_update_server.add_insecure_port(f"[::]:{self.RPC_SERVER_PORT_CLUSTER_UPDATE}")
        self.cluster_info_update_server.start()
        self._initialization()

    def _assertion(self):
        assert len(self.TYPE_MAPPING_BOUNDARY) + 1 == len(self.SERVER_TYPE_LIST)

    def _initialization(self):
        # instantiate Invoker instances list, the invoker instance will initialize the Core object
        invoker_id_counter = 0
        for _type, spec_map_lst in self.cluster_spec_dict:
            self.server_types.append(_type)
            for spec in spec_map_lst:
                self.id_2_invoker[invoker_id_counter] = Invoker(invoker_id_counter, spec['host'], _type,
                                                                spec['mem_capacity'], spec['num_cores'],
                                                                {i: {'id': i, 'max_freq': spec['max_freq'],
                                                                     'min_freq': spec['min_freq'],
                                                                     'desired_freq': spec['desired_freq']} for i in
                                                                 range(spec['num_cores'])},
                                                                spec['max_pinned_container_per_core']
                                                                )
                if _type not in self.type_2_invoker:
                    self.type_2_invoker[_type] = set()
                self.type_2_invoker[_type].add(self.id_2_invoker[invoker_id_counter])
                invoker_id_counter += 1
        self.most_recent_killed_container_cache = {invoker_id: deque(maxlen=self.MOST_RECENT_KILLED_CONTAINER_SET_LIMIT)
                                                   for invoker_id in self.id_2_invoker.keys()}

    def _update_invoker_state(self):
        # update the invoker state after controller rpc update
        for invoker in self.id_2_invoker.values():
            warm_sum:int = 0
            busy_sum:int = 0
            warming_sum:int = 0
            # loop all functions
            for invk_2_container_set in self.func_2_busyinfo.values():
                busy_sum += len(invk_2_container_set[invoker])
            for invk_2_container_set in self.func_2_warminfo.values():
                warm_sum += len(invk_2_container_set[invoker])
            for invk_2_container_set in self.func_2_warminginfo.values():
                warming_sum += len(invk_2_container_set[invoker])
            invoker.num_busy_container =  busy_sum
            invoker.num_warm_container = warm_sum
            invoker.num_warming_container = warming_sum

    # def _reset_core_pinning(self):
    #     for invoker in self.id_2_invoker.values():
    #         for core in invoker.id_2_core.values():
    #             core.num_pinned_container = 0



    def reset(self, seed=None, options=None):
        pass

    def _map_action(self, simple_action: np.ndarray) -> Dict[int, Action]:
        # the output is sample from Normal distribution, they should be mapped as real control operation
        # adding default sub-action to the simple action
        action_cpp = {}
        for idx in range(self.NUM_ACTIVE_FUNC):
            # mapped_action = [None, 3000, None, 1.0]  # <delta, freq, type, target_load>
            slice_ = slice(idx * 2, (idx + 1) * 2)
            actions = simple_action[slice_]
            # delta dimension
            if actions[0] < -self.ACTION_MAPPING_BOUNDARY:
                delta = -1
            elif -self.ACTION_MAPPING_BOUNDARY <= actions[0] < self.ACTION_MAPPING_BOUNDARY:
                delta = 0
            else:
                delta = 1
            # server type dimension, return an index by bineary search
            type_ = self.SERVER_TYPE_LIST[bisect(self.TYPE_MAPPING_BOUNDARY, actions[1])]
            action_cpp[self.active_func_ids[idx]] = Action(container_delta=delta, type=type_)
        # print(action_cpp)
        return action_cpp

    def _find_proper_invoker_to_place_container(self, candidate_set: Set[Invoker]) -> Invoker:
        # prefer to put a container to the invoker with the least number of normalized container
        # the caller must make sure the candidate_set is not empty
        candidate_lst = list(candidate_set)
        score_lst = [0] * len(candidate_set)
        for i, invoker in enumerate(candidate_lst):
            score_lst[i] = invoker.get_total_num_container() / invoker.num_cores
        index_min = min(range(len(score_lst)), key=score_lst.__getitem__)
        return candidate_lst[index_min]

    def delete_container_multiple_pinning(self, func_id_str: str, type: str) -> Optional[str]:
        lst_warmset: List[frozenset[Container]] = [st for invk, st in self.func_2_warminfo[func_id_str].items() if
                                                   invk.type == type]
        lst_busyset = [st for invk, st in self.func_2_busyinfo[func_id_str].items() if invk.type == type]
        lst_warmingset = [st for invk, st in self.func_2_warminginfo[func_id_str].items() if invk.type == type]
        lst_warm: List[Container] = [container for s in lst_warmset for container in s]  # flatten the list
        lst_busyset = [container for s in lst_busyset for container in s]
        lst_warmingset = [container for s in lst_warmingset for container in s]
        candidate_lst: List[Container] = list(
            chain.from_iterable([lst_warm, lst_warmingset, lst_busyset]))  # flatten list of list
        if candidate_lst:
            self.actionRealizeCounter.delete_success += 1
            # find the container that has the maximum number of sibling containers that pinns to its first core.
            candidate_lst.sort(key=lambda x: x.pinned_core[0].num_pinned_container,
                               reverse=True)  # stable sort
            for cand in candidate_lst:
                invoker_id = cand.invoker.id
                if cand not in self.most_recent_killed_container_cache[invoker_id]:
                    # NOTE, (1) there is a small chance that the container id is reuse (2) here we must use container
                    # str id instead of container object since the different object might represent the same physical container in this implementation
                    self.most_recent_killed_container_cache[invoker_id].append(cand.id)
                    host_invoker: Invoker = cand.invoker
                    host_invoker.rpc_delete_container(cand.id, self.strId_2_funcs[func_id_str].name)
                    logging.info("Deleting container {} on invoker {}".format(cand, host_invoker.id))
                    return cand.id  # make sure only delete one
        return None  #

    def add_container_with_multiple_pinning(self, action: Action, func: Func):
        # TODO, if no relex of action, then no need to scan the invoker set each time
        mem_req = func.mem_req
        target_load = action.target_load
        candidates_pass_type: Set[Invoker] = self.type_2_invoker[action.type]
        candidates_pass_mem: Set[Invoker] = {invoker for invoker in self.id_2_invoker.values() if
                                             invoker.free_mem >= mem_req and not (
                                                     self.CONSIDER_CONTAINER_PINN_PER_CORE_LIMIT and invoker.is_all_core_pinned_to_uplimit())}
        candidates_pass_load: Set[Invoker] = {invoker for invoker in self.id_2_invoker.values() if
                                              invoker.last_utilization_record <= target_load}
        # meet all
        meet_all = candidates_pass_mem.intersection(candidates_pass_type, candidates_pass_load)
        select_invoker = None
        if meet_all:  # different from CPP, no relex here, check cpp code for detail
            select_invoker = self._find_proper_invoker_to_place_container(meet_all)
        if select_invoker:
            self.actionRealizeCounter.add_success += 1
            core_preference_lst = select_invoker.get_core_preference_list()
            select_invoker.rpc_add_container(action_name=func.name, pinned_core=core_preference_lst[0:func.cpu_req])
            logging.info("Successfully create container for function {}", func.name)
        else:
            self.actionRealizeCounter.add_fail += 1
            logging.info("Fail to realize action of adding container for function {}", func.name)

    # realize the action in the openwhisk system
    def take_action(self, mapped_action: Dict[int, Action]):
        # mapped_action: {id, Action}
        for funcid, action in mapped_action.items():
            if action.container_delta > 0:  # add container
                self.add_container_with_multiple_pinning(action, self.strId_2_funcs[self.intId_2_funcStrName[funcid]])
            elif action.container_delta < 0:
                self.delete_container_multiple_pinning(func_id_str=self.intId_2_funcStrName[funcid], type=action.type)

    def step(self, action: np.ndarray):
        mapped_action: Dict[int, Action] = self._map_action(action)
        self.take_action(mapped_action)
        sla_latency = self.get_sla_latency_for_reward()  # contain all function, not jut active function
        type_2_utilizations = self.compute_utilization()
        reward_dict = compute_reward_using_overall_stat(type_2_utilizations=type_2_utilizations,
                                                        func_2_latencies=sla_latency,
                                                        server_power_specs=self.SERVER_POWER_SPECS,
                                                        latency_factor=self.RATIO_BASED_LATENCY_FACTOR)
        state = self.get_obs()
        return state, reward_dict, False, False, self.state_info

    # get the features of All function so that to choose the active working functions.
    def select_from_all_state(self, time_window_size_millis: int, coeff: float, bucket_millis: int) -> None:
        pass

    def get_sla_latency_for_reward(self):
        pass

    def compute_utilization(self) -> Dict:
        pass

    def _rpc_get_arrival_info(self) -> tuple[dict[int, int], dict[int, int]]:
        # get the arrival info from a rpc call to the routing rpc server
        response: routing_pb2.GetArrivalInfoResponse = self.routing_stub.GetArrivalInfo(routing_pb2.EmptyRequest)
        return response.query_count_1s, response.query_count_3s

    def get_obs(self):
        pass

    def register_func(self, namesp, name, mem_req, cpu_req, invoker_2_referenceExecTime):
        # TODO, register into the openwhisk system, via openwhisk CLI
        func_id = self.func_id_counter
        self.strId_2_funcs[name] = Func(id=func_id, namesp=namesp, name=name, mem_req=mem_req,
                                        cpu_req=cpu_req,
                                        invoker_2_referenceExecTime=invoker_2_referenceExecTime)
        self.intId_2_funcStrName[func_id] = name
        self.func_2_warminfo[name] = {self.id_2_invoker[i]: set() for i in self.id_2_invoker.keys()}
        self.func_2_busyinfo[name] = {self.id_2_invoker[i]: set() for i in self.id_2_invoker.keys()}
        self.func_2_warminginfo[name] = {self.id_2_invoker[i]: set() for i in self.id_2_invoker.keys()}
        self.func_id_counter += 1  # increase the function id counter
        return func_id

    # ----------for collecting runtime info---------
    def get_avg_busy_container_utilization_per_type(self, func_ids: List):
        # get avg container utilization per type for each function
        pass


if __name__ == "__main__":
    pass
