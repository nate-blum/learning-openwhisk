import os
import config_local
import config
trace_root = config_local.trace_root_path
from sub_config import config_two_func

wandb_group_name = "RealOpenWhiskTraining2Funcs"

SLA_PERCENTAGE = 99
SLOT_DURATION_SECOND = 2
num_envs = 6
trajectory_len = 40


max_num_update = 4000
#init_std = [50 ** 2, 50 ** 2, 50 ** 2, 50 ** 2]  # for testing purpose
init_std = [10 ** 2, 10 ** 2, 10 ** 2, 10 ** 2]  # [func1Delta, func1Type, func2Delta, func2Type]
min_std = [0.5, 0.5, 0.5, 0.5]
std_decay = 0.9998
#-----------------Action Mapping------------------------------------
action_mapping_boundary = 10
type_mapping_boundary = [0]
#-------------------------------------------------------------------


select_func_params = {
    'more_than_2_funcs': False,
    # [********>this affect the features<********]
    'arrival_delta_window_size_millis': 60000,  # should be less than the arrival_q_time_range_limit (1 min)
    'ema_coeff': 0.4,
    'bucket_millis': 2000  # 2 second
}
select_func_weight = {
    # whether to use random selection
    'random_select': False,
    #-------------------
    'arrival_delta': 0.4,
    'cold_start': 0,
    'latency_slack': 0.6
}

workload_config = {
    'workload_line_start': 0,
    'random_start': True,
    #'trace_file': os.path.join(trace_root, 'faas_top5funcs_day2_scaledown_300.txt')
    #'trace_file': os.path.join(trace_root, 'workload_multiple_funcs_43111_range18000000_scale80.csv')
    # 'trace_file': os.path.join(trace_root, 'workload_range3600000_scale100.csv')
    'trace_file': config_two_func.workload_file
}
initialize_env = {
    'whether_initialize_env': True,
    'warm_cnt_per_type': 1
}
log_mode = 'warn'

reward_setting = {
    'reward_func': 'ratio',  # ratio or naive
    'latency_factor': 0.5,  # effective only when reward func is ratio, a value in (0,1)
    # -------------------------------------------------
    # only for naive reward function
    'naive_pow_coeff': 10,
    'naive_sla_coeff': 1,
    'clip_sla': 3000 # whether to clip sla reward if the reward value is too large, 0 means no clip
}
note = f"rewardRatio{reward_setting['latency_factor']}_T{trajectory_len}_2ContainerPerCore_" + config.CONFIG_NOTE

NN = {
    'activation': 'elu',  # *** impact factor ***
    'lr': 3e-4,  # *** impact factor ***
    'gamma': 1,
    'clip': 0.2,
    'hidden_size': 64,
    'normalize_method': 'greenDRL',  # 'batch' (like stable baseline PPO) or 'no_normalize' or 'greenDRL' # *** impact factor ***
    'state_clip': True,
    # ------------ for stable baseline like mini-batch update--------------
    'epoch': 1,
    'minibatch_sz': None
}
# for define features and related
params = {
    'util_update_interval': 1000,  # 1 second, note this should be a constant
    'util_q_size': 10,  # this should match the slot duration, at least all records in a slot (10s)
    # use the latency information for invocations within this second to compute the state feature
    'latency_record_range_cared': 2000, # [********>this affect the features, consider this with slot duration<********]
    'latency_record_time_range_limit': 10000,  # 10 second,latency & execution time queue size limit
    'arrival_q_time_range_limit': 150000,  # 2.5 min
    'relax_deletion_requirement': 0,  # no relax
    'Discard_Invocation_SlaViolation_Factor': 3,
    # if the value is 3, meaning the discard invocation has 3 * sla latency
    'consider_core_interference': 1,  # whether allowing multiple containers pinned to the same cores
    'consider_container_pinning_per_core_limit': 1,  # will be converted to boolean
    'max_pinned_container_per_core': 2  # should be used with the previous together
}

eval_config = {
    'T': 30,
    'std': [0.1, 0.1, 0.1, 0.1],
    'workload_line_start': 200,
    'log_mode': 'warn',
    'eval_every_n_update': 2
}
