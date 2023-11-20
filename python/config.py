#from sub_config import config_dummy_two_func_0406 as sub_config
#from sub_config import config_two_func_0406 as sub_config
from sub_config import config_two_func as sub_config # xe and xs-06
#CONFIG_NOTE = "realWsk"
CONFIG_NOTE = sub_config.CONFIG_NOTE
#cluster_state_name = 'cluster_state'
server_power_specs = sub_config.server_power_specs
pdu_outlet_list = sub_config.pdu_outlets

cluster_spec_dict = sub_config.cluster_spec_dict
default_svr_type = sub_config.default_svr_type


#workload_characteristics = sub_config.workload_characteristics
workload_config = sub_config.workload_config

func_spec_dict = sub_config.func_spec_dict



# NOTE, this must match the get_obs method in the environment
input_space_spec = {
    'func_state_dim': 6 + 7 * len(cluster_spec_dict),
    #'func_state_dim': 6 + 4 * len(cluster_spec_dict),
    'cluster_state_dim': 1 * len(cluster_spec_dict),
    'n_func': 2  # active function
}

for k in server_power_specs.keys():
    for spec_dic in cluster_spec_dict[k]:
        assert (server_power_specs[k]['num_core'] == spec_dic['num_cores'])
