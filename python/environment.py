from typing import Dict, List, Deque, NamedTuple, Union, Optional, Set, Final
from operator import itemgetter
import grpc
import numpy as np
from bisect import bisect
from invoker_client import invoker_pb2 as invoker_types
from invoker_client import invoker_pb2_grpc as invoker_service

from data_structure import LatencyInfo, RoutingResult, EnvConfigs, Action

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
        self.last_utilization_record = 0  # most recent utilization record
        self.MAX_PINNED_CONTAINER_PER_CORE: Final[int] = max_pinned_container_per_core
        self.id_2_core: Dict[int, Core] = {}
        for id, spec in core_spec_dict.items():
            self.id_2_core[id] = Core(**spec)

        # -------------rpc channel-----------------
        self.channel = grpc.insecure_channel(self.hostname)
        self.stub = invoker_service.InvokerServiceStub(channel=self.channel)

    def is_all_core_pinned_to_uplimit(self):
        res = True
        for core in self.id_2_core.values():
            if core.num_pinned_container < self.MAX_PINNED_CONTAINER_PER_CORE:
                res = False
                break
        return res


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


class Loadbalancer:
    def __init__(self) -> None:
        pass

    def route_invocation(func_id) -> int:
        raise NotImplementedError


class Cluster:
    NUM_ACTIVE_FUNC: int = config.input_space_spec['n_func']
    ACTION_MAPPING_BOUNDARY: int = training_configs.action_mapping_boundary
    TYPE_MAPPING_BOUNDARY: List[int] = training_configs.type_mapping_boundary
    SERVER_TYPE_LIST: List[str] = list(config.cluster_spec_dict.keys())
    CONSIDER_CONTAINER_PINN_PER_CORE_LIMIT = bool(training_configs.params['consider_container_pinning_per_core_limit'])

    def __init__(self, cluster_spec_dict, func_spec_dict: Dict[str, Dict], nn_func_input_count=2) -> None:

        self.func_id_counter = 0
        self.id_2_funcs: Dict[int, Func] = {}  # funcid: func_name/action
        self.id_2_invoker: Dict[int, Invoker] = {}
        self.type_2_invoker: Dict[str, Set[Invoker]] = {}
        self.func_2_warminfo = {}  # {func_id: {invoker_id: containerCount}}
        self.func_2_busyinfo = {}
        self.func_2_warminginfo = {}

        self.func_2_warminfoSorted = {}  # {func_id: [....(invoker, warmNum)...descending order...]}, updated on each heartbeat
        self.func_2_busyinfoSorted = {}
        self.func_2_warminginfoSorted = {}

        self.cluster_spec_dict = cluster_spec_dict
        self.server_type_lst = list(cluster_spec_dict.keys())
        self.func_spec_dict = func_spec_dict
        self.all_func_ids = []
        self.id_2_funcname = {}
        self.funcname_2_id = {}
        for name, spec in self.func_spec_dict.items():
            func_id = self.register_func(**spec)
            self.all_func_ids.append(func_id)
            self.id_2_funcname[func_id] = name
            self.funcname_2_id[name] = func_id
        self.active_func_ids = self.all_func_ids[:nn_func_input_count]

    def _assertion(self):
        assert len(self.TYPE_MAPPING_BOUNDARY) + 1 == len(self.SERVER_TYPE_LIST)

    # find a proper invoker and try to trigger a cold start
    def _find_proper_invoker_cold_start(self, func_id) -> Optional[int]:
        pass

    # NOTE,What is different from the simulator: the routing heuristic might not always have the most up-to-date cluster view
    def route_invocation(self, func_id) -> Union[int, RoutingResult]:
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

    # heuristic for placement of container
    def find_proper_invoker_to_place_container(self, action: Action, func: Func):
        candidates_pass_type: Set[Invoker] = self.type_2_invoker[action.type]
        candidates_pass_mem: Set[Invoker] = {invoker for invoker in self.id_2_invoker.values() if
                                             invoker.free_mem >= func.mem_req and not (
                                                         self.CONSIDER_CONTAINER_PINN_PER_CORE_LIMIT and invoker.is_all_core_pinned_to_uplimit())}
        candidates_pass_load: Set[Invoker] = {invoker for invoker in self.id_2_invoker.values() if
                                              invoker.last_utilization_record <= action.target_load}

    # realize the action in the openwhisk system
    def take_action(self, mapped_action: Dict[int, Action]):
        for funcid, action in mapped_action.items():
            pass

    def step(self, action: np.ndarray):
        mapped_action: Dict[int, Action] = self._map_action(action)

    # get the features of All function so that to choose the active working functions.
    def select_from_all_state(self, time_window_size_millis: int, coeff: float, bucket_millis: int) -> None:
        pass

    def get_obs(self):
        pass

    def register_func(self, namesp, name, mem_req, cpu_req, invoker_2_referenceExecTime):
        func_id = self.func_id_counter
        self.id_2_funcs[func_id] = Func(id=func_id, namesp=namesp, name=name, mem_req=mem_req,
                                        cpu_req=cpu_req,
                                        invoker_2_referenceExecTime=invoker_2_referenceExecTime)
        self.func_id_counter += 1  # increase the function id counter
        return func_id

    def add_container(self, func_id):
        pass

    def delete_container(self, func_id):
        pass

    # ----------for collecting runtime info---------
    def get_avg_busy_container_utilization_per_type(self, func_ids: List):
        # get avg container utilization per type for each function
        pass
