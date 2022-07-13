from retry import retry
from zipfile import ZipFile, BadZipFile
import pulsar
import sys
import os
from viaa.configuration import ConfigParser
from viaa.observability import logging

from cloudevents.events import (
    CEMessageMode,
    Event,
    EventAttributes,
    EventOutcome,
    PulsarBinding,
)

configParser = ConfigParser()
log = logging.get_logger(__name__, config=configParser)

APP_NAME = "unzip-service"
client = pulsar.Client(
    f"pulsar://{configParser.app_cfg['pulsar']['host']}:{configParser.app_cfg['pulsar']['port']}"
)


@retry(pulsar.ConnectError, tries=10, delay=1, backoff=2)
def create_producer():
    return client.create_producer(
        configParser.app_cfg["unzip-service"]["producer_topic"]
    )


@retry(pulsar.ConnectError, tries=10, delay=1, backoff=2)
def subscribe():
    return client.subscribe(
        configParser.app_cfg["unzip-service"]["consumer_topic"],
        subscription_name=APP_NAME,
    )


producer = create_producer()
consumer = subscribe()


def handle_event(event: Event):
    """
    Handles an incoming pulsar event.
    If the event has a succesful outcome, the incoming zip will be extracted and an event will be produced.
    """
    if (
        not event.has_successful_outcome()
        or event.get_data()["outcome"] != EventOutcome.SUCCESS.to_str()
    ):
        log.info(f"Dropping non succesful event: {event.get_data()}")
        return

    log.debug(f"Incoming event: {event.get_data()}")

    try:
        filename = event.get_data()["destination"]
    except KeyError as e:
        error_msg = (
            f"The data payload of the incoming event is not conform. Missing key: {e}."
        )
        log.warning(error_msg)
        outcome = EventOutcome.FAIL
        data = {
            "source": "N/A",
            "message": error_msg,
        }
        send_event(data, outcome, event.correlation_id)
        return

    basename = os.path.basename(filename)
    extract_path = os.path.join(
        configParser.app_cfg["unzip-service"]["target_folder"], basename
    )
    data = {"destination": extract_path, "source": filename}

    try:
        with ZipFile(filename, "r") as zipObj:
            # Extract all the contents of zip file in the target directory
            zipObj.extractall(extract_path)

        outcome = EventOutcome.SUCCESS
        data["message"] = f"The bag '{basename}' unzipped in '{extract_path}'"
    except BadZipFile:
        outcome = EventOutcome.FAIL
        data["message"] = f"{filename} is not a a valid zipfile."
        log.warning(f"{filename} is not a a valid zipfile.")
    except OSError:
        outcome = EventOutcome.FAIL
        data["message"] = f"{filename} does not exit."
        log.warning(f"{filename} does not exit.")

    log.info(data["message"])
    send_event(data, outcome, event.correlation_id)


def send_event(data: dict, outcome: EventOutcome, correlation_id: str):
    attributes = EventAttributes(
        type=configParser.app_cfg["unzip-service"]["producer_topic"],
        source=APP_NAME,
        subject=data["source"],
        correlation_id=correlation_id,
        outcome=outcome,
    )

    event = Event(attributes, data)
    create_msg = PulsarBinding.to_protocol(event, CEMessageMode.STRUCTURED.value)

    producer.send(
        create_msg.data,
        properties=create_msg.attributes,
        event_timestamp=event.get_event_time_as_int(),
    )


if __name__ == "__main__":
    try:
        while True:
            msg = consumer.receive()
            event = PulsarBinding.from_protocol(msg)

            handle_event(event)

            consumer.acknowledge(msg)
    except KeyboardInterrupt as exception:
        client.close()
        exit()
