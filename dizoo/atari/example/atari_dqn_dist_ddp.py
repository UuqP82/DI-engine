from copy import deepcopy
from ditk import logging
from ding.model import DQN
from ding.policy import DQNPolicy
from ding.envs import DingEnvWrapper, SubprocessEnvManagerV2
from ding.data import DequeBuffer
from ding.config import compile_config
from ding.framework import task, ding_init
from ding.framework.context import OnlineRLContext
from ding.framework.middleware import OffPolicyLearner, StepCollector, interaction_evaluator, data_pusher, \
    eps_greedy_handler, CkptSaver, context_exchanger, model_exchanger, termination_checker, nstep_reward_enhancer, \
    online_logger
from ding.utils import set_pkg_seed
from dizoo.atari.envs.atari_env import AtariEnv
from dizoo.atari.config.serial.pong.pong_dqn_config import main_config, create_config


logging.getLogger().setLevel(logging.INFO)
main_config.exp_name = 'pong_dqn_seed0_ditask_dist_ddp'


def learner():
    cfg = compile_config(main_config, create_cfg=create_config, auto=True)
    ding_init(cfg)
    set_pkg_seed(cfg.seed, use_cuda=cfg.policy.cuda)

    model = DQN(**cfg.policy.model)
    policy = DQNPolicy(cfg.policy, model=model, enable_field=['learn'])
    buffer_ = DequeBuffer(size=cfg.policy.other.replay_buffer.replay_buffer_size)

    with task.start(async_mode=False, ctx=OnlineRLContext()):
        assert task.router.is_active, "Please execute this script with ditask! See note in the header."
        logging.info("Learner running on node {}".format(task.router.node_id))

        from ding.utils import DistContext, get_rank
        with DistContext():
            rank = get_rank()
            task.use(
                context_exchanger(
                    send_keys=["train_iter"],
                    recv_keys=["trajectories", "episodes", "env_step", "env_episode"],
                    skip_n_iter=0
                )
            )
            task.use(model_exchanger(model, is_learner=True))
            task.use(nstep_reward_enhancer(cfg))
            task.use(data_pusher(cfg, buffer_))
            task.use(OffPolicyLearner(cfg, policy.learn_mode, buffer_))
            if rank == 0:
                task.use(CkptSaver(policy, cfg.exp_name, train_freq=1000))
            task.run()


def collector():
    cfg = compile_config(main_config, create_cfg=create_config, auto=True)
    ding_init(cfg)
    set_pkg_seed(cfg.seed, use_cuda=cfg.policy.cuda)

    model = DQN(**cfg.policy.model)
    policy = DQNPolicy(cfg.policy, model=model, enable_field=['collect'])
    collector_cfg = deepcopy(cfg.env)
    collector_cfg.is_train = True
    collector_env = SubprocessEnvManagerV2(
        env_fn=[lambda: AtariEnv(collector_cfg) for _ in range(cfg.env.collector_env_num)], cfg=cfg.env.manager
    )

    with task.start(async_mode=False, ctx=OnlineRLContext()):
        assert task.router.is_active, "Please execute this script with ditask! See note in the header."
        logging.info("Collector running on node {}".format(task.router.node_id))

        task.use(
            context_exchanger(
                send_keys=["trajectories", "episodes", "env_step", "env_episode"],
                recv_keys=["train_iter"],
                skip_n_iter=1
            )
        )
        task.use(model_exchanger(model, is_learner=False))
        task.use(eps_greedy_handler(cfg))
        task.use(StepCollector(cfg, policy.collect_mode, collector_env))
        task.use(termination_checker(max_env_step=int(1e7)))
        task.run()


def evaluator():
    cfg = compile_config(main_config, create_cfg=create_config, auto=True)
    ding_init(cfg)
    set_pkg_seed(cfg.seed, use_cuda=cfg.policy.cuda)

    model = DQN(**cfg.policy.model)
    policy = DQNPolicy(cfg.policy, model=model, enable_field=['eval'])
    evaluator_cfg = deepcopy(cfg.env)
    evaluator_cfg.is_train = False
    evaluator_env = SubprocessEnvManagerV2(
        env_fn=[lambda: AtariEnv(evaluator_cfg) for _ in range(cfg.env.evaluator_env_num)], cfg=cfg.env.manager
    )

    with task.start(async_mode=False, ctx=OnlineRLContext()):
        assert task.router.is_active, "Please execute this script with ditask! See note in the header."
        logging.info("Evaluator running on node {}".format(task.router.node_id))

        task.use(context_exchanger(recv_keys=["train_iter", "env_step"], skip_n_iter=1))
        task.use(model_exchanger(model, is_learner=False))
        task.use(interaction_evaluator(cfg, policy.eval_mode, evaluator_env))
        task.use(CkptSaver(policy, cfg.exp_name, save_finish=False))
        task.use(online_logger(record_train_iter=True))
        task.run()
