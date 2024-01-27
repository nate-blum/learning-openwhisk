import os
import sys
sys.path.append("../")
import config_local
CONFIG_NOTE = "twoRealFuncs"

# separate in case a function can have multiple SLA
# workload_characteristics = {
#     'sla': {
#         'func1': 500,
#         'func2': 1000
#     }
# }

server_power_specs = {
    'xs': {'static': 20, 'peak': 100, 'num_core': 16},
    'xe': {'static': 20, 'peak': 130, 'num_core': 8}
}
pdu_outlets = [ 11, 23, 24]

default_svr_type = 'xe'

# used for generate workload
workload_characteristics = {
    # 'sla': {
    #     'img-resize': 150,
    #     'ocr-img': 4000
    #},
    'type': {
        'img-resize': 'binary',
        'ocr-img': 'binary'
    },
    'data_file': {
        'img-resize': '/local/kuozhang-local/nsf-backup/faas-profiler/functions/img-resize/piton.png',
        'ocr-img': '/local/kuozhang-local/nsf-backup/faas-profiler/functions/ocr-img/pitontable.jpg'
    }
}

workload_config= {
    'workload_line_start': 0,
    'random_start': True,
    #'trace_file': os.path.join(trace_root, 'faas_top5funcs_day2_scaledown_300.txt')
    #'trace_file': os.path.join(trace_root, 'workload_multiple_funcs_43111_range18000000_scale80.csv')
    # 'trace_file': os.path.join(trace_root, 'workload_range3600000_scale100.csv')
    #'trace_file': os.path.join(config_local.project_root,'python/workload/workload_dummy_range1hour_scale200.csv')
    #'trace_file': os.path.join(config_local.project_root, 'python/workload/workload_real_funcs_range_constant_rate200_1hour.csv')
     'trace_file': os.path.join(config_local.project_root, 'python/workload/workload_real_funcs_range1hour_scale400.csv')
}

func_spec_dict = {
    "img-resize": {'name': "img-resize", 'namesp': "guest", 'mem_req': 256, 'cpu_req': 2,
                   'cpu_intensive': True, 'mem_intensive': False, 'io_intensive': False,
                   'invoker_2_referenceExecTime': {'xs': 170, 'xe': 110},
                   'sla': 170},
    "ocr-img": {'name': "ocr-img", 'namesp': "guest", 'mem_req': 256, 'cpu_req': 2,
                'cpu_intensive': True, 'mem_intensive': False, 'io_intensive': False,
                'invoker_2_referenceExecTime': {'xs': 3600, 'xe': 2400},
                'sla': 8000},
}


#NOTE, it must match the order defined in ansible host
cluster_spec_dict = {
    # "xe": [{"count": 1, "mem_capacity": 20000, "num_cores": 8, "per_core_dvfs": 1, "max_freq": 3000,
    #          "min_freq": 2000, "desired_freq": 3000, "host": f"panic-cloud-xs-{i:02d}.cs.rutgers.edu",
    #          "max_pinned_container_per_core": 2} for i in range(2)],
    "xe": [{"count": 1, "mem_capacity": 20000, "num_cores": 8, "per_core_dvfs": 1, "max_freq": 3000,
            "min_freq": 2000, "desired_freq": 3000, "host": f"panic-cloud-xe3nv-03.cs.rutgers.edu",
            "max_pinned_container_per_core": 2}
           ],
    "xs": [{"count": 1, "mem_capacity": 20000, "num_cores": 16, "per_core_dvfs": 1, "max_freq": 3000,
            "min_freq": 2000, "desired_freq": 3000, "host": f"panic-cloud-xs-06.cs.rutgers.edu",
            "max_pinned_container_per_core": 2}
           ],
}

state_clip_value = 30000

