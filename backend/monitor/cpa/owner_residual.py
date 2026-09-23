"""Infer owner usage that is outside the collected request logs."""

from dataclasses import dataclass
from decimal import Decimal

from .capacity_estimate import particle_capacity_estimate

ZERO = Decimal("0")
HUNDRED = Decimal("100")
MONEY_PRECISION = Decimal("0.000001")


@dataclass(frozen=True)
class OwnerResidual:
    """A read-time estimate, never a synthetic request event."""

    participant_id: int
    amount_usd: Decimal


def owner_at(bindings, account_id: int, observed_at):
    """Return the account fallback owner active at an observation timestamp."""

    for binding in reversed(bindings.get(("account", account_id), ())):
        if binding.started_at <= observed_at and (
            binding.ended_at is None or observed_at < binding.ended_at
        ):
            return binding.participant_id
    return None


def estimate_unlogged_owner_cost(
    *,
    account_id: int,
    observation,
    known_cost: Decimal,
    bindings,
) -> OwnerResidual | None:
    """Estimate cumulative upstream cost not present in the request log.

    The quota percentage is observed continuously.  The particle posterior
    supplies the current full-cycle dollar capacity, so the cumulative
    upstream spend estimate is ``capacity * used_percent / 100``.  The
    difference from collected, bound-key costs is assigned to the active
    account owner.  Callers must additionally verify that collection coverage
    is complete before applying it.
    """

    if observation is None or observation.upstream_used_percent <= ZERO:
        return None
    participant_id = owner_at(bindings, account_id, observation.observed_at)
    if participant_id is None:
        return None
    posterior = particle_capacity_estimate(observation)
    if posterior:
        if posterior["prior_only"]:
            return None
        capacity = Decimal(str(posterior["capacity_usd"]))
    elif observation.valid_sample and observation.effective_usd_per_percent > ZERO:
        # The constant-average model does not expose a particle posterior.
        capacity = observation.effective_usd_per_percent * HUNDRED
    else:
        return None
    consumed = (
        capacity
        * min(HUNDRED, max(ZERO, observation.upstream_used_percent))
        / HUNDRED
    )
    residual = max(ZERO, consumed - max(ZERO, known_cost)).quantize(
        MONEY_PRECISION
    )
    if residual <= ZERO:
        return None
    return OwnerResidual(participant_id=participant_id, amount_usd=residual)
