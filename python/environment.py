from typing import Dict,List,Deque,NamedTuple,Union,Optional
from operator import itemgetter
import grpc
from invoker_client import invoker_pb2 as invoker_types
from invoker_client import invoker_pb2_grpc as invoker_service

from data_structure import LatencyInfo, RoutingResult


class Invoker:
    def __init__(self, id, host, type, mem_capacity, num_cores) -> None:
        self.id = id
        self.hostname = host
        self.type = type
        self.mem_capacity = mem_capacity
        self.num_cores = num_cores
        self.num_warm_container = 0
        self.num_busy_container = 0
        # rpc channel
        self.channel = grpc.insecure_channel(self.hostname)
        self.stub = invoker_service.InvokerServiceStub(channel=self.channel)


class Func:
    def __init__(self, id, namesp, name, mem_req, cpu_req, invoker_2_referenceExecTime):
        self.id = id
        self.namesp = namesp
        self.name = name
        self.mem_req = mem_req
        self.cpu_req = cpu_req
        self.invoker_2_referenceExecTime = invoker_2_referenceExecTime


# Monitor collects all the necessary info for agent to make decsion
class Monitor:
    def __init__(self):
        self.func_2_arrival_queue:Dict[int, Deque] = {}
        self.func_2_slaLaency_for_reward: Dict[int,LatencyInfo] = {}
        self.func_2_nColdStart = {}


class Loadbalancer:
    def __init__(self) -> None:
        pass

    def route_invocation(func_id) -> int:
        raise NotImplementedError


class Cluster:
    def __init__(self, cluster_spec_dict, func_spec_dict: Dict[str, Dict], nn_func_input_count = 2) -> None:
        self.func_id_counter = 0
        self.id_2_funcs = {}  # funcid: func_name/action
        self.id_2_invoker = {}
        self.func_2_warminfo = {}  # {func_id: {invoker_id: containerCount}}
        self.func_2_busyinfo = {}
        self.func_2_warminginfo = {}

        self.func_2_warminfoSorted = {} # {func_id: [....(invoker, warmNum)...descending order...]}, updated on each heartbeat
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

    # find a proper invoker and try to trigger a cold start
    def _find_proper_invoker_cold_start(self, func_id)->Optional[int]:
        pass

    # NOTE,What is different from the simulator: the routing heuristic might not always have the most up-to-date cluster view
    def route_invocation(self, func_id)->Union[int,RoutingResult]:
        warminfo_sorted = self.func_2_warminfoSorted[func_id]
        if warminfo_sorted:
            return warminfo_sorted[0] # invoker with the maximum of number of warm container for the function
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

    def step(self, action):
        pass

    # get the features of All function so that to choose the active working functions.
    def select_from_all_state(self, time_window_size_millis:int, coeff: float, bucket_millis: int)->None:
        pass
    def get_obs(self):
        pass

    def register_func(self, namesp, name, mem_req, cpu_req, invoker_2_referenceExecTime):
        func_id = self.func_id_counter
        self.id_2_funcs[func_id] = Func(id=func_id, namesp=namesp, name=name, mem_req=mem_req,
                                                     cpu_req=cpu_req,
                                                     invoker_2_referenceExecTime=invoker_2_referenceExecTime)
        self.func_id_counter += 1 # increase the function id counter
        return func_id

    def add_container(self, func_id):
        pass

    def delete_container(self, func_id):
        pass
    # ----------for collecting runtime info---------
    def get_avg_busy_container_utilization_per_type(self, func_ids: List):
       # get avg container utilization per type for each function
       pass

