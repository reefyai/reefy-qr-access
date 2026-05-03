"""In-memory event bus for real-time SSE updates."""

import threading
import queue
import json


class EventBus:
    """Pub/sub event bus. Detector pushes events, SSE clients subscribe."""

    def __init__(self):
        self._subscribers = []
        self._lock = threading.Lock()

    def subscribe(self):
        """Create a new subscriber queue."""
        q = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        """Remove a subscriber queue."""
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def publish(self, event_data):
        """Push an event to all subscribers."""
        msg = json.dumps(event_data)
        with self._lock:
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)


# Global singleton (access-log events)
event_bus = EventBus()

# Separate bus for email-job state changes. Kept independent so the
# logs SSE handler doesn't have to filter unrelated payloads, and the
# email SSE handler doesn't pick up access-log noise.
email_bus = EventBus()
