# Minibatch update is like stable baseline PPO
import sys
import os
import time
import uuid
from pprint import pprint
import logging
from typing import Tuple, NamedTuple
import numpy as np
import wandb
from environment import Cluster
# ------------ might need to be config ----------------------------
import training_configs
import config
# ------------ might need to be config ----------------------------
import torch
import torch.nn as nn
from torch import Tensor
import utility
import config_local

# torch.manual_seed(0)
# set device to cpu or cuda
DEVICE = torch.device('cpu')
if (torch.cuda.is_available()):
    DEVICE = torch.device('cuda:0')
    torch.cuda.empty_cache()
    print("Device set to : " + str(torch.cuda.get_device_name(DEVICE)))
else:
    print("Device set to : cpu")


class ActorSimple(nn.Module):
    def __init__(self, func_state_dim, num_func, cluster_state_dim, action_dim, hidden_size,
                 activation):
        super().__init__()
        self.num_func = num_func
        self.activation = nn.Tanh() if activation == 'tanh' else nn.ELU()
        self.fully_nn_input_size = func_state_dim * num_func + cluster_state_dim
        self.actor_nn = nn.Sequential(
            nn.Linear(self.fully_nn_input_size, hidden_size),
            self.activation,
            nn.Linear(hidden_size, hidden_size),
            self.activation,
            nn.Linear(hidden_size, action_dim)
        )

    def act(self, state: Tensor, std: Tensor):
        policy_dist = self._get_policy_dist(state, std)
        action = policy_dist.sample()
        action_logprob = policy_dist.log_prob(action)
        return action, action_logprob

    def act_deterministic(self, state: Tensor):
        mean = self.actor_nn(state)
        return mean

    def evaluate_action(self, states: Tensor, actions: Tensor, std: Tensor) -> Tensor:
        policy_dist = self._get_policy_dist(states, std)
        action_probs = policy_dist.log_prob(actions)
        return action_probs

    def _get_policy_dist(self, states: Tensor, std: Tensor):
        mean = self.actor_nn(states)
        cov = torch.diag(std)
        policy_dist = torch.distributions.MultivariateNormal(mean, covariance_matrix=cov)
        return policy_dist


class RolloutBufferSamples(NamedTuple):
    states: torch.Tensor
    actions: torch.Tensor
    old_log_prob: torch.Tensor
    advantages: torch.Tensor


