"""Persisted scheduling and challenge handling for an injected account transport."""

import asyncio
import random
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta, timezone
from pathlib import Path

from loguru import logger

from sqlite_runtime import connect_sqlite


class PlatformChallenge(Exception):
    """An account transport stopped for a platform verification or risk signal."""


@dataclass(frozen=True)
class AccountTraffic48h:
    exposures: int
    views: int

    def __post_init__(self):
        if any(type(count) is not int or count < 0 for count in (self.exposures, self.views)):
            raise ValueError("48 小时曝光量和浏览量必须为非负整数")


class AccountNurtureScheduler:
    """Record offline sessions and reconcile authorized account health observations."""

    def __init__(self, database, *, rng=None):
        self.database = database
        self.rng = rng if rng is not None else random.Random()

    def simulate_session(self, account_id, keyword, *, when):
        state = self.database.get_account_nurture_state(account_id, when=when)
        if state is None or state["base_lat"] is None or state["base_lon"] is None or not state["current_city"]:
            raise ValueError("账号需要设置商圈坐标和城市")
        if not keyword:
            raise ValueError("搜索关键词不能为空")
        events = (
            {"action": "home_feed"},
            {"action": "local_trending", "city": state["current_city"],
             "dwell_seconds": self.rng.randint(5, 15)},
            {"action": "category_search", "keyword": keyword},
        )
        if not self.database.record_account_nurture_session(account_id, when):
            raise ValueError("会话日期早于账号已记录的日期，或账号状态已移除")
        logger.info("账号 {} 离线养号会话已记录，动作数 {}", account_id, len(events))
        return events

    def reconcile_traffic(self, account_id, *, recent: AccountTraffic48h, previous: AccountTraffic48h,
                          min_exposure_ratio: float, health_score: float,
                          recovery_health_score: float) -> None:
        import rate_limiter

        if not 0 < min_exposure_ratio <= 1 or not 0 <= recovery_health_score <= 100:
            raise ValueError("曝光保留比例必须在 (0, 1] 内，恢复健康分必须在 0 至 100 之间")
        silent = None
        if previous.exposures > 0:
            dropped = recent.exposures < previous.exposures * min_exposure_ratio
            if dropped:
                silent = True
            elif health_score >= recovery_health_score:
                circuit = rate_limiter.circuit_status()
                if (not circuit["global_seconds"] and account_id not in circuit["accounts"]
                        and account_id not in circuit["half_open"]):
                    silent = False
        self.database.update_account_nurture_state(account_id, health_score=health_score,
                                                   is_silent_mode=silent)
        if silent is True:
            logger.warning("账号 {} 近 48 小时曝光从 {} 降至 {}，浏览从 {} 变为 {}，已静默",
                           account_id, previous.exposures, recent.exposures, previous.views, recent.views)

    def reconcile_seller_stats(self, account_id, platform_account_id, response) -> bool:
        from seller_api import parse_seller_exposure_stats

        rows = parse_seller_exposure_stats(response, platform_account_id)
        return self.evaluate_exposure_health(account_id, rows)

    def evaluate_exposure_health(self, account_id, rows) -> bool:
        days = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("date"), str):
                raise ValueError("卖家统计日期缺失")
            day = date.fromisoformat(row["date"])
            if day.isoformat() != row["date"] or day in days:
                raise ValueError("卖家统计日期格式错误或重复")
            days[day] = AccountTraffic48h(exposures=row.get("exposure"), views=row.get("pv"))
        previous_day, recent_day = sorted(days)[-2:]
        if recent_day - previous_day != timedelta(days=1):
            raise ValueError("卖家统计最近两天必须连续")
        previous, recent = days[previous_day], days[recent_day]
        if previous.exposures <= 50 or (previous.exposures - recent.exposures) * 5 < previous.exposures * 3:
            return False
        applied = self.database.record_account_health_event(
            account_id, recent_day.isoformat(), event="exposure_drop",
        )
        if applied:
            logger.warning("账号 {} 曝光从 {} 降至 {}，浏览从 {} 变为 {}，健康分扣 35",
                           account_id, previous.exposures, recent.exposures, previous.views, recent.views)
        return applied

    def poll_seller_stats(self, stop=None) -> None:
        import rate_limiter
        import seller_api

        if not seller_api.enabled():
            return
        for account_id, cookies in self.database.get_all_cookies().items():
            if stop is not None and stop.is_set():
                return
            if (not self.database.get_cookie_status(account_id)
                    or rate_limiter.circuit_blocked(account_id)[0]):
                continue
            try:
                rows = seller_api.fetch_seller_exposure_stats(cookies)
                if self.database.get_cookie(account_id) == cookies:
                    self.evaluate_exposure_health(account_id, rows)
            except seller_api.SellerApiError as exc:
                from mtop_response import RISK_MARKERS, has_marker
                if has_marker(" | ".join(exc.ret), RISK_MARKERS):
                    rate_limiter.trip_circuit(account_id, reason="卖家统计接口收到平台风控反馈")
                else:
                    logger.warning("账号 {} 卖家统计请求未成功", account_id)
            except Exception as exc:
                logger.warning("账号 {} 卖家统计同步失败（{}）", account_id, type(exc).__name__)

    async def run_seller_stats(self, stop) -> None:
        while not stop.is_set():
            await asyncio.to_thread(self.poll_seller_stats, stop)
            try:
                await asyncio.wait_for(stop.wait(), 3600)
            except asyncio.TimeoutError:
                continue


