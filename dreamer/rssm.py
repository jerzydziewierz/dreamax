from dreamer.utils import initializer
from dreamer.configuartion import DreamerConfiguration, RssmConfig
from distutils.command.config import config
from typing import Tuple, Optional

import haiku as hk
import jax
import jax.nn as jnn
import jax.numpy as jnp
from tensorflow_probability.substrates import jax as tfp
tfd = tfp.distributions


State = Tuple[jnp.ndarray, jnp.ndarray]
Action = jnp.ndarray
Observation = jnp.ndarray


class Prior(hk.Module):
    def __init__(self, config: RssmConfig, initialization: str):
        super().__init__()
        self.c = config
        self.initialization = initialization

    def __call__(self, prev_state: State, prev_action: Action) -> Tuple[tfd.MultivariateNormalDiag, State]:
        stoch, det = prev_state
        cat = jnp.concatenate([prev_action, stoch], -1)
        x = jnn.elu(hk.Linear(self.c.deterministic_size, name='h1',
                    w_init=initializer(self.initialization))(cat))
        x, det = hk.GRU(
            self.c.deterministic_size,
            w_i_init=initializer(self.initialization),
            w_h_init=hk.initializers.Orthogonal()
        )(x, det)
        x = jnn.elu(hk.Linear(self.c.hidden, name='h2',
                    w_init=initializer(self.initialization))(x))
        x = hk.Linear(self.c.stochastic_size * 2, name='h3',
                      w_init=initializer(self.initialization))(x)
        mean, stddev = jnp.split(x, 2, -1)
        stddev = jnn.softplus(stddev) + 0.1
        prior = tfd.MultivariateNormalDiag(mean, stddev)
        sample = prior.sample(seed=hk.next_rng_key())
        return prior, (sample, det)


class Posterior(hk.Module):
    def __init__(self, config: RssmConfig, inititialization: str):
        super().__init__()
        self.c = config
        self.initialization = inititialization

    def __call__(self, prev_state: State, observation: Observation) -> Tuple[tfd.MultivariateNormalDiag, State]:
        _, det = prev_state
        cat = jnp.concatenate([det, observation], -1)
        x = jnn.elu(hk.Linear(self.c.hidden, name='h1',
                    w_init=initializer(self.initialization))(x))
        mean, stddev = jnp.split(x, 2, -1)
        stddev = jnn.softplus(stddev) + 0.1
        posterior = tfd.MultivariateNormalDiag(mean, stddev)
        sample = posterior.sample(seed=hk.next_rng_key())
        return posterior, (sample, det)


def init_state(batch_size: int, stochastic_size: int, deterministic_size: int, dtype: Optional[jnp.dtype] = jnp.float32) -> State:
    return (jnp.zeros((batch_size, stochastic_size), dtype),
            jnp.zeros((batch_size, deterministic_size), dtype))


class RSSM(hk.Module):
    def __init__(self, config: DreamerConfiguration):
        super().__init__()
        self.c = config
        self.prior = Prior(config.rssm, config.initialization)
        self.posterior = Posterior(config.rssm, config.initialization)

    def __call__(self, prev_state: State, prev_action: Action, observation: Observation) -> Tuple[Tuple[tfd.MultivariateNormalDiag, tfd.MultivariateNormalDiag], State]:
        prior, state = self.prior(prev_state, prev_action)
        posterior, state = self.posterior(state, observation)
        return (prior, posterior), state

    def generate_sequence(self, initial_features: jnp.ndarray, actor: hk.Transformed, actor_params: hk.Params, actions=None) -> jnp.ndarray:
        def vec(state):
            return jnp.concatenate(state, -1)

        horizon = self.c.imag_horizon if actions is None else actions.shape[1]
        sequence = jnp.zeros(
            (initial_features.shape[0], horizon,
             self.c.rssm.stochastic_size + self.c.rssm.deterministic_size)
        )
        state = jnp.split(initial_features, (self.c.rssm.stochastic_size,), -1)
        keys = hk.next_rng_keys(horizon)
        for t, key in enumerate(keys):
            action = actor.apply(
                actor_params,
                jax.lax.stop_gradient(vec(state))
            ).sample(seed=key) if actions is None else actions[:, t]
            _, state = self.prior(state, action)
            sequence = sequence.at[:, t].set(vec(state))
        return sequence

    def observe_sequence(
        self, observations: Observation, actions: Action
    ) -> Tuple[Tuple[tfd.MultivariateNormalDiag, tfd.MultivariateNormalDiag], jnp.ndarray]:
        priors, posteriors = [], []
        features = jnp.zeros(
            observations.shape[:2] + (self.c.rssm.stochastic_size + self.c.rssm.deterministic_size,))
        state = init_state(
            observations.shape[0], self.c.rssm.stochastic_size, self.c.rssm.deterministic_size)
        for t in range(observations.shape[1]):
            (prior, posterior), state = self.__call__(
                state, actions[:, t], observations[:, t])
            priors.append((prior.mean(), prior.stddev()))
            posteriors.append((posterior.mean(), posterior.stddev()))
            features = features.at[:, t].set(jnp.concatenate(state, -1))

        def joint_mvn(dists):
            mus, stddevs = jnp.asarray(
                list(zip(*dists))).transpose((0, 2, 1, 3))
            return tfd.MutlivariateNormalDiag(mus, stddevs)

        prior = joint_mvn(priors)
        posterior = joint_mvn(posteriors)
        return (prior, posterior), features
