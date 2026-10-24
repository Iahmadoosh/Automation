import random
import time
from collections.abc import Callable


class Throttle:
    """Randomized human-scale pauses between individual browser actions."""

    def __init__(
        self,
        min_delay: float,
        max_delay: float,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if min_delay < 0 or max_delay < min_delay:
            raise ValueError("Require 0 <= min_delay <= max_delay")
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 - timing jitter, not crypto

    def pause(self, scale: float = 1.0) -> float:
        delay = self._rng.uniform(self.min_delay, self.max_delay) * scale
        self._sleep(delay)
        return delay


class RateLimiter:
    """Enforces a minimum (jittered) interval between expensive operations, e.g. applications."""

    def __init__(
        self,
        min_interval: float,
        jitter: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self.min_interval = min_interval
        self.jitter = jitter
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()  # noqa: S311 - timing jitter, not crypto
        self._last: float | None = None

    def wait(self) -> float:
        waited = 0.0
        if self._last is not None:
            target = self.min_interval * (1 + self._rng.uniform(0, self.jitter))
            waited = max(0.0, target - (self._clock() - self._last))
            if waited:
                self._sleep(waited)
        self._last = self._clock()
        return waited


def with_backoff[T](
    fn: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Call fn, retrying listed exceptions with exponential backoff and jitter."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    rng = rng or random.Random()  # noqa: S311 - timing jitter, not crypto
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            if attempt == attempts:
                raise
            ceiling = min(max_delay, base_delay * 2 ** (attempt - 1))
            delay = rng.uniform(ceiling / 2, ceiling)
            if on_retry:
                on_retry(attempt, exc, delay)
            sleep(delay)
    raise AssertionError("unreachable")
