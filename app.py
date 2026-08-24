from __future__ import annotations

import atexit
import base64
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from flask import Flask, jsonify, render_template, request, send_from_directory
from pywebpush import WebPushException, webpush


BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
DB_PATH = INSTANCE_DIR / "monitor.sqlite3"
VAPID_PRIVATE_KEY_PATH = INSTANCE_DIR / "vapid_private.pem"

DATA_URL = os.getenv("DATA_URL", "https://www.tboo.ru/gpn/data.json")
CITY_NAME = os.getenv("CITY_NAME", "Раменское")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "600"))
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:local-monitor@example.com")

TARGET_FUELS = {
    "92": "АИ-92",
    "95": "АИ-95",
    "G-95": "АИ-95 G-Drive",
}

# АЗС №194 находится рядом с Раменским, но источник относит её к Сафоново.
# Ограничение r=136 не позволяет захватить одноимённые АЗС из других регионов.
EXTRA_STATIONS = {("АЗС №194", "136")}

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("gpn-monitor")

app = Flask(__name__, instance_path=str(INSTANCE_DIR))
stop_event = threading.Event()
check_lock = threading.Lock()


def utc_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=20)
    connection.row_factory = sqlite3.Row
    return connection


def init_storage() -> None:
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    with db_connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                endpoint TEXT PRIMARY KEY,
                subscription_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS availability (
                station_key TEXT NOT NULL,
                fuel_code TEXT NOT NULL,
                available INTEGER NOT NULL,
                price TEXT NOT NULL DEFAULT '',
                checked_at TEXT NOT NULL,
                PRIMARY KEY (station_key, fuel_code)
            );

            CREATE TABLE IF NOT EXISTS stations (
                station_key TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                city TEXT NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                source_updated TEXT NOT NULL DEFAULT '',
                checked_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                station_key TEXT NOT NULL,
                station_name TEXT NOT NULL,
                fuel_code TEXT NOT NULL,
                fuel_label TEXT NOT NULL,
                price TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )


def get_or_create_vapid_keys() -> tuple[Path, str]:
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    if not VAPID_PRIVATE_KEY_PATH.exists():
        private_key = ec.generate_private_key(ec.SECP256R1())
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        VAPID_PRIVATE_KEY_PATH.write_bytes(pem)
        log.info("Создан новый VAPID-ключ: %s", VAPID_PRIVATE_KEY_PATH)

    private_key = serialization.load_pem_private_key(
        VAPID_PRIVATE_KEY_PATH.read_bytes(), password=None
    )
    public_numbers = private_key.public_key().public_numbers()
    uncompressed = (
        b"\x04"
        + public_numbers.x.to_bytes(32, "big")
        + public_numbers.y.to_bytes(32, "big")
    )
    public_key = base64.urlsafe_b64encode(uncompressed).rstrip(b"=").decode("ascii")
    return VAPID_PRIVATE_KEY_PATH, public_key


def station_key(station: dict[str, Any]) -> str:
    return f"{station.get('n', 'АЗС')}|{station.get('la')}|{station.get('lo')}"


def yandex_maps_url(latitude: float, longitude: float) -> str:
    return (
        "https://yandex.ru/maps/"
        f"?ll={longitude}%2C{latitude}&z=17&pt={longitude}%2C{latitude}"
    )


def fetch_city_stations() -> tuple[list[dict[str, Any]], str]:
    response = requests.get(
        DATA_URL,
        timeout=(10, 30),
        headers={"User-Agent": "Ramenskoe-GPN-Monitor/1.0"},
    )
    response.raise_for_status()
    payload = response.json()
    stations = []
    for item in payload.get("stations", []):
        if str(item.get("b", "")).upper() != "GPN":
            continue
        city_matches = str(item.get("c", "")).strip().casefold() == CITY_NAME.casefold()
        explicitly_included = (
            str(item.get("n", "")).strip(), str(item.get("r", "")).strip()
        ) in EXTRA_STATIONS
        if city_matches or explicitly_included:
            stations.append(item)
    return stations, str(payload.get("updated", ""))


