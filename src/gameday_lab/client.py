"""Part C — ResilientClient: compose classification, retry, breaker and fallback.

The simulator calls ``start`` for every logical LLM call, then ``on_success``
or ``on_error`` for every attempt. You answer with ``Send`` (try a provider,
maybe after a delay) or ``GiveUp`` (user gets a static degraded answer).

Tests: tests/test_client.py, then `make compare` (6 incident tapes).

Per-call scratch space lives in ``call.state`` (a dict). Suggested keys:
    "ladder": list[str]  providers this call may use, in order
    "idx":    int        position of the provider we are currently on
    "tries":  int        attempts already made on that provider

Decision table for ``on_error`` (implement it!):

| ErrorKind        | breaker              | then                                                    |
|------------------|----------------------|---------------------------------------------------------|
| CLIENT_ERROR     | release (not health) | GiveUp("client_error") — never retry, never fail over   |
| QUOTA_EXHAUSTED  | release              | cooldown provider for quota_cooldown_s; fail over now   |
| RATE_LIMITED     | release              | cooldown provider until now + retry_after; fail over now|
| OVERLOADED /     | on_failure           | retry SAME provider after backoff.delay(tries) if       |
| SERVER_ERROR /   |                      |   tries < max_attempts_per_provider AND budget allows   |
| TIMEOUT          |                      |   AND it fits the deadline; otherwise fail over         |

Cooldowns are per provider and shared by all calls (other calls must avoid a
rate-limited provider too). When every provider in the ladder is unusable,
you may wait for a *short* (<= 5 s) rate-limit cooldown if it fits the deadline.
"""

from __future__ import annotations

import random
from typing import Any

from gameday_lab.breaker import BreakerState, Permit, SlidingWindowBreaker  # noqa: F401
from gameday_lab.errors import ErrorKind, classify, counts_toward_breaker  # noqa: F401
from gameday_lab.retry import FullJitterBackoff, RetryBudget
from gameday_lab.sim.contracts import (
    Action,
    Attempt,
    BaseClient,
    CallContext,
    GiveUp,
    ProviderError,
    ProviderInfo,
    Send,
)

RATE_LIMIT_WAIT_MAX_S = 5.0  # only wait for short (rate-limit) cooldowns, never for quota


