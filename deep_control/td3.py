import argparse
import copy
import time

import gym
import numpy as np
import tensorboardX
import torch
import torch.nn.functional as F
import tqdm

from . import envs, replay, run, utils

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def td3(
    agent,
    env,
    buffer,
    num_steps=1_000_000,
    max_episode_steps=100_000,
    batch_size=64,
    tau=0.005,
    actor_lr=1e-4,
    critic_lr=1e-3,
    gamma=0.99,
    sigma_start=0.2,
    sigma_final=0.1,
    sigma_anneal=10_000,
    theta=0.15,
    eval_interval=5000,
    eval_episodes=10,
    warmup_steps=1000,
    actor_clip=None,
    critic_clip=None,
    actor_l2=0.0,
    critic_l2=0.0,
    delay=2,
    target_noise_scale=0.2,
    save_interval=100_000,
    c=0.5,
    name="td3_run",
    render=False,
    save_to_disk=True,
    log_to_disk=True,
    verbosity=0,
    gradient_updates_per_step=1,
):

    agent.to(device)
    max_act = env.action_space.high[0]

    # initialize target networks
    target_agent = copy.deepcopy(agent)
    target_agent.to(device)
    utils.hard_update(target_agent.actor, agent.actor)
    utils.hard_update(target_agent.critic1, agent.critic1)
    utils.hard_update(target_agent.critic2, agent.critic2)

    random_process = utils.OrnsteinUhlenbeckProcess(
        size=env.action_space.shape,
        sigma=sigma_start,
        sigma_min=sigma_final,
        n_steps_annealing=sigma_anneal,
        theta=theta,
    )

    critic1_optimizer = torch.optim.Adam(
        agent.critic1.parameters(), lr=critic_lr, weight_decay=critic_l2
    )
    critic2_optimizer = torch.optim.Adam(
        agent.critic2.parameters(), lr=critic_lr, weight_decay=critic_l2
    )
    actor_optimizer = torch.optim.Adam(
        agent.actor.parameters(), lr=actor_lr, weight_decay=actor_l2
    )

    if save_to_disk or log_to_disk:
        save_dir = utils.make_process_dirs(name)
    if log_to_disk:
        # create tb writer, save hparams
        writer = tensorboardX.SummaryWriter(save_dir)

    run.warmup_buffer(buffer, env, warmup_steps, max_episode_steps)

    done = True
    learning_curve = []

    steps_iter = range(num_steps)
    if verbosity:
        steps_iter = tqdm.tqdm(steps_iter)

    for step in steps_iter:
        if done:
            state = env.reset()
            random_process.reset_states()
            steps_this_ep = 0
            done = False
        action = agent.forward(state)
        noisy_action = run.exploration_noise(action, random_process, max_act)
        next_state, reward, done, info = env.step(noisy_action)
        buffer.push(state, noisy_action, reward, next_state, done)
        state = next_state
        steps_this_ep += 1
        if steps_this_ep >= max_episode_steps:
            done = True

        update_policy = step % delay == 0
        for _ in range(gradient_updates_per_step):
            learn(
                buffer,
                target_agent,
                agent,
                actor_optimizer,
                critic1_optimizer,
                critic2_optimizer,
                env.action_space.high[0],
                batch_size,
                target_noise_scale,
                c,
                gamma,
                critic_clip,
                actor_clip,
                update_policy,
            )

            # move target model towards training model
            if update_policy:
                utils.soft_update(target_agent.actor, agent.actor, tau)
                # original td3 impl only updates critic targets with the actor...
                utils.soft_update(target_agent.critic1, agent.critic1, tau)
                utils.soft_update(target_agent.critic2, agent.critic2, tau)

        if step % eval_interval == 0 or step == num_steps - 1:
            mean_return = run.evaluate_agent(
                agent, env, eval_episodes, max_episode_steps, render
            )
            if log_to_disk:
                writer.add_scalar("return", mean_return, step)

            learning_curve.append((step, mean_return))

        if step % save_interval == 0 and save_to_disk:
            agent.save(save_dir)

    if save_to_disk:
        agent.save(save_dir)
    return agent


