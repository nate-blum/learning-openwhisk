from typing import Final
from invoker_client import invoker_pb2 as invoker_types
from invoker_client.invoker_pb2 import DeleteContainerWithIdRequest, SuccessResponse, ResetInvokerRequest
from invoker_client import invoker_pb2_grpc as invoker_service
import grpc
import pickle
import logging

class Core:
    def __init__(self, id, max_freq, min_freq, desired_freq):
        self.id: int = id
        self.num_pinned_container: int = 0
        self.max_freq: int = max_freq
        self.min_freq: int = min_freq
        self.desired_freq: int = desired_freq

    def __str__(self):
        return f"NP:{self.num_pinned_container}"


class Invoker:
    RPC_PORT = "50051"

    def __init__(self, id, host, type, mem_capacity, num_cores, core_spec_dict: dict[int, dict],
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
        self.id_2_core: dict[int, Core] = {}
        for id, spec in core_spec_dict.items():
            self.id_2_core[id] = Core(**spec)

        # -------------rpc channel-----------------
        self.channel = grpc.insecure_channel(f"{self.hostname}:{self.RPC_PORT}")
        self.stub = invoker_service.InvokerServiceStub(channel=self.channel)
        # -------------------------------------------
        self.py_runtime_client = None

    def __str__(self) -> str:
        core_str = "_".join([core.__str__() for core in self.id_2_core.values()])
        return (
            f"id:{self.id},type:{self.type},num_container:[w{self.num_warm_container}b{self.num_busy_container}wi{self.num_warming_container}] [{core_str}]")

    def __repr__(self) -> str:
        return self.__str__()

    def rpyc_get_container_stats(self) -> dict:
        return pickle.loads(self.py_runtime_client.root.get_container_utilization())

    def rpyc_reset_container_util_collecting_runtime(self):
        self.py_runtime_client.root.reset()

    def rpyc_get_container_ids(self)->list[str]:
        return pickle.loads(self.py_runtime_client.root.get_container_ids())

    def rpc_add_container(self, action_name: str, pinned_core: list[int]) -> None:
        # logging.info(f"Starting a new container for {action_name}, pin core: {pinned_core} on {self.id}")
        resp = self.stub.NewWarmedContainer(invoker_types.NewWarmedContainerRequest(actionName=action_name,
                                                                                    corePin=",".join(
                                                                                        [str(core) for core in
                                                                                         pinned_core]),
                                                                                    #corePin= "",
                                                                                    params={}))
        # logging.info(f"adding container res: {resp}")

    def rpc_delete_container(self, container_id: str, func_name: str):
        # TODO, how the Success Response is determined from Scala runtime
        return
        response: SuccessResponse = self.stub.DeleteContainerWithId(
            DeleteContainerWithIdRequest(containerId=container_id))

    def rpc_reset_invoker(self):
        res = self.stub.ResetInvoker(ResetInvokerRequest())
        logging.info(f"Resetting the invoker: {self.id}")
        return res

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
    def __init__(self, str_id: str, pinned_core: list[Core], invoker: Invoker):
        self.id: str = str_id
        self.pinned_core: list[Core] = pinned_core
        self.invoker: Invoker = invoker

    def __repr__(self):
        return f"id:{self.id},pinnedCore:{[c.id for c in self.pinned_core]},invk_id:{self.invoker.id}"

class Func:
    def __init__(self, id, namesp, name, mem_req, cpu_req, sla, invoker_2_referenceExecTime):
        self.id = id
        self.namesp = namesp
        self.name = name
        self.mem_req = mem_req
        self.cpu_req = cpu_req
        self.sla = sla
        self.invokerType_2_referenceExecTime: dict[str, int] = invoker_2_referenceExecTime