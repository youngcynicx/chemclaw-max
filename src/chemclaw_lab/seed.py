from __future__ import annotations

import json
from pathlib import Path
from .db import connect


def seed_from_json(db_path: str | Path, seed_path: str | Path) -> None:
    conn = connect(db_path)
    data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
    chemicals = [
        {
            **chemical,
            "hazard_level": chemical.get("hazard_level", "low"),
            "max_auto_delta": chemical.get("max_auto_delta"),
        }
        for chemical in data.get("chemicals", [])
        ]
    try:
        conn.executemany(
            """
            INSERT OR IGNORE INTO users (user_id, name, role)
            VALUES (:user_id, :name, :role)
            """,
            data.get("users", []),
        )
        conn.executemany(
            """
            INSERT OR IGNORE INTO projects (project_id, name, description)
            VALUES (:project_id, :name, :description)
            """,
            data.get("projects", []),
        )
        conn.executemany(
            """
            INSERT OR IGNORE INTO chemicals
              (chemical_id, name, formula, location, amount, unit, threshold,
               hazard_level, max_auto_delta)
            VALUES
              (:chemical_id, :name, :formula, :location, :amount, :unit, :threshold,
               :hazard_level, :max_auto_delta)
            """,
            chemicals,
        )
        conn.executemany(
            """
            INSERT OR IGNORE INTO instruments (instrument_id, name, status, location)
            VALUES (:instrument_id, :name, :status, :location)
            """,
            data.get("instruments", []),
        )
        conn.executemany(
            """
            INSERT OR IGNORE INTO bookings
              (booking_id, instrument_id, user_id, project_id, start_time, end_time, status)
            VALUES
              (:booking_id, :instrument_id, :user_id, :project_id, :start_time, :end_time, :status)
            """,
            data.get("bookings", []),
        )
        conn.executemany(
            """
            INSERT OR IGNORE INTO inventory_transactions
              (transaction_id, chemical_id, user_id, project_id,
               transaction_type, amount_delta, unit, raw_text)
            VALUES
              (:transaction_id, :chemical_id, :user_id, :project_id,
               :transaction_type, :amount_delta, :unit, :raw_text)
            """,
            data.get("inventory_transactions", []),
        )
        conn.executemany(
            """
            INSERT OR IGNORE INTO experiment_events
              (event_id, project_id, user_id, event_type, title, details, source_text)
            VALUES
              (:event_id, :project_id, :user_id, :event_type, :title, :details, :source_text)
            """,
            data.get("experiment_events", []),
        )
        conn.commit()
    finally:
        conn.close()