class RolloutBuffer:
    def __init__(self, size, n_env, obs_dim, action_dim, ppo_obj, gamma):
        self.size = size
        self.n_env = n_env
        self.obs_dim = obs_dim
        self.action_dim = action_dim
        self.ppo_obj: PPO = ppo_obj
        self.gamma = gamma
        self.reset()

    def reset(self) -> None:
        self.actions = np.zeros((self.size, self.n_env, self.action_dim), dtype=np.float32)
        self.states = np.zeros((self.size, self.n_env, self.obs_dim), dtype=np.float32)
        self.logprobs = np.zeros((self.size, self.n_env), dtype=np.float32)
        self.rewards = np.zeros((self.size, self.n_env), dtype=np.float32)
        self.rewards_power = np.zeros((self.size, self.n_env), dtype=np.float32)
        self.rewards_sla = np.zeros((self.size, self.n_env), dtype=np.float32)
        self.utilizations = {'util_{}'.format(svr): np.zeros((self.size, self.n_env), dtype=np.float32) for svr in
                             self.ppo_obj.server_type_lst}
        self.advantages = np.zeros((self.size, self.n_env), dtype=np.float32)
        self.generator_ready = False

    @staticmethod
    def swap_and_flatten(arr: np.ndarray) -> np.ndarray:
        """
        Swap and then flatten axes 0 (buffer_size) and 1 (n_envs)
        to convert shape from [n_steps, n_envs, ...] (when ... is the shape of the features)
        to [n_steps * n_envs, ...] (which maintain the order)

        :param arr:
        :return:
        """
        shape = arr.shape
        if len(shape) < 3:
            shape = (*shape, 1)
        return arr.swapaxes(0, 1).reshape(shape[0] * shape[1], *shape[2:])

    def compute_advantage_time_dependent_baseline(self):
        # only calculate the advantage based on time-dependent baseline, no normalization
        T = self.size
        gains = np.zeros((T, self.n_env), dtype=np.float32)  # shape [T, n_envs]
        discounted_reward = np.zeros(self.n_env)
        for idx, reward in enumerate(reversed(self.rewards)):
            if self.gamma == 1:
                discounted_reward += reward
            else:
                discounted_reward = reward + (self.gamma * discounted_reward)
            gains[T - idx - 1] = discounted_reward

        baselines_mean = np.mean(gains, 1)  # [T,]
        baselines_mean = np.expand_dims(baselines_mean, 1)  # [T, 1]
        baselines_mean = np.repeat(baselines_mean, self.n_env, axis=1)  # [T, n_env]
        self.advantages = gains - baselines_mean  # [T, n_env]

    def compute_advantage_and_normalize_greenDRL(self):
        # compute the advantages using greenDRL like method
        T = self.size
        gains = np.zeros((T, self.n_env), dtype=np.float32)  # shape [T, n_envs]
        discounted_reward = np.zeros(self.n_env)
        for idx, reward in enumerate(reversed(self.rewards)):
            if self.gamma == 1:
                discounted_reward += reward
            else:
                discounted_reward = reward + (self.gamma * discounted_reward)
            gains[T - idx - 1] = discounted_reward

        baselines_mean = np.mean(gains, 1)  # [T,]
        baselines_std = np.std(gains, 1)  # [T, ]
        baselines_mean = np.expand_dims(baselines_mean, 1)  # [T, 1]
        baselines_std = np.expand_dims(baselines_std, 1)
        baselines_mean = np.repeat(baselines_mean, self.n_env, axis=1)  # [T, n_env]
        baselines_std = np.repeat(baselines_std, self.n_env, axis=1)
        self.advantages = (gains - baselines_mean) / (baselines_std + 1e-8)  # [T, n_env]

    def get(self, batch_size=None):
        indices = np.random.permutation(self.size * self.n_env)
        if not self.generator_ready:
            _tensor_names = [
                'states',
                'actions',
                'logprobs',
                'advantages'
            ]
            for tensor in _tensor_names:
                self.__dict__[tensor] = self.swap_and_flatten(self.__dict__[tensor])
            self.generator_ready = True
        total = self.size * self.n_env
        if batch_size is None:
            batch_size = total
        start_idx = 0
        while start_idx < total:
            yield self._get_samples((indices[start_idx: start_idx + batch_size]))
            start_idx += batch_size

    def _get_samples(self, batch_inds: np.ndarray) -> RolloutBufferSamples:
        data = (
            self.states[batch_inds],
            self.actions[batch_inds],
            self.logprobs[batch_inds].flatten(),  # bug fixed, the shape must match !!!
            self.advantages[batch_inds].flatten()
        )
        return RolloutBufferSamples(*tuple(map(torch.FloatTensor, data)))


