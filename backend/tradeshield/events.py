"""Activity log. Category 'decision' = why an order was allowed/blocked.
Category 'incident' = halts, kill switch, shield changes, recovery, mismatches."""


class EventLog:
    def __init__(self, db, clock):
        self.db, self.clock = db, clock
        self.listeners = []          # callables(event_dict) - used to push events over WebSocket

    def log(self, category, kind, message, sub_id=None, data=None):
        ts = self.clock()
        eid = self.db.add_event(ts, category, kind, sub_id, message, data)
        ev = {"id": eid, "ts": ts, "category": category, "kind": kind, "sub_id": sub_id,
              "message": message, "data": data or {}}
        for cb in list(self.listeners):
            try:
                cb(ev)
            except Exception:
                pass
        return ev
