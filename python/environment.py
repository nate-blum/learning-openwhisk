import sys
import os
import threading
import config_local
import rpyc
from statistics import mean
from collections import defaultdict
from pprint import pprint, pformat

sys.path.append('./invoker_client')
sys.path.append('./controller_server')
import logging
import random
import time
import subprocess
from typing import Dict, Tuple, List, Deque, NamedTuple, Union, Optional, Set, Final
from collections.abc import Iterable
import grpc
import numpy as np
from multiprocessing import Process, Queue
from concurrent import futures
from threading import Lock
from collections import deque
from itertools import chain
from bisect import bisect

from controller_server import clusterstate_pb2, clusterstate_pb2_grpc, routing_pb2_grpc, routing_pb2
from controller_server.routing_pb2 import EmptyRequest
from data_structure import LatencyInfo, RoutingResult, Action, ActionRealizeCounter
from reward import Reward
from grpc_reflection.v1alpha import reflection
from google.protobuf.internal.containers import ScalarMap

import utility
import training_configs  # training config
import config  # cluster environment config
import auth
from load_balance import start_rpc_routing_server_process
from power import PDU_reader
from workload_generator import start_workload_process
from db_client import DB
from invocation_store import Invocation, InvocationStore
import state_collector
from common import Invoker, Container, Func

global_signal_queue = Queue()  # sending signal to control workload generator's reset/start
SLOT_DURATION = training_configs.SLOT_DURATION_SECOND
SERVER_RPC_THREAD_COUNT = 8  # for routing
SERVER_RPC_THREAD_COUNT_CLUSTER_UPDATE = 4  # TODO, reconsider the number
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
ARRIVAL_Q_TIMER_INTERVAL_SEC = 0.5
ARRIVAL_Q_TIME_RANGE_LIMIT = int(120e9)  # 120second, 2 minute, in nanosecond
EMA_TIME_WINDOW_NSEC = 30_000_000_000  # 0.5 min in nanosecond #NOTE, rethink the interval
ARRIVAL_EMA_BUCKET_NSEC = 2_000_000_000  # 2 second in nanosecond
ARRIVAL_EMA_COEFF = 0.4
SELECT_FUNC_ARRIVAL_EMA_WEIGHT = training_configs.select_func_weight['arrival_delta']
SELECT_FUNC_LATENCY_SLACK_WEIGHT = training_configs.select_func_weight['latency_slack']
do_state_clip: bool = training_configs.NN['state_clip']
state_clip_value = training_configs.NN['state_clip_value']
PDU_HOST = 'panic-pdu-01.cs.rutgers.edu'
PDU_OUTLET_LST = config.pdu_outlet_list
PDU_SAMPLE_INTERVAL = 0.4
WORKLOAD_TRACE_FILE = config.workload_config['trace_file']
WORKLOAD_START_POINTER = config.workload_config['workload_line_start']
WSK_PATH = config_local.wsk_path
SSH_USER_NAME = auth.user_name
SSH_PASSWD = auth.passwd
INVOKER_PYTHON_PATH = config_local.invoker_py_path
INVOKER_SOURCE_PATH = config_local.invoker_source_path
INVOKER_PY_RPC_PORT = config_local.invoker_py_rpc_port
INIT_WARM_CONTAINER_COUNT_PER_TYPE = training_configs.initialize_env['warm_cnt_per_type']
# ---
MORE_THAN_2_FUNC = training_configs.select_func_params['more_than_2_funcs']


class Stats:
    def __init__(self):
        self.cold_start_count_lock = Lock()
        self.func_2_cold_start: defaultdict[str, int] = defaultdict(int)

    def get_cold_start_count(self, func: str):
        with self.cold_start_count_lock:
            return self.func_2_cold_start[func]

    def increase_cold_start_count(self, func: str):
        with self.cold_start_count_lock:
            self.func_2_cold_start[func] += 1

    def reset_coldstart(self):
        with self.cold_start_count_lock:
            self.func_2_cold_start.clear()


