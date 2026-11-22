"""带宽限速（令牌桶的"虚拟时间"写法）。

同一用户的所有连接、所有传输共用一个限速器，所以 VIP / 普通用户的速度上限是按"身份"而不是按连接计算。
acquire(n)：声明将要传输 n 字节，必要时睡眠，使长期平均速率不超过 rate。
"""
import asyncio
import time


class RateLimiter:
    def __init__(self, rate: int, burst_seconds: float = 0.25):
        self.rate = float(rate)             # 字节/秒
        self.burst = burst_seconds          # 允许的突发时长，让小文件/首包不被拖慢
        self._t = 0.0                       # 下一次允许发送的时间点

    def reserve(self, n: int, now: float = None) -> float:
        """登记 n 字节，返回需要等待的秒数（不睡眠，便于测试）。"""
        now = time.monotonic() if now is None else now
        self._t = max(self._t, now - self.burst)
        delay = max(0.0, self._t - now)
        self._t += n / self.rate
        return delay

    async def acquire(self, n: int) -> None:
        delay = self.reserve(n)
        if delay > 0:
            await asyncio.sleep(delay)