class AccountNurturing:
    start = 10 * 3600
    end = 21 * 3600

    def __init__(self, path, *, rng=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.rng = rng or random.Random()
        with closing(connect_sqlite(self.path)) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS nurturing_days (
                    account TEXT NOT NULL, day TEXT NOT NULL, actions INTEGER NOT NULL DEFAULT 0,
                    blocked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (account, day)
                );
                CREATE TABLE IF NOT EXISTS nurturing_slots (
                    account TEXT NOT NULL, day TEXT NOT NULL, run_at INTEGER NOT NULL,
                    claimed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (account, day, run_at)
                );
            """)

    def schedule(self, account, day):
        with closing(connect_sqlite(self.path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            created = conn.execute("INSERT OR IGNORE INTO nurturing_days(account, day) VALUES(?,?)",
                                   (account, day)).rowcount
            if created:
                slots = self.rng.sample(range(self.start, self.end), self.rng.randint(1, 2))
                conn.executemany(
                    "INSERT INTO nurturing_slots(account, day, run_at) VALUES(?,?,?)",
                    [(account, day, slot) for slot in slots],
                )
            return [row[0] for row in conn.execute(
                "SELECT run_at FROM nurturing_slots WHERE account=? AND day=? ORDER BY run_at",
                (account, day),
            )]

    def reserve_action(self, account, day):
        self.schedule(account, day)
        with closing(connect_sqlite(self.path)) as conn, conn:
            return bool(conn.execute(
                "UPDATE nurturing_days SET actions=actions+1 "
                "WHERE account=? AND day=? AND actions<5 AND blocked=0", (account, day),
            ).rowcount)

    def _claim_slot(self, account, day, second):
        self.schedule(account, day)
        with closing(connect_sqlite(self.path)) as conn, conn:
            return bool(conn.execute(
                "UPDATE nurturing_slots SET claimed=1 WHERE account=? AND day=? AND claimed=0 "
                "AND run_at=(SELECT min(run_at) FROM nurturing_slots "
                "WHERE account=? AND day=? AND run_at<=? AND claimed=0) "
                "AND EXISTS (SELECT 1 FROM nurturing_days WHERE account=? AND day=? AND blocked=0 AND actions<5) "
                "AND NOT EXISTS (SELECT 1 FROM nurturing_slots WHERE account=? AND day=? AND claimed=1)",
                (account, day, account, day, second, account, day, account, day),
            ).rowcount)

    def _block(self, account, day):
        with closing(connect_sqlite(self.path)) as conn, conn:
            conn.execute("UPDATE nurturing_days SET blocked=1 WHERE account=? AND day=?", (account, day))

    def _finish_slot(self, account, day):
        with closing(connect_sqlite(self.path)) as conn, conn:
            conn.execute("UPDATE nurturing_slots SET claimed=2 WHERE account=? AND day=? AND claimed=1",
                         (account, day))

    async def run_due(self, account, transport, *, when, sleep=asyncio.sleep):
        started = time.monotonic()
        if when.tzinfo is None:
            raise ValueError("账号调度时间必须包含时区")
        local = when.astimezone(timezone(timedelta(hours=8)))
        second = local.hour * 3600 + local.minute * 60 + local.second
        day = local.date().isoformat()
        if not self.start <= second < self.end or not await asyncio.to_thread(
            self._claim_slot, account, day, second,
        ):
            return "idle"

        async def execute(action, *args):
            await sleep(self.rng.uniform(3, 10))
            if second + time.monotonic() - started >= self.end:
                return False, None
            if not await asyncio.to_thread(self.reserve_action, account, day):
                return False, None
            return True, await action(account, *args)

        try:
            allowed, items = await execute(transport.recommendations)
            if not allowed:
                return "quota"
            unique_items = list(dict.fromkeys(items))
            viewed = self.rng.sample(unique_items, min(len(unique_items), self.rng.randint(1, 2)))
            for item in viewed:
                allowed, _ = await execute(transport.detail, item)
                if not allowed:
                    return "quota"
            if viewed:
                allowed, _ = await execute(transport.interact, self.rng.choice(viewed))
                if not allowed:
                    return "quota"
            return "completed"
        except PlatformChallenge:
            await asyncio.to_thread(self._block, account, day)
            logger.warning("账号 {} 遇到平台验证，当日余下活动已停止", account)
            return "challenge"
        finally:
            await asyncio.to_thread(self._finish_slot, account, day)
