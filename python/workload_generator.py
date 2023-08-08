from multiprocessing import Queue


class WorkloadGenerator:
    # finite state machine for the generator:
    #   -------{Reset}------------->----{generating}-------
    #          ^                                          |
    #          |                                         |
    #         --------------------------------------------
    def __init__(self, shared_queue: Queue, start_pointer:int):
        self.q: Queue = shared_queue # sharing command from main process to workload generator process
        self.state:str = "reset"
        self.line_pointer = start_pointer

    def generate_workload(self):
        # based on finite state machine
        while True:
            match self.state:
                case "reset":
                    if not self.q.empty():
                        signal = self.q.get()
                        if signal[0] == "start":
                            self.state = "start"
                        else: # must be signal[0] == "reset"
                            self.line_pointer = signal[1]
                case "start":
                    if not self.q.empty(): # new signal comes in
                        signal = self.q.get()
                        if signal[0] == "reset":
                            self.state = "reset"
                            self.line_pointer=signal[1]
                            continue
                    self.send_request()

    def send_request(self):
        #TODO, how to timing the request
        pass

def start_workload_process(q:Queue, start_pointer:int):
    workload_generator = WorkloadGenerator(q, start_pointer)
    workload_generator.generate_workload() # blocking call










