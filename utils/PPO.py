# -*- coding: utf-8 -*-
"""
Created on Tue Mar  2 11:07:57 2021

@author: alessiobrini
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.optim.lr_scheduler import ExponentialLR
from torch.distributions import Normal, Categorical
import numpy as np
from typing import Optional, Union
import pdb
import sys


# To set an initialization similar to TF2
# https://discuss.pytorch.org/t/how-i-can-set-an-initialization-for-conv-kernels-similarly-to-keras/30473
# lr scheduling
# https://www.kaggle.com/isbhargav/guide-to-pytorch-learning-rate-scheduling
################################ Class to create a Deep Q Network model ################################
class PPOActorCritic(nn.Module):
    def __init__(
        self,
        seed: int,
        input_shape: int,
        activation: str,
        hidden_units_value: list,
        hidden_units_actor: list,
        num_actions: int,
        batch_norm_input: bool,
        batch_norm_value_out: bool,
        policy_type: str,
        std: float = 0.0,
        modelname: str = "PPO",
    ):

        super(PPOActorCritic, self).__init__()

        torch.manual_seed(seed)
        # set dimensionality of input/output depending on the model
        inp_dim = input_shape[0]
        out_dim = num_actions
        self.policy_type = policy_type

        # set flag for batch norm as attribute
        self.bnflag_input = batch_norm_input

        critic_modules = []

        if self.bnflag_input:
            # affine false sould be equal to center and scale to False in TF2
            critic_modules.append(nn.BatchNorm1d(inp_dim, affine=False))

        # self.input_layer = InputLayer(input_shape=inp_shape)
        # set of hidden layers
        for i in range(len(hidden_units_value)):
            if i == 0:
                critic_modules.append(nn.Linear(inp_dim, hidden_units_value[i]))
                if activation == "relu":
                    critic_modules.append(nn.ReLU())
                elif activation == "tanh":
                    critic_modules.append(nn.Tanh())
                else:
                    print("Activation selected not available")
                    sys.exit()
            else:
                critic_modules.append(
                    nn.Linear(hidden_units_value[i - 1], hidden_units_value[i])
                )
                if activation == "relu":
                    critic_modules.append(nn.ReLU())
                elif activation == "tanh":
                    critic_modules.append(nn.Tanh())
                else:
                    print("Activation selected not available")
                    sys.exit()

        if batch_norm_value_out:
            critic_modules = critic_modules + [
                nn.Linear(hidden_units_value[-1], 1),
                nn.BatchNorm1d(1, affine=False),
            ]
        else:
            critic_modules = critic_modules + [nn.Linear(hidden_units_value[-1], 1)]

        actor_modules = []

        if self.bnflag_input:
            # affine false sould be equal to center and scale to False in TF2
            actor_modules.append(nn.BatchNorm1d(inp_dim, affine=False))

        # self.input_layer = InputLayer(input_shape=inp_shape)
        # set of hidden layers
        for i in range(len(hidden_units_actor)):
            if i == 0:
                actor_modules.append(nn.Linear(inp_dim, hidden_units_actor[i]))
                if activation == "relu":
                    actor_modules.append(nn.ReLU())
                elif activation == "tanh":
                    actor_modules.append(nn.Tanh())
                else:
                    print("Activation selected not available")
                    sys.exit()
            else:
                actor_modules.append(
                    nn.Linear(hidden_units_actor[i - 1], hidden_units_actor[i])
                )
                if activation == "relu":
                    actor_modules.append(nn.ReLU())
                elif activation == "tanh":
                    actor_modules.append(nn.Tanh())
                else:
                    print("Activation selected not available")
                    sys.exit()

        actor_modules = actor_modules + [nn.Linear(hidden_units_actor[-1], out_dim)]

        # pdb.set_trace()
        self.critic = nn.Sequential(*critic_modules)

        self.actor = nn.Sequential(*actor_modules)

        # TODO insert here flag for cont/discrete
        if self.policy_type == "continuous":

            self.log_std = nn.Parameter(torch.ones(1, out_dim) * std)

        elif self.policy_type == "discrete":
            pass
        # I could add an initial offset to be sure that output actions are sufficiently low

        # I didn't use apply because I wanted different init for different layer
        # I guess it should be ok anyway but it has to be tested
        self.init_weights()

    def forward(self, x):
        value = self.critic(x)
        if self.policy_type == "continuous":

            mu = self.actor(x)
            std = self.log_std.exp().expand_as(
                mu
            )  # make the tensor of the same size of mu
            dist = Normal(mu, std)

        elif self.policy_type == "discrete":

            logits = self.actor(x)
            # correct when logits contain nans
            if torch.isnan(logits).sum() > 0:
                logits = (
                    torch.empty(logits.shape)
                    .uniform_(-0.01, 0.01)
                    .type(torch.FloatTensor)
                )

            dist = Categorical(logits=logits)

        return dist, value

    def init_weights(self):
        # to access module and layer of an architecture
        # https://discuss.pytorch.org/t/how-to-access-to-a-layer-by-module-name/83797/2
        for layer in self.actor[:-1]:
            if isinstance(layer, nn.Linear):
                nn.init.xavier_uniform_(layer.weight)
                nn.init.zeros_(layer.bias)

        # carefully initialize last layer
        nn.init.normal_(self.actor[-1].weight, mean=0.0, std=0.01)
        nn.init.constant_(self.actor[-1].bias, 0.01)

        for layer in self.critic[:-2]:
            if isinstance(layer, nn.Linear):
                nn.init.xavier_uniform_(layer.weight)
                nn.init.zeros_(layer.bias)
        # carefully initialize last layer
        for layer in self.critic[-2:]:
            if isinstance(layer, nn.Linear):
                nn.init.normal_(layer.weight, mean=0.0, std=0.01)
                nn.init.constant_(layer.bias, 0.01)


# ############################### DQN ALGORITHM ################################
class PPO:
    def __init__(
        self,
        seed: int,
        gamma: float,
        tau: float,
        clip_param: float,
        vf_c: float,
        ent_c: float,
        input_shape: int,
        hidden_units_value: list,
        hidden_units_actor: list,
        batch_size: int,
        lr: float,
        activation: str,
        optimizer_name: str,
        batch_norm_input: bool,
        batch_norm_value_out: bool,
        action_space,
        policy_type: str,
        pol_std: float,
        beta_1: float = 0.9,
        beta_2: float = 0.999,
        eps_opt: float = 1e-07,
        lr_schedule: Optional[str] = None,
        exp_decay_rate: Optional[float] = None,
        rng=None,
        modelname: str = "PPO act_crt",
    ):

        if rng is not None:
            self.rng = rng
        else:
            self.rng = np.random.RandomState(seed)

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        # self.device = torch.device('cpu')
        self.gamma = gamma
        self.tau = tau
        self.clip_param = clip_param
        self.vf_c = vf_c
        self.ent_c = ent_c
        self.batch_size = batch_size
        self.beta_1 = beta_1
        self.eps_opt = eps_opt
        self.action_space = action_space
        self.policy_type = policy_type
        self.num_actions = self.action_space.get_n_actions(policy_type=self.policy_type)
        self.batch_norm_input = batch_norm_input

        self.experience = {
            "state": [],
            "action": [],
            "reward": [],
            "log_prob": [],
            "value": [],
            "returns": [],
            "advantage": [],
        }

        self.model = PPOActorCritic(
            seed,
            input_shape,
            activation,
            hidden_units_value,
            hidden_units_actor,
            self.num_actions,
            batch_norm_input,
            batch_norm_value_out,
            self.policy_type,
            pol_std,
            modelname,
        )

        self.optimizer_name = optimizer_name
        if optimizer_name == "adam":
            self.optimizer = optim.Adam(
                self.model.parameters(),
                lr=lr,
                betas=(beta_1, beta_2),
                eps=eps_opt,
                weight_decay=0,
                amsgrad=False,
            )
        elif optimizer_name == "rmsprop":
            self.optimizer = optim.RMSprop(
                self.model.parameters(),
                lr=lr,
                alpha=beta_1,
                eps=eps_opt,
                weight_decay=0,
                momentum=0,
                centered=False,
            )

        if lr_schedule == "exponential":
            self.scheduler = ExponentialLR(
                optimizer=self.optimizer,
                gamma=exp_decay_rate,
            )
        else:
            self.scheduler = None

    def train(self, state, action, old_log_probs, return_, advantage):

        self.model.train()

        dist, value = self.model(state)
        entropy = dist.entropy().mean()
        new_log_probs = dist.log_prob(action)

        ratio = (new_log_probs - old_log_probs).exp()  # log properties
        surr1 = ratio * advantage
        surr2 = (
            torch.clamp(ratio, 1.0 - self.clip_param, 1.0 + self.clip_param) * advantage
        )

        actor_loss = -torch.min(surr1, surr2).mean()
        critic_loss = (return_ - value).pow(2).mean()

        # the loss is negated in order to be maximized
        self.loss = self.vf_c * critic_loss + actor_loss - self.ent_c * entropy

        self.optimizer.zero_grad()
        self.loss.backward()
        self.optimizer.step()

        if self.scheduler:
            self.scheduler.step()

    def act(self, states):
        # useful when the states are single dimensional
        self.model.eval()
        # make 1D tensor to 2D
        states = torch.from_numpy(states).float().unsqueeze(0)
        states = states.to(self.device)
        return self.model(states)

    def compute_gae(self, next_value, recompute_value=False):

        if recompute_value:
            for i in range(len(self.experience["value"])):

                _, value = self.act(self.experience["state"][i])

                self.experience["value"][i] = value.detach().cpu().numpy().ravel()

        rewards = self.experience["reward"]
        values = self.experience["value"]

        values = values + [next_value]
        gae = 0
        returns = []
        for step in reversed(range(len(rewards))):
            delta = rewards[step] + self.gamma * values[step + 1] - values[step]
            gae = delta + self.gamma * self.tau * gae
            returns.insert(0, gae + values[step])

        # add estimated returns and advantages to the experience
        self.experience["returns"] = returns

        advantage = [returns[i] - values[i] for i in range(len(returns))]
        self.experience["advantage"] = advantage

    # add way to reset experience after one rollout
    def add_experience(self, exp):
        for key, value in exp.items():
            self.experience[key].append(value)

    def reset_experience(self):

        self.experience = {
            "state": [],
            "action": [],
            "reward": [],
            "log_prob": [],
            "value": [],
            "returns": [],
            "advantage": [],
        }

    def ppo_iter(self):
        # pick a batch from the rollout
        states = np.asarray(self.experience["state"])
        actions = np.asarray(self.experience["action"])
        log_probs = np.asarray(self.experience["log_prob"])
        returns = np.asarray(self.experience["returns"])
        advantage = np.asarray(self.experience["advantage"])

        len_rollout = states.shape[0]
        for _ in range(len_rollout // self.batch_size):
            rand_ids = self.rng.randint(0, len_rollout, self.batch_size)

            yield (
                torch.from_numpy(states[rand_ids, :]).float().to(self.device),
                torch.from_numpy(actions[rand_ids, :]).float().to(self.device),
                torch.from_numpy(log_probs[rand_ids, :]).float().to(self.device),
                torch.from_numpy(returns[rand_ids, :]).float().to(self.device),
                torch.from_numpy(advantage[rand_ids, :]).float().to(self.device),
            )


if __name__ == "__main__":

    model = PPOActorCritic(
        seed=12,
        input_shape=(8,),
        hidden_units=[256, 128],
        num_actions=1,
        batch_norm_input=True,
        std=0.0,
        modelname="PPO",
    )