class ResilientClient(BaseClient):
    name = "resilient"

    def __init__(
        self,
        providers: dict[str, ProviderInfo],
        fallback_order: list[str] | tuple[str, ...],
        settings: dict[str, Any],
        seed: int = 0,
    ) -> None:
        """PROVIDED: reads the `client:` block of configs/gameday.yaml."""
        self.providers = providers
        self.order = list(fallback_order)
        self.timeout_s = float(settings.get("attempt_timeout_s", 8.0))
        self.max_attempts_per_provider = int(settings.get("max_attempts_per_provider", 2))
        self.min_time_left_s = float(settings.get("min_time_left_s", 1.0))
        self.quota_cooldown_s = float(settings.get("quota_cooldown_s", 300.0))
        self.backoff = FullJitterBackoff(
            float(settings.get("backoff_base_s", 0.25)),
            float(settings.get("backoff_cap_s", 4.0)),
            random.Random(seed),
        )
        self.budget = RetryBudget(
            float(settings.get("retry_budget_ratio", 0.1)),
            float(settings.get("retry_budget_min_per_s", 0.5)),
            float(settings.get("retry_budget_max_tokens", 20.0)),
        )
        breaker_cfg = dict(settings.get("breaker", {}))
        self.breakers = {name: SlidingWindowBreaker(name, **breaker_cfg) for name in providers}
        self.cooldown_until: dict[str, float] = {name: 0.0 for name in providers}

    # ------------------------------------------------------------------ provided helper
    def _send(self, call: CallContext, provider: str, now: float, delay: float) -> Action | None:
        """PROVIDED. Build a Send for ``provider`` (after ``delay``), or None if its breaker refuses.

        Takes a breaker permit (carried in ``tag``), and caps the attempt timeout
        so it never runs past the call's deadline. Returns GiveUp("deadline") if
        there is not enough time left for a meaningful attempt.
        """
        permit = self.breakers[provider].allow(now)
        if permit is None:
            return None
        timeout = min(self.timeout_s, call.deadline - (now + delay))
        if timeout < self.min_time_left_s:
            self.breakers[provider].release(permit, now)
            return GiveUp("deadline")
        return Send(provider, timeout_s=timeout, delay_s=delay, tag=permit)

    # ------------------------------------------------------------------ TODO
    def ladder(self, task: str) -> list[str]:
        """``self.order`` filtered by capability —
        a "tool" call may only use providers with ``supports_tools``."""
        if task == "tool":
            return [name for name in self.order if self.providers[name].supports_tools]
        return list(self.order)

    def _failover(self, call: CallContext, now: float) -> Action:
        """Move to the next provider and route immediately."""
        call.state["idx"] = int(call.state.get("idx", 0)) + 1
        call.state["tries"] = 0
        return self._route(call, now, delay=0.0)

    def _route(self, call: CallContext, now: float, delay: float = 0.0) -> Action:
        """Pick the provider for the next attempt."""
        ladder: list[str] = call.state["ladder"]
        idx = int(call.state.get("idx", 0))
        send_at = now + delay

        while idx < len(ladder):
            provider = ladder[idx]
            call.state["idx"] = idx
            if self.cooldown_until.get(provider, 0.0) > send_at:
                idx += 1
                call.state["tries"] = 0
                delay = 0.0
                send_at = now
                continue
            action = self._send(call, provider, now, delay)
            if isinstance(action, GiveUp):
                return action
            if action is not None:
                return action
            # breaker refused — skip to next provider immediately
            idx += 1
            call.state["tries"] = 0
            delay = 0.0
            send_at = now

        call.state["idx"] = idx
        # Ladder exhausted: optionally wait for a short rate-limit cooldown
        best_wait: float | None = None
        best_provider: str | None = None
        for provider in ladder:
            cool = self.cooldown_until.get(provider, 0.0)
            wait = cool - now
            if wait <= 0 or wait > RATE_LIMIT_WAIT_MAX_S:
                continue
            if self.breakers[provider].state is BreakerState.OPEN:
                # still might become HALF_OPEN soon; check open duration
                opened = self.breakers[provider].opened_at
                open_left = self.breakers[provider].open_duration_s - (now - opened)
                if open_left > 0 and cool <= now + open_left:
                    # cooldown ends while still OPEN — skip
                    continue
                if open_left > wait:
                    wait = open_left
            if now + wait + self.min_time_left_s > call.deadline:
                continue
            if best_wait is None or wait < best_wait:
                best_wait = wait
                best_provider = provider

        if best_wait is not None and best_provider is not None:
            call.state["idx"] = ladder.index(best_provider)
            call.state["tries"] = 0
            action = self._send(call, best_provider, now, best_wait)
            if isinstance(action, GiveUp):
                return action
            if action is not None:
                return action

        return GiveUp("no_provider_available")

    def start(self, call: CallContext, now: float) -> Action:
        """Record the request in the retry budget, initialise call.state and route."""
        self.budget.record_request(now)
        call.state["ladder"] = self.ladder(call.task)
        call.state["idx"] = 0
        call.state["tries"] = 0
        return self._route(call, now, delay=0.0)

    def on_success(self, attempt: Attempt, now: float, latency_s: float) -> None:
        """Report success + latency to the provider's breaker."""
        permit = attempt.tag
        if isinstance(permit, Permit):
            self.breakers[attempt.provider].on_success(permit, now, latency_s)

    def on_error(self, attempt: Attempt, err: ProviderError, now: float) -> Action:
        """Implement the decision table in the module docstring."""
        kind = classify(err)
        permit = attempt.tag
        breaker = self.breakers[attempt.provider]
        call = attempt.call

        if counts_toward_breaker(kind):
            if isinstance(permit, Permit):
                breaker.on_failure(permit, now)
        else:
            if isinstance(permit, Permit):
                breaker.release(permit, now)

        if kind is ErrorKind.CLIENT_ERROR:
            return GiveUp("client_error")

        if kind is ErrorKind.QUOTA_EXHAUSTED:
            self.cooldown_until[attempt.provider] = now + self.quota_cooldown_s
            return self._failover(call, now)

        if kind is ErrorKind.RATE_LIMITED:
            retry_after = err.retry_after if err.retry_after is not None else 1.0
            self.cooldown_until[attempt.provider] = now + retry_after
            return self._failover(call, now)

        # OVERLOADED / SERVER_ERROR / TIMEOUT — retry same provider or fail over
        tries = int(call.state.get("tries", 0)) + 1
        call.state["tries"] = tries
        if (
            tries < self.max_attempts_per_provider
            and self.budget.try_acquire_retry(now)
        ):
            delay = self.backoff.delay(tries)
            action = self._send(call, attempt.provider, now, delay)
            if isinstance(action, GiveUp):
                return action
            if action is not None:
                return action
            # breaker refused the retry — fall through to failover

        return self._failover(call, now)

    def on_cancel(self, call: CallContext, tag: Any, now: float) -> None:
        """A delayed attempt was dropped (deadline) — release its permit."""
        if isinstance(tag, Permit):
            self.breakers[tag.breaker].release(tag, now)
