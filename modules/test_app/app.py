from __future__ import annotations

from datetime import datetime

from flask import Flask, jsonify, request
from sqlalchemy import Column, Integer, String, select
from sqlalchemy.orm import DeclarativeBase

from modules.database_handler import TransactionManager
from modules.timezone_handler import TimezoneAware


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    timezone = Column(String(50), nullable=False)
    hour = Column(Integer, nullable=False)
    minute = Column(Integer, nullable=False)
    created_utc = Column(String(30), nullable=False)


manager = TransactionManager("sqlite:///test_app.db")


def create_app() -> Flask:
    app = Flask(__name__)
    Base.metadata.create_all(manager.engine)

    # ── Timezone routes ──

    @app.route("/time/now/<path:tz_name>")
    def get_now(tz_name: str):
        """Get current time in a timezone."""
        try:
            tz = TimezoneAware(tz_name)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        return jsonify({
            "timezone": tz.timezone_name,
            "now": tz.now.isoformat(),
            "utc": tz.utc.isoformat(),
            "timestamp": tz.timestamp,
        })

    @app.route("/time/diff")
    def time_diff():
        """
        Compare two times.
        Query params: tz1, hour1, min1, tz2, hour2, min2
        Example: /time/diff?tz1=America/Sao_Paulo&hour1=11&min1=0&tz2=America/Sao_Paulo&hour2=13&min2=0
        """
        try:
            tz1_name = request.args["tz1"]
            h1, m1 = int(request.args.get("hour1", 0)), int(request.args.get("min1", 0))
            tz2_name = request.args["tz2"]
            h2, m2 = int(request.args.get("hour2", 0)), int(request.args.get("min2", 0))
        except (KeyError, ValueError) as e:
            return jsonify({"error": f"Missing or invalid param: {e}"}), 400

        today = datetime.now().date()
        t1 = TimezoneAware(tz1_name, datetime(today.year, today.month, today.day, h1, m1))
        t2 = TimezoneAware(tz2_name, datetime(today.year, today.month, today.day, h2, m2))

        delta = t2 - t1
        return jsonify({
            "t1": str(t1),
            "t2": str(t2),
            "difference": str(delta),
            "total_seconds": delta.total_seconds(),
            "t1_utc": t1.utc.isoformat(),
            "t2_utc": t2.utc.isoformat(),
            "t2_gt_t1": t2 > t1,
            "t2_eq_t1": t2 == t1,
        })

    @app.route("/time/convert/<path:tz_from>/<path:tz_to>")
    def convert_time(tz_from: str, tz_to: str):
        """Convert current time from one timezone to another."""
        try:
            source = TimezoneAware(tz_from)
            converted = source.to(tz_to)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        return jsonify({
            "from": str(source),
            "to": str(converted),
            "same_instant": source == converted,
        })

    # ── Database routes ──

    @app.route("/events", methods=["POST"])
    def create_event():
        """
        Create an event.
        JSON body: {"name": "Meeting", "timezone": "America/Sao_Paulo", "hour": 14, "minute": 30}
        """
        data = request.get_json(force=True)
        try:
            tz = TimezoneAware(data["timezone"])
        except (KeyError, ValueError) as e:
            return jsonify({"error": str(e)}), 400

        event = Event(
            name=data["name"],
            timezone=data["timezone"],
            hour=data.get("hour", 0),
            minute=data.get("minute", 0),
            created_utc=tz.utc.isoformat(),
        )

        with manager.session() as session:
            session.add(event)
            session.flush()
            event_id = event.id

        return jsonify({"id": event_id, "created_utc": event.created_utc}), 201

    @app.route("/events", methods=["GET"])
    def list_events():
        """List all events with their timezone-aware times."""
        with manager.read_only() as session:
            events = session.execute(select(Event)).scalars().all()
            result = []
            for e in events:
                tz = TimezoneAware(e.timezone, datetime(
                    datetime.now().year, datetime.now().month, datetime.now().day,
                    e.hour, e.minute,
                ))
                result.append({
                    "id": e.id,
                    "name": e.name,
                    "timezone": e.timezone,
                    "local_time": str(tz),
                    "utc_time": tz.utc.isoformat(),
                    "created_utc": e.created_utc,
                })

        return jsonify(result)

    @app.route("/events/bulk", methods=["POST"])
    def bulk_create_events():
        """
        Bulk create events.
        JSON body: [{"name": "...", "timezone": "...", "hour": 10, "minute": 0}, ...]
        """
        data = request.get_json(force=True)
        if not isinstance(data, list):
            return jsonify({"error": "Expected a JSON array"}), 400

        items = []
        for entry in data:
            try:
                tz = TimezoneAware(entry["timezone"])
            except (KeyError, ValueError) as e:
                return jsonify({"error": str(e)}), 400
            items.append(Event(
                name=entry["name"],
                timezone=entry["timezone"],
                hour=entry.get("hour", 0),
                minute=entry.get("minute", 0),
                created_utc=tz.utc.isoformat(),
            ))

        with manager.session() as session:
            count = manager.bulk_operation(session, items, batch_size=10)

        return jsonify({"created": count}), 201

    @app.route("/events/nested-demo", methods=["POST"])
    def nested_demo():
        """
        Demo nested transactions.
        Creates a 'safe' event, then attempts a nested savepoint that fails.
        The safe event persists; the nested one rolls back.
        """
        with manager.session() as session:
            safe = Event(
                name="Safe Event",
                timezone="UTC",
                hour=12, minute=0,
                created_utc=TimezoneAware("UTC").utc.isoformat(),
            )
            session.add(safe)
            session.flush()

            nested_rolled_back = False
            try:
                with manager.nested(session):
                    risky = Event(
                        name="Risky Event",
                        timezone="UTC",
                        hour=13, minute=0,
                        created_utc=TimezoneAware("UTC").utc.isoformat(),
                    )
                    session.add(risky)
                    session.flush()
                    raise RuntimeError("Simulated nested failure")
            except RuntimeError:
                nested_rolled_back = True

        return jsonify({
            "safe_event_persisted": True,
            "nested_rolled_back": nested_rolled_back,
            "message": "'Safe Event' was saved, 'Risky Event' was rolled back.",
        })

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, port=5000)
