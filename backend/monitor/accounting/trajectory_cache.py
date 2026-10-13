"""Bounded, process-local numerical replay reuse, never response/permission caching."""

from dataclasses import asdict, fields
import hashlib
import json
import logging
import pickle
from threading import Lock

import numpy as np
from django.core.cache import caches

logger = logging.getLogger(__name__)
CACHE_VERSION = "trajectory-input-v1"
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
CACHE_SECONDS = 600
_locks = tuple(Lock() for _ in range(16))


def replay_fingerprint(
    model_input, *, account_id, algorithm, seed, config, capacity_profile
):
    """Hash every model field, including array dtype/shape and full data bytes.

    Counts, latest observation IDs, or a fact revision cannot identify edits to
    historical evidence. Build fresh inputs first, then hash their exact content.
    """
    digest = hashlib.sha256()
    metadata = {
        "cache_version": CACHE_VERSION,
        "account_id": account_id,
        "algorithm": algorithm,
        "seed": seed,
        "config": asdict(config),
        "capacity_profile": asdict(capacity_profile),
    }
    digest.update(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode())
    for field in fields(model_input):
        value = getattr(model_input, field.name)
        digest.update(field.name.encode())
        if isinstance(value, np.ndarray):
            descriptor = (value.dtype.str, value.shape, value.nbytes)
            digest.update(repr(descriptor).encode())
            digest.update(value.tobytes(order="C"))
        else:
            digest.update(repr(value).encode())
    return "trajectory:" + digest.hexdigest()


def cached_replay(
    model_input, *, account_id, algorithm, seed, config, capacity_profile, compute
):
    key = replay_fingerprint(
        model_input,
        account_id=account_id,
        algorithm=algorithm,
        seed=seed,
        config=config,
        capacity_profile=capacity_profile,
    )
    try:
        cache = caches["trajectory"]
    except Exception:
        # An optional cache must never make the read-only endpoint unavailable.
        return compute(
            model_input, seed=seed, config=config, capacity_profile=capacity_profile
        )
    with _locks[int(key[-8:], 16) % len(_locks)]:
        try:
            artifact = cache.get(key)
            if artifact is not None:
                # Only this dedicated, local cache stores these internal bytes.
                # Deserialization also isolates arrays from caller mutations.
                return pickle.loads(artifact)
        except Exception:
            logger.warning("Trajectory cache read failed; recalculating", exc_info=True)
        result = compute(
            model_input, seed=seed, config=config, capacity_profile=capacity_profile
        )
        try:
            artifact = pickle.dumps(result, protocol=pickle.HIGHEST_PROTOCOL)
            if len(artifact) <= MAX_ARTIFACT_BYTES:
                cache.set(key, artifact, timeout=CACHE_SECONDS)
        except Exception:
            logger.warning(
                "Trajectory cache write failed; returning fresh replay", exc_info=True
            )
        return result