def learn(
    buffer,
    target_agent,
    agent,
    actor_optimizer,
    critic1_optimizer,
    critic2_optimizer,
    max_act,
    batch_size,
    target_noise_scale,
    c,
    gamma,
    critic_clip,
    actor_clip,
    update_policy=True,
):

    per = isinstance(buffer, replay.PrioritizedReplayBuffer)
    if per:
        batch, imp_weights, priority_idxs = buffer.sample(batch_size)
        imp_weights = imp_weights.to(device)
    else:
        batch = buffer.sample(batch_size)

    # prepare transitions for models
    state_batch, action_batch, reward_batch, next_state_batch, done_batch = batch
    state_batch = state_batch.to(device)
    next_state_batch = next_state_batch.to(device)
    action_batch = action_batch.to(device)
    reward_batch = reward_batch.to(device)
    done_batch = done_batch.to(device)

    agent.train()

    with torch.no_grad():
        # create critic targets (clipped double Q learning)
        target_action_s2 = target_agent.actor(next_state_batch)
        target_noise = torch.clamp(
            target_noise_scale * torch.randn(*target_action_s2.shape).to(device), -c, c
        )
        # target smoothing
        target_action_s2 = torch.clamp(
            target_action_s2 + target_noise, -max_act, max_act
        )
        target_action_value_s2 = torch.min(
            target_agent.critic1(next_state_batch, target_action_s2),
            target_agent.critic2(next_state_batch, target_action_s2),
        )
        td_target = reward_batch + gamma * (1.0 - done_batch) * target_action_value_s2

    # update first critic
    agent_critic1_pred = agent.critic1(state_batch, action_batch)
    td_error1 = td_target - agent_critic1_pred
    if per:
        critic1_loss = (imp_weights * 0.5 * (td_error1 ** 2)).mean()
    else:
        critic1_loss = 0.5 * (td_error1 ** 2).mean()
    critic1_optimizer.zero_grad()
    critic1_loss.backward()
    if critic_clip:
        torch.nn.utils.clip_grad_norm_(agent.critic1.parameters(), critic_clip)
    critic1_optimizer.step()

    # update second critic
    agent_critic2_pred = agent.critic2(state_batch, action_batch)
    td_error2 = td_target - agent_critic2_pred
    if per:
        critic2_loss = (imp_weights * 0.5 * (td_error2 ** 2)).mean()
    else:
        critic2_loss = 0.5 * (td_error2 ** 2).mean()
    critic2_optimizer.zero_grad()
    critic2_loss.backward()
    if critic_clip:
        torch.nn.utils.clip_grad_norm_(agent.critic2.parameters(), critic_clip)
    critic2_optimizer.step()

    if update_policy:
        # actor update
        agent_actions = agent.actor(state_batch)
        actor_loss = -agent.critic1(state_batch, agent_actions).mean()
        actor_optimizer.zero_grad()
        actor_loss.backward()
        if actor_clip:
            torch.nn.utils.clip_grad_norm_(agent.actor.parameters(), actor_clip)
        actor_optimizer.step()

    if per:
        new_priorities = (abs(td_error1) + 1e-5).cpu().detach().squeeze(1).numpy()
        buffer.update_priorities(priority_idxs, new_priorities)


