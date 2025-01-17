from ray.rllib.agents.dqn.dqn import calculate_rr_weights

from ray.rllib.utils.framework import try_import_tf, \
    try_import_tfp
from egpo_utils.sac_pid.sac_pid_policy import TargetNetworkMixin, ActorCriticOptimizerMixin, ComputeTDErrorMixin, UpdatePenaltyMixin

tf, _, _ = try_import_tf()
tf1 = tf
tfp = try_import_tfp()
import numpy as np
from ray.rllib.agents.dqn.dqn_tf_policy import postprocess_nstep_and_prio
from ray.rllib.evaluation.worker_set import WorkerSet
from ray.rllib.execution.concurrency_ops import Concurrently
from ray.rllib.execution.metric_ops import StandardMetricsReporting
from ray.rllib.execution.replay_buffer import LocalReplayBuffer
from ray.rllib.execution.replay_ops import Replay, StoreToReplayBuffer
from ray.rllib.execution.rollout_ops import ParallelRollouts
from ray.rllib.execution.train_ops import TrainOneStep, UpdateTargetNetwork
from ray.rllib.policy.policy import LEARNER_STATS_KEY
from ray.rllib.policy.sample_batch import SampleBatch
from ray.rllib.utils.typing import TrainerConfigDict
from ray.tune.utils.util import merge_dicts
from ray.util.iter import LocalIterator

from egpo_utils.sac_pid.sac_pid import SACPIDTrainer, validate_config
from egpo_utils.sac_pid.sac_pid_policy import SACPIDConfig, SACPIDPolicy, UpdatePenalty, get_dist_class
from egpo_utils.sac_pid.sac_pid_model import ConstrainedSACModel

# Update penalty
#

NEWBIE_ACTION = "newbie_action"
WARMUP_ACTION4BC = "warmup_action_for_bc"
TAKEOVER = "takeover"

EGPOConfig = merge_dicts(SACPIDConfig,
                         {
                                    "info_cost_key": "takeover_cost",
                                    "info_total_cost_key": "total_takeover_cost",
                                    "takeover_data_discard": False,
                                    "alpha": 7.0,
                                    "il_agent_coef": 0.,
                                    "il_expert_coef": 0.,
                                    "no_cql": False,
                                    "no_reward": False,  # this will disable the native reward from env
                                    "fix_lambda": False,
                                    "lambda_init": 150,
                                    "no_cost_minimization": False,
                                })


def validate_saver_config(config):
    validate_config(config)
    assert config["info_cost_key"] == "takeover_cost" and config["info_total_cost_key"] == "total_takeover_cost"


class UpdateSaverPenalty(UpdatePenalty):
    def __init__(self, workers):
        super(UpdateSaverPenalty, self).__init__(workers)
        self.takeover_data_discard = self.workers.local_worker().get_policy().config["takeover_data_discard"]

    def __call__(self, *args, **kwargs):
        sample_batch = super(UpdateSaverPenalty, self).__call__(*args, **kwargs)
        infos = sample_batch.get(SampleBatch.INFOS)

        if infos is None:
            return sample_batch
        if self.takeover_data_discard and infos[0]["takeover"] and not sample_batch[SampleBatch.DONES][0]:
            # discard takeover data
            sample_batch = sample_batch.slice(0, 0)
            # for key in batch.keys():
            #     batch[key] = batch[key][:-1]
        return sample_batch


