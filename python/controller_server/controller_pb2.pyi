from google.protobuf import wrappers_pb2 as _wrappers_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class EmptyRequest(_message.Message):
    __slots__ = []
    def __init__(self) -> None: ...

class GetArrivalInfoResponse(_message.Message):
    __slots__ = ["query_count_1s", "query_count_5s"]
    class QueryCount1sEntry(_message.Message):
        __slots__ = ["key", "value"]
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: int
        value: int
        def __init__(self, key: _Optional[int] = ..., value: _Optional[int] = ...) -> None: ...
    class QueryCount5sEntry(_message.Message):
        __slots__ = ["key", "value"]
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: int
        value: int
        def __init__(self, key: _Optional[int] = ..., value: _Optional[int] = ...) -> None: ...
    QUERY_COUNT_1S_FIELD_NUMBER: _ClassVar[int]
    QUERY_COUNT_5S_FIELD_NUMBER: _ClassVar[int]
    query_count_1s: _containers.ScalarMap[int, int]
    query_count_5s: _containers.ScalarMap[int, int]
    def __init__(self, query_count_1s: _Optional[_Mapping[int, int]] = ..., query_count_5s: _Optional[_Mapping[int, int]] = ...) -> None: ...

class GetInvocationRouteRequest(_message.Message):
    __slots__ = ["actionName"]
    ACTIONNAME_FIELD_NUMBER: _ClassVar[int]
    actionName: str
    def __init__(self, actionName: _Optional[str] = ...) -> None: ...

class GetInvocationRouteResponse(_message.Message):
    __slots__ = ["invokerHost"]
    INVOKERHOST_FIELD_NUMBER: _ClassVar[int]
    invokerHost: str
    def __init__(self, invokerHost: _Optional[str] = ...) -> None: ...

class InvokerStateByAction(_message.Message):
    __slots__ = ["invokerStateByAction"]
    class InvokerStateByActionEntry(_message.Message):
        __slots__ = ["key", "value"]
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: ContainerStatesByInvoker
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[ContainerStatesByInvoker, _Mapping]] = ...) -> None: ...
    INVOKERSTATEBYACTION_FIELD_NUMBER: _ClassVar[int]
    invokerStateByAction: _containers.MessageMap[str, ContainerStatesByInvoker]
    def __init__(self, invokerStateByAction: _Optional[_Mapping[str, ContainerStatesByInvoker]] = ...) -> None: ...

class ContainerStatesByInvoker(_message.Message):
    __slots__ = ["containerStatesByInvoker"]
    class ContainerStatesByInvokerEntry(_message.Message):
        __slots__ = ["key", "value"]
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: ContainerStateCounts
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[ContainerStateCounts, _Mapping]] = ...) -> None: ...
    CONTAINERSTATESBYINVOKER_FIELD_NUMBER: _ClassVar[int]
    containerStatesByInvoker: _containers.MessageMap[str, ContainerStateCounts]
    def __init__(self, containerStatesByInvoker: _Optional[_Mapping[str, ContainerStateCounts]] = ...) -> None: ...

class ContainerStateCounts(_message.Message):
    __slots__ = ["stateCounts"]
    class StateCountsEntry(_message.Message):
        __slots__ = ["key", "value"]
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: int
        def __init__(self, key: _Optional[str] = ..., value: _Optional[int] = ...) -> None: ...
    STATECOUNTS_FIELD_NUMBER: _ClassVar[int]
    stateCounts: _containers.ScalarMap[str, int]
    def __init__(self, stateCounts: _Optional[_Mapping[str, int]] = ...) -> None: ...

class ClusterState(_message.Message):
    __slots__ = ["healthyInvokerCount", "states"]
    HEALTHYINVOKERCOUNT_FIELD_NUMBER: _ClassVar[int]
    STATES_FIELD_NUMBER: _ClassVar[int]
    healthyInvokerCount: int
    states: InvokerStateByAction
    def __init__(self, healthyInvokerCount: _Optional[int] = ..., states: _Optional[_Union[InvokerStateByAction, _Mapping]] = ...) -> None: ...

class UpdateClusterStateRequest(_message.Message):
    __slots__ = ["clusterState"]
    CLUSTERSTATE_FIELD_NUMBER: _ClassVar[int]
    clusterState: ClusterState
    def __init__(self, clusterState: _Optional[_Union[ClusterState, _Mapping]] = ...) -> None: ...

class UpdateClusterStateResponse(_message.Message):
    __slots__ = []
    def __init__(self) -> None: ...
