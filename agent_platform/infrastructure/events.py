"""Best-effort commit wakeups; persisted event cursors remain the authority.

Redis carries only tenant/run identifiers. It is neither an event store nor an
authorization source. A bounded background loop handles all remote I/O, so a
failed notification cannot delay a model delta inside a tenant transaction.
"""

import asyncio
import hashlib
import json
import threading
from contextlib import asynccontextmanager
from uuid import uuid4

from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

_PENDING = "platform_commit_notifications"
_configuration_lock = threading.Lock()


def commit_notification(connection, tenant_id: str, run_id: str, sequence: int):
    """Coalesce an event wakeup in the transaction managed by Database."""
    pending = connection.info.get(_PENDING)
    if pending is not None:
        pending[(tenant_id, run_id)] = max(
            sequence, pending.get((tenant_id, run_id), 0)
        )


class EventWatch:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.event = asyncio.Event()
        self.version = 0
        self._lock = threading.Lock()
        self._scheduled = False

    def schedule(self):
        with self._lock:
            if self._scheduled:
                return
            self._scheduled = True
        try:
            self.loop.call_soon_threadsafe(self.wake)
        except RuntimeError:
            with self._lock:
                self._scheduled = False

    def wake(self):
        with self._lock:
            self._scheduled = False
        self.version += 1
        self.event.set()

    async def wait(self, observed: int, timeout: float = 2.0) -> bool:
        # Clear before comparing: a notification between the DB read and wait
        # must cause an immediate replay rather than a lost-wakeup timeout.
        self.event.clear()
        if self.version != observed:
            return True
        try:
            await asyncio.wait_for(self.event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False


class EventNotifier:
    def __init__(
        self,
        redis_url: str | None = None,
        *,
        namespace: str = "default",
        redis_factory=Redis.from_url,
        io_timeout: float = 0.1,
        retry_seconds: float = 0.1,
        retry_max_seconds: float = 5.0,
        max_pending: int = 4096,
    ):
        self.redis_url = redis_url
        self.channel = "agent-platform:events:v1:" + namespace
        self._origin = uuid4().hex
        self._factory = redis_factory
        self._io_timeout = io_timeout
        self._retry_seconds = retry_seconds
        self._retry_max_seconds = retry_max_seconds
        self._max_pending = max_pending
        self._lock = threading.Lock()
        self._watches = {}
        self._pending = {}
        self._stop = threading.Event()
        self._work = threading.Event()
        self._thread = None
        self._closed = False
        self._metrics = {
            "committed_wakeups": 0,
            "redis_published": 0,
            "redis_received": 0,
            "redis_failures": 0,
            "coalesced_or_dropped": 0,
            "redis_connected": False,
        }
        if redis_url:
            self._thread = threading.Thread(
                target=self._run, name="platform-event-notifier", daemon=True
            )
            self._thread.start()

    def stats(self):
        with self._lock:
            return {
                **self._metrics,
                "pending": len(self._pending),
                "watchers": sum(len(group) for group in self._watches.values()),
                "redis_configured": bool(self.redis_url),
            }

    def _wake(self, key):
        with self._lock:
            watches = tuple(self._watches.get(key, ()))
        for watch in watches:
            # Coalesce local callbacks too: a stalled event loop cannot grow a
            # callback queue for every delta. The next replay returns all rows.
            watch.schedule()

    def publish(self, tenant_id: str, run_id: str, sequence: int):
        """Nonblocking: only local wakeups and bounded in-memory enqueueing."""
        key = (tenant_id, run_id)
        self._wake(key)
        with self._lock:
            if self._closed:
                return
            self._metrics["committed_wakeups"] += 1
            if self.redis_url:
                if key in self._pending:
                    self._metrics["coalesced_or_dropped"] += 1
                elif len(self._pending) >= self._max_pending:
                    # Polling repairs a lost wakeup; never grow without bound.
                    self._pending.pop(next(iter(self._pending)))
                    self._metrics["coalesced_or_dropped"] += 1
                self._pending[key] = max(sequence, self._pending.get(key, 0))
        self._work.set()

    @asynccontextmanager
    async def watch(self, tenant_id: str, run_id: str):
        key, watch = (tenant_id, run_id), EventWatch()
        with self._lock:
            self._watches.setdefault(key, set()).add(watch)
        try:
            yield watch
        finally:
            with self._lock:
                group = self._watches.get(key)
                if group:
                    group.discard(watch)
                    if not group:
                        self._watches.pop(key, None)

    def _received(self, message):
        if message.get("type") != "message":
            return
        data = message.get("data")
        if not isinstance(data, (str, bytes)) or len(data) > 512:
            return
        try:
            payload = json.loads(data)
            if not isinstance(payload, dict):
                return
            if payload.get("origin") == self._origin:
                return  # Local commit already woke this process's subscribers.
            tenant, run = payload["tenant_id"], payload["run_id"]
            if not all(
                isinstance(value, str) and 0 < len(value) <= 64
                for value in (tenant, run)
            ):
                return
        except (KeyError, TypeError, ValueError):
            return
        with self._lock:
            self._metrics["redis_received"] += 1
        self._wake((tenant, run))

    def _run(self):
        delay = self._retry_seconds
        while not self._stop.is_set():
            client = subscriber = None
            try:
                client = self._factory(
                    self.redis_url,
                    socket_timeout=self._io_timeout,
                    socket_connect_timeout=self._io_timeout,
                    decode_responses=True,
                    retry_on_timeout=False,
                    retry=Retry(NoBackoff(), 0),
                )
                subscriber = client.pubsub(ignore_subscribe_messages=True)
                subscriber.subscribe(self.channel)
                with self._lock:
                    self._metrics["redis_connected"] = True
                delay = self._retry_seconds
                # Reconnect also wakes local readers to repair messages lost
                # while disconnected, without trusting any Redis payload.
                with self._lock:
                    keys = tuple(self._watches)
                for key in keys:
                    self._wake(key)
                while not self._stop.is_set():
                    with self._lock:
                        pending = tuple(self._pending.items())[:32]
                    for (tenant, run), sequence in pending:
                        client.publish(
                            self.channel,
                            json.dumps(
                                {
                                    "tenant_id": tenant,
                                    "run_id": run,
                                    "sequence": sequence,
                                    "origin": self._origin,
                                }
                            ),
                        )
                        with self._lock:
                            if self._pending.get((tenant, run)) == sequence:
                                self._pending.pop((tenant, run), None)
                            self._metrics["redis_published"] += 1
                    if message := subscriber.get_message(timeout=0.02):
                        self._received(message)
                    elif not pending:
                        self._work.wait(0.02)
                        self._work.clear()
            except (RedisError, OSError, ValueError):
                with self._lock:
                    self._metrics["redis_failures"] += 1
                    self._metrics["redis_connected"] = False
                if self._stop.wait(delay):
                    return
                delay = min(delay * 2, self._retry_max_seconds)
            finally:
                if subscriber is not None:
                    try:
                        subscriber.close()
                    except (RedisError, OSError):
                        pass
                if client is not None:
                    try:
                        client.close()
                    except (RedisError, OSError):
                        pass

    def close(self):
        with self._lock:
            self._closed = True
        self._stop.set()
        self._work.set()
        if self._thread:
            self._thread.join(timeout=0.5)


def configure_notifications(db, redis_url: str | None = None) -> EventNotifier:
    """One notifier per database composition, shared by embedded API/Worker."""
    with _configuration_lock:
        notifier = getattr(db, "event_notifier", None)
        if notifier is None:
            url = db.engine.url
            namespace = hashlib.sha256(
                repr((url.host, url.port, url.database)).encode()
            ).hexdigest()[:20]
            notifier = EventNotifier(redis_url, namespace=namespace)
            db.event_notifier = notifier
        elif notifier.redis_url != redis_url:
            raise ValueError("Database event notifier is already configured")
        return notifier