def execution_plan(workers: WorkerSet,
                   config: TrainerConfigDict) -> LocalIterator[dict]:
    if config.get("prioritized_replay"):
        prio_args = {
            "prioritized_replay_alpha": config["prioritized_replay_alpha"],
            "prioritized_replay_beta": config["prioritized_replay_beta"],
            "prioritized_replay_eps": config["prioritized_replay_eps"],
        }
    else:
        prio_args = {}

    local_replay_buffer = LocalReplayBuffer(
        num_shards=1,
        learning_starts=config["learning_starts"],
        buffer_size=config["buffer_size"],
        replay_batch_size=config["train_batch_size"],
        replay_mode=config["multiagent"]["replay_mode"],
        replay_sequence_length=config["replay_sequence_length"],
        **prio_args)

    rollouts = ParallelRollouts(workers, mode="bulk_sync")

    # Update penalty
    rollouts = rollouts.for_each(UpdateSaverPenalty(workers))

    # We execute the following steps concurrently:
    # (1) Generate rollouts and store them in our local replay buffer. Calling
    # next() on store_op drives this.
    store_op = rollouts.for_each(StoreToReplayBuffer(local_buffer=local_replay_buffer))

    def update_prio(item):
        samples, info_dict = item
        if config.get("prioritized_replay"):
            prio_dict = {}
            for policy_id, info in info_dict.items():
                # TODO(sven): This is currently structured differently for
                #  torch/tf. Clean up these results/info dicts across
                #  policies (note: fixing this in torch_policy.py will
                #  break e.g. DDPPO!).
                td_error = info.get("td_error",
                                    info[LEARNER_STATS_KEY].get("td_error"))
                prio_dict[policy_id] = (samples.policy_batches[policy_id]
                                        .data.get("batch_indexes"), td_error)
            local_replay_buffer.update_priorities(prio_dict)
        return info_dict

    # (2) Read and train on experiences from the replay buffer. Every batch
    # returned from the LocalReplay() iterator is passed to TrainOneStep to
    # take a SGD step, and then we decide whether to update the target network.
    post_fn = config.get("before_learn_on_batch") or (lambda b, *a: b)
    replay_op = Replay(local_buffer=local_replay_buffer) \
        .for_each(lambda x: post_fn(x, workers, config)) \
        .for_each(TrainOneStep(workers)) \
        .for_each(update_prio) \
        .for_each(UpdateTargetNetwork(
        workers, config["target_network_update_freq"]))

    # Alternate deterministically between (1) and (2). Only return the output
    # of (2) since training metrics are not available until (2) runs.
    train_op = Concurrently(
        [store_op, replay_op],
        mode="round_robin",
        output_indexes=[1],
        round_robin_weights=calculate_rr_weights(config))

    return StandardMetricsReporting(train_op, workers, config)


def postprocess_trajectory(policy,
                           sample_batch: SampleBatch,
                           other_agent_batches=None,
                           episode=None):
    # if sample_batch.count > 1:
    #     raise ValueError
    # Put the actions to batch
    infos = sample_batch.get(SampleBatch.INFOS)
    if (infos is not None) and (infos[0] != 0.0):
        sample_batch[NEWBIE_ACTION] = sample_batch.copy()[SampleBatch.ACTIONS]
        sample_batch[SampleBatch.ACTIONS] = np.array([info["raw_action"] for info in infos])
        # disable cql loss during warmup
        if "warmup" in infos[0]:
            sample_batch[NEWBIE_ACTION] = np.where(np.array([info["warmup"] for info in infos]).reshape(-1, 1), 
                                                sample_batch[SampleBatch.ACTIONS], 
                                                sample_batch[NEWBIE_ACTION])
        # sample_batch[WARMUP_ACTION4BC] = np.where(np.array([info["warmup"] for info in infos]).reshape(-1, 1), 
        #                                           sample_batch[NEWBIE_ACTION],
        #                                           sample_batch[SampleBatch.ACTIONS], 
        #                                           )

        sample_batch[TAKEOVER] = np.array(
            [info[TAKEOVER] for info in sample_batch[SampleBatch.INFOS]])
        sample_batch[policy.config["info_cost_key"]] = np.array(
            [info[policy.config["info_cost_key"]] for info in sample_batch[SampleBatch.INFOS]]
        ).astype(sample_batch[SampleBatch.REWARDS].dtype)
        sample_batch[policy.config["info_total_cost_key"]] = np.array(
            [info[policy.config["info_total_cost_key"]] for info in sample_batch[SampleBatch.INFOS]]
        ).astype(sample_batch[SampleBatch.REWARDS].dtype)
        if policy.config["no_reward"]:
            sample_batch[SampleBatch.REWARDS] = np.zeros_like(sample_batch[SampleBatch.REWARDS])
    else:
        assert episode is None, "Only during initialization, can we see empty infos."
        sample_batch[policy.config["info_cost_key"]] = np.zeros_like(sample_batch[SampleBatch.REWARDS])
        sample_batch[policy.config["info_total_cost_key"]] = np.zeros_like(sample_batch[SampleBatch.REWARDS])
        sample_batch[NEWBIE_ACTION] = np.zeros_like(sample_batch[SampleBatch.ACTIONS])
        sample_batch[TAKEOVER] = np.zeros_like(sample_batch[SampleBatch.DONES])
    batch = postprocess_nstep_and_prio(policy, sample_batch)
    assert policy.config["info_cost_key"] in batch
    assert policy.config["info_total_cost_key"] in batch
    assert TAKEOVER in batch
    assert NEWBIE_ACTION in batch
    return batch


