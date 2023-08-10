import sys
import logging
import time
from typing import Dict, Tuple, List, Deque, NamedTuple, Union, Optional, Set, Final
from collections.abc import Iterable
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
from invoker_client.invoker_pb2 import DeleteContainerWithIdRequest, SuccessResponse
from invoker_client import invoker_pb2_grpc as invoker_service
from controller_server import clusterstate_pb2, clusterstate_pb2_grpc, routing_pb2_grpc, routing_pb2
from data_structure import LatencyInfo, RoutingResult, Action, ActionRealizeCounter
from reward import compute_reward_using_overall_stat
from grpc_reflection.v1alpha import reflection
from google.protobuf.internal.containers import ScalarMap

import training_configs  # training config
import config  # cluster environment config
from load_balance import start_rpc_routing_server_process
from state_collector import WskClusterInfoCollector
from power import PDU_reader
from workload_generator import start_workload_process

signal_queue = Queue()  # sending signal to control workload generator's reset/start
SLOT_DURATION = training_configs.SLOT_DURATION_SECOND
SERVER_RPC_THREAD_COUNT = 8  # for routing
SERVER_RPC_THREAD_COUNT_CLUSTER_UPDATE = 2  # 1 should be grood enough to handle, as only one rpc at a time
RPC_ROUTING_SERVER_PORT = "50051"  # RPC server port, routing server
RPC_SERVER_PORT_CLUSTER_UPDATE = "50052"  # RPC server port, cluster state update
NUM_ACTIVE_FUNC: int = config.input_space_spec['n_func']
ACTION_MAPPING_BOUNDARY: int = training_configs.action_mapping_boundary
TYPE_MAPPING_BOUNDARY: List[int] = training_configs.type_mapping_boundary
SERVER_TYPE_LIST: List[str] = list(config.cluster_spec_dict.keys())
CONSIDER_CONTAINER_PINN_PER_CORE_LIMIT = bool(training_configs.params['consider_container_pinning_per_core_limit'])
MOST_RECENT_KILLED_CONTAINER_SET_LIMIT = 10
SERVER_POWER_SPECS = config.server_power_specs
RATIO_BASED_LATENCY_FACTOR = training_configs.reward_setting['latency_factor']
DEFAULT_SERVER_TYPE = config.default_svr_type
ARRIVAL_Q_TIMER_INTERVAL_SEC = 0.2
ARRIVAL_Q_TIME_RANGE_LIMIT = int(120e9)  # 120second, 2 minute, in nanosecond
EMA_TIME_WINDOW_NSEC = 60_000_000_000  # 1min in nanosecond
ARRIVAL_EMA_BUCKET_NSEC = 2_000_000_000  # 2 second in nanosecond
ARRIVAL_EMA_COEFF = 0.4
do_state_clip: bool = training_configs.NN['state_clip']
state_clip_value = 2000
PDU_HOST = 'panic-pdu-01.cs.rutgers.edu'
PDU_OUTLET_LST = [21, 22]
PDU_SAMPLE_INTERVAL = 0.4
WORKLOAD_TRACE_FILE = training_configs.workload_config['trace_file']
WORKLOAD_START_POINTER = training_configs.workload_config['workload_line_start']
WSK_PATH = "/local/kuozhang-local/nsf-backup/openwhisk/bin/wsk"


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
        # TODO, how the Success Response is determined from Scala runtime
        response: SuccessResponse = self.stub.DeleteContainerWithId(
            DeleteContainerWithIdRequest(containerId=container_id))

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

    def get_total_core_pinning_count(self):
        # NOTE, this is different than the total number of container on this invoker,
        #  as a container can take up more than one core. This measure how busy the core is on average
        total = 0
        for core in self.id_2_core.values():
            total += core.num_pinned_container
        return total


class Container:
    def __init__(self, str_id: str, pinned_core: List[Core], invoker: Invoker):
        self.id: str = str_id
        self.pinned_core: List[Core] = pinned_core
        self.invoker: Invoker = invoker


class Func:
    def __init__(self, id, namesp, name, mem_req, cpu_req, sla, invoker_2_referenceExecTime):
        self.id = id
        self.namesp = namesp
        self.name = name
        self.mem_req = mem_req
        self.cpu_req = cpu_req
        self.sla = sla
        self.invokerType_2_referenceExecTime: dict[str, int] = invoker_2_referenceExecTime