def send_push_to_all(payload: dict[str, Any]) -> int:
    private_key_path, _ = get_or_create_vapid_keys()
    with db_connect() as db:
        subscriptions = db.execute(
            "SELECT endpoint, subscription_json FROM subscriptions"
        ).fetchall()

    delivered = 0
    stale_endpoints: list[str] = []
    for row in subscriptions:
        try:
            webpush(
                subscription_info=json.loads(row["subscription_json"]),
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=str(private_key_path),
                vapid_claims={"sub": VAPID_SUBJECT},
                ttl=3600,
            )
            delivered += 1
        except WebPushException as exc:
            status_code = getattr(exc.response, "status_code", None)
            if status_code in (404, 410):
                stale_endpoints.append(row["endpoint"])
            log.warning("Не удалось отправить push (%s): %s", status_code, exc)
        except Exception:
            log.exception("Неожиданная ошибка отправки push")

    if stale_endpoints:
        with db_connect() as db:
            db.executemany(
                "DELETE FROM subscriptions WHERE endpoint = ?",
                [(endpoint,) for endpoint in stale_endpoints],
            )
    return delivered


def check_fuel(*, notify: bool = True) -> dict[str, Any]:
    if not check_lock.acquire(blocking=False):
        return {"ok": False, "message": "Проверка уже выполняется"}

    try:
        stations, source_updated = fetch_city_stations()
        checked_at = utc_now()
        notifications: list[dict[str, Any]] = []

        with db_connect() as db:
            initialized = db.execute(
                "SELECT value FROM meta WHERE key = 'baseline_initialized'"
            ).fetchone()
            is_first_check = initialized is None

            for station in stations:
                key = station_key(station)
                name = str(station.get("n", "АЗС"))
                station_city = str(station.get("c", CITY_NAME))
                latitude = float(station["la"])
                longitude = float(station["lo"])
                db.execute(
                    """
                    INSERT INTO stations (
                        station_key, name, city, latitude, longitude,
                        source_updated, checked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(station_key) DO UPDATE SET
                        name=excluded.name,
                        city=excluded.city,
                        latitude=excluded.latitude,
                        longitude=excluded.longitude,
                        source_updated=excluded.source_updated,
                        checked_at=excluded.checked_at
                    """,
                    (key, name, station_city, latitude, longitude, source_updated, checked_at),
                )

                fuels = {str(f[0]): f for f in station.get("f", []) if len(f) >= 3}
                for fuel_code, fuel_label in TARGET_FUELS.items():
                    item = fuels.get(fuel_code, [fuel_code, "", 0])
                    price = str(item[1] or "")
                    available = bool(item[2])
                    previous = db.execute(
                        """
                        SELECT available FROM availability
                        WHERE station_key = ? AND fuel_code = ?
                        """,
                        (key, fuel_code),
                    ).fetchone()

                    appeared = (
                        not is_first_check
                        and previous is not None
                        and not bool(previous["available"])
                        and available
                    )
                    db.execute(
                        """
                        INSERT INTO availability (
                            station_key, fuel_code, available, price, checked_at
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(station_key, fuel_code) DO UPDATE SET
                            available=excluded.available,
                            price=excluded.price,
                            checked_at=excluded.checked_at
                        """,
                        (key, fuel_code, int(available), price, checked_at),
                    )

                    if appeared:
                        event = {
                            "station_key": key,
                            "station_name": name,
                            "station_city": station_city,
                            "fuel_code": fuel_code,
                            "fuel_label": fuel_label,
                            "price": price,
                            "latitude": latitude,
                            "longitude": longitude,
                        }
                        notifications.append(event)
                        db.execute(
                            """
                            INSERT INTO events (
                                created_at, station_key, station_name,
                                fuel_code, fuel_label, price
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (checked_at, key, name, fuel_code, fuel_label, price),
                        )

            db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('baseline_initialized', '1')"
            )
            db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_check', ?)",
                (checked_at,),
            )
            db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('source_updated', ?)",
                (source_updated,),
            )
            db.execute("DELETE FROM meta WHERE key = 'last_error'")

        delivered = 0
        if notify:
            for event in notifications:
                price_text = f" · {event['price']} ₽/л" if event["price"] else ""
                delivered += send_push_to_all(
                    {
                        "title": f"Появился {event['fuel_label']}",
                        "body": (
                            f"{event['station_name']}, {event['station_city']}"
                            f"{price_text}"
                        ),
                        "tag": f"fuel-{event['station_key']}-{event['fuel_code']}",
                        "url": yandex_maps_url(event["latitude"], event["longitude"]),
                    }
                )

        log.info(
            "Проверено станций: %d, новых появлений: %d, push: %d",
            len(stations),
            len(notifications),
            delivered,
        )
        return {
            "ok": True,
            "stations": len(stations),
            "appearances": len(notifications),
            "push_delivered": delivered,
            "source_updated": source_updated,
            "first_check": is_first_check,
        }
    except Exception as exc:
        checked_at = utc_now()
        log.exception("Ошибка проверки топлива")
        with db_connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_check', ?)",
                (checked_at,),
            )
            db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_error', ?)",
                (str(exc),),
            )
        return {"ok": False, "message": str(exc)}
    finally:
        check_lock.release()


def monitor_loop() -> None:
    check_fuel()
    while not stop_event.wait(POLL_INTERVAL_SECONDS):
        check_fuel()


def status_payload() -> dict[str, Any]:
    with db_connect() as db:
        meta = {
            row["key"]: row["value"]
            for row in db.execute("SELECT key, value FROM meta").fetchall()
        }
        rows = db.execute(
            """
            SELECT s.station_key, s.name, s.city, s.latitude, s.longitude,
                   a.fuel_code, a.available, a.price, a.checked_at
            FROM stations s
            JOIN availability a ON a.station_key = s.station_key
            ORDER BY s.name, a.fuel_code
            """
        ).fetchall()
        subscription_count = db.execute(
            "SELECT COUNT(*) AS count FROM subscriptions"
        ).fetchone()["count"]
        events = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT 20"
            ).fetchall()
        ]

    stations: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = stations.setdefault(
            row["station_key"],
            {
                "name": row["name"],
                "city": row["city"],
                "latitude": row["latitude"],
                "longitude": row["longitude"],
                "map_url": yandex_maps_url(row["latitude"], row["longitude"]),
                "fuels": [],
            },
        )
        entry["fuels"].append(
            {
                "code": row["fuel_code"],
                "label": TARGET_FUELS.get(row["fuel_code"], row["fuel_code"]),
                "available": bool(row["available"]),
                "price": row["price"],
                "checked_at": row["checked_at"],
            }
        )

    return {
        "city": CITY_NAME,
        "interval_seconds": POLL_INTERVAL_SECONDS,
        "source_url": DATA_URL,
        "last_check": meta.get("last_check"),
        "source_updated": meta.get("source_updated"),
        "last_error": meta.get("last_error"),
        "subscriptions": subscription_count,
        "stations": list(stations.values()),
        "events": events,
    }


@app.get("/")
def index():
    return render_template("index.html", city=CITY_NAME)


@app.get("/sw.js")
def service_worker():
    response = send_from_directory(BASE_DIR / "static", "sw.js")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/status")
def api_status():
    return jsonify(status_payload())


@app.get("/api/vapid-public-key")
def api_vapid_public_key():
    _, public_key = get_or_create_vapid_keys()
    return jsonify({"publicKey": public_key})


@app.post("/api/subscribe")
def api_subscribe():
    subscription = request.get_json(silent=True) or {}
    endpoint = subscription.get("endpoint")
    keys = subscription.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        return jsonify({"ok": False, "message": "Некорректная push-подписка"}), 400
    with db_connect() as db:
        db.execute(
            """
            INSERT INTO subscriptions (endpoint, subscription_json, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                subscription_json=excluded.subscription_json
            """,
            (endpoint, json.dumps(subscription), utc_now()),
        )
    return jsonify({"ok": True})


@app.post("/api/unsubscribe")
def api_unsubscribe():
    payload = request.get_json(silent=True) or {}
    endpoint = payload.get("endpoint")
    if endpoint:
        with db_connect() as db:
            db.execute("DELETE FROM subscriptions WHERE endpoint = ?", (endpoint,))
    return jsonify({"ok": True})


@app.post("/api/test-notification")
def api_test_notification():
    delivered = send_push_to_all(
        {
            "title": "Тест уведомлений",
            "body": "Монитор бензина в Раменском работает.",
            "tag": "fuel-monitor-test",
            "url": "/",
        }
    )
    return jsonify({"ok": delivered > 0, "delivered": delivered})


@app.post("/api/check-now")
def api_check_now():
    result = check_fuel()
    return jsonify(result), (200 if result.get("ok") else 503)


def main() -> None:
    init_storage()
    get_or_create_vapid_keys()
    monitor = threading.Thread(target=monitor_loop, name="fuel-monitor", daemon=True)
    monitor.start()
    atexit.register(stop_event.set)
    log.info("Монитор открыт: http://127.0.0.1:8080")
    app.run(host="127.0.0.1", port=8080, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()

