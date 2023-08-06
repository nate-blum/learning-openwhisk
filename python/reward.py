from typing import Dict, List, Tuple,Callable
import numpy as np


def compute_reward_using_overall_stat(get_power:Callable , func_2_latencies,
                                      cluster_peak_pw: float, latency_factor: float) -> Dict[
    str, float]:
    # pass a callable so that delay the computation of power as late as possible to collect as many records as possible

    rewards = {}
    preserve_ratio = compute_latency_reward_overall_stat(func_2_latencies)
    power_ratio = compute_cluster_power_ratio(total_pw= get_power(), cluster_peak=cluster_peak_pw)
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
        if latency_p99 < sla:  # NOTE, assuming all invocation for the same function have the same SLA
            preserve_ratios.append(1)  # no violation
        else:
            preserve_ratios.append(sla / latency_p99)
    return np.mean(preserve_ratios)  # result should be between 0 and 1


def compute_cluster_power_ratio(total_pw: float, cluster_peak: float):
    return total_pw / cluster_peak


def compute_power(static, peak, utilization):
    return static + (peak - static) * utilization