class Cluster:
    DEFAULT_SERVER_TYPE = DEFAULT_SERVER_TYPE

    def __init__(self, cluster_spec_dict, func_spec_dict: Dict[str, Dict], nn_func_input_count=2) -> None:
        self.time_stamp = utility.get_curr_time()
        print(f"------------------------>Initializing Cluster, timestamp: {self.time_stamp}<---------------------")
        self.setup_logging()
        self.stats = Stats()
        self.func_id_counter = 0
        self.strId_2_funcs: Dict[str, Func] = {}  # funcid_str: func_name/action
        self.intId_2_funcStrName: Dict[int, str] = {}
        self.funcname_2_id = {}
        self.func_2_sla: Dict[str, int] = {}

        self.id_2_invoker: Dict[int, Invoker] = {}
        self.type_2_invoker: Dict[str, Set[Invoker]] = {}

        self.cluster_state_lock = Lock()
        self.func_2_warminfo: Dict[
            str, Dict[Invoker, frozenset[Container]]] = {}  # {func_id: {invoker: SetOfContainer}}
        self.func_2_busyinfo: Dict[str, Dict[Invoker, frozenset[Container]]] = {}
        self.func_2_warminginfo: Dict[str, Dict[Invoker, frozenset[Container]]] = {}

        # FIFO, must use dict as container might have the same id on different invoker (docker runtime)
        self.most_recent_killed_container_cache: dict[int, Deque[str]] = None

        self.cluster_spec_dict: dict = cluster_spec_dict
        # self.server_type_lst = list(cluster_spec_dict.keys())
        self.server_types: list[str] = []
        self.serverType_2_index: dict[str, int] = {}
        self.func_spec_dict = func_spec_dict
        self.all_func_ids = []
        self.actionRealizeCounter = ActionRealizeCounter()
        self._initialization()  # must start before the rpc server b/c rpc server use the invoker instances, and before register of function
        for name, spec in self.func_spec_dict.items():
            func_id = self.register_func(**spec)
            self.all_func_ids.append(func_id)
        self.nn_func_input_count = nn_func_input_count
        self.active_func_ids = self.all_func_ids[:nn_func_input_count]
        self.cluster_peak_pw = self._get_cluster_peak()
        self.db = DB()
        self.db.create_index(['end'])
        self.func_2_invocation2Arrival: dict[str, dict[str, int]] = defaultdict(dict)  # arrival time is in nanosecond
        self.first_update_arrival_event = threading.Event()
        self.last_query_db_since = round(time.time() * 1000)
        self.query_db_start_time = self.last_query_db_since  # only be reset on each reset
        self.state_info = None
        self.invocation_store = InvocationStore()
        self.reward = Reward(self)
        self.curr_step = 0

        # TODO, what if the server is not up, but the rpc request has been sent ?
        # -------------------Start Load Balancer process and the rpc server-----------------------------
        self.load_balancer_process = Process(target=start_rpc_routing_server_process,
                                             args=(RPC_ROUTING_SERVER_PORT, SERVER_RPC_THREAD_COUNT,
                                                   DEFAULT_SERVER_TYPE, ARRIVAL_Q_TIMER_INTERVAL_SEC,
                                                   ARRIVAL_Q_TIME_RANGE_LIMIT, EMA_TIME_WINDOW_NSEC,
                                                   ARRIVAL_EMA_BUCKET_NSEC, ARRIVAL_EMA_COEFF,
                                                   RPC_SERVER_PORT_CLUSTER_UPDATE), daemon=True)  # avoid self usage
        self.load_balancer_process.start()
        logging.info("load balance process started")
        # --------------------------------Start Workload Generation Process-----------------------------------
        self.workload_generate_process = Process(target=start_workload_process,
                                                 args=(global_signal_queue, WORKLOAD_START_POINTER, WORKLOAD_TRACE_FILE,
                                                       WSK_PATH), daemon=True)
        self.workload_generate_process.start()
        # ---------------- Set up rpc client for query arrival info(should before cluster update rpc server, b/c the cluster
        # state update server might use the stub for sending rpc request)-----------------------------------
        self.routing_channel = grpc.insecure_channel(f'localhost:{RPC_ROUTING_SERVER_PORT}')
        self.routing_stub = routing_pb2_grpc.RoutingServiceStub(self.routing_channel)  # channel is thread safe
        logging.info("routing service client started")
        # -------------------Start Cluster Update RPC server--------------------------------------------
        self.cluster_info_update_server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=SERVER_RPC_THREAD_COUNT_CLUSTER_UPDATE))
        clusterstate_pb2_grpc.add_ClusterStateServiceServicer_to_server(state_collector.WskClusterInfoCollector(self),
                                                                        self.cluster_info_update_server)
        reflection.enable_server_reflection(
            (clusterstate_pb2.DESCRIPTOR.services_by_name["ClusterStateService"].full_name,
             reflection.SERVICE_NAME), self.cluster_info_update_server)
        self.cluster_info_update_server.add_insecure_port(f"0.0.0.0:{RPC_SERVER_PORT_CLUSTER_UPDATE}")
        self.cluster_info_update_server.start()
        logging.info("cluster state service started")
        # ----------------------------PUD thread--------------------------------------------
        self.pdu = PDU_reader(PDU_HOST, PDU_OUTLET_LST, PDU_SAMPLE_INTERVAL)
        self.pdu.start_thread()
        logging.info("pdu thread started")
        logging.info("waiting the first cluster state update")
        self.first_update_arrival_event.wait()

    def print_state(self):
        print("-----------------------------State Begin-----------------------------------------")
        pprint(self.id_2_invoker)
        pprint(self.func_2_warminfo)
        pprint(self.func_2_busyinfo)
        pprint(self.func_2_warminginfo)
        print("-----------------------------State End--------------------------------------------")

    def setup_logging(self):
        # file handler
        # file_handler = logging.FileHandler(os.path.join('logs', 'log_{}'.format(self.time_stamp)), mode='w')
        # file_logger_formatter = logging.Formatter('%(message)s')
        # file_handler.setFormatter(file_logger_formatter)
        # file_handler.setLevel(logging.INFO)
        # stream handler
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_logger_formatter = logging.Formatter('[%(asctime)s][%(levelname)s][%(filename)s %(lineno)d] %(message)s')
        stream_handler.setFormatter(stream_logger_formatter)
        # stream_handler.setLevel(logging.DEBUG)
        # must be called in main thread before any sub-thread starts
        logging.basicConfig(level=logging.INFO, handlers=[stream_handler])

    def _assertion(self):
        assert len(TYPE_MAPPING_BOUNDARY) + 1 == len(SERVER_TYPE_LIST)

    def _check_healthy_on_each_step(self):
        assert self.pdu.pdu_thread.is_alive(), "PDU thread dead!"
        assert self.load_balancer_process.is_alive(), "Routing process dead!"
        assert self.workload_generate_process.is_alive(), "Workload generator process dead!"

    def _initialization(self):
        # instantiate Invoker instances, the invoker instance will initialize the Core object
        invoker_id_counter = 0
        cluster_peak = 0
        for _type, spec_map_lst in self.cluster_spec_dict.items():
            self.server_types.append(_type)
            for spec in spec_map_lst:
                cluster_peak += SERVER_POWER_SPECS[_type]['peak']
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
        self.most_recent_killed_container_cache = {invoker_id: deque(maxlen=MOST_RECENT_KILLED_CONTAINER_SET_LIMIT)
                                                   for invoker_id in self.id_2_invoker.keys()}
        invoker_host_lst = [i.hostname for i in self.id_2_invoker.values()]
        # initialize invoker python runtime
        for host in invoker_host_lst:
            # bug: use "" inside '', make the command in one line
            subprocess.Popen(f'sshpass -p {SSH_PASSWD} ssh {SSH_USER_NAME}@{host} "pkill -f stats_collect"',
                             shell=True).wait()
            p = subprocess.Popen(
                f'sshpass -p {SSH_PASSWD} ssh {SSH_USER_NAME}@{host} " nohup {INVOKER_PYTHON_PATH} {os.path.join(INVOKER_SOURCE_PATH, "python/invoker_runtime/stats_collect_server.py")} >nohup_ow_{host}.log  2>&1 &"',
                shell=True)
            (stdout, stderr) = p.communicate(timeout=10)
            if p.returncode != 0:
                logging.error(f"Starting invoker python runtime on host {host} fail, {stdout}, {stderr}")
            else:
                logging.info(f"Starting invoker python runtime on host {host} succeed, {stdout}, {stderr}")
        time.sleep(5)
        for invk in self.id_2_invoker.values():
            invk.py_runtime_client = rpyc.connect(invk.hostname, INVOKER_PY_RPC_PORT)
        logging.info("Env initialization done")

    def update_invoker_state(self):
        # update the invoker state after controller rpc update, this method is called with in a lock
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
        # logging.info(f"invokers state:\n {self.id_2_invoker.values()}")
        # logging.info(f"func_2_busy: {self.func_2_busyinfo}")

    def pre_creation_container(self, func_id_list):
        for type_ in self.server_types:
            for _ in range(INIT_WARM_CONTAINER_COUNT_PER_TYPE):
                # {func_num_id: Action}
                action_mp: dict[int, Action] = {}
                for func_id in func_id_list:
                    action_mp[func_id] = Action(container_delta=1, freq=3000, type=type_, target_load=1.0)
                self.take_action(action_mp)

    def block_until_reset_invoker_finish(self):
        start_t = time.time()
        logging.info(f"......Blocking until reset invoker finishes........")
        for invoker in self.id_2_invoker.values():
            while True:
                container_list = invoker.rpyc_get_container_ids()
                if not container_list:
                    break
                #print("container list from docker runtime==>", container_list)
                time.sleep(0.2)
                if time.time() - start_t > 300:
                    assert False, "Timeout while waiting reset result"
            logging.info(f"Resetting finished for invoker_{invoker.id}")
        logging.info(f"Blocking for {time.time() - start_t}sec for resetting invokers")

    # NOTE, should be used in a period during which no reset/deletion/creation happen
    def check_consistency_cluster_state(self):
        id_2_isConsistent = {id: False for id in self.id_2_invoker.keys()}
        start_t = time.time()
        while True:
            for id, invoker in self.id_2_invoker.items():
                if id_2_isConsistent[id]:
                    continue
                set_of_containerId: set[str] = set()  # all the busy and warm containerIds
                with self.cluster_state_lock:
                    for func, invk_2_container in self.func_2_warminfo.items():
                        set_of_containerId.update([c.id for c in invk_2_container[invoker]])
                    for func, invk_2_container in self.func_2_busyinfo.items():
                        set_of_containerId.update([c.id for c in invk_2_container[invoker]])
                container_lst_from_docker_runtime: list[str] = invoker.rpyc_get_container_ids()
                # check (1), the agent's view of containers should always be a subset of the docker runtime's view
                if not set_of_containerId.issubset(container_lst_from_docker_runtime):
                    logging.error(
                        f"Set of containerIs from agent view is not a subset of docker runtime view:\n "
                        f"agentView:{set_of_containerId}, dockerRuntimeView:{container_lst_from_docker_runtime}")
                    assert False, "Inconsistent view"
                # check (2) at some point the two set should be consistent
                if set_of_containerId == set(container_lst_from_docker_runtime):
                    id_2_isConsistent[id] = True
                else:
                    logging.info(
                        f'Inconsistent View (invoker_{id}), owView vs docker:\n{sorted(list(set_of_containerId))}\n{sorted(container_lst_from_docker_runtime)}')
            # check if the view of each invoker state is consistent
            all_pass = True
            for is_consistent in id_2_isConsistent.values():
                if not is_consistent:
                    all_pass = False
                    break
            if all_pass:
                logging.info(
                    f"View of each invoker state is consistent for docker runtime and agent, converge time: {time.time() - start_t}")
                break
            if time.time() - start_t > 30:  # ten second
                logging.error(f"Inconsistent view after timeout: {id_2_isConsistent}")
                assert False, "Inconsistent view"

    def terminate_trajectory(self):
        # stop workload
        # ---------LastStep--------StopWorkload-------Reward-----Reset--->
        global_signal_queue.put(["reset", 0])
        logging.info("------------->---->---->Terminating workload<-------<-----------------")

    def reset(self, seed=None, options=None):
        self.curr_step = 0
        self.last_query_db_since = round(
            time.time() * 1000)  # try to cover as early as possible even might catch record in previous round (will be ignored, when calcuate reward)
        time.sleep(
            4)  # wait until invocation sent from the last step is settled (in queue buffered or executed), but still it is possible
        # a last step invocation get db recorded and is queried at the next first time step
        logging.info(
            f"----------------------------------------Start Resetting-----------------------------------------------")
        # NOTE, make sure relevant data structure is reset correctly
        for invoker in self.id_2_invoker.values():
            invoker.rpc_reset_invoker()  # reset the invoker: delete all containers
        for invoker in self.id_2_invoker.values():
            invoker.rpyc_reset_container_util_collecting_runtime()
        self.routing_stub.ResetRoutingServiceState(EmptyRequest())
        self.actionRealizeCounter.clear()
        self.func_2_invocation2Arrival.clear()
        self.invocation_store.reset()
        for v in self.most_recent_killed_container_cache.values():
            v.clear()
        self.stats.reset_coldstart()  # reset cold start count, in lock
        self.reward.reset()
        #time.sleep(4)  # wait the state of container to settle
        self.block_until_reset_invoker_finish()
        self.active_func_ids = self.all_func_ids[:self.nn_func_input_count]
        if MORE_THAN_2_FUNC:
            self.pre_creation_container(self.all_func_ids)
        else:
            self.pre_creation_container(self.active_func_ids)
        # self.last_query_db_since = round(time.time() * 1000)
        workload_line_start = 0
        if options:
            workload_line_start = options['workload_start']
            if 'random_start' in options and options['random_start']:
                workload_line_start += random.randint(0, 2000)
        self.first_update_arrival_event.clear()
        self.first_update_arrival_event.wait()
        # time.sleep(6)  # wait the state to settle down, primarily container utilization
        global_signal_queue.put(["start", workload_line_start])
        obs = self.get_obs(func_2_tail_latency={}, func_2_invoker2latencyList=defaultdict(lambda: defaultdict(list)))
        self.pdu.clear_samples()
        return obs, self.state_info

    def map_action(self, simple_action: np.ndarray) -> Dict[int, Action]:
        # the output is sample from Normal distribution, they should be mapped as real control operation
        # adding default sub-action to the simple action
        action_cpp = {}
        for idx in range(NUM_ACTIVE_FUNC):
            # mapped_action = [None, 3000, None, 1.0]  # <delta, freq, type, target_load>
            slice_ = slice(idx * 2, (idx + 1) * 2)
            actions = simple_action[slice_]
            # delta dimension
            if actions[0] < -ACTION_MAPPING_BOUNDARY:
                delta = -1
            elif -ACTION_MAPPING_BOUNDARY <= actions[0] < ACTION_MAPPING_BOUNDARY:
                delta = 0
            else:
                delta = 1
            # server type dimension, return an index by bineary search
            type_ = SERVER_TYPE_LIST[bisect(TYPE_MAPPING_BOUNDARY, actions[1])]
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
        # NOTE, won't delete a warming because warming container does not have an real id
        with self.cluster_state_lock:  # use lock, give me a peace of mind
            lst_warmset: List[frozenset[Container]] = [st for invk, st in self.func_2_warminfo[func_id_str].items() if
                                                       invk.type == type]
            lst_busyset = [st for invk, st in self.func_2_busyinfo[func_id_str].items() if invk.type == type]
            # lst_warmingset = [st for invk, st in self.func_2_warminginfo[func_id_str].items() if invk.type == type]
            lst_warm: List[Container] = [container for s in lst_warmset for container in s]  # flatten the list
            lst_busyset = [container for s in lst_busyset for container in s]
            # lst_warmingset = [container for s in lst_warmingset for container in s]
            candidate_lst: List[Container] = list(
                chain.from_iterable([lst_warm,
                                     # lst_warmingset,  # warming container does not have an id
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
                                                     CONSIDER_CONTAINER_PINN_PER_CORE_LIMIT and invoker.is_all_core_pinned_to_uplimit())}
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
            logging.info(
                f"Create container for function {func.name}, corePinning {core_preference_lst[0:func.cpu_req]}, on invoker {select_invoker.id}")
        else:
            self.actionRealizeCounter.add_fail += 1
            logging.info(f"Fail to realize action of adding container for function {func.name}")

    # realize the action in the openwhisk system
    def take_action(self, mapped_action: Dict[int, Action]):
        # mapped_action: {id, Action}
        logging.info(f'\n              =====>Taking action<======\n {pformat(mapped_action)}')
        for funcid, action in mapped_action.items():
            if action.container_delta > 0:  # add container
                self.add_container_with_multiple_pinning(action, self.strId_2_funcs[self.intId_2_funcStrName[funcid]])
            elif action.container_delta < 0:
                self.delete_container_multiple_pinning(func_id_str=self.intId_2_funcStrName[funcid], type=action.type)

    def step(self, action: np.ndarray, is_last=False):
        # logging.info(
        #     f"---------------------------------------------------------Step-------------------------------------------------------------")
        mapped_action: Dict[int, Action] = self.map_action(action)
        self.take_action(mapped_action)
        if is_last:  # if it is the last step, stop the workload
            self.terminate_trajectory()
        time.sleep(SLOT_DURATION)  # NOTE, do not consider system process latency
        db_activations = self.update_activation_record()
        activation_2_invoker: routing_pb2.GetRoutingResultDictResponse = self.routing_stub.GetRoutingResultDict(
            EmptyRequest())  # Must after db query, as some db record does not have instanceId, need use this result
        # compute_reward_start_t = time.time()
        # contain queued,      not contain queued
        reward_dict, func_2_tail_latency, func_2_invoker2latencyList = self.reward.compute_reward_using_overall_stat(
            db_activations=db_activations,
            func_2_invocation2Arrival=self.func_2_invocation2Arrival,
            func_2_sla=self.func_2_sla,
            get_power=self.pdu.get_average_power,
            cluster_peak_pw=self.cluster_peak_pw,
            latency_factor=RATIO_BASED_LATENCY_FACTOR,
            activation_2_invoker=activation_2_invoker.res_dict,
            invocation_store=self.invocation_store)
        self.pdu.clear_samples()  # a step can affect not the current slot but next slot
        # t_reward_done =time.time()
        # logging.info(f"Compute reward time {t_reward_done- compute_reward_start_t}")
        state = self.get_obs(func_2_tail_latency,
                             func_2_invoker2latencyList)  # TODO: Make sure it is okay to put this method after reward method
        # logging.info(f"Get obs time: {time.time()-t_reward_done}")
        self.stats.reset_coldstart() #BUGFIX, you need reset on every step, what about others
        logging.info(
            f'\n[----------Reward {self.curr_step}----------->]\n{reward_dict}\n      func_2_tail_latency (contain queued)--->\n{func_2_tail_latency}\n     fun_2_invoker2LatencyList (not containing queued)--->\n{func_2_invoker2latencyList}')
        logging.info(f"Abnormal record:\n {self.reward.abnormal_record}")
        logging.info(f"------------->Invoker state:\n{self.id_2_invoker}")
        logging.info(f">------------------------------------Step {self.curr_step} Done----------------------------------------------<")
        if is_last:
            self.check_consistency_cluster_state()
        self.curr_step += 1

        return state, reward_dict, False, False, self.state_info

    def get_execution_time(self, db_activations: list[dict]):
        ...

    # def step_dummy(self):
    #     self._check_healthy_on_each_step()
    #     self.pdu.clear_samples()
    #     time.sleep(2)

    # get the features of All function so that to choose the active working functions.
    def select_from_all_state(self, ema_dict: ScalarMap[str, float],
                              func_2_tail_latency: dict[str, float]) -> Optional[list[str]]:
        # NOTE,The ema_dict might not contain all the registered function, different from simulator, same for `func_2_tail_latency`
        # This method might change the input parameters
        for func in self.strId_2_funcs.keys():
            if func not in ema_dict:
                ema_dict[func] = 0  # if a function does not appear, just set it to 0, consistent to Simulator
        delta_ema_ndarray = np.array(list(ema_dict.values()))
        delta_ema_mean = delta_ema_ndarray.mean()
        delta_ema_std = delta_ema_ndarray.std()

        func_2_latencySlack: dict[str, float] = {}
        for func in self.strId_2_funcs.keys():
            if func not in func_2_tail_latency:
                func_2_tail_latency[
                    func] = 0  # if a function does not appear, set it to 0 which is consistent to Simulator
        for func, tail_latency in func_2_tail_latency.items():
            func_2_latencySlack[func] = self.func_2_sla[func] - tail_latency

        if not MORE_THAN_2_FUNC:
            return
        func_2_score = {}
        latency_slack_ndarray = np.array(list(func_2_latencySlack.values()))
        latency_slack_mean = latency_slack_ndarray.mean()
        latency_slack_std = latency_slack_ndarray.std()
        # delta ema is normalized within each function and then across all function, latency slack only normalized
        # across functions. This is consistent to Simulator
        # NOTE always consider the impact caused by different *SCALA* of function arrivals
        for func in self.strId_2_funcs.keys():
            ema_term = (ema_dict[func] - delta_ema_mean) / (delta_ema_std + 1e-6)
            latency_slack_term = (func_2_latencySlack[func] - latency_slack_mean) / (
                    latency_slack_std + 1e-6)
            func_2_score[
                func] = SELECT_FUNC_ARRIVAL_EMA_WEIGHT * ema_term - SELECT_FUNC_LATENCY_SLACK_WEIGHT * latency_slack_term

        sorted_score = sorted(func_2_score.items(), key=lambda x: x[1], reverse=True)  # decreasing order
        self.active_func_ids = [self.funcname_2_id[sorted_score[0][0]], self.funcname_2_id[sorted_score[-1][0]]]
        return [sorted_score[0][0], sorted_score[-1][0]]

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

    def _state_get_num_container_per_type(self, func_str: str) -> dict[str, list[int, int, int]]:
        res_dict = {type_: [0, 0, 0] for type_ in self.server_types}  # (warm, warming, busy)
        with self.cluster_state_lock:
            for invoker, set_container in self.func_2_warminfo[func_str].items():
                res_dict[invoker.type][0] += len(set_container)
            for invoker, set_container in self.func_2_warminginfo[func_str].items():  # okay is warming has no id
                res_dict[invoker.type][1] += len(set_container)
            for invoker, set_container in self.func_2_busyinfo[func_str].items():
                res_dict[invoker.type][2] += len(set_container)
        return res_dict

    def update_activation_record(self) -> list[dict]:
        resp: routing_pb2.GetInvocationDictResponse = self.routing_stub.GetInvocationDict(routing_pb2.EmptyRequest())
        # logging.info(f"GetInvocationDict Result:\n{resp}")
        curr_time = round(time.time() * 1000)  # database time is in millisecond
        # NOTE, it is import the db query happen before local activation record update
        start_t = time.time()
        db_activations = \
            self.db.GetActivationRecordsEndTimeSinceUntil(since=self.last_query_db_since - 1000, end=curr_time)['docs']
        query_latency = time.time() - start_t
        logging.info(f'Db query time: {query_latency}')
        assert query_latency < 0.2, "db query latency > 0.2 sec"
        # db_activations = self.db.GetActivationRecordsSince(since=self.last_query_db_since, until=curr_time)['docs'] # NOTE,BUG  could miss record whose start time is within last window but finished in the current window
        self.last_query_db_since = curr_time  # guarantee no missing of record in database
        # update the local invocation dict based on rpc result
        for func, listRecord in resp.func2_invocationRecordList.items():
            for invocation, arrTime in zip(listRecord.invocationId, listRecord.arrivalTime):
                assert invocation not in self.func_2_invocation2Arrival[func]
                self.func_2_invocation2Arrival[func][invocation] = arrTime  # the time is in nanosecond
                self.invocation_store.init_invocation(invocation, arrTime, func)
        # logging.info(f"====>Function_2_invocation2arrival:\n{self.func_2_invocation2Arrival}")
        # logging.info(f"DB_activations:\n{db_activations}")
        return db_activations

    def compute_utilization(self) -> Dict:
        pass

    def get_obs(self, func_2_tail_latency: dict[str, float],  # include queued
                func_2_invoker2latencyList: defaultdict[str, defaultdict[int, list]]  # do not include queued
                ):
        _debug_dict_ = defaultdict(dict)  # temporary data structure for debug printing
        func_2_type2latencyList: defaultdict[str, defaultdict[str, list]] = defaultdict(lambda: defaultdict(list))
        for func_name, invoker2LatencyList in func_2_invoker2latencyList.items():
            for invoker_id, latency_lst in invoker2LatencyList.items():
                func_2_type2latencyList[func_name][self.id_2_invoker[invoker_id].type].extend(latency_lst)
        arrival_info: routing_pb2.GetArrivalInfoResponse = self.routing_stub.GetArrivalInfo(routing_pb2.EmptyRequest())
        # NOTE, ** the tail latency includes record in the waiting queue, which is different from the simulator which only has
        # finished invocation record. The period during which to calculate the tail latency is also different: this has all the
        # records that are in the queue. The simulator has latency record during a fixed period of window (the consecutive windows may be overlapped).
        # NOTE, ** if execution is FIFO, including waiting record or not should make a lot difference
        select_func: list[str] = self.select_from_all_state(arrival_info.func_2_arrivalEma,
                                                            func_2_tail_latency)  # the parameter might be changed
        # Some (func, type) pair might not be in the default dict
        # func_2_containerUtilization = self.get_avg_busy_container_utilization_per_type(select_func)
        nn_state = []
        cluster_state = []  # not tired to a specific function
        core_avg_pinned: list[
            float] = self._state_get_avg_pinned_container_per_core_per_type()  # [serverType1Res, SererType2Res]
        cluster_state.extend(core_avg_pinned)
        for func_id in self.active_func_ids:
            func_str = self.intId_2_funcStrName[func_id]
            func: Func = self.strId_2_funcs[func_str]
            func_state_vec = [arrival_info.query_count_1s[func_str],
                              arrival_info.query_count_3s[func_str],
                              func.cpu_req,
                              func.mem_req,
                              arrival_info.func_2_arrivalEma[func_str],
                              # NOTE the real meaning of "cold start" with different setting. It's different than Openwhisk
                              self.stats.get_cold_start_count(func_str)
                              # func_2_tail_latency[func_str]  # NOTE, rethink: new added, include all queueing jobs
                              ]
            _debug_dict_[func_str]['cold_start_cnt'] = self.stats.get_cold_start_count(func_str)
            _debug_dict_[func_str]['3s_req_count'] = arrival_info.query_count_3s[func_str]

            container_per_type_dict = self._state_get_num_container_per_type(func_str)
            for type_ in self.server_types:
                type_tail_latency = np.percentile(func_2_type2latencyList[func_str][type_], 95) if \
                    func_2_type2latencyList[func_str][type_] else 0
                func_state_vec.append(
                    self.func_2_sla[func_str] - type_tail_latency)  # TODO, rethink, 95 or 99, including buffered or not
                func_state_vec.append(container_per_type_dict[type_][1])  # warming count
                func_state_vec.append(
                    container_per_type_dict[type_][0] + container_per_type_dict[type_][2])  # warm + busy
                # container_util = func_2_containerUtilization[func_str][type_] if (
                #             func_str in func_2_containerUtilization and type_ in
                #             func_2_containerUtilization[func_str]) else 0
                # func_state_vec.append(container_util)
                # TODO, rethink the scale and cap's effect, rethink whey this feature is important
                func_state_vec.append(
                    func.sla - func.invokerType_2_referenceExecTime[type_])  # sla-referenceExecTime_ThisType
                _debug_dict_[func_str][f'containerCnt_Warm+busy_{type_}'] = container_per_type_dict[type_][0] + \
                                                                            container_per_type_dict[type_][2]
                # _debug_dict_[func_str][f'container_util_{type_}'] = container_util
            nn_state.append(func_state_vec)
        nn_state.append(cluster_state)
        nn_state = np.concatenate(nn_state, dtype=np.float32).flatten()
        logging.info('\n----->NN State Input<----')
        pprint(_debug_dict_)
        if do_state_clip:
            return np.clip(nn_state, -state_clip_value, state_clip_value)
        else:
            return nn_state

    def register_func(self, **kwargs):
        # TODO, register into the openwhisk system, via openwhisk CLI
        func_id = self.func_id_counter
        name = kwargs['name']
        self.strId_2_funcs[name] = Func(id=func_id, namesp=kwargs['namesp'], name=kwargs['name'],
                                        mem_req=kwargs['mem_req'],
                                        cpu_req=kwargs['cpu_req'], sla=kwargs['sla'],
                                        invoker_2_referenceExecTime=kwargs['invoker_2_referenceExecTime'])
        self.func_2_sla[name] = kwargs['sla']
        self.intId_2_funcStrName[func_id] = name
        self.funcname_2_id[name] = func_id
        self.func_2_warminfo[name] = {self.id_2_invoker[i]: frozenset() for i in self.id_2_invoker.keys()}
        self.func_2_busyinfo[name] = {self.id_2_invoker[i]: frozenset() for i in self.id_2_invoker.keys()}
        self.func_2_warminginfo[name] = {self.id_2_invoker[i]: frozenset() for i in self.id_2_invoker.keys()}
        self.func_id_counter += 1  # increase the function id counter
        return func_id

    # ----------for collecting runtime info---------
    def get_avg_busy_container_utilization_per_type(self, func_strs: List[str]) -> defaultdict[str, dict[str, float]]:
        # NOTE, no warming container involved
        # get avg container utilization per type for each function
        res: defaultdict[str, dict[str, float]] = defaultdict(dict)  # {func: {type, utilization}}
        for func in func_strs:
            utilization = defaultdict(list)  # {type: [utils]}
            with self.cluster_state_lock:
                invk_2_busy = self.func_2_busyinfo[func]
                for invk, container_set in invk_2_busy.items():
                    if not container_set:
                        continue
                    container_2_util = invk.rpyc_get_container_stats()  # TODO could merge the two call
                    for contr in container_set:
                        try:
                            utilization[invk.type].append(container_2_util[contr.id])
                        except KeyError:
                            logging.error(
                                f"No container utilization record for busy container {contr} of function {func} on invoker: {invk.id}")
                            assert False
                invk_2_warm = self.func_2_warminfo[func]
                for invk, container_set in invk_2_warm.items():
                    if not container_set:
                        continue
                    container_2_util = invk.rpyc_get_container_stats()
                    # logging.info(
                    #     f"The docker utilization dict for func {func} on invoker id {invk.id}:\n{container_2_util}")
                    for contr in container_set:
                        try:
                            utilization[invk.type].append(container_2_util[contr.id])
                        except KeyError:
                            logging.error(
                                f"No container utilization record for warm container ({contr}) of function {func} on invoker: {invk.id}")
                            assert False
            for type, util_lst in utilization.items():
                res[func][type] = mean(
                    util_lst)  # if type is in the dict there must be at least one element in the least
            logging.info(f"Container utilization for func \n{func} {utilization}")
        return res

    def _get_cluster_peak(self):
        total = 0
        for type_, svr_lst in self.cluster_spec_dict.items():
            for svr in svr_lst:
                total += config.server_power_specs[type_]['peak']
        return total


def test_popen_remote():
    host = "panic-cloud-xs-06.cs.rutgers.edu"
    # p = subprocess.Popen(
    #     f"sshpass -p {SSH_PASSWD} ssh {SSH_USER_NAME}@{host}"
    #     f" 'nohup ls  > nohup.txt 2>&1 &'",
    #     shell=True)
    p = subprocess.Popen(
        f'sshpass -p {SSH_PASSWD} ssh {SSH_USER_NAME}@{host} " nohup {INVOKER_PYTHON_PATH} {os.path.join(INVOKER_SOURCE_PATH, "python/invoker_runtime/stats_collect_server.py")} >nohup_ow.log  2>&1 &"',
        shell=True)
    (stdout, stderr) = p.communicate(timeout=5)
    if p.returncode != 0:
        logging.error(f"Starting invoker python runtime on host {host} fail, {stdout}, {stderr}")
    else:
        logging.info(f"Starting invoker python runtime on host {host} succeed, {stdout}, {stderr}")
    exit()


if __name__ == "__main__":
    import tester

    cluster = Cluster(cluster_spec_dict=config.cluster_spec_dict, func_spec_dict=config.func_spec_dict,
                      nn_func_input_count=2)
    cluster.reset()
    # cluster.id_2_invoker[1].rpc_add_container('func1', [0])
    # cluster.id_2_invoker[1].rpc_reset_invoker()
    t = tester.Test(cluster)
    t.test_create_container_issue_requests()
    # pprint(cluster.func_2_invocation2Arrival)

    # cluster.id_2_invoker[0].rpc_add_container("helloPython", [0, 1])
    # while True:
    #     pprint.pprint(cluster.id_2_invoker[0].rpyc_get_container_stats())
    #     time.sleep(1)
    # cluster.step_dummy()
    # cluster.print_state()
