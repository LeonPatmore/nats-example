import asyncio
import json
import logging
import uuid

import nats
import pytest
import pytest_asyncio
from nats.js.api import StreamConfig, StorageType

from utils.json_server_helpers import JsonServer
from utils.nats_consumer import NatsConsumer
from utils.nats_processor_instance import LocalProcessInstance
from utils.wait_for import wait_for


@pytest_asyncio.fixture
async def nats_client():
    client = await nats.connect()
    yield client, client.jetstream()
    await client.close()


@pytest_asyncio.fixture
async def setup_nats_stream(nats_client):
    _, js = nats_client
    stream_info = await js.add_stream(StreamConfig(name="test-stream",
                                                   subjects=["nats.test"],
                                                   storage=StorageType.MEMORY))

    logging.info("Stream info is " + json.dumps(stream_info.as_dict()))


@pytest_asyncio.fixture(scope="session")
async def start_processor():
    processor = LocalProcessInstance()
    processor.start()
    yield processor
    processor.stop()


@pytest_asyncio.fixture
async def setup_nats_stream_dlq(nats_client) -> NatsConsumer:
    _, js = nats_client

    stream_info = await js.add_stream(StreamConfig(name="test-stream-dlq",
                                                   subjects=[
                                                       "$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.test-stream.*"],
                                                   storage=StorageType.MEMORY))

    consumer = NatsConsumer(js, "$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.test-stream.*")
    task = asyncio.create_task(consumer.start())
    yield consumer
    logging.info("Cleaning up dlq consumer")
    task.cancel()


@pytest.fixture(scope="session")
def json_server() -> JsonServer:
    return JsonServer(host="localhost", port=3010)


async def generate_message(js, delay: int = 0, error: bool = False) -> tuple:
    my_id = str(uuid.uuid4())
    logging.info(f"Generating a new ID {my_id}")
    pub_ack = await js.publish("nats.test",
                               headers={"id": my_id, "delay": str(delay), "error": str(error).lower()},
                               payload="cool".encode())
    logging.info("Pub ack is " + json.dumps(pub_ack.as_dict()))
    return my_id, pub_ack


@pytest.mark.asyncio
async def test_simple_message_is_processed(nats_client, start_processor, json_server: JsonServer):
    _, js = nats_client
    my_id, _ = await generate_message(js)

    await wait_for(lambda: json_server.get_resource("messages", my_id))


@pytest.mark.asyncio
async def test_message_that_errors_goes_to_dlq(nats_client,
                                               start_processor,
                                               setup_nats_stream,
                                               setup_nats_stream_dlq: NatsConsumer):
    _, js = nats_client
    _, pub_ack = await generate_message(js, error=True)

    await wait_for(lambda: next(x for x in setup_nats_stream_dlq.msgs
                                if json.loads(x.data)["stream_seq"] == pub_ack.seq))


@pytest.mark.asyncio
async def test_message_that_does_not_ack_in_time_goes_to_dlq(nats_client,
                                                             start_processor,
                                                             setup_nats_stream,
                                                             setup_nats_stream_dlq: NatsConsumer):
    _, js = nats_client
    _, pub_ack = await generate_message(js, delay=30)

    await wait_for(lambda: next(x for x in setup_nats_stream_dlq.msgs
                                if json.loads(x.data)["stream_seq"] == pub_ack.seq))
