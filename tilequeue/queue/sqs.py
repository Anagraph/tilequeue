from boto import connect_sqs
from boto.sqs.message import RawMessage
from tilequeue.tile import coord_marshall_int
from tilequeue.tile import CoordMessage
from tilequeue.tile import deserialize_coord
from tilequeue.tile import serialize_coord
from redis import StrictRedis


class SqsQueue(object):

    def __init__(self, sqs_queue, redis_client, is_seeding=False):
        self.sqs_queue = sqs_queue
        self.redis_client = redis_client
        self.inflight_key = "tilequeue.in-flight"
        self.is_seeding = is_seeding

    def enqueue(self, coord):
        if not self._inflight(coord):
            payload = serialize_coord(coord)
            message = RawMessage()
            message.set_body(payload)
            self.sqs_queue.write(message)
            self._add_to_flight(coord)

    def _write_batch(self, coords):
        assert len(coords) <= 10
        values = []
        msg_tuples = []

        for i, coord in enumerate(coords):
            msg_tuples.append((str(i), serialize_coord(coord), 0))
            values.append(coord_marshall_int(coord))

        self.sqs_queue.write_batch(msg_tuples)
        self.redis_client.sadd(self.inflight_key, *values)

    def _inflight(self, coord):
        return (not self.is_seeding) and self.redis_client.sismember(
            self.inflight_key, coord_marshall_int(coord))

    def _add_to_flight(self, coord):
        self.redis_client.sadd(self.inflight_key,
                               coord_marshall_int(coord))

    def enqueue_batch(self, coords):
        buffer = []
        n_queued = 0
        n_in_flight = 0
        for coord in coords:
            if self._inflight(coord):
                n_in_flight += 1
            else:
                n_queued += 1
                buffer.append(coord)
                if len(buffer) == 10:
                    self._write_batch(buffer)
                    del buffer[:]
        if buffer:
            self._write_batch(buffer)

        return n_queued, n_in_flight

    def read(self, max_to_read=1):
        coord_messages = []
        messages = self.sqs_queue.get_messages(num_messages=max_to_read,
                                               attributes=["SentTimestamp"])
        for message in messages:
            data = message.get_body()
            coord = deserialize_coord(data)
            if coord is None:
                # log?
                continue
            coord_message = CoordMessage(coord, message)
            coord_messages.append(coord_message)
        return coord_messages

    def job_done(self, message):
        coord_str = message.get_body()
        coord = deserialize_coord(coord_str)
        coord_int = coord_marshall_int(coord)
        self.redis_client.srem(self.inflight_key, coord_int)
        self.sqs_queue.delete_message(message)

    def jobs_done(self, messages):
        coord_ints = []
        for message in messages:
            coord_str = message.get_body()
            coord = deserialize_coord(coord_str)
            coord_int = coord_marshall_int(coord)
            coord_ints.append(coord_int)
        self.redis_client.srem(self.inflight_key, *coord_ints)
        self.sqs_queue.delete_message_batch(messages)

    def clear(self):
        self.redis_client.delete(self.inflight_key)
        n = 0
        while True:
            msgs = self.sqs_queue.get_messages(10)
            if not msgs:
                break
            self.sqs_queue.delete_message_batch(msgs)
            n += len(msgs)
        return n

    def close(self):
        pass


def get_sqs_queue(queue_name, redis_host, redis_port, redis_db,
                  aws_access_key_id=None, aws_secret_access_key=None):
    conn = connect_sqs(aws_access_key_id, aws_secret_access_key)
    queue = conn.get_queue(queue_name)
    assert queue is not None, \
        'Could not get sqs queue with name: %s' % queue_name
    queue.set_message_class(RawMessage)
    redis_client = StrictRedis(redis_host, redis_port, redis_db)
    return SqsQueue(queue, redis_client)
