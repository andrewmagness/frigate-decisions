"""Entry point: wire MQTT <-> Engine, run the scheduler loop."""

from __future__ import annotations

import json
import logging
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import paho.mqtt.client as mqtt

from .config import ConfigError, Settings, load_question_config, validate_for_provider
from .decisions import make_provider
from .engine import Engine
from .frigate import FrigateClient
from .store import Recorder

log = logging.getLogger("frigate_decisions")


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        qconfig = load_question_config(settings.questions_file)
        validate_for_provider(qconfig, settings.decisions_provider)
    except (OSError, ConfigError) as exc:
        log.error("cannot load %s: %s", settings.questions_file, exc)
        return 2
    log.info("loaded %d question sets, %d cameras, %d schedules; provider %s, model %s",
             len(qconfig.question_sets), len(qconfig.cameras), len(qconfig.schedules),
             settings.decisions_provider, settings.decision_model)

    recorder = None
    if settings.data_dir:
        try:
            recorder = Recorder(settings.data_dir)
            log.info("recording events and feedback to %s", settings.data_dir)
        except OSError as exc:
            log.error("cannot use DATA_DIR %s: %s", settings.data_dir, exc)
            return 2

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=settings.mqtt_client_id)
    if settings.mqtt_username:
        client.username_pw_set(settings.mqtt_username, settings.mqtt_password)

    def publish(topic: str, payload: str, retain: bool) -> None:
        client.publish(topic, payload, qos=1, retain=retain)

    engine = Engine(
        settings, qconfig,
        make_provider(settings),
        FrigateClient(settings.frigate_url, settings.frigate_token, settings.frigate_verify_ssl),
        publish,
        recorder=recorder,
    )
    pool = ThreadPoolExecutor(max_workers=settings.workers, thread_name_prefix="decide")
    ha_status_topic = f"{settings.discovery_prefix}/status"

    def send_discovery() -> None:
        if settings.discovery_enabled:
            for topic, payload in engine.discovery_messages():
                client.publish(topic, payload, qos=1, retain=True)

    def on_connect(c, _userdata, _flags, reason_code, _props):
        if reason_code.is_failure:
            log.error("MQTT connect failed: %s", reason_code)
            return
        log.info("MQTT connected to %s:%s", settings.mqtt_host, settings.mqtt_port)
        c.subscribe([(engine.reviews_topic, 1), (engine.request_topic, 1), (engine.feedback_topic, 1),
                     (ha_status_topic, 1)])
        c.publish(engine.availability_topic, "online", qos=1, retain=True)
        send_discovery()

    def guarded(fn, payload):
        try:
            fn(payload)
        except Exception:  # never let a worker die silently
            log.exception("handler failed")

    def on_message(_c, _userdata, msg):
        if msg.topic == ha_status_topic:
            if msg.payload == b"online":
                send_discovery()
            return
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            log.warning("ignoring non-JSON message on %s", msg.topic)
            return
        if msg.topic == engine.reviews_topic:
            pool.submit(guarded, engine.handle_review, payload)
        elif msg.topic == engine.request_topic:
            pool.submit(guarded, engine.handle_request, payload)
        elif msg.topic == engine.feedback_topic:
            pool.submit(guarded, engine.handle_feedback, payload)

    client.on_connect = on_connect
    client.on_message = on_message
    client.will_set(engine.availability_topic, "offline", qos=1, retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.connect_async(settings.mqtt_host, settings.mqtt_port, keepalive=60)
    client.loop_start()

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    while not stop.wait(15):
        for sched in engine.due_schedules(datetime.now()):
            log.info("running schedule %s", sched.name)
            pool.submit(guarded, engine.run_schedule, sched)

    log.info("shutting down")
    if client.is_connected():
        try:
            client.publish(engine.availability_topic, "offline", qos=1, retain=True).wait_for_publish(5)
        except (RuntimeError, ValueError):
            pass
    client.loop_stop()
    client.disconnect()
    pool.shutdown(wait=False, cancel_futures=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
