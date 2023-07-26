from typing import NamedTuple
from enum import Enum

class LatencyInfo(NamedTuple):
    sla: int
    arrival_time: int
    start_time: int
    latency: int
    type:str # TODO, make sure we can get the type info

class RoutingResult(Enum):
    DISCARD = 0