def parse_args():
    parser = argparse.ArgumentParser(description="Train agent with TD3")
    parser.add_argument(
        "--env", type=str, default="Pendulum-v0", help="training environment"
    )
    parser.add_argument(
        "--num_steps", type=int, default=10 ** 6, help="number of training steps",
    )
    parser.add_argument(
        "--max_episode_steps",
        type=int,
        default=100000,
        help="maximum steps per episode",
    )
    parser.add_argument(
        "--batch_size", type=int, default=256, help="training batch size"
    )
    parser.add_argument(
        "--tau", type=float, default=0.005, help="for model parameter % update"
    )
    parser.add_argument(
        "--actor_lr", type=float, default=1e-4, help="actor learning rate"
    )
    parser.add_argument(
        "--critic_lr", type=float, default=1e-3, help="critic learning rate"
    )
    parser.add_argument(
        "--gamma", type=float, default=0.99, help="gamma, the discount factor"
    )
    parser.add_argument("--sigma_final", type=float, default=0.1)
    parser.add_argument(
        "--sigma_anneal",
        type=float,
        default=100_000,
        help="How many steps to anneal sigma over.",
    )
    parser.add_argument(
        "--theta",
        type=float,
        default=0.15,
        help="theta for Ornstein Uhlenbeck process computation",
    )
    parser.add_argument(
        "--sigma_start",
        type=float,
        default=0.2,
        help="sigma for Ornstein Uhlenbeck process computation",
    )
    parser.add_argument(
        "--eval_interval",
        type=int,
        default=5000,
        help="how often to test the agent without exploration (in episodes)",
    )
    parser.add_argument(
        "--eval_episodes",
        type=int,
        default=10,
        help="how many episodes to run for when testing",
    )
    parser.add_argument(
        "--warmup_steps", type=int, default=1000, help="warmup length, in steps"
    )
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--actor_clip", type=float, default=None)
    parser.add_argument("--critic_clip", type=float, default=None)
    parser.add_argument("--name", type=str, default="td3_run")
    parser.add_argument("--actor_l2", type=float, default=0.0)
    parser.add_argument("--critic_l2", type=float, default=0.0)
    parser.add_argument("--delay", type=int, default=2)
    parser.add_argument("--target_noise_scale", type=float, default=0.2)
    parser.add_argument("--save_interval", type=int, default=100_000)
    parser.add_argument("--c", type=float, default=0.5)
    parser.add_argument("--verbosity", type=int, default=1)
    parser.add_argument("--gradient_updates_per_step", type=int, default=1)
    parser.add_argument("--prioritized_replay", action="store_true")
    parser.add_argument("--buffer_size", type=int, default=1_000_000)
    parser.add_argument("--skip_save_to_disk", action="store_true")
    parser.add_argument("--skip_log_to_disk", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    agent, env = envs.load_env(args.env, "td3")

    if args.prioritized_replay:
        buffer_t = replay.PrioritizedReplayBuffer
    else:
        buffer_t = replay.ReplayBuffer
    buffer = buffer_t(
        args.buffer_size,
        state_shape=env.observation_space.shape,
        action_shape=env.action_space.shape,
    )

    print(f"Using Device: {device}")
    agent = td3(
        agent,
        env,
        buffer,
        num_steps=args.num_steps,
        max_episode_steps=args.max_episode_steps,
        batch_size=args.batch_size,
        tau=args.tau,
        actor_lr=args.actor_lr,
        critic_lr=args.critic_lr,
        gamma=args.gamma,
        sigma_start=args.sigma_start,
        sigma_final=args.sigma_final,
        sigma_anneal=args.sigma_anneal,
        theta=args.theta,
        eval_interval=args.eval_interval,
        eval_episodes=args.eval_episodes,
        warmup_steps=args.warmup_steps,
        actor_clip=args.actor_clip,
        critic_clip=args.critic_clip,
        actor_l2=args.actor_l2,
        critic_l2=args.critic_l2,
        delay=args.delay,
        target_noise_scale=args.target_noise_scale,
        save_interval=args.save_interval,
        c=args.c,
        name=args.name,
        render=args.render,
        verbosity=args.verbosity,
        gradient_updates_per_step=args.gradient_updates_per_step,
        save_to_disk=not args.skip_save_to_disk,
        log_to_disk=not args.skip_log_to_disk,
    )
