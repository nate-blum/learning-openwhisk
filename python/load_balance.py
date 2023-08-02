import time
from numpy.random import choice
import numpy as np
from typing import Dict, List, Tuple
from collections import deque, defaultdict
import grpc
from concurrent import futures
from multiprocessing import Process
from controller_server.server import ControllerService
from controller_server import controller_pb2 as controller_types
from controller_server import controller_pb2_grpc as controller_service
from time import time_ns
from threading import Lock, Thread
from multiprocessing import Queue
from environment import Invoker


class WskControllerService(controller_service.ControllerServiceServicer):
    TIMER_INTERVAL_SEC = 0.1

    def __init__(self, queue: Queue, default_server_type:str):
        self.DEFAULT_SERVER_TYPE = default_server_type
        # might be accessed from multiple thread
        self.func_2_arrivalQueue1Sec = defaultdict(
            deque)  # deque's append operation is thread-safe, but we still use lock
        self.func_2_arrivalQueue3Sec = defaultdict(deque)
        # the container count should  be > 0
        self.func_2_warminfoSorted: Dict[int, List[Tuple[
            int, int]]] = {}  # {func_id: [....(invokerId, warmNum)...descending order...]}, updated on each heartbeat
        self.func_2_busyinfoSorted: Dict[int, List[Tuple[int, int]]] = {}
        self.func_2_warminginfoSorted: Dict[int, List[Tuple[int, int]]] = {}

        self.func_2_containerSumList: Dict[int, List[int]] = {}
        self.func_2_invokerId: Dict[int, int] = {} # pair with the above
        self.func_2_containerCountSum: Dict[int, int] = {} # {function: sumOfAllContainerInCluster}
        self.lock_1sec = Lock()
        self.lock_3sec = Lock()
        self.process_queue = queue

        self.timer_update_arrival_info_thread = Thread(target=self._threaded_update_arrival_queue,
                                                       args=(self.TIMER_INTERVAL_SEC,), daemon=True)
        self.timer_update_arrival_info_thread.start()

    def _select_invoker_to_dipatch(self, func_id):
        # TODO, settle down the id is int or str
        # NOTE,Current heuristic: route to a invoker with the probability proportional to how many container
        #  the invoker has (sum of warm, warming, busy) this is different than the simulator.
        try:
            lst_count = self.func_2_containerSumList[func_id]
            lst_invokerId = self.func_2_invokerId[func_id]
            total = self.func_2_containerCountSum[func_id]
            return choice(lst_invokerId, p=np.array(lst_count) / total )
        except KeyError: # no corresponding container, cold start
            #TODO cold start heuristic
            return 0

    def GetInvocationRoute(self, request, context):
        t = time_ns()
        func_id = 0  # TODO
        with self.lock_1sec:
            self.func_2_arrivalQueue1Sec[func_id].append(t)  # NOTE, should be thread-safe
        with self.lock_3sec:
            self.func_2_arrivalQueue3Sec[func_id].append(t)
        res = self._select_invoker_to_dipatch(func_id)
        return controller_types.GetInvocationRouteResponse(invokerInstanceId=res)

    def _threaded_update_arrival_queue(self, interval_sec):
        # update the shared arrival queue in a separated thread, so that when the rpc is called, there won't too much
        # work to do
        while True:
            curr_time_ns = time_ns()
            with self.lock_1sec:
                for func_id, arrival_deque in self.func_2_arrivalQueue1Sec.items():
                    while len(arrival_deque) and curr_time_ns - arrival_deque[0] > 1e9:
                        arrival_deque.popleft()
            with self.lock_3sec:
                for func_id, arrival_deque in self.func_2_arrivalQueue3Sec.items():
                    while len(arrival_deque) and curr_time_ns - arrival_deque[0] > 3e9:
                        arrival_deque.popleft()
            time.sleep(interval_sec)  # every 100 millisecond

    def GetArrivalInfo(self, request, context):
        # call by the agent to collect arrival info
        res_1s = {}
        res_3s = {}
        curr_time_ns = time_ns()
        with self.lock_1sec:
            for func_id, arrival_deque in self.func_2_arrivalQueue1Sec.items():
                while len(arrival_deque) and curr_time_ns - arrival_deque[0] > 1e9:
                    arrival_deque.popleft()
                res_1s[func_id] = len(arrival_deque)
        curr_time_ns = time_ns()
        with self.lock_3sec:
            for func_id, arrival_deque in self.func_2_arrivalQueue3Sec.items():
                while len(arrival_deque) and curr_time_ns - arrival_deque[0] > 3e9:
                    arrival_deque.popleft()
                res_3s[func_id] = len(arrival_deque)
        return controller_types.GetArrivalInfoResponse(query_count_1s=res_1s, query_count_3s=res_3s)


def start_rpc_routing_server_process(queue: Queue, rpc_server_port: str, max_num_thread_rpc_server: int):
    rpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_num_thread_rpc_server))
    controller_service.add_ControllerServiceServicer_to_server(WskControllerService(queue), rpc_server)
    rpc_server.add_insecure_port(f"0.0.0.0:{rpc_server_port}")
    rpc_server.start()  # non blocking
    rpc_server.wait_for_termination()
    return rpc_server
