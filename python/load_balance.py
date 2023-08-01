import time
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


class WskControllerService(controller_service.ControllerServiceServicer):
    TIMER_INTERVAL_SEC = 0.1

    def __init__(self, q:Queue):
        # might be accessed from multiple thread, deque: [old, new]
        self.func_2_arrivalQueue1Sec = defaultdict(deque)  # deque's append operation is thread-safe, but we still use lock
        self.func_2_arrivalQueue3Sec = defaultdict(deque)
        self.lock_1sec = Lock()
        self.lock_3sec = Lock()
        self.process_queue = q

        self.timer_update_arrival_info_thread = Thread(target=self._threaded_update_arrival_queue,
                                                       args=(self.TIMER_INTERVAL_SEC,), daemon=True)
        self.timer_update_arrival_info_thread.start()

    def GetInvocationRoute(self, request, context):
        t = time_ns()
        func_id = 0  # TODO
        with self.lock_1sec:
            self.func_2_arrivalQueue1Sec[func_id].append(t)  # NOTE, should be thread-safe
        with self.lock_3sec:
            self.func_2_arrivalQueue3Sec[func_id].append(t)
        return controller_types.GetInvocationRouteResponse(invokerHost="panic-cloud-xs-01.cs.rutgers.edu:50051")

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



def start_rpc_routing_server_process(queue:Queue, rpc_server_port: str, max_num_thread_rpc_server: int):
    rpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_num_thread_rpc_server))
    controller_service.add_ControllerServiceServicer_to_server(WskControllerService(queue), rpc_server)
    rpc_server.add_insecure_port(f"0.0.0.0:{rpc_server_port}")
    rpc_server.start()  # non blocking
    rpc_server.wait_for_termination()
    return rpc_server


