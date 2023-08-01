import logging
from typing import Dict, Tuple, List, Deque, NamedTuple, Union, Optional, Set, Final
from operator import itemgetter
import grpc
import numpy as np
from concurrent import futures
from collections import deque
from itertools import chain
from bisect import bisect
from invoker_client import invoker_pb2 as invoker_types
from invoker_client import invoker_pb2_grpc as invoker_service
from controller_server.server import ControllerService
from controller_server import controller_pb2 as controller_types
from  controller_server import controller_pb2_grpc as controller_service

from data_structure import LatencyInfo, RoutingResult, Action, ActionRealizeCounter
from reward import compute_reward_using_overall_stat

import training_configs  # training config
import config  # cluster environment config


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
        self.id = id
        self.hostname = host
        self.type = type
        self.mem_capacity = mem_capacity
        self.free_mem = mem_capacity
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


# Monitor collects aggregated info for agent to make decision
class Monitor:
    def __init__(self):
        self.func_2_arrival_queue: Dict[int, Deque] = {}
        self.func_2_slaLaency_for_reward: Dict[int, LatencyInfo] = {}
        self.func_2_nColdStart = {}


class Cluster:
    SERVER_RPC_THREAD_COUNT = 4
    RPC_SERVER_PORT = "" # RPC server port
    NUM_ACTIVE_FUNC: int = config.input_space_spec['n_func']
    ACTION_MAPPING_BOUNDARY: int = training_configs.action_mapping_boundary
    TYPE_MAPPING_BOUNDARY: List[int] = training_configs.type_mapping_boundary
    SERVER_TYPE_LIST: List[str] = list(config.cluster_spec_dict.keys())
    CONSIDER_CONTAINER_PINN_PER_CORE_LIMIT = bool(training_configs.params['consider_container_pinning_per_core_limit'])
    MOST_RECENT_KILLED_CONTAINER_SET_LIMIT = 10
    SERVER_POWER_SPECS = config.server_power_specs
    RATIO_BASED_LATENCY_FACTOR = training_configs.reward_setting['latency_factor']

    def __init__(self, cluster_spec_dict, func_spec_dict: Dict[str, Dict], nn_func_input_count=2) -> None:

        self.func_id_counter = 0
        self.id_2_funcs: Dict[int, Func] = {}  # funcid: func_name/action
        self.id_2_invoker: Dict[int, Invoker] = {}
        self.type_2_invoker: Dict[str, Set[Invoker]] = {}
        self.id_2_container: Dict[str, Container] = {}

        self.func_2_warminfo: Dict[int, Dict[Invoker, Set[str]]] = {}  # {func_id: {invoker: SetOfContainer}}
        self.func_2_busyinfo: Dict[int, Dict[Invoker, Set[str]]] = {}
        self.func_2_warminginfo: Dict[int, Dict[Invoker, Set[str]]] = {}

        self.func_2_warminfoSorted:Dict[int, List[Tuple[Invoker,int]]] = {}  # {func_id: [....(invoker, warmNum)...descending order...]}, updated on each heartbeat
        self.func_2_busyinfoSorted:Dict[int, List[Tuple[Invoker,int]]] = {}
        self.func_2_warminginfoSorted:Dict[int, List[Tuple[Invoker,int]]] = {}

        # FIFO
        self.most_recent_killed_container_cache: Deque[str] = deque(maxlen=self.MOST_RECENT_KILLED_CONTAINER_SET_LIMIT)

        self.cluster_spec_dict = cluster_spec_dict
        self.server_type_lst = list(cluster_spec_dict.keys())
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
        self.rpc_server = self._set_up_rpc_server()
    def _set_up_rpc_server(self):
        rpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=self.SERVER_RPC_THREAD_COUNT))
        controller_service.add_ControllerServiceServicer_to_server(ControllerService(), rpc_server)
        rpc_server.add_insecure_port( f"0.0.0.0:{self.RPC_SERVER_PORT}")
        rpc_server.start() # non blocking
        return rpc_server

    def _assertion(self):
        assert len(self.TYPE_MAPPING_BOUNDARY) + 1 == len(self.SERVER_TYPE_LIST)

    # find a proper invoker and try to trigger a cold start
    def _find_proper_invoker_cold_start(self, func_id) -> Optional[int]:
        pass

    # NOTE,What is different from the simulator: the routing heuristic might not always have the most up-to-date cluster view
    def route_invocation(self, func_id) -> Union[Tuple[Invoker,int], RoutingResult]:
        warminfo_sorted = self.func_2_warminfoSorted[func_id]
        if warminfo_sorted:
            return warminfo_sorted[0]  # invoker with the maximum of number of warm container for the function
        busyinfo_sorted = self.func_2_busyinfoSorted[func_id]
        if busyinfo_sorted:
            return busyinfo_sorted[0]
        warminginfo_sorted = self.func_2_warminginfoSorted[func_id]
        if warminginfo_sorted:
            return warminginfo_sorted[0]
        # no warm, busy, warming, just create one (cold start)
        find_res = self._find_proper_invoker_cold_start(func_id)
        if find_res is None:
            return RoutingResult.DISCARD

    def _reset_openwhisk_cluster(self):
        # @TODO
        # reset the openwhisk cluster to an initial state
        pass

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

    def delete_container_multiple_pinning(self, func_id: int, type: str) -> Optional[str]:
        lst_warmset: List[Set[str]] = [st for invk, st in self.func_2_warminfo[func_id].items() if invk.type == type]
        lst_busyset = [st for invk, st in self.func_2_busyinfo[func_id].items() if invk.type == type]
        lst_warmingset = [st for invk, st in self.func_2_warminginfo[func_id].items() if invk.type == type]
        lst_warm: List[str] = [container for s in lst_warmset for container in s]  # flatten the list
        lst_busyset = [container for s in lst_busyset for container in s]
        lst_warmingset = [container for s in lst_warmingset for container in s]
        candidate_lst: List[str] = list(
            chain.from_iterable([lst_warm, lst_warmingset, lst_busyset]))  # flatten list of list
        if candidate_lst:
            self.actionRealizeCounter.delete_success += 1
            # find the container that has the maximum number of sibling containers that pinns to its first core.
            candidate_lst.sort(key=lambda x: self.id_2_container[x].pinned_core[0].num_pinned_container,
                               reverse=True)  # stable sort
            for cand in candidate_lst:
                if cand not in self.most_recent_killed_container_cache:
                    self.most_recent_killed_container_cache.append(cand)
                    host_invoker: Invoker = self.id_2_container[cand].invoker
                    host_invoker.rpc_delete_container(cand, self.id_2_funcs[func_id].name)
                    logging.info("Deleting container {} on invoker {}".format(cand, host_invoker.id))
                    return cand  # make sure only delete one
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
                self.add_container_with_multiple_pinning(action, self.id_2_funcs[funcid])
            elif action.container_delta < 0:
                self.delete_container_multiple_pinning(func_id=funcid, type=action.type)

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
        return state, reward_dict, False,False, self.state_info

    # get the features of All function so that to choose the active working functions.
    def select_from_all_state(self, time_window_size_millis: int, coeff: float, bucket_millis: int) -> None:
        pass

    def get_sla_latency_for_reward(self):
        pass

    def compute_utilization(self)->Dict:
        pass

    def get_obs(self):
        pass

    def register_func(self, namesp, name, mem_req, cpu_req, invoker_2_referenceExecTime):
        # TODO, register into the openwhisk system
        func_id = self.func_id_counter
        self.id_2_funcs[func_id] = Func(id=func_id, namesp=namesp, name=name, mem_req=mem_req,
                                        cpu_req=cpu_req,
                                        invoker_2_referenceExecTime=invoker_2_referenceExecTime)
        self.func_id_counter += 1  # increase the function id counter
        return func_id

    def delete_container(self, func_id):
        pass

    # ----------for collecting runtime info---------
    def get_avg_busy_container_utilization_per_type(self, func_ids: List):
        # get avg container utilization per type for each function
        pass