class Cluster:
    SLOT_DURATION = training_configs.SLOT_DURATION_SECOND
    SERVER_RPC_THREAD_COUNT = 8  # for routing
    SERVER_RPC_THREAD_COUNT_CLUSTER_UPDATE = 2  # 1 should be grood enough to handle, as only one rpc at a time
    NUM_ACTIVE_FUNC: int = config.input_space_spec['n_func']
    ACTION_MAPPING_BOUNDARY: int = training_configs.action_mapping_boundary
    TYPE_MAPPING_BOUNDARY: List[int] = training_configs.type_mapping_boundary
    SERVER_TYPE_LIST: List[str] = list(config.cluster_spec_dict.keys())
    CONSIDER_CONTAINER_PINN_PER_CORE_LIMIT = bool(training_configs.params['consider_container_pinning_per_core_limit'])
    MOST_RECENT_KILLED_CONTAINER_SET_LIMIT = 10
    SERVER_POWER_SPECS = config.server_power_specs
    RATIO_BASED_LATENCY_FACTOR = training_configs.reward_setting['latency_factor']
    DEFAULT_SERVER_TYPE = config.default_svr_type
    ARRIVAL_Q_TIMER_INTERVAL_SEC = 0.2
    ARRIVAL_Q_TIME_RANGE_LIMIT = int(120e9)  # 120second, 2 minute, in nanosecond
    EMA_TIME_WINDOW_NSEC = 60_000_000_000  # 1min in nanosecond
    ARRIVAL_EMA_BUCKET_NSEC = 2_000_000_000  # 2 second in nanosecond
    ARRIVAL_EMA_COEFF = 0.4
    do_state_clip: bool = training_configs.NN['state_clip']
    state_clip_value = 2000
    PDU_HOST = 'panic-pdu-01.cs.rutgers.edu'
    PDU_OUTLET_LST = [21, 22]
    PDU_SAMPLE_INTERVAL = 0.4
    WORKLOAD_TRACE_FILE = training_configs.workload_config['trace_file']
    WORKLOAD_START_POINTER = training_configs.workload_config['workload_line_start']

    def __init__(self, cluster_spec_dict, func_spec_dict: Dict[str, Dict], nn_func_input_count=2) -> None:
        self.setup_logging()
        self.func_id_counter = 0
        self.strId_2_funcs: Dict[str, Func] = {}  # funcid_str: func_name/action
        self.intId_2_funcStrName: Dict[int, str] = {}
        self.funcname_2_id = {}

        self.id_2_invoker: Dict[int, Invoker] = {}
        self.type_2_invoker: Dict[str, Set[Invoker]] = {}

        self.cluster_state_lock = Lock()
        self.func_2_warminfo: Dict[
            str, Dict[Invoker, frozenset[Container]]] = {}  # {func_id: {invoker: SetOfContainer}}
        self.func_2_busyinfo: Dict[str, Dict[Invoker, frozenset[Container]]] = {}
        self.func_2_warminginfo: Dict[str, Dict[Invoker, frozenset[Container]]] = {}

        # FIFO, must use dict as container might have the same id on different invoker (docker runtime)
        self.most_recent_killed_container_cache: dict[int, Deque[str]] = None

        self.cluster_spec_dict = cluster_spec_dict
        self.server_type_lst = list(cluster_spec_dict.keys())
        self.server_types = []
        self.serverType_2_index: dict[str, int] = {}
        self.func_spec_dict = func_spec_dict
        self.all_func_ids = []
        self.actionRealizeCounter = ActionRealizeCounter()
        for name, spec in self.func_spec_dict.items():
            func_id = self.register_func(**spec)
            self.all_func_ids.append(func_id)
        self.active_func_ids = self.all_func_ids[:nn_func_input_count]
        self.state_info = None
        self.cluster_peak_pw = None

        self._initialization()  # must start before the rpc server b/c rpc server use the invoker instances
        # TODO, what if the server is not up, but the rpc request has been sent ?
        # -------------------Start Load Balancer process and the rpc server-----------------------------
        self.load_balancer_process = Process(target=start_rpc_routing_server_process,
                                             args=(RPC_ROUTING_SERVER_PORT, SERVER_RPC_THREAD_COUNT,
                                                   DEFAULT_SERVER_TYPE, ARRIVAL_Q_TIMER_INTERVAL_SEC,
                                                   ARRIVAL_Q_TIME_RANGE_LIMIT, EMA_TIME_WINDOW_NSEC,
                                                   ARRIVAL_EMA_BUCKET_NSEC, ARRIVAL_EMA_COEFF,
                                                   RPC_SERVER_PORT_CLUSTER_UPDATE), daemon=True)  # avoid self usage
        self.load_balancer_process.start()
        # --------------------------------Start Worklaod Generation Process-----------------------------------
        self.workload_generate_process = Process(target=start_workload_process,
                                                 args=(signal_queue, WORKLOAD_START_POINTER, WORKLOAD_TRACE_FILE,
                                                       WSK_PATH))
        # ---------------- Set up rpc client for query arrival info(should before cluster update rpc server, b/c the cluster
        # state update server might use the stub for sending rpc request)-----------------------------------
        self.routing_channel = grpc.insecure_channel(f'localhost:{RPC_ROUTING_SERVER_PORT}')
        self.routing_stub = routing_pb2_grpc.RoutingServiceStub(self.routing_channel)  # channel is thread safe
        # -------------------Start Cluster Update RPC server--------------------------------------------
        self.cluster_info_update_server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=self.SERVER_RPC_THREAD_COUNT_CLUSTER_UPDATE))
        clusterstate_pb2_grpc.add_ClusterStateServiceServicer_to_server(WskClusterInfoCollector(self),
                                                                        self.cluster_info_update_server)
        reflection.enable_server_reflection(
            clusterstate_pb2.DESCRIPTOR.services_by_name["WskClusterInfoCollector"].full_name,
            reflection.SERVICE_NAME, self.cluster_info_update_server)
        self.cluster_info_update_server.add_insecure_port(f"[::]:{RPC_SERVER_PORT_CLUSTER_UPDATE}")
        self.cluster_info_update_server.start()
        # ----------------------------PUD thread--------------------------------------------
        self.pdu = PDU_reader(self.PDU_HOST, self.PDU_OUTLET_LST, self.PDU_SAMPLE_INTERVAL)
        self.pdu.start_thread()

    def setup_logging(self):
        # file handler
        # file_handler = logging.FileHandler(os.path.join('logs', 'log_{}'.format(self.time_stamp)), mode='w')
        # file_logger_formatter = logging.Formatter('%(message)s')
        # file_handler.setFormatter(file_logger_formatter)
        # file_handler.setLevel(logging.INFO)
        # stream handler
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_logger_formatter = logging.Formatter('[%(asctime)s][%(levelname)s] %(message)s')
        stream_handler.setFormatter(stream_logger_formatter)
        # stream_handler.setLevel(logging.DEBUG)
        # must be called in main thread before any sub-thread starts
        logging.basicConfig(level=logging.DEBUG, handlers=[stream_handler])

    def _assertion(self):
        assert len(self.TYPE_MAPPING_BOUNDARY) + 1 == len(self.SERVER_TYPE_LIST)

    def _check_healthy_on_each_step(self):
        assert self.pdu.pdu_thread.is_alive(), "PDU thread dead!"
        assert self.load_balancer_process.is_alive(), "Routing process dead!"

    def _initialization(self):
        # instantiate Invoker instances, the invoker instance will initialize the Core object
        invoker_id_counter = 0
        cluster_peak = 0
        for _type, spec_map_lst in self.cluster_spec_dict:
            self.server_types.append(_type)
            for spec in spec_map_lst:
                cluster_peak += self.SERVER_POWER_SPECS[_type]['peak']
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
        self.cluster_peak_pw = cluster_peak
        for i, _type in enumerate(self.server_types):
            self.serverType_2_index[_type] = i
        self.most_recent_killed_container_cache = {invoker_id: deque(maxlen=self.MOST_RECENT_KILLED_CONTAINER_SET_LIMIT)
                                                   for invoker_id in self.id_2_invoker.keys()}
        logging.info("Env initialization done")

    def _update_invoker_state(self):
        # update the invoker state after controller rpc update
        for invoker in self.id_2_invoker.values():
            warm_sum: int = 0
            busy_sum: int = 0
            warming_sum: int = 0
            # loop all functions
            for invk_2_container_set in self.func_2_busyinfo.values():
                busy_sum += len(invk_2_container_set[invoker])
            for invk_2_container_set in self.func_2_warminfo.values():
                warm_sum += len(invk_2_container_set[invoker])
            for invk_2_container_set in self.func_2_warminginfo.values():
                warming_sum += len(invk_2_container_set[invoker])
            invoker.num_busy_container = busy_sum
            invoker.num_warm_container = warm_sum
            invoker.num_warming_container = warming_sum

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

    def find_proper_invoker_to_place_container(self, candidate_set: Iterable[Invoker]) -> Invoker:
        # prefer to put a container to the invoker with the least number of normalized container
        # the caller must make sure the candidate_set is not empty
        candidate_lst: list[Invoker] = list(candidate_set)
        score_lst: list[float] = [0] * len(candidate_lst)
        for i, invoker in enumerate(candidate_lst):
            score_lst[i] = invoker.get_total_num_container() / invoker.num_cores
        index_min = min(range(len(score_lst)), key=score_lst.__getitem__)
        return candidate_lst[index_min]

    def delete_container_multiple_pinning(self, func_id_str: str, type: str) -> Optional[str]:
        lst_warmset: List[frozenset[Container]] = [st for invk, st in self.func_2_warminfo[func_id_str].items() if
                                                   invk.type == type]
        lst_busyset = [st for invk, st in self.func_2_busyinfo[func_id_str].items() if invk.type == type]
        #lst_warmingset = [st for invk, st in self.func_2_warminginfo[func_id_str].items() if invk.type == type]
        lst_warm: List[Container] = [container for s in lst_warmset for container in s]  # flatten the list
        lst_busyset = [container for s in lst_busyset for container in s]
        #lst_warmingset = [container for s in lst_warmingset for container in s]
        candidate_lst: List[Container] = list(
            chain.from_iterable([lst_warm,
                                 #lst_warmingset,  # warming container does not have an id
                                 lst_busyset]))  # flatten list of list
        if candidate_lst:
            self.actionRealizeCounter.delete_success += 1
            # find the container that has the maximum number of sibling containers that pinns to its first core.
            candidate_lst.sort(key=lambda x: x.pinned_core[0].num_pinned_container,
                               reverse=True)  # stable sort
            for cand in candidate_lst:
                invoker_id = cand.invoker.id
                if cand.id not in self.most_recent_killed_container_cache[invoker_id]:
                    # NOTE, (1) there is a small chance that the container id is reuse (2) here we must use container
                    # str id instead of container object since the different object might represent the same physical
                    # container in this implementation
                    self.most_recent_killed_container_cache[invoker_id].append(cand.id)
                    host_invoker: Invoker = cand.invoker
                    host_invoker.rpc_delete_container(cand.id, self.strId_2_funcs[func_id_str].name)
                    logging.info("Deleting container {} on invoker {}".format(cand.id, host_invoker.id))
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
            select_invoker = self.find_proper_invoker_to_place_container(meet_all)
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
        self.pdu.clear_samples()  # clear sample just before taking action
        self.take_action(mapped_action)
        time.sleep(self.SLOT_DURATION)  # NOTE, do not consider system process latency

        state = self.get_obs()
        sla_latency = self.get_sla_latency_for_reward()  # contain all function, not jut active function
        reward_dict = compute_reward_using_overall_stat(get_power=self.pdu.get_average_power,
                                                        func_2_latencies=sla_latency,
                                                        cluster_peak_pw=self.cluster_peak_pw,
                                                        latency_factor=self.RATIO_BASED_LATENCY_FACTOR)
        return state, reward_dict, False, False, self.state_info

    # get the features of All function so that to choose the active working functions.
    def select_from_all_state(self, ema_dict: ScalarMap[str, float]) -> None:
        delta_ema_ndarray = np.array(list(ema_dict.values()))
        delta_ema_mean = delta_ema_ndarray.mean()
        delta_ema_std = delta_ema_ndarray.std()

        self.active_func_ids = [0, 1]  # TODO, update active function id

    def _state_get_avg_pinned_container_per_core_per_type(self) -> list[float]:
        # get the average number of pinned container per core per type (NOTE,in simulator, we use "num_free_core" which is not
        # enough as it can not capture the multiple-pinning case) should be call after state update rpc
        total_container_pinned = [0] * len(self.server_types)
        total_num_core = [0] * len(self.server_types)
        for invoker in self.id_2_invoker.values():
            idx = self.serverType_2_index[invoker.type]
            total_container_pinned[idx] += invoker.get_total_core_pinning_count()
            total_num_core[idx] += invoker.num_cores
        return [total_pin / total_num_core for total_pin, total_num_core in zip(total_container_pinned, total_num_core)]

    def _state_get_num_container_per_type(self, func_str: str) -> dict[str, tuple[int, int, int]]:
        res_dict = {type_: (0, 0, 0) for type_ in self.server_types}  # (warm, warming, busy)
        for invoker, set_container in self.func_2_warminfo[func_str].items():
            res_dict[invoker.type][0] += len(set_container)
        for invoker, set_container in self.func_2_warminginfo[func_str].items(): # okay is warming has no id
            res_dict[invoker.type][1] += len(set_container)
        for invoker, set_container in self.func_2_busyinfo[func_str].items():
            res_dict[invoker.type][2] += len(set_container)
        return res_dict

    def get_sla_latency_for_reward(self):
        pass

    def compute_utilization(self) -> Dict:
        pass

    def get_obs(self):
        arrival_info: routing_pb2.GetArrivalInfoResponse = self.routing_stub.GetArrivalInfo(routing_pb2.EmptyRequest)
        self.select_from_all_state(arrival_info.func_2_arrivalEma)
        nn_state = []
        cluster_state = []  # not tired to a specific function
        core_avg_pinned: list[
            float] = self._state_get_avg_pinned_container_per_core_per_type()  # [serverType1Res, SererType2Res]
        cluster_state.append(core_avg_pinned)
        for func_id in self.active_func_ids:
            func_str = self.intId_2_funcStrName[func_id]
            func: Func = self.strId_2_funcs[func_str]
            func_state_vec = [arrival_info.query_count_1s[func_str],
                              arrival_info.query_count_3s[func_str],
                              func.cpu_req,
                              func.mem_req,
                              arrival_info.func_2_arrivalEma[func_str]
                              ]
            container_per_type_dict = self._state_get_num_container_per_type(func_str)
            for type_ in self.server_types:
                func_state_vec.append(container_per_type_dict[type_][1])  # warming count
                func_state_vec.append(
                    container_per_type_dict[type_][0] + container_per_type_dict[type_][2])  # warm + busy
                # TODO, rethink the scale and cap's effect, rethink whey this feature is important
                func_state_vec.append(
                    func.sla - func.invokerType_2_referenceExecTime[type_])  # sla-referenceExecTime_ThisType
            nn_state.append(func_state_vec)
        nn_state.append(cluster_state)
        nn_state = np.concatenate(nn_state, dtype=np.float32).flatten()
        if self.do_state_clip:
            return np.clip(nn_state, -self.state_clip_value, self.state_clip_value)
        else:
            return nn_state

    def register_func(self, namesp, name, mem_req, cpu_req, sla, invoker_2_referenceExecTime):
        # TODO, register into the openwhisk system, via openwhisk CLI
        func_id = self.func_id_counter
        self.strId_2_funcs[name] = Func(id=func_id, namesp=namesp, name=name, mem_req=mem_req,
                                        cpu_req=cpu_req, sla=sla,
                                        invoker_2_referenceExecTime=invoker_2_referenceExecTime)
        self.intId_2_funcStrName[func_id] = name
        self.funcname_2_id[name] = func_id
        self.func_2_warminfo[name] = {self.id_2_invoker[i]: frozenset() for i in self.id_2_invoker.keys()}
        self.func_2_busyinfo[name] = {self.id_2_invoker[i]: frozenset() for i in self.id_2_invoker.keys()}
        self.func_2_warminginfo[name] = {self.id_2_invoker[i]: frozenset() for i in self.id_2_invoker.keys()}
        self.func_id_counter += 1  # increase the function id counter
        return func_id

    # ----------for collecting runtime info---------
    def get_avg_busy_container_utilization_per_type(self, func_ids: List):
        # get avg container utilization per type for each function
        pass


if __name__ == "__main__":
    cluster = Cluster(cluster_spec_dict=config.cluster_spec_dict,func_spec_dict=config.func_spec_dict,nn_func_input_count=2)




