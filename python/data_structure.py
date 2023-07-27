from typing import NamedTuple, List
from enum import Enum

class LatencyInfo(NamedTuple):
    sla: int
    arrival_time: int
    start_time: int
    latency: int
    type:str # TODO, make sure we can get the type info

class RoutingResult(Enum):
    DISCARD = 0

class EnvConfigs(NamedTuple):
    num_active_func: int
    action_mapping_boundary: int # only for container creation or deletion
    type_mapping_boundary: List[int]
    server_type_lst: List[str]

class Action(NamedTuple):
    container_delta: int
    freq: int = 3000
    type: str = None
    target_load: float = 1.0
