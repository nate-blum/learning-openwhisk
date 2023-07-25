CONFIG_NOTE = "cpu_req=1_"
cluster_state_name = 'cluster_state'
server_power_specs = {
    'xs': {'static': 20, 'peak': 100, 'num_core': 16},
    'xe': {'static': 20, 'peak': 130, 'num_core': 8}
}

cluster_spec_dict = {"xs": {"count": 2, "mem_capacity": 20000, "num_cores": 16, "per_core_dvfs": 1, "max_freq": 3000,
                            "min_freq": 2000, "desired_freq": 3000},
                     "xe": {"count": 2, "mem_capacity": 20000, "num_cores": 8, "per_core_dvfs": 1, "max_freq": 3000,
                            "min_freq": 2000, "desired_freq": 3000}
                     }
default_svr_type = 'xe'
# reward_weight = {
#     'latency': 1,
#     'power': 50
# }

# separate in case a function can have multiple SLA
workload_characteristics = {
    'sla': {
        'func0': 1000,
        'func1': 250,
        'func2': 1000,
        'func3': 250,
        'func4': 1000,
    }
}

func_spec_dict = {
    "func0": {'name': "func0", 'namesp': "default", 'mem_req': 1000, 'cpu_req': 1,
              'cpu_intensive': True, 'mem_intensive': False, 'io_intensive': False,
              'container_start_latency': 1000, 'start_up_cpu_ratio': 1, 'invoker_2_duration': {'xs': 800, 'xe': 500}},

    "func1": {'name': "func1", 'namesp': "default", 'mem_req': 1000, 'cpu_req': 1,
              'cpu_intensive': True, 'mem_intensive': False, 'io_intensive': False,
              'container_start_latency': 1000, 'start_up_cpu_ratio': 1, 'invoker_2_duration': {'xs': 300, 'xe': 200}},

    "func2": {'name': "func2", 'namesp': "default", 'mem_req': 1000, 'cpu_req': 1,
              'cpu_intensive': True, 'mem_intensive': False, 'io_intensive': False,
              'container_start_latency': 1000, 'start_up_cpu_ratio': 1, 'invoker_2_duration': {'xs': 800, 'xe': 500}},

    "func3": {'name': "func3", 'namesp': "default", 'mem_req': 1000, 'cpu_req': 1,
              'cpu_intensive': True, 'mem_intensive': False, 'io_intensive': False,
              'container_start_latency': 1000, 'start_up_cpu_ratio': 1, 'invoker_2_duration': {'xs': 300, 'xe': 200}},

    "func4": {'name': "func4", 'namesp': "default", 'mem_req': 1000, 'cpu_req': 1,
              'cpu_intensive': True, 'mem_intensive': False, 'io_intensive': False,
              'container_start_latency': 1000, 'start_up_cpu_ratio': 1, 'invoker_2_duration': {'xs': 800, 'xe': 500}},
}

#NOTE, this must match the get_obs method in the environment
input_space_spec = {
    'func_state_dim': 5 + 5 * len(cluster_spec_dict),
    'cluster_state_dim': 1 * len(cluster_spec_dict),
    'n_func': 2 # active function
}

for k in server_power_specs.keys():
    assert (server_power_specs[k]['num_core'] == cluster_spec_dict[k]['num_cores'])
