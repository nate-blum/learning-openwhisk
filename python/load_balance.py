import sys
import os
import time
import logging
from numpy.random import choice
import numpy as np
from typing import Dict, List, Tuple
from collections import deque, defaultdict
import grpc
from concurrent import futures
from controller_server import routing_pb2
from controller_server import routing_pb2_grpc, clusterstate_pb2_grpc
from controller_server.clusterstate_pb2 import GetRoutingColdStartRequest, GetRoutingColdStartResponse
from time import time_ns
from threading import Lock, Thread
from environment import Invoker


class WskRoutingService(routing_pb2_grpc.RoutingServiceServicer):
    # TIMER_INTERVAL_SEC = 0.2
    # ARRIVAL_Q_TIME_RANGE_LIMIT = int(120e9)  # 120second, 2 minute, in nanosecond
    # EMA_TIME_WINDOW_NSEC = 60_000_000_000  # 1min in nanosecond
    # BUCKET_NSEC = 2_000_000_000  # 2 second in nanosecond
    # ARRIVAL_EMA_COEFF = 0.4

    def __init__(self, default_server_type: str, q_update_timer_interval_sec: float, arrival_q_time_range_limit: int,
                 ema_time_window_nsec: int, ema_bucket_nsec: int, arrival_ema_coeff: float,
                 cluster_update_rpc_server_port: str):
        # ----------------------------------------Configs-------------------------------
        self.DEFAULT_SERVER_TYPE: str = default_server_type
        self.TIMER_INTERVAL_SEC: float = q_update_timer_interval_sec
        self.ARRIVAL_Q_TIME_RANGE_LIMIT: int = arrival_q_time_range_limit
        self.EMA_TIME_WINDOW_NSEC: int = ema_time_window_nsec
        self.BUCKET_NSEC: int = ema_bucket_nsec
        self.ARRIVAL_EMA_COEFF: float = arrival_ema_coeff
        # -------------------------------------------------------------------------------
        self.setup_logging()
        # might be accessed from multiple thread
        self.func_2_arrivalQueue: defaultdict[str, deque] = defaultdict(
            deque)  # deque's append operation is thread-safe, but we still use lock

        self.func_2_containerSumList: Dict[str, List[int]] = {}
        self.func_2_invokerId: Dict[str, List[int]] = {}  # pair with the above
        self.func_2_containerCountSum: Dict[str, int] = {}  # {function: sumOfAllContainerInCluster}
        self.lock_routing_info = Lock()
        self.lock_arrival_q = Lock()
        #
        self.cluster_update_channel = grpc.insecure_channel(f'localhost:{cluster_update_rpc_server_port}')
        self.cluster_update_stub = clusterstate_pb2_grpc.ClusterStateServiceStub(self.cluster_update_channel)

        self.timer_update_arrival_info_thread = Thread(target=self._threaded_update_arrival_queue,
                                                       args=(self.TIMER_INTERVAL_SEC,), daemon=True)
        self.timer_update_arrival_info_thread.start()

    def setup_logging(self):
        # file handler
        file_handler = logging.FileHandler(os.path.join('logs', 'log_{}'.format(self.time_stamp)), mode='w')
        file_logger_formatter = logging.Formatter('[%(asctime)s][%(levelname)s] %(message)s')
        file_handler.setFormatter(file_logger_formatter)
        file_handler.setLevel(logging.DEBUG)
        # stream handler
        #stream_handler = logging.StreamHandler(sys.stdout)
        #stream_logger_formatter = logging.Formatter('[%(asctime)s][%(levelname)s] %(message)s')
        #stream_handler.setFormatter(stream_logger_formatter)
        # stream_handler.setLevel(logging.DEBUG)
        # must be called in main thread before any sub-thread starts
        logging.basicConfig(level=logging.DEBUG, handlers=[file_handler])

    def _select_invoker_to_dispatch(self, func_id_str: str) -> int:
        # NOTE,Current heuristic: route to a invoker with the probability proportional to how many container
        #  the invoker has (sum of warm, warming, busy) this is different than the simulator. Cold start
        #  heuristic: 1) choose the default server (fast startup) and 2) choose one with least number of normalized
        #  container 3) rely on the Openwhisk to start a container
        try:
            with self.lock_routing_info:
                lst_count = self.func_2_containerSumList[func_id_str]
                lst_invokerId = self.func_2_invokerId[func_id_str]
                total = self.func_2_containerCountSum[func_id_str]
                return choice(lst_invokerId, p=np.array(lst_count) / total)
        except KeyError:  # no corresponding container, cold start
            response: GetRoutingColdStartResponse = self.cluster_update_stub.GetRoutingColdStart(
                GetRoutingColdStartRequest(func_str=func_id_str))
            return response.invoker_selected

    def GetInvocationRoute(self, request: routing_pb2.GetInvocationRouteRequest, context):
        logging.info("Received routing request from OW controller")
        func_id_str: str = request.actionName
        t = time_ns()
        with self.lock_arrival_q:
            self.func_2_arrivalQueue[func_id_str].appendleft(t)  # NOTE, should be thread-safe
        res: int = self._select_invoker_to_dispatch(func_id_str)
        return routing_pb2.GetInvocationRouteResponse(invokerInstanceId=res)

    def NotifyClusterInfo(self, request: routing_pb2.NotifyClusterInfoRequest, context):
        logging.info("Receive state update notification")
        with self.lock_routing_info:
            for func_id_str, container_counter in request.func_2_ContainerCounter.items():
                self.func_2_containerSumList[func_id_str] = container_counter.count
                self.func_2_invokerId[func_id_str] = container_counter.invokerId
                self.func_2_containerCountSum[func_id_str] = sum(container_counter.count)
        return routing_pb2.NotifyClusterInfoResponse()

    def _threaded_update_arrival_queue(self, interval_sec):
        # update the shared arrival queue in a separated thread, so that its length is maintained
        while True:
            curr_time_ns = time_ns()
            with self.lock_arrival_q:
                for func_id, arrival_deque in self.func_2_arrivalQueue.items():
                    while len(arrival_deque) and curr_time_ns - arrival_deque[-1] > self.ARRIVAL_Q_TIME_RANGE_LIMIT:
                        arrival_deque.pop()
            time.sleep(interval_sec)

    def GetArrivalInfo(self, request, context):
        logging.info("Receive get arrival info RPC")
        assert self.timer_update_arrival_info_thread.is_alive(), "Timer_update_arrival_info thread dead"  # periodic check
        # call by the agent to collect arrival info, this won't lock a lot of time as the background helper thread
        res_1s = {}
        res_3s = {}
        curr_time_ns = time_ns()
        with self.lock_arrival_q:
            for func_id, arrival_deque in self.func_2_arrivalQueue.items():
                counter_1s = 0
                counter_23s = 0
                for item in arrival_deque:
                    delta = curr_time_ns - item
                    if delta <= 1_000_000_000:
                        counter_1s += 1
                    elif 1_000_000_000 < delta <= 3_000_000_000:
                        counter_23s += 1
                    else:
                        break
                res_1s[func_id] = counter_1s
                res_3s[func_id] = counter_1s + counter_23s
        with self.lock_arrival_q:  # do not put all the computation under one-time lock (locking too long time is not good)
            res = {}
            num_buckets = self.EMA_TIME_WINDOW_NSEC // self.BUCKET_NSEC
            curr_time_ns = time_ns()
            for func_id, arrival_deque in self.func_2_arrivalQueue.items():
                delta = [0] * num_buckets
                for t in arrival_deque:
                    bucket_index = (curr_time_ns - t) // self.BUCKET_NSEC
                    if bucket_index < num_buckets:
                        delta[bucket_index] += 1
                    else:
                        break
                most_recent_bucket_arrival = delta[0]  # for normalization
                delta = [delta[i] - delta[i + 1] for i in
                         range(num_buckets - 1)]  # compute the real delta, len = num_buckets -1
                ema = delta[num_buckets - 2]  # last element
                for i in reversed(range(num_buckets - 2)):  # from [num_buckets-3 to 0]
                    ema = self.ARRIVAL_EMA_COEFF * delta[i] + (1 - self.ARRIVAL_EMA_COEFF) * ema
                res[func_id] = ema / (most_recent_bucket_arrival + 1e-6)
        return routing_pb2.GetArrivalInfoResponse(query_count_1s=res_1s, query_count_3s=res_3s, func_2_arrivalEma=res)


def start_rpc_routing_server_process(rpc_server_port: str, max_num_thread_rpc_server: int,
                                     default_svr_type: str, q_update_timer_interval_sec: float,
                                     arrival_q_time_range_limit: int, ema_time_window_nsec: int,
                                     ema_bucket_nsec: int, arrival_ema_coeff: float,
                                     cluster_update_rpc_server_port: str):
    rpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_num_thread_rpc_server))
    routing_pb2_grpc.add_RoutingServiceServicer_to_server(
        WskRoutingService(default_svr_type, q_update_timer_interval_sec,
                          arrival_q_time_range_limit, ema_time_window_nsec, ema_bucket_nsec, arrival_ema_coeff,
                          cluster_update_rpc_server_port),
        rpc_server)
    rpc_server.add_insecure_port(f"0.0.0.0:{rpc_server_port}")
    rpc_server.start()  # non blocking
    rpc_server.wait_for_termination()
