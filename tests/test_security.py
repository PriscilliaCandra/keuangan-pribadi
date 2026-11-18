from keuangan.security import RateLimiter


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_blocks_after_limit_and_recovers_after_window():
    clock = Clock()
    rl = RateLimiter(3, 60, now=clock)
    assert not rl.blocked("a")
    for _ in range(3):
        rl.record("a")
    assert rl.blocked("a") and rl.retry_after("a") > 0
    clock.t += 59
    assert rl.blocked("a")
    clock.t += 2
    assert not rl.blocked("a") and rl.retry_after("a") == 0


def test_keys_are_independent_and_reset_clears():
    rl = RateLimiter(2, 60, now=Clock())
    rl.record("a"); rl.record("a")
    assert rl.blocked("a") and not rl.blocked("b")
    rl.reset("a")
    assert not rl.blocked("a")
    rl.reset("never-seen")  # tidak error


def test_old_events_expire_individually():
    clock = Clock()
    rl = RateLimiter(2, 60, now=clock)
    rl.record("a")
    clock.t += 40
    rl.record("a")
    assert rl.blocked("a")
    clock.t += 25            # kejadian pertama kedaluwarsa, kedua belum
    assert not rl.blocked("a")


def test_idle_keys_are_dropped_from_memory():
    clock = Clock()
    rl = RateLimiter(5, 10, now=clock)
    for i in range(100):
        rl.record(i)
    clock.t += 11
    for i in range(100):
        rl.blocked(i)
    assert rl._events == {}
