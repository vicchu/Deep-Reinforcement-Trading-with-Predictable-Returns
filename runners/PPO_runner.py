# 0. importing section initialize logger.--------------------------------------
import argparse
from re import S
import gin
import logging
import os
import time
import pdb
import sys
from typing import Union
from tqdm import tqdm
import torch
import torch.nn as nn
import numpy as np
import pandas as pd
from joblib import Parallel, delayed

from utils.common import (
    GeneratePathFolder,
    save_gin,
)
from utils.spaces import (
    ActionSpace,
    ResActionSpace,
)
from utils.simulation import DataHandler
from agents.PPO import PPO
from utils.tools import get_action_boundaries, get_bet_size, CalculateLaggedSharpeRatio
from utils.test import Out_sample_vs_gp
from utils.math_tools import unscale_action, unscale_asymmetric_action
from utils.mixin_core import MixinCore


@gin.configurable()
class PPO_runner(MixinCore):
    def __init__(
        self,
        env_cls: object,
        MV_res: bool,
        experiment_type: str,
        seed: int,
        episodes: int,
        epochs: int,
        len_series: Union[int or None],
        dt: int,
        rollouts_pct_num: float,
        save_freq: int,
        use_GPU: bool,
        outputDir: str = "outputs",
        outputClass: str = "PPO",
        outputModel: str = "test",
        varying_pars: Union[list or None] = None,
        varying_type: str = "chunk",
        num_cores: int = None,
        universal_train: bool = False,
    ):

        self.logging.info("Starting model setup")
        self._setattrs()

        self.rng = np.random.RandomState(self.seed)

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        if self.dt != 1.0:
            self.len_series = int(self.len_series * ((1/self.dt)*self.rollouts_pct_num))

        self.N_train = self.episodes * self.len_series
        self.col_names_oos = [
            str(e) for e in np.arange(0, self.episodes + 1, save_freq)[1:]
        ]

        self.savedpath = GeneratePathFolder(
            outputDir,
            outputClass,
            outputModel,
            varying_pars,
            varying_type,
            self.N_train,
        )
        if save_freq and not os.path.exists(os.path.join(self.savedpath, "ckpt")):
            os.makedirs(os.path.join(self.savedpath, "ckpt"))
        elif save_freq and os.path.exists(os.path.join(self.savedpath, "ckpt")):
            pass
        logging.info("Successfully generated path to save outputs...")

    def run(self):
        """Wrapper for keyboard interrupt."""
        try:
            self.set_up_training()
            self.training_agent()
        except (KeyboardInterrupt, SystemExit):
            self.logging.debug("Exit on KeyboardInterrupt or SystemExit")
            sys.exit()

    def set_up_training(self):

        self.logging.debug("Simulating Data")
        # Modify hyperparams to deal with a large cross section
        n_assets = gin.query_parameter('%N_ASSETS')
        if n_assets:
            self._get_hyperparams_n_assets(n_assets,self.rng)

        self.data_handler = DataHandler(N_train=self.len_series, rng=self.rng)
        if self.experiment_type == "GP":
            self.data_handler.generate_returns()
        else:
            self.data_handler.generate_returns()
            # TODO check if these method really fit and change the parameters in the gin file
            self.data_handler.estimate_parameters()

        self.logging.debug("Instantiating action space")
        if self.MV_res:
            self.action_space = ResActionSpace()
        else:
            action_range, ret_quantile, holding_quantile = get_action_boundaries(
                N_train=self.N_train,
                f_speed=self.data_handler.f_speed,
                returns=self.data_handler.returns,
                factors=self.data_handler.factors,
            )

            gin.query_parameter("%ACTION_RANGE")[0] = action_range
            self.action_space = ActionSpace()

        self.logging.debug("Instantiating market environment")
        self.env = self.env_cls(
            N_train=self.N_train,
            f_speed=self.data_handler.f_speed,
            returns=self.data_handler.returns,
            factors=self.data_handler.factors,
        )

        self.logging.debug("Instantiating DQN model")
        input_shape = self.env.get_state_dim()

        step_size = (
            self.len_series / gin.query_parameter("PPO.batch_size")
        ) * gin.query_parameter("%EPOCHS")
        gin.bind_parameter("PPO.step_size", step_size)

        self.train_agent = PPO(
            input_shape=input_shape, action_space=self.action_space, rng=self.rng
        )

        self.train_agent.add_tb_diagnostics(self.savedpath,self.epochs)

        self.logging.debug("Instantiating Out of sample tester")
        self.oos_test = Out_sample_vs_gp(
            savedpath=self.savedpath,
            tag="PPO",
            experiment_type=self.experiment_type,
            env_cls=self.env_cls,
            MV_res=self.MV_res,
        )

        self.oos_test.init_series_to_fill(iterations=self.col_names_oos)

    def training_agent(self):
        """
        Main routine to train and test the DRL algorithm. The steps are:

        1. Load the dataset, metadata, any model output and any pre-loaded
        data (cached_data).
        2. Start the Backtrader engine and initialize the broker object.
        3. Instantiate the environment.
        4. Instantiate the model for the agent.
        5. Train the model according to a chosen technique.
        6. Test the model out-of-sample.
        7. Log the performance data, plot, save configuration file and
            the runner logger output.

        Once this is done, the backtest is over and all of the artifacts
        are saved in `_exp/experiment_name/_backtests/`.
        """

        self.logging.debug("Start training...")
        for e in tqdm(iterable=range(self.episodes), desc="Running episodes..."):

            if e > 0 and self.universal_train:
                if self.experiment_type == "GP":
                    self.data_handler.generate_returns(disable_tqdm=True)
                else:
                    self.data_handler.generate_returns(disable_tqdm=True)
                    # TODO check if these method really fit and change the parameters in the gin file
                    self.data_handler.estimate_parameters()

                self.env.returns = self.data_handler.returns
                if self.data_handler.datatype != "alpha_term_structure":
                    self.env.factors = self.data_handler.factors
            
            self.logging.debug("Training...")

            self.collect_rollouts()

            self.update(e)

            if self.save_freq and ((e + 1) % self.save_freq == 0):  # TODO or e+1?

                torch.save(
                    self.train_agent.model.state_dict(),
                    os.path.join(
                        self.savedpath, "ckpt", "PPO_{}_ep_weights.pth".format(e + 1)
                    ),
                )

                self.logging.debug("Testing...")
                n_assets = gin.query_parameter('%N_ASSETS')
                if n_assets<3 or n_assets == None:
                    self.oos_test.run_test(it=e + 1, test_agent=self.train_agent)
        if n_assets<3 or n_assets == None:
            self.oos_test.save_series()
            # self.train_agent.save_diagnostics(path=self.savedpath)


        save_gin(os.path.join(self.savedpath, "config.gin"))
        logging.info("Config file saved")

    # def testing_agent(self):

    #     self.logging.debug("Loading Data")
    #     df_train, df_test, df = self.load_data()

    #     self.logging.debug("Instantiating action space")
    #     self.action_space = ActionSpace()

    #     self.test_env = self.env_cls(dataframe=df_test)

    #     self.logging.debug("Instantiating benchmark agent")
    #     self.benchmark_agent = self.bnch_agent_cls(
    #         symbol=self.symbol,
    #         start_year=self.start_year,
    #         split_year=self.split_year,
    #         end_year=self.end_year,
    #         df=df,
    #     )
    #     self.benchmark_agent._get_parameters()
    #     self.benchmark_test_env = self.bnch_env_cls(
    #         dataframe=self.benchmark_agent.df_test
    #     )

    #     self.logging.debug("Loading pretrained model")
    #     input_shape = self.test_env.get_state_dim()

    #     self.train_agent = self.agent_cls(
    #         input_shape=input_shape, action_space=self.action_space, rng=self.rng
    #     )

    #     self.train_agent.model.load_state_dict(
    #         torch.load(
    #             os.path.join(self.savedpath, "ckpt", "PPO_{}_ep_weights.pth".format(self.episodes))
    #         )
    #     )

    #     self.logging.debug("Instantiating Out of sample tester")
    #     self.oos_test = Out_sample_vs_gp(
    #         test_agent=self.train_agent,
    #         test_env=self.test_env,
    #         benchmark_agent=self.benchmark_agent,
    #         benchmark_env=self.benchmark_test_env,
    #         savedpath=self.savedpath,
    #         test_symbols = [self.symbol],
    #         tag="PPO",
    #     )

    #     self.logging.debug("Testing...")
    #     res_df, res_bench_df = self.oos_test.run_test(self.oos_test, return_output=True)

    #     return res_df, res_bench_df

    def collect_rollouts(self):

        state = self.env.reset()

        self.train_agent.reset_experience()

        for i in range(len(self.env.returns) - 2):
            dist, value = self.train_agent.act(state)

            if self.train_agent.policy_type == "continuous":
                action = dist.sample()

                log_prob = dist.log_prob(action)
                clipped_action = nn.Tanh()(action).cpu().numpy().ravel()
                action = action.cpu().numpy().ravel()
                if self.MV_res:
                    unscaled_action = unscale_asymmetric_action(
                        self.action_space.action_range[0],self.action_space.action_range[1], clipped_action
                    )
                else:
                    unscaled_action = unscale_action(
                        self.action_space.action_range[0], clipped_action
                    )

            elif self.train_agent.policy_type == "discrete":
                action = dist.sample()
                log_prob = dist.log_prob(action)

                clipped_action = np.array(
                    [self.action_space.values[action]], dtype="float32"
                )
                unscaled_action = clipped_action
                action = np.array([action], dtype="float32")

            else:
                print("Select a policy as continuous or discrete")
                sys.exit()

            if self.MV_res:
                if len(unscaled_action)>1:
                    next_state, Result = self.env.MV_res_step(
                        state, unscaled_action, i, tag="PPO"
                    )
                else:
                    next_state, Result = self.env.MV_res_step(
                        state, unscaled_action[0], i, tag="PPO"
                    )
            else:
                next_state, Result, _ = self.env.step(
                    state, unscaled_action[0], i, tag="PPO"
                )

            exp = {
                "state": state,
                "action": action,
                "reward": Result["Reward_PPO"],
                "log_prob": log_prob.detach()
                .cpu()
                .numpy()
                .ravel(),  # avoid require_grad and go back to numpy array
                "value": value.detach().cpu().numpy().ravel(),
            }

            self.train_agent.add_experience(exp)

            state = next_state

            _, self.next_value = self.train_agent.act(next_state)
            # compute the advantage estimate from the given rollout
            self.train_agent.compute_gae(self.next_value.detach().cpu().numpy().ravel())

    def update(self,episode):
        for i in range(self.epochs):  # run for more than one epochs
            for j,(
                state,
                action,
                old_log_probs,
                return_,
                advantage,
            ) in enumerate(self.train_agent.ppo_iter()):

                self.train_agent.train(state, action, old_log_probs, return_, advantage, iteration=j, epoch=i, episode=episode)

            # recompute gae to avoid stale advantages
            if i == len(range(self.epochs)) - 1:
                pass
            else:
                self.train_agent.compute_gae(
                    self.next_value.detach().cpu().numpy().ravel(), recompute_value=True
                )

    def _get_hyperparams_n_assets(self,n_assets,rng):
        gin.bind_parameter('%HALFLIFE',[[rng.randint(low=5,high=150)] for _ in range(n_assets)])
        gin.bind_parameter('%INITIAL_ALPHA',[[np.round(rng.uniform(low=0.0,high=0.004),5)] for _ in range(n_assets)])
        gin.bind_parameter('%F_PARAM',[[1.0] for _ in range(n_assets)])
        gin.bind_parameter('%CORRELATION',list(np.round(rng.uniform(size=(int((n_assets**2 - n_assets)/2))),5)))