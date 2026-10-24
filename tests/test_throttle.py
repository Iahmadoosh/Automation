import random

import pytest

from core.throttle import RateLimiter, Throttle, with_backoff


def test_throttle_pause_within_bounds():
    slept: list[float] = []
    throttle = Throttle(1.0, 2.0, sleep=slept.append, rng=random.Random(1))
    for _ in range(20):
        throttle.pause()
    assert all(1.0 <= s <= 2.0 for s in slept)


def test_throttle_rejects_bad_bounds():
    with pytest.raises(ValueError):
        Throttle(3, 1)


def test_rate_limiter_waits_remaining_interval():
    now = [100.0]
    slept: list[float] = []
    limiter = RateLimiter(60, jitter=0, sleep=slept.append, clock=lambda: now[0])
    assert limiter.wait() == 0
    now[0] += 20
    assert limiter.wait() == pytest.approx(40)


def test_backoff_retries_then_succeeds():
    calls = {"n": 0}
    slept: list[float] = []

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("slow")
        return "ok"

    assert with_backoff(flaky, base_delay=1, sleep=slept.append, retry_on=(TimeoutError,)) == "ok"
    assert len(slept) == 2 and 0.5 <= slept[0] <= 1 and 1 <= slept[1] <= 2


def test_backoff_does_not_retry_unlisted_errors():
    def boom() -> None:
        raise KeyError("x")

    with pytest.raises(KeyError):
        with_backoff(boom, retry_on=(TimeoutError,), sleep=lambda _: None)


def test_backoff_gives_up_after_attempts():
    with pytest.raises(TimeoutError):
        with_backoff(
            lambda: (_ for _ in ()).throw(TimeoutError()),
            attempts=2,
            retry_on=(TimeoutError,),
            sleep=lambda _: None,
        )