def sac_actor_critic_loss(policy, model: ConstrainedSACModel, _, train_batch):
    _ = train_batch[policy.config["info_total_cost_key"]]  # Touch this item, this is helpful in ray 1.2.0

    # Setup the lambda multiplier.
    with tf.variable_scope('lambda'):
        param_init = 1e-8 if not policy.config["fix_lambda"] else policy.config["lambda_init"]
        lambda_param = tf.get_variable(
            'lambda_value',
            initializer=float(param_init),
            trainable=False,
            dtype=tf.float32
        )
    policy.lambda_value = lambda_param

    # Should be True only for debugging purposes (e.g. test cases)!
    deterministic = policy.config["_deterministic_loss"]

    model_out_t, _ = model({
        "obs": train_batch[SampleBatch.CUR_OBS],
        "is_training": policy._get_is_training_placeholder(),
    }, [], None)

    model_out_tp1, _ = model({
        "obs": train_batch[SampleBatch.NEXT_OBS],
        "is_training": policy._get_is_training_placeholder(),
    }, [], None)

    target_model_out_tp1, _ = policy.target_model({
        "obs": train_batch[SampleBatch.NEXT_OBS],
        "is_training": policy._get_is_training_placeholder(),
    }, [], None)

    # Discrete case.
    if model.discrete:
        raise ValueError("Doesn't support yet")
    # Continuous actions case.
    else:
        # Sample simgle actions from distribution.
        action_dist_class = get_dist_class(policy.config, policy.action_space)
        action_dist_t = action_dist_class(
            model.get_policy_output(model_out_t), policy.model)
        #* sample from the current policy
        policy_t = action_dist_t.sample() if not deterministic else \
            action_dist_t.deterministic_sample()
        log_pis_t = tf.expand_dims(action_dist_t.logp(policy_t), -1)
        log_expert_a_t = action_dist_t.logp(train_batch[SampleBatch.ACTIONS])
        log_agent_a_t  = action_dist_t.logp(train_batch[NEWBIE_ACTION])
        action_dist_tp1 = action_dist_class(
            model.get_policy_output(model_out_tp1), policy.model)
        policy_tp1 = action_dist_tp1.sample() if not deterministic else \
            action_dist_tp1.deterministic_sample()
        log_pis_tp1 = tf.expand_dims(action_dist_tp1.logp(policy_tp1), -1)

        # Q-values for the actually selected actions.
        q_t = model.get_q_values(model_out_t, train_batch[SampleBatch.ACTIONS])
        if policy.config["twin_q"]:
            twin_q_t = model.get_twin_q_values(
                model_out_t, train_batch[SampleBatch.ACTIONS])

        # Cost Q-Value for actually selected actions
        c_q_t = model.get_cost_q_values(model_out_t, train_batch[SampleBatch.ACTIONS])
        if policy.config["twin_cost_q"]:
            twin_c_q_t = model.get_twin_cost_q_values(
                model_out_t, train_batch[SampleBatch.ACTIONS])

        # Q-values for current policy in given current state.
        q_t_det_policy = model.get_q_values(model_out_t, policy_t)
        if policy.config["twin_q"]:
            twin_q_t_det_policy = model.get_twin_q_values(
                model_out_t, policy_t)
            q_t_det_policy = tf.reduce_min(
                (q_t_det_policy, twin_q_t_det_policy), axis=0)

        # Cost Q-values for current policy in given current state.
        c_q_t_det_policy = model.get_cost_q_values(model_out_t, policy_t)
        if policy.config["twin_cost_q"]:
            twin_c_q_t_det_policy = model.get_twin_cost_q_values(
                model_out_t, policy_t)
            c_q_t_det_policy = tf.reduce_min(
                (c_q_t_det_policy, twin_c_q_t_det_policy), axis=0)

        # target q network evaluation
        q_tp1 = policy.target_model.get_q_values(target_model_out_tp1,
                                                 policy_tp1)
        if policy.config["twin_q"]:
            twin_q_tp1 = policy.target_model.get_twin_q_values(
                target_model_out_tp1, policy_tp1)
            # Take min over both twin-NNs.
            q_tp1 = tf.reduce_min((q_tp1, twin_q_tp1), axis=0)

        # target c-q network evaluation
        c_q_tp1 = policy.target_model.get_cost_q_values(target_model_out_tp1,
                                                        policy_tp1)
        if policy.config["twin_cost_q"]:
            twin_c_q_tp1 = policy.target_model.get_twin_cost_q_values(
                target_model_out_tp1, policy_tp1)
            # Take min over both twin-NNs.
            c_q_tp1 = tf.reduce_min((c_q_tp1, twin_c_q_tp1), axis=0)

        q_t_selected = tf.squeeze(q_t, axis=len(q_t.shape) - 1)
        if policy.config["twin_q"]:
            twin_q_t_selected = tf.squeeze(twin_q_t, axis=len(twin_q_t.shape) - 1)

        # c_q_t selected
        c_q_t_selected = tf.squeeze(c_q_t, axis=len(c_q_t.shape) - 1)
        if policy.config["twin_cost_q"]:
            twin_c_q_t_selected = tf.squeeze(twin_c_q_t, axis=len(twin_c_q_t.shape) - 1)

        q_tp1 -= model.alpha * log_pis_tp1

        q_tp1_best = tf.squeeze(input=q_tp1, axis=len(q_tp1.shape) - 1)
        q_tp1_best_masked = (1.0 - tf.cast(train_batch[SampleBatch.DONES],
                                           tf.float32)) * q_tp1_best

    c_q_tp1_best = tf.squeeze(input=c_q_tp1, axis=len(c_q_tp1.shape) - 1)
    c_q_tp1_best_masked = \
        (1.0 - tf.cast(train_batch[SampleBatch.DONES], tf.float32)) * \
        c_q_tp1_best

    # compute RHS of bellman equation
    q_t_selected_target = tf.stop_gradient(
        train_batch[SampleBatch.REWARDS] +
        policy.config["gamma"] ** policy.config["n_step"] * q_tp1_best_masked)

    # Compute Cost of bellman equation.
    c_q_t_selected_target = tf.stop_gradient(train_batch[policy.config["info_cost_key"]] +
                                             policy.config["gamma"] ** policy.config["n_step"] * c_q_tp1_best_masked)

    # Compute the TD-error (potentially clipped).
    base_td_error = tf.math.abs(q_t_selected - q_t_selected_target)
    if policy.config["twin_q"]:
        twin_td_error = tf.math.abs(twin_q_t_selected - q_t_selected_target)
        td_error = 0.5 * (base_td_error + twin_td_error)
    else:
        td_error = base_td_error

    # Compute the Cost TD-error (potentially clipped).
    base_c_td_error = tf.math.abs(c_q_t_selected - c_q_t_selected_target)
    if policy.config["twin_cost_q"]:
        twin_c_td_error = tf.math.abs(twin_c_q_t_selected - c_q_t_selected_target)
        c_td_error = 0.5 * (base_c_td_error + twin_c_td_error)
    else:
        c_td_error = base_c_td_error

    # conservative loss
    newbie_q_t = model.get_q_values(model_out_t, train_batch[NEWBIE_ACTION])
    if policy.config["twin_q"]:
        newbie_twin_q_t = model.get_twin_q_values(
            model_out_t, train_batch[NEWBIE_ACTION])

    newbie_q_t_selected = tf.squeeze(newbie_q_t, axis=len(newbie_q_t.shape) - 1)
    if policy.config["twin_q"]:
        newbie_twin_q_t_selected = tf.squeeze(newbie_twin_q_t, axis=len(newbie_twin_q_t.shape) - 1)

    # add conservative loss
    if policy.config["no_cql"]:
        critic_loss = [0.5 * tf.keras.losses.MSE(y_true=q_t_selected_target, y_pred=q_t_selected)]
    else:
        critic_loss = [
            0.5 * tf.keras.losses.MSE(
                y_true=q_t_selected_target, y_pred=q_t_selected) - tf.reduce_mean((tf.cast(train_batch[TAKEOVER],
                                                                                        tf.float32)) * policy.config[
                                                                                    "alpha"] * (
                                                                                        q_t_selected - newbie_q_t_selected))]
    if policy.config["twin_q"]:
        if policy.config["no_cql"]:
            loss = 0.5 * tf.keras.losses.MSE(y_true=q_t_selected_target, y_pred=twin_q_t_selected)
        else:
            loss = 0.5 * tf.keras.losses.MSE(y_true=q_t_selected_target, y_pred=twin_q_t_selected) - \
                    tf.reduce_mean((tf.cast(train_batch[TAKEOVER], tf.float32)) * policy.config["alpha"] * \
                    (twin_q_t_selected - newbie_twin_q_t_selected))
        critic_loss.append(loss)

    # add cost critic
    critic_loss.append(
        0.5 * tf.keras.losses.MSE(
            y_true=c_q_t_selected_target, y_pred=c_q_t_selected))
    if policy.config["twin_cost_q"]:
        critic_loss.append(0.5 * tf.keras.losses.MSE(
            y_true=c_q_t_selected_target, y_pred=twin_c_q_t_selected))

    # Alpha- and actor losses.
    # Note: In the papers, alpha is used directly, here we take the log.
    # Discrete case: Multiply the action probs as weights with the original
    # loss terms (no expectations needed).
    if model.discrete:
        raise ValueError("Didn't support discrete mode yet")
    else:
        alpha_loss = -tf.reduce_mean(
            model.log_alpha *
            tf.stop_gradient(log_pis_t + model.target_entropy))
        if policy.config["only_evaluate_cost"]:
            actor_loss = tf.reduce_mean(
                model.alpha * log_pis_t - q_t_det_policy)
            cost_loss = 0
            reward_loss = actor_loss
        else:
            reward_loss = tf.reduce_mean(
                model.alpha * log_pis_t - q_t_det_policy)
            cost_loss = tf.reduce_mean(policy.lambda_value * c_q_t_det_policy)
            actor_loss = tf.reduce_mean(
                model.alpha * log_pis_t - q_t_det_policy + policy.lambda_value * c_q_t_det_policy)
        actor_loss = actor_loss / (1 + policy.lambda_value) if policy.config["normalize"] else actor_loss

    # add imitation loss to alpha loss
    # imitating both expert and agent itself
    self_regularization_loss = -policy.config["il_agent_coef"] * log_agent_a_t
    bc_loss = -policy.config["il_expert_coef"] * log_expert_a_t
    # self_regularization_loss = - 0.05 * log_agent_a_t
    # print("Actor loss", actor_loss)
    # print("il loss", self_regularization_loss)

    # save for stats function
    policy.policy_t = policy_t
    policy.cost_loss = cost_loss
    policy.reward_loss = reward_loss
    policy.mean_batch_cost = train_batch[policy.config["info_cost_key"]]
    policy.q_t = q_t
    policy.c_q_tp1 = c_q_tp1
    policy.c_q_t = c_q_t
    policy.td_error = td_error
    policy.c_td_error = c_td_error
    policy.actor_loss = actor_loss + self_regularization_loss + bc_loss
    policy.critic_loss = critic_loss
    policy.c_td_target = c_q_t_selected_target
    policy.alpha_loss = alpha_loss
    policy.alpha_value = model.alpha
    policy.target_entropy = model.target_entropy
    policy.self_regularization_loss = self_regularization_loss
    policy.bc_loss = bc_loss

    # in a custom apply op we handle the losses separately, but return them
    # combined in one loss for now
    return actor_loss + tf.math.add_n(critic_loss) + alpha_loss 

    
