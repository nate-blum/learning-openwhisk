import logging
from typing import Dict, List, Tuple, Callable
import numpy as np
from collections import defaultdict
import time


def compute_reward_using_overall_stat(db_activations: list, func_2_invocation2Arrival: dict[str, dict[str, int]],
                                      func_2_sla, get_power: Callable,
                                      cluster_peak_pw: float, latency_factor: float) -> Dict[
    str, float]:
    # pass a callable so that delay the computation of power as late as possible to collect as many records as possible

    rewards = {}
    preserve_ratio = compute_latency_reward_ratio_based_wsk(db_activations,func_2_invocation2Arrival,func_2_sla)
    power_ratio = compute_cluster_power_ratio(total_pw=get_power(), cluster_peak=cluster_peak_pw)
    rewards['power'] = -power_ratio * (1 - latency_factor)
    rewards['sla'] = preserve_ratio * latency_factor
    rewards['all'] = rewards['sla'] + rewards['power']
    return rewards


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


def compute_latency_reward_ratio_based_wsk(db_activations: list, func_2_invocation2Arrival: dict[str, dict[str, int]],
                                           func_2_sla):
    func_2_type2Latency = defaultdict(lambda: defaultdict(list))
    latency_dict = defaultdict(list)  # {function: [...latencies...]}
    # process latency record in the database
    for activation in db_activations:
        if activation['name'][:23] == 'invokerHealthTestAction':
            continue
        func_name = activation['name']  # TODO, make sure the function name is what has been registered
        waitTime = None
        for item in activation['annotations']:
            if item['key'] == 'waitTime':
                waitTime = item['value']
                break
        latency = activation['duration'] + waitTime  # in millisecond
        latency_dict[func_name].append(latency)
        #func_2_type2Latency[func_name][activation['type']].append(latency) #TODO, to finish
        # remove the activation queried from database respond from local invocation dict
        # the db record must be in the local invocation dict
        try:
            del func_2_invocation2Arrival[func_name][activation['activationId']]
        except Exception as e:
            logging.error(f"Delete activation record failed: {e}")
            assert False
    # compute latency for activation that are still in the local activation dict
    curr_time = time.time_ns()
    for func, invocation2Arrival in func_2_invocation2Arrival:
        for invocation, arrivalTime in invocation2Arrival.items():
            latency_dict[func].append(round((curr_time - arrivalTime) / 1_000_000))  # convert nanosecond to millisecond
    preserve_ratios = []
    for func, latency_lst in latency_dict.items():
        if not latency_lst:
            continue
        p99 = np.percentile(latency_lst, 99)
        if p99 < func_2_sla[func]:
            preserve_ratios.append(1)
        else:
            preserve_ratios.append(func_2_sla[func] / p99)
    return np.mean(preserve_ratios)


def compute_cluster_power_ratio(total_pw: float, cluster_peak: float):
    return total_pw / cluster_peak


def compute_power(static, peak, utilization):
    return static + (peak - static) * utilization
