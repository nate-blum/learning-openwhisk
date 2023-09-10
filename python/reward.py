import logging
from typing import Dict, Callable
import numpy as np
from collections import defaultdict
import time
from training_configs import SLA_PERCENTAGE
from invocation_store import InvocationStore


class Reward:
    def __init__(self, cluster):
        self.cluster = cluster
        # in order to not missing any record in the db, the db query window has small overlap. The following is used
        # to guarantee no duplicated record is processed
        self.activation_curr_round: set[str] = set()
        self.activation_last_round:set[str] = set()

    def compute_reward_using_overall_stat(self, db_activations: list,
                                          func_2_invocation2Arrival: dict[str, dict[str, int]],
                                          func_2_sla, get_power: Callable,
                                          cluster_peak_pw: float, latency_factor: float,
                                          activation_2_invoker: dict[str, int], invocation_store: InvocationStore) -> \
    tuple[
        Dict[str, float], dict[str, float], defaultdict[str, defaultdict[int, list]]]:
        # pass a callable so that delay the computation of power as late as possible to collect as many records as possible

        rewards = {}
        #  Contains Queued,  Not Contain Queued
        preserve_ratio, func_2_tail_latency, func_2_invoker2latencyList = self.compute_latency_reward_ratio_based_wsk(
            db_activations, func_2_invocation2Arrival, func_2_sla, activation_2_invoker, invocation_store)
        power_ratio = self.compute_cluster_power_ratio(total_pw=get_power(), cluster_peak=cluster_peak_pw)
        rewards['power'] = -power_ratio * (1 - latency_factor)
        rewards['sla'] = preserve_ratio * latency_factor
        rewards['all'] = rewards['sla'] + rewards['power']
        return rewards, func_2_tail_latency, func_2_invoker2latencyList

    def compute_latency_reward_ratio_based_wsk(self, db_activations: list,
                                               func_2_invocation2Arrival: dict[str, dict[str, int]],
                                               func_2_sla, activation_2_invoker: dict[str, int],
                                               invocation_store: InvocationStore) -> tuple[
        float, dict[str, float], defaultdict[str, defaultdict[int, list]]]:
        func_2_invoker2Latency: defaultdict[str, defaultdict[int, list]] = defaultdict(
            lambda: defaultdict(list))
        func_2_latencyList: defaultdict[str, list] = defaultdict(list)  # {function: [...latencies...]}
        # process latency record in the database
        num_db_record = len(db_activations)
        num_local_invocation_record = sum([len(i) for i in func_2_invocation2Arrival.values()])
        _validation_early_arrival_db_record = defaultdict(
            lambda: defaultdict(lambda: int(1e20)))  # dummy max {func:{invoker: time}}
        for activation in db_activations:
            if activation['name'][:23] == 'invokerHealthTestAction':
                continue
            func_name = activation['name']  # TODO, make sure the function name is what has been registered
            activation_id = activation['activationId']
            if activation_id in self.activation_last_round: # duplicated record, has been processed, skip
                continue
            else:
                self.activation_curr_round.add(activation_id)
            waitTime = None
            for item in activation['annotations']:
                if item['key'] == 'waitTime':
                    waitTime = item['value']
                    break
            latency = activation['duration'] + waitTime  # in millisecond
            func_2_latencyList[func_name].append(latency)
            try:
                invoker_id_int = activation['instanceId']
                func_2_invoker2Latency[func_name][invoker_id_int].append(latency)
            except KeyError:
                invoker_id_int = activation_2_invoker[activation_id]
                func_2_invoker2Latency[func_name][invoker_id_int].append(latency)
                logging.warning(
                    f"Missing instanceId info in db for: {activation_id} on {activation_2_invoker[activation_id]}")
            if activation['end'] - latency < _validation_early_arrival_db_record[func_name][invoker_id_int]:
                _validation_early_arrival_db_record[func_name][invoker_id_int] = activation['end'] - latency
            invocation_store.set_finish_time(invocation_id=activation_id, finish_time=activation['end'],
                                             invoker=invoker_id_int)
            # remove the activation queried from database respond from local invocation dict
            # the db record must be in the local invocation dict
            try:
                del func_2_invocation2Arrival[func_name][activation['activationId']]
            except Exception as e:
                logging.error(f"Delete activation record failed (db record must be in local arrival dict): {e}")
                assert False
        self.activation_last_round = self.activation_curr_round.copy()
        self.activation_curr_round.clear()
        _validate_FIFO_execution(func_2_invocation2Arrival, _validation_early_arrival_db_record, activation_2_invoker)
        invocation_store.check_invocation_fifo(activation_2_invoker)
        num_local_invocation_record_after = sum([len(i) for i in func_2_invocation2Arrival.values()])
        # logging.info(
        #     f"func_2_invocation2Arrival # before: {num_local_invocation_record} # after:{num_local_invocation_record_after}, # db queried: {num_db_record}")
        # include activation that are still in the local activation dict (in the queue)
        curr_time = time.time_ns()
        for func, invocation2Arrival in func_2_invocation2Arrival.items():
            for invocation, arrivalTime in invocation2Arrival.items():
                func_2_latencyList[func].append(
                    round((curr_time - arrivalTime) / 1_000_000))  # convert nanosecond to millisecond
        preserve_ratios = []
        func_2_tail_latency: dict[str, float] = {}
        for func, latency_lst in func_2_latencyList.items():
            if not latency_lst:
                continue
            p99 = np.percentile(latency_lst, SLA_PERCENTAGE)
            func_2_tail_latency[func] = p99
            if p99 < func_2_sla[func]:
                preserve_ratios.append(1)
            else:
                preserve_ratios.append(func_2_sla[func] / p99)
        return float(np.mean(preserve_ratios)), func_2_tail_latency, func_2_invoker2Latency

    def compute_cluster_power_ratio(self, total_pw: float, cluster_peak: float):
        return total_pw / cluster_peak

    def compute_power(self, static, peak, utilization):
        return static + (peak - static) * utilization

    # def compute_latency_reward_overall_stat(funcid_2_latencies: Dict[int, List[Tuple]]):
    #     preserve_ratios = []  # preserve ratio for different functions
    #     for func, sla_latencies in funcid_2_latencies.items():
    #         if len(sla_latencies) == 0:
    #             continue
    #         latency_lst = []
    #         # fill all the latency result
    #         for sla, latency, _ in sla_latencies:  # _ is type
    #             latency_lst.append(latency)
    #         latency_p99 = np.percentile(latency_lst, 99)
    #         if latency_p99 < sla:  # NOTE, assuming all invocation for the same function have the same SLA
    #             preserve_ratios.append(1)  # no violation
    #         else:
    #             preserve_ratios.append(sla / latency_p99)
    #     return np.mean(preserve_ratios)  # result should be between 0 and 1


def _validate_FIFO_execution(func_2_invocation2Arrival: dict[str, dict[str, int]],
                             _validation_early_arrival_db_record: defaultdict[str, defaultdict],
                             activation_2_invoker: dict[str, int]):
    for func, invo2Arrival in func_2_invocation2Arrival.items():
        for invo, arrival in invo2Arrival.items():
            # db has at least one record
            if _validation_early_arrival_db_record[func][activation_2_invoker[invo]] != int(1e20) and round(
                    arrival / 1_000_000) < _validation_early_arrival_db_record[func][activation_2_invoker[invo]]:
                logging.error(
                    f"@@@@@@@@@@@@@==>Record with arrival time older than current older db arrival exist, probably indicating Not FIFO: "
                    f"time difference (millisecond):{_validation_early_arrival_db_record[func][activation_2_invoker[invo]] - round(arrival / 1_000_000)}, activationID:{invo}")