def stats(policy, train_batch):
    return {
        # "policy_t": policy.policy_t,
        # "td_error": policy.td_error,
        "mean_td_error": tf.reduce_mean(policy.td_error),
        "mean_c_td_error": tf.reduce_mean(policy.c_td_error),
        "actor_loss": tf.reduce_mean(policy.actor_loss),
        "critic_loss": tf.reduce_mean(policy.critic_loss[:2] if policy.config["twin_q"] else policy.critic_loss[0]),
        "cost_critic_loss": tf.reduce_mean(
            policy.critic_loss[2:] if policy.config["twin_q"] else policy.critic_loss[1]),
        "alpha_loss": tf.reduce_mean(policy.alpha_loss),
        "self_il_loss": tf.reduce_mean(policy.self_regularization_loss),
        "bc_loss": tf.reduce_mean(policy.bc_loss),
        "lambda_value": tf.reduce_mean(policy.lambda_value),
        "alpha_value": tf.reduce_mean(policy.alpha_value),
        "target_entropy": tf.constant(policy.target_entropy),
        "c_td_target": tf.reduce_mean(policy.c_td_target),
        "mean_q": tf.reduce_mean(policy.q_t),
        "mean_c_q": tf.reduce_mean(policy.c_q_t),
        "max_q": tf.reduce_max(policy.q_t),
        "max_c_q": tf.reduce_max(policy.c_q_t),
        "min_q": tf.reduce_min(policy.q_t),
        "min_c_q": tf.reduce_min(policy.c_q_t),
        "c_q_tp1": tf.reduce_mean(policy.c_q_tp1),
        "mean_batch_cost": tf.reduce_mean(policy.mean_batch_cost),
        "reward_loss": tf.reduce_mean(policy.reward_loss),
        "cost_loss": tf.reduce_mean(policy.cost_loss)
    }

class ConditionalUpdatePenaltyMixin(UpdatePenaltyMixin):
    def update_penalty(self, batch: SampleBatch):
        if not self.config["fix_lambda"]:
            return super(ConditionalUpdatePenaltyMixin, self).update_penalty(batch)
        return 0, 0

EGPOPolicy = SACPIDPolicy.with_updates(name="EGPOPolicy",
                                       get_default_config=lambda: EGPOConfig,
                                       postprocess_fn=postprocess_trajectory,
                                       stats_fn=stats,
                                       mixins=[
                                            TargetNetworkMixin, ActorCriticOptimizerMixin, ComputeTDErrorMixin, ConditionalUpdatePenaltyMixin
                                       ],
                                       loss_fn=sac_actor_critic_loss)

EGPOTrainer = SACPIDTrainer.with_updates(name="EGPOTrainer",
                                         default_config=EGPOConfig,
                                         default_policy=EGPOPolicy,
                                         get_policy_class=lambda config: EGPOPolicy,
                                         validate_config=validate_config,
                                         execution_plan=execution_plan,
                                         )
