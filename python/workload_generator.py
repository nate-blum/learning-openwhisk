from multiprocessing import Queue
import os
import sys
from time import time, sleep
import pandas as pd
from openwhisk_client import OpenwhiskClient
import logging
import config_local
from utility import get_curr_time


class WorkloadGenerator:
    MAX_INVOCATION = 3 # for testing
    def __init__(self, shared_queue: Queue, start_pointer: int, trace_file: str, wsk_path: str):
        self.q: Queue = shared_queue  # sharing command from main process to workload generator process
        self.state: str = "reset"
        self.line_pointer = start_pointer
        self.trace = pd.read_csv(trace_file, header=None)
        self.trace_size = len(self.trace)
        self.ow_client = OpenwhiskClient(wsk_path)
        self.last_req_t = time()
        # ---------------------------------------
        self.binary_data_cache = {}
        self.sto_stream = self.setup_logging()
        self.count = 0

    def setup_logging(self):
        # file handler
        file_handler = logging.FileHandler(
            os.path.join(config_local.wsk_log_dir, 'workloadGeneratorLog_{}'.format(get_curr_time())), mode='w')
        file_logger_formatter = logging.Formatter('[%(asctime)s][%(levelname)s][%(filename)s %(lineno)d] %(message)s')
        file_handler.setFormatter(file_logger_formatter)
        file_handler.setLevel(logging.INFO)
        # stream handler
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_logger_formatter = logging.Formatter('[%(asctime)s][%(levelname)s][%(filename)s %(lineno)d] %(message)s')
        stream_handler.setFormatter(stream_logger_formatter)
        stream_handler.setLevel(logging.INFO)
        # must be called in main thread before any sub-thread starts
        logging.basicConfig(level=logging.INFO, handlers=[stream_handler, file_handler])
        return stream_handler

    def generate_workload(self):
        # based on finite state machine
        # finite state machine for the generator:
        #   -------{Reset}------------->----{generating}-------
        #          ^                                          |
        #          |                                         |
        #         --------------------------------------------
        logging.info("Start Generate Workload")
        while True:
            match self.state:
                case "reset":
                    if not self.q.empty():
                        signal = self.q.get()
                        if signal[0] == "start":
                            self.state = "start"
                            logging.info(f"workload FSM state change: {self.state}->Start")
                        else:  # must be signal[0] == "reset"
                            self.line_pointer = signal[1]
                            self.last_req_t = 0
                            logging.info(f"workload FSM state change: {self.state}->Reset")
                case "start":
                    if not self.q.empty():  # new signal comes in
                        signal = self.q.get()
                        if signal[0] == "reset":
                            self.state = "reset"
                            self.line_pointer = signal[1]
                            self.last_req_t = 0
                            logging.info(f"workload FSM state change: {self.state}->Reset")
                            continue
                    self.send_request()
                    self.count += 1
                    if self.count > self.MAX_INVOCATION:
                        break

    def send_request(self):
        mod_line = (self.line_pointer + self.trace_size) % self.trace_size
        func_name = self.trace.iloc[mod_line, 0]
        elapse_to_prev = self.trace.iloc[mod_line, 1]  # in millisecond
        data_file = self.trace.iloc[mod_line, 2]  # image data file
        request_type = self.trace.iloc[mod_line, 3]
        logging.info(f"Sending request: {func_name}")
        if (
                diff := time() - self.last_req_t) <= elapse_to_prev / 1000:  # assuming the interval in trace is in millisecond
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
                self.ow_client.invoke_common(action=func_name)
        mod_line += 1
        self.last_req_t = time()


def start_workload_process(q: Queue, start_pointer: int, trace_file: str, wsk_path: str):
    workload_generator = WorkloadGenerator(q, start_pointer, trace_file, wsk_path)
    workload_generator.generate_workload()  # blocking call