class PPO:
    # using time dependent baseline to compute the advantage
    def __init__(self, env: Cluster, num_evn, func_state_dim, num_func, cluster_state_dim,
                 action_dim,
                 hidden_size, activation, clip, gamma, trajectory_len, init_stds, std_decay_rate, min_std,
                 max_n_update, batch_size, epoch, lr=3e-4,
                 train_mode: bool = True):
        self.time_stamp = utility.get_curr_time()
        self.setup_logging()
        self.num_traj = num_evn
        # self.eval_env: Cluster = eval_env
        # assert (num_evn > 1)
        self.env = env
        self.obs_dim = self._get_obs_dim(func_state_dim, num_func, cluster_state_dim)
        self.action_dim = action_dim
        self.policy = ActorSimple(func_state_dim, num_func, cluster_state_dim, action_dim, hidden_size,
                                  activation).to(DEVICE)
        self.optimizer = torch.optim.Adam(params=self.policy.actor_nn.parameters(),
                                          lr=lr)
        self.MseLoss = nn.MSELoss()

        self.stds: torch.Tensor = torch.FloatTensor(init_stds).to(DEVICE)
        self.gamma = gamma
        self.clip = clip
        self.trajectory_len = trajectory_len
        self.std_decay_rate = std_decay_rate
        self.min_std = min_std
        self.max_n_update = max_n_update
        self.train_mode = train_mode
        # self.cluster_state_name = self.env.get_attr('cluster_state_name')[0]
        self.server_type_lst = self.env.server_types
        self.n_epochs = epoch
        self.batch_size = batch_size
        self.buffers = RolloutBuffer(size=trajectory_len, n_env=num_evn, obs_dim=self.obs_dim, action_dim=action_dim,
                                     ppo_obj=self, gamma=self.gamma)
        self.normalize_method = training_configs.NN['normalize_method']
        self.training_update_cnt = 0

    def setup_logging(self):
        # file handler
        # file_handler = logging.FileHandler(os.path.join('logs', 'log_{}'.format(self.time_stamp)), mode='w')
        # file_logger_formatter = logging.Formatter('%(message)s')
        # file_handler.setFormatter(file_logger_formatter)
        # file_handler.setLevel(logging.INFO)
        # stream handler
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_logger_formatter = logging.Formatter('[%(asctime)s][%(levelname)s] %(message)s')
        stream_handler.setFormatter(stream_logger_formatter)
        # stream_handler.setLevel(logging.DEBUG)
        # must be called in main thread before any sub-thread starts
        # logging.basicConfig(level=logging.INFO, handlers=[stream_handler])
        logging.basicConfig(level=logging.DEBUG, handlers=[stream_handler])

    def _get_obs_dim(self, func_dim, num_func, cluster_obs_dim):
        return func_dim * num_func + cluster_obs_dim

    def select_action(self, states: np.ndarray, stds: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        # stds = torch.FloatTensor(stds).to(DEVICE)
        with torch.no_grad():
            states = torch.FloatTensor(states).to(DEVICE)
            actions, action_logprobs = self.policy.act(states, stds)
        return actions, action_logprobs

    def collect_rollouts_real_ow_cluster(self, T: int):
        for i in range(self.num_traj):
            logging.info(
                f"\n\n============================>Begin Trajectory ({self.training_update_cnt}-{i}) Rollout<=================================")
            state, info = self.env.reset(
                options={'workload_start': config.workload_config['workload_line_start'],
                         'random_start': config.workload_config['random_start']})
            for t in range(T):
                actions, action_logprobs = self.select_action(state, self.stds)  # return is of Tensor type
                self.buffers.states[t][i] = np.array(state).copy()
                actions_np = actions.detach().cpu().numpy()
                # [rewards_dict, rewards_dict2]
                state, reward, terminated, truncated, _ = self.env.step(actions_np,
                                                                        t == T - 1)  # NOTE, do I need detach
                self.buffers.actions[t][i] = np.array(actions_np).copy()
                # print(self.buffers.logprobs[t].shape,action_logprobs.clone().cpu().numpy().shape )
                self.buffers.logprobs[t][i] = action_logprobs.clone().cpu().numpy()
                rewards_all = reward['all']
                self.buffers.rewards[t][i] = rewards_all
                # self._extract_info_for_monitor(rewards, infos, t)
            logging.info(f"============================>Trajectory ({self.training_update_cnt}-{i}) Rollout Done<=================================")

    # def _extract_info_for_monitor(self, rewards: dict, infos: dict, t):
    #     # save rewards from every env
    #     rewards_power = [r['power'] for r in rewards]
    #     rewards_sla = [r['sla'] for r in rewards]
    #     self.buffers.rewards_power[t] = rewards_power
    #     self.buffers.rewards_sla[t] = rewards_sla
    #     # only extract from the first environment for utilization
    #     cluster_state = infos[self.cluster_state_name][0]
    #     res = {'util_{}'.format(svr_type): cluster_state['utilization_{}'.format(svr_type)] for svr_type in
    #            self.server_type_lst}
    #     for svr, util in res.items():
    #         self.buffers.utilizations[svr][t] = util

    def _compute_advantage(self) -> np.ndarray:
        T = self.trajectory_len
        gains = np.zeros((T, self.num_traj), dtype=np.float32)  # shape [T, n_envs]
        discounted_reward = np.zeros(self.num_traj)
        for idx, reward in enumerate(reversed(self.buffers.rewards)):
            if self.gamma == 1:
                discounted_reward += reward
            else:
                discounted_reward = reward + (self.gamma * discounted_reward)
            gains[T - idx - 1] = discounted_reward

        # NOTE, the following normalization is equivalent to normalizing the advantage,
        #  this is different than normalization is Stable baseline
        baselines_mean = np.mean(gains, 1)  # [T,]
        baselines_std = np.std(gains, 1)  # [T, ]
        baselines_mean = np.expand_dims(baselines_mean, 1)  # [T, 1]
        baselines_std = np.expand_dims(baselines_std, 1)
        baselines_mean = np.repeat(baselines_mean, self.num_traj, axis=1)  # [T, n_env]
        baselines_std = np.repeat(baselines_std, self.num_traj, axis=1)
        advantages = (gains - baselines_mean) / (baselines_std + 1e-8)  # [T, n_env]
        return advantages

    def decay_std(self):
        self.stds = self.stds * self.std_decay_rate
        for i in range(len(self.stds)):
            if self.stds[i].item() < self.min_std[i]:
                self.stds[i] = self.min_std[i]

    def train(self):
        wandb_init(self.time_stamp, self.read_all_config())
        while self.training_update_cnt < self.max_n_update:
            t = time.time()
            self.collect_rollouts_real_ow_cluster(self.trajectory_len)
            if training_configs.NN['normalize_method'] == 'greenDRL':
                self.buffers.compute_advantage_and_normalize_greenDRL()  # normalized by GreenDRL like method
            elif training_configs.NN['normalize_method'] == 'batch' or training_configs.NN[
                'normalize_method'] == 'no_normalize':
                self.buffers.compute_advantage_time_dependent_baseline()  # no normalize applied
            else:
                assert False
            for epoch in range(self.n_epochs):
                for rollout_data in self.buffers.get(self.batch_size):
                    actions = rollout_data.actions
                    log_prob = self.policy.evaluate_action(rollout_data.states, actions, self.stds)
                    advantages = rollout_data.advantages
                    if self.normalize_method == "batch":  # normalize using stable baseline method only when the method is batch
                        advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

                    ratio = torch.exp(log_prob - rollout_data.old_log_prob)
                    policy_loss_1 = advantages * ratio
                    policy_loss_2 = advantages * torch.clamp(ratio, 1 - self.clip, 1 + self.clip)
                    policy_loss = -torch.min(policy_loss_1, policy_loss_2).mean()
                    self.optimizer.zero_grad()
                    policy_loss.backward()
                    self.optimizer.step()

            # self.wandb_log(loss, num_update)
            # wandb.watch(self.policy, log="gradients", log_freq=20)
            if self.training_update_cnt % training_configs.eval_config['eval_every_n_update'] == 0:
                # print(ratio.detach().numpy())
                self.evaluate(self.training_update_cnt)
                # self.print_action_from_buffer()
            self.buffers.reset()
            self.decay_std()
            self.training_update_cnt += 1
            logging.info(
                "[{}] Training iteration {} done in {} second".format(self.time_stamp, self.training_update_cnt,
                                                                      time.time() - t))
            if self.training_update_cnt % training_configs.checkpoint_freq_in_steps == 0 and self.training_update_cnt != 0:
                torch.save(self.policy.state_dict(),
                           os.path.join(config_local.torch_model_save_dir,
                                        '{}_{}.pth'.format(self.time_stamp, self.training_update_cnt)))

    def evaluate(self, step):
        logging.info(f'--------------------------->>>Start evaluating {step}<<<<---------------------------------')
        T = training_configs.eval_config['T']
        std = torch.FloatTensor(training_configs.eval_config['std'])
        state, info = self.env.reset(
            options={'workload_start': training_configs.eval_config['workload_line_start']})
        state_lst = np.zeros((T, self.obs_dim), dtype=np.float32)
        action_lst = np.zeros((T, self.action_dim), dtype=np.float32)
        rewards_lst = np.zeros((T,), dtype=np.float32)
        rewards_power_lst = np.zeros((T,), dtype=np.float32)
        rewards_sla_lst = np.zeros((T,), dtype=np.float32)
        utilization_dict = []  # vec of dict
        for t in range(T):
            action, action_logpro = self.select_action(state, std)
            state_lst[t] = state
            action_np = action.detach().cpu().numpy()
            mapped_action = self.env.map_action(action_np)
            #logging.info(f'Mapped action:\n{mapped_action}')
            state, rewards, terminated, truncated, info = self.env.step(action_np, t == T - 1)
            action_lst[t] = action_np
            rewards_lst[t] = rewards['all']
            rewards_power_lst[t] = rewards['power']
            rewards_sla_lst[t] = rewards['sla']
            # utilizations = {
            #     'util_{}'.format(svr_type): info[self.cluster_state_name]['utilization_{}'.format(svr_type)]
            #     for svr_type in
            #     self.server_type_lst}
            # utilization_dict.append(utilizations)

            print(self.env.id_2_invoker.values())
        wandb.log({'reward': np.mean(rewards_lst), 'reward_sla': np.mean(rewards_sla_lst),
                   'reward_power': np.mean(rewards_power_lst)}, step=step)

    def wandb_log(self, loss, n_iter):
        wandb.log({'loss': loss.item(), 'reward_mean': np.mean(self.buffers.rewards),
                   'reward_sla': np.mean(self.buffers.rewards_sla),
                   'reward_power': np.mean(self.buffers.rewards_power)}, step=n_iter)
        # for k, v in self.buffers.utilizations.items():
        #     wandb.log({k: np.mean(v)}, step=n_iter)

    def read_all_config(self):
        res = {}
        res['NameTimeStamp'] = self.time_stamp
        train_config = {'std_decay': str(training_configs.std_decay),
                        'trajectory_len': training_configs.trajectory_len,
                        'n_env': training_configs.num_envs,
                        'init_std': str(training_configs.init_std),
                        'max_n_update': training_configs.max_num_update,
                        }
        res['train_configs'] = train_config
        res['reward_func_setting'] = training_configs.reward_setting
        res['NN'] = training_configs.NN
        res['slot_duration'] = training_configs.SLOT_DURATION_SECOND
        res['workload_config'] = config.workload_config
        res['params'] = training_configs.params
        res['select_func_params'] = training_configs.select_func_params
        res['select_func_weights'] = training_configs.select_func_weight
        res['func_spec'] = config.func_spec_dict
        #res['func_sla'] = config.workload_characteristics
        res['evaluate_config'] = training_configs.eval_config
        res['cluster_spec'] = config.cluster_spec_dict
        res['Config_Note'] = config.CONFIG_NOTE
        return res

    # def print_action_from_buffer(self) -> None:
    #     count_dict = [{'1': 0, '-1': 0, '0': 0} for _ in range(self.eval_env.num_func_input)]
    #     for t in self.buffers.actions:
    #         for simple_action in t:
    #             res = self.eval_env.map_action_for_monitor(simple_action)  # res: {funcid:(delta,type)}
    #             for func_index in range(self.eval_env.num_func_input):
    #                 count_dict[func_index][str(res[func_index][0])] += 1
    #     print(count_dict)


def wandb_init(timestamp: str, config: dict = None):
    WANDB_MODE = 'disabled'
    wandb.login()
    wandb.init(dir=config_local.wandb_dir,
               config=config,
               project='real_ow',
               group=training_configs.wandb_group_name,
               name=training_configs.note + timestamp,
               id=str(uuid.uuid4())
               # mode=WANDB_MODE
               )
    logging.info("Wandb Initialized")


if __name__ == '__main__':
    # a single environment for evaluation
    # eval_env = Cluster(cluster_spec_dict=config.cluster_spec_dict, func_spec_dict=config.func_spec_dict,
    #                    nn_func_input_count=config.input_space_spec['n_func'])
    env = Cluster(cluster_spec_dict=config.cluster_spec_dict, func_spec_dict=config.func_spec_dict,
                  nn_func_input_count=config.input_space_spec['n_func'])
    ppo = PPO(env=env, num_evn=1,
              func_state_dim=config.input_space_spec['func_state_dim'],
              num_func=config.input_space_spec['n_func'],
              cluster_state_dim=config.input_space_spec['cluster_state_dim'],
              action_dim=4, hidden_size=training_configs.NN['hidden_size'],
              activation=training_configs.NN['activation'], clip=training_configs.NN['clip'],
              gamma=training_configs.NN['gamma'],
              trajectory_len=training_configs.trajectory_len, init_stds=training_configs.init_std,
              std_decay_rate=training_configs.std_decay, min_std=training_configs.min_std,
              max_n_update=training_configs.max_num_update, batch_size=training_configs.NN['minibatch_sz'],
              epoch=training_configs.NN['epoch'], lr=training_configs.NN['lr'])
    ppo.train()
