from typing import Dict, List, Tuple
import numpy as np


def compute_reward_using_overall_stat(type_2_utilizations: dict, func_2_latencies,
                                      server_power_specs: Dict[str, Dict[str, float]], latency_factor:float)->Dict[str,float]:
    '''
    compute the reward based on overall stat instead of the sum of  per invocation reward
    :param type_2_utilizations:
    :param func_2_latencies:  contains latency for all function, not just active function
    :return:
    '''
    rewards = {}
    preserve_ratio = compute_latency_reward_overall_stat(func_2_latencies)
    power_ratio = compute_cluster_power_ratio(type_2_utilizations=type_2_utilizations,
                                              server_power_specs=server_power_specs)
    rewards['power'] = -power_ratio * (1 - latency_factor)
    rewards['sla'] = preserve_ratio * latency_factor
    rewards['all'] = rewards['sla'] + rewards['power']
    return rewards


def compute_latency_reward_overall_stat(funcid_2_latencies: Dict[int, List[Tuple]]):
    preserve_ratios = []  # preserve ratio for different functions
    for func, sla_latencies in funcid_2_latencies.items():
        if len(sla_latencies) == 0:
            continue
        latency_lst = []
        # fill all the latency result
        for sla, latency, _ in sla_latencies:  # _ is type
            latency_lst.append(latency)
        latency_p99 = np.percentile(latency_lst, 99)
        if latency_p99 < sla:# NOTE, assuming all invocation for the same function have the same SLA
            preserve_ratios.append(1)  # no violation
        else:
            preserve_ratios.append(sla / latency_p99)
    return np.mean(preserve_ratios)  # result should be between 0 and 1


def compute_cluster_power_ratio(type_2_utilizations: dict, server_power_specs: Dict[str, Dict[str, float]]):
    total = 0
    peak = 0
    for type, utils in type_2_utilizations.items():
        for util in utils:
            total += compute_power(server_power_specs[type]['static'], server_power_specs[type]['peak'], util)
        peak += server_power_specs[type]['peak'] * len(utils)
    return total / peak


def compute_power(static, peak, utilization):
    return static + (peak - static) * utilization
