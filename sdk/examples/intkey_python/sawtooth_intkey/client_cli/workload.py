# Copyright 2017 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ------------------------------------------------------------------------------

import argparse
import logging
import random
import threading
import time
from collections import namedtuple
from datetime import datetime

import sawtooth_signing as signing
from sawtooth_sdk.workload.workload_generator import WorkloadGenerator
from sawtooth_sdk.workload.sawtooth_workload import Workload
from sawtooth_sdk.client.stream import Stream
from sawtooth_sdk.protobuf import batch_pb2
from sawtooth_sdk.protobuf.validator_pb2 import Message
from sawtooth_intkey.client_cli.create_batch import create_intkey_transaction
from sawtooth_intkey.client_cli.create_batch import create_batch

LOGGER = logging.getLogger(__name__)

IntKeyState = namedtuple('IntKeyState', ['name', 'stream', 'value'])


class IntKeyWorkload(Workload):
    """
    This workload is for the Sawtooth Integer Key transaction family.  In
    order to guarantee that batches of transactions are submitted at a
    relatively constant rate, when the transaction callbacks occur the
    following actions occur:

    1.  If there are no pending batches (on_all_batches_committed),
        a new key is created.
    2.  If a batch is committed, the corresponding key is > 1000000
        either an increment is made (if < 10000000) or a new
        key is created (if >= 10000000).
    3.  If a batch's status has been checked and it has not been
        committed, create a key (to get a new batch) and let
        the simulator know that the old transaction should be put back in
        the queue to be checked again if it is pending.
    """
    def __init__(self, delegate, args):
        super(IntKeyWorkload, self).__init__(delegate, args)
        self._streams = []
        self._pending_batches = {}
        self._lock = threading.Lock()
        self._delegate = delegate
        self._deps = {}
        self._private_key = signing.generate_privkey()
        self._public_key = signing.generate_pubkey(self._private_key)

    def on_will_start(self):
        pass

    def on_will_stop(self):
        for stream in self._streams:
            time.sleep(5)
            stream.close()

    def on_validator_discovered(self, url):
        stream = Stream(url)
        self._streams.append(stream)

    def on_validator_removed(self, url):
        with self._lock:
            self._streams = [s for s in self._streams if s.url != url]
            self._pending_batches = \
                {t: g for t, g in self._pending_batches.items()
                 if g.stream.url != url}

    def on_all_batches_committed(self):
        self._create_new_key()

    def on_batch_committed(self, batch_id):
        with self._lock:
            key = self._pending_batches.pop(batch_id, None)

        if key is not None:
            if key.value < 1000000:
                txn = create_intkey_transaction(
                    verb="inc",
                    name=key.name,
                    value=1,
                    deps=[self._deps[key.name]],
                    private_key=self._private_key,
                    public_key=self._public_key)

                batch = create_batch(
                    transactions=[txn],
                    private_key=self._private_key,
                    public_key=self._public_key)

                batch_id = batch.header_signature

                batch_list = batch_pb2.BatchList(batches=[batch])
                key.stream.send(
                    message_type=Message.CLIENT_BATCH_SUBMIT_REQUEST,
                    content=batch_list.SerializeToString())

                with self._lock:
                    self._pending_batches[batch.header_signature] = \
                        IntKeyState(
                        name=key.name,
                        stream=key.stream,
                        value=key.value + 1)
                self.delegate.on_new_batch(batch_id, key.stream)

        else:
            LOGGER.debug('Key %s completed', key.name)
            self._create_new_key()

    def on_batch_not_yet_committed(self):
        self._create_new_key()

    def _create_new_key(self):
        with self._lock:
            stream = random.choice(self._streams) if \
                len(self._streams) > 0 else None

        batch_id = None
        if stream is not None:
            name = datetime.now().isoformat()
            txn = create_intkey_transaction(
                verb="set",
                name=name,
                value=0,
                deps=[],
                private_key=self._private_key,
                public_key=self._public_key)

            batch = create_batch(
                transactions=[txn],
                private_key=self._private_key,
                public_key=self._public_key)

            self._deps[name] = txn.header_signature
            batch_id = batch.header_signature

            batch_list = batch_pb2.BatchList(batches=[batch])
            stream.send(
                message_type=Message.CLIENT_BATCH_SUBMIT_REQUEST,
                content=batch_list.SerializeToString())

            with self._lock:
                self._pending_batches[batch_id] = \
                    IntKeyState(name=name, stream=stream, value=0)

            self.delegate.on_new_batch(batch_id, stream)


def do_workload(args):
    """
    Create WorkloadGenerator and IntKeyWorkload. Set IntKey workload in
    generator and run.
    """
    generator = WorkloadGenerator(args)
    workload = IntKeyWorkload(generator, args)
    generator.set_workload(workload)
    generator.run()


def add_workload_parser(subparsers, parent_parser):
    parser = subparsers.add_parser(
        'workload',
        parents=[parent_parser],
        formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument('--rate',
                        type=int,
                        help='Batch rate in batches per second. '
                             'Should be greater then 0.',
                        default=10)
    parser.add_argument('-d', '--display-frequency',
                        type=int,
                        help='time in seconds between display of batches '
                             'rate updates.',
                        default=30)
    parser.add_argument('-u', '--urls',
                        help='comma separated urls of validators to connect '
                        'to.',
                        default="tcp://127.0.0.1:40000")
