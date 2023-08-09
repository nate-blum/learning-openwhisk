from multiprocessing import Queue
from time import time, sleep
import pandas as pd
from openwhisk_client import OpenwhiskClient

class WorkloadGenerator:
    def __init__(self, shared_queue: Queue, start_pointer: int, trace_file: str, wsk_path: str):
        self.q: Queue = shared_queue  # sharing command from main process to workload generator process
        self.state: str = "reset"
        self.line_pointer = start_pointer
        self.trace = pd.read_csv(trace_file, header=None)
        self.trace_size = len(self.trace)
        self.ow_client = OpenwhiskClient(wsk_path)
        self.last_req_t = 0
        #---------------------------------------
        self.binary_data_cache = {}


    def generate_workload(self):
        # based on finite state machine
        # finite state machine for the generator:
        #   -------{Reset}------------->----{generating}-------
        #          ^                                          |
        #          |                                         |
        #         --------------------------------------------
        while True:
            match self.state:
                case "reset":
                    if not self.q.empty():
                        signal = self.q.get()
                        if signal[0] == "start":
                            self.state = "start"
                        else:  # must be signal[0] == "reset"
                            self.line_pointer = signal[1]
                            self.last_req_t = 0
                case "start":
                    if not self.q.empty():  # new signal comes in
                        signal = self.q.get()
                        if signal[0] == "reset":
                            self.state = "reset"
                            self.line_pointer = signal[1]
                            self.last_req_t = 0
                            continue
                    self.send_request()

    def send_request(self):
        mod_line = (self.line_pointer + self.trace_size) % self.trace_size
        func_name = self.trace.iloc[mod_line, 0]
        elapse_to_prev =  self.trace.iloc[mod_line,1]
        data_file = self.trace.iloc[mod_line,2] # image data file
        request_type = self.trace.iloc[mod_line, 2]
        if (diff := time() - self.last_req_t) <= elapse_to_prev / 1000: # assuming the interval in trace is in millisecond
            sleep(diff)
        match request_type:
            case "binary":
                try:
                    data = self.binary_data_cache[data_file]
                except KeyError:
                    data = open(data_file, "rb").read()
                    self.binary_data_cache[data_file] = data
                self.ow_client.invoke_binary_data(action=func_name, data=data)
            case _:
                pass
        self.last_req_t = time()



def start_workload_process(q: Queue, start_pointer: int, trace_file: str, wsk_path:str):
    workload_generator = WorkloadGenerator(q, start_pointer, trace_file, wsk_path)
    workload_generator.generate_workload()  # blocking call
