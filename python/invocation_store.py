import logging
from collections import defaultdict


class Invocation:
    def __init__(self, invocation_id, arrival_time, func):
        self.invocation_id = invocation_id
        self.arrival_time = arrival_time
        self.func = func
        self.finish_time = None
        self.routed_invoker = None

    def __str__(self):
        return f'({self.invocation_id} Arr:{self.arrival_time}, Latency:{self.finish_time - self.arrival_time if self.finish_time else "N/A"})'


class InvocationStore:
    def __init__(self):
        # self.func_2_invk2invocationDict: defaultdict[str, defaultdict[int, dict[str, Invocation]]] = defaultdict(
        #     lambda: defaultdict(dict))
        self.invocationid_2_invocation: dict[str, Invocation] = {}
    def reset(self):
        self.invocationid_2_invocation.clear()

    def init_invocation(self, invocation_id, arrival_time, func):
        obj = Invocation(invocation_id, round(arrival_time/1_000_000), func)
        # self.func_2_invk2invocationDict[func][invoker_id][invocation_id] = obj
        self.invocationid_2_invocation[invocation_id] = obj

    def is_in_store(self, activation_id:str):
        return activation_id in self.invocationid_2_invocation
    def set_finish_time(self, invocation_id, finish_time, invoker):
        self.invocationid_2_invocation[invocation_id].finish_time = finish_time
        self.invocationid_2_invocation[invocation_id].routed_invoker = invoker

    def check_invocation_fifo(self, invocation_2_invoker: dict[str, int]):
        # check if invocation for the same function and invoker is FIFO
        func_2_invoker2activationList = defaultdict(lambda: defaultdict(list))
        for invocation_id, obj in self.invocationid_2_invocation.items():
            if obj.finish_time:  # has logged into db
                func_2_invoker2activationList[obj.func][obj.routed_invoker].append(obj)
            else:  # finish time is not clear yet
                # NOTE, there is a chance that the invocation has not marked its routed invoker
                try:
                    invoker_id = invocation_2_invoker[invocation_id]
                    func_2_invoker2activationList[obj.func][invoker_id].append(obj)
                except KeyError as e:
                    logging.error(f"Missing routed invoker info for invocation: {obj}")
                    assert False
        for func, _dict in func_2_invoker2activationList.items():
            for invoker, activation_lst in _dict.items():
                activation_lst.sort(key=lambda x: x.arrival_time)
                print(f"#############==========>Activation List For {func} invoker {invoker}:")
                print(list(map(str, activation_lst)))
