import random
import struct
from collections import defaultdict
import math

import gevent
from gevent.event import Event
from gevent.queue import Queue
from pytest import fixture, mark, raises

import honeybadgerbft.core.honeybadger
# reload(honeybadgerbft.core.honeybadger)
from honeybadgerbft.core.honeybadger import HoneyBadgerBFT
from honeybadgerbft.crypto.threshsig.boldyreva import dealer
from honeybadgerbft.crypto.threshenc import tpke
from honeybadgerbft.core.honeybadger import BroadcastTag

# from google.protobuf.internal.decoder import _DecodeVarint as varint_decoder
from google.protobuf.internal.decoder import _DecodeVarint32 as varint_decoder
from google.protobuf.internal import encoder

import miguel.proto.envelopewrapper_pb2 as envelopewrapper
from misc.utils import Transaction, encodeMyTransaction, decodeMyTransaction

import time
import socket


@fixture
def recv_queues(request):
    from honeybadgerbft.core.honeybadger import BroadcastReceiverQueues
    number_of_nodes = getattr(request, 'N', 4)
    queues = {
        tag.value: [Queue() for _ in range(number_of_nodes)]
        for tag in BroadcastTag if tag != BroadcastTag.TPKE
        }
    queues[BroadcastTag.TPKE.value] = Queue()
    return BroadcastReceiverQueues(**queues)


from pytest import mark


def simple_router(N, maxdelay=0.005, seed=None):
    """Builds a set of connected channels, with random delay

    :return: (receives, sends)
    """
    rnd = random.Random(seed)
    # if seed is not None: print 'ROUTER SEED: %f' % (seed,)

    queues = [Queue() for _ in range(N)]
    _threads = []

    def makeSend(i):
        def _send(j, o):
            delay = rnd.random() * maxdelay
            if not i % 3:
                delay *= 1000
            # delay = 0.1
            # print 'SEND   %8s [%2d -> %2d] %2.1f' % (o[0], i, j, delay*1000), o[1:]
            gevent.spawn_later(delay, queues[j].put_nowait, (i, o))

        return _send

    def makeRecv(j):
        def _recv():
            (i, o) = queues[j].get()
            # print 'RECV %8s [%2d -> %2d]' % (o[0], i, j)
            return (i, o)

        return _recv

    return ([makeSend(i) for i in range(N)],
            [makeRecv(j) for j in range(N)])


### Test asynchronous common subset
def _test_honeybadger(N=4, f=1, B=1, seed=None, port=5000):
    sid = 'sidA'
    # Generate threshold sig keys
    sPK, sSKs = dealer(N, f + 1, seed=seed)
    # Generate threshold enc keys
    ePK, eSKs = tpke.dealer(N, f + 1)

    rnd = random.Random(seed)
    # print 'SEED:', seed
    router_seed = rnd.random()
    sends, recvs = simple_router(N, seed=router_seed)

    final_tx_queues = [Queue() for _ in range(N)]

    badgers = [None] * N
    threads = [None] * N
    for i in range(N):
        badgers[i] = HoneyBadgerBFT(sid, i, B, N, f,
                                    sPK, sSKs[i], ePK, eSKs[i],
                                    sends[i], recvs[i], final_tx_queues[i])
        threads[i] = gevent.spawn(badgers[i].run)

    def submit_tx(tx):
        for b in badgers:
            b.submit_tx(tx)

    hyperledger_helper = HyperledgerHelper(port, B, submit_tx)
    hyperledger_helper.connect_socket()

    receiver_thread = gevent.spawn(hyperledger_helper.run_receiver)
    sender_thread = gevent.spawn(hyperledger_helper.run_sender, final_tx_queues[0])

    try:
        outs = [threads[i].get() for i in range(N)]

        # Consistency check
        assert len(set(outs)) == 1

    except KeyboardInterrupt:
        gevent.killall(threads)
        gevent.kill(receiver_thread)
        gevent.kill(sender_thread)
        raise


# @mark.skip('python 3 problem with gevent')
def test_honeybadger():
    _test_honeybadger()


@mark.parametrize('message', ('broadcast message',))
@mark.parametrize('node_id', range(4))
@mark.parametrize('tag', [e.value for e in BroadcastTag])
@mark.parametrize('sender', range(4))
def test_broadcast_receiver_loop(sender, tag, node_id, message, recv_queues):
    from honeybadgerbft.core.honeybadger import broadcast_receiver_loop
    recv = Queue()
    recv.put((sender, (tag, node_id, message)))
    gevent.spawn(broadcast_receiver_loop, recv.get, recv_queues)
    recv_queue = getattr(recv_queues, tag)
    if tag != BroadcastTag.TPKE.value:
        recv_queue = recv_queue[node_id]
    assert recv_queue, get() == (sender, message)


@mark.parametrize('message', ('broadcast message',))
@mark.parametrize('node_id', range(4))
@mark.parametrize('tag', ('BogusTag', None, 123))
@mark.parametrize('sender', range(4))
def test_broadcast_receiver_loop_raises(sender, tag, node_id, message, recv_queues):
    from honeybadgerbft.core.honeybadger import broadcast_receiver_loop
    from honeybadgerbft.exceptions import UnknownTagError
    recv = Queue()
    recv.put((sender, (tag, node_id, message)))
    with raises(UnknownTagError) as exc:
        broadcast_receiver_loop(recv.get, recv_queues)
    expected_err_msg = 'Unknown tag: {}! Must be one of {}.'.format(
        tag, BroadcastTag.__members__.keys())
    assert exc.value.args[0] == expected_err_msg
    recv_queues_dict = recv_queues._asdict()
    tpke_queue = recv_queues_dict.pop(BroadcastTag.TPKE.value)
    assert tpke_queue.empty()
    assert all([q.empty() for queues in recv_queues_dict.values() for q in queues])


# HOST = "localhost"
HOST = "host.docker.internal"
TIME_OUT = 2
transaction_dict = {}


class HyperledgerHelper():
    def __init__(self, port, B, submit_tx_func, socket=None):
        self.B = B
        self.port = port
        self.submit_tx_func = submit_tx_func
        self.sock = socket

    def run_receiver(self):
        i = 0
        while (True):
            new_tr = []
            start_time = time.time()
            print("Waiting for " + str(self.B) + " envelopes...")
            for j in range(self.B):
                if time.time() - start_time > TIME_OUT:
                    print("Timed out. Sending " + str(len(new_tr)) + " transactions...")
                    break
                env = self.receive_envelope()
                if env is None:
                    continue
                new_tr.append(str(i))
                transaction_dict[i] = env
                i += 1

            if len(new_tr) > 0:
                print("Submitting " + str(len(new_tr)) + " envelopes to honeybadger orderers")
                self.submit_tx_func(",".join(new_tr))

    def run_sender(self, queue):
        while True:
            envs = queue.get()
            print("Fetching transactions " + ", ".join(envs))
            new_envs_ids = []
            for _envs in envs:
                envelope_ids = _envs.split(",")
                for id in envelope_ids:
                    new_envs_ids.append(int(id))

            new_envs_ids = list(dict.fromkeys(new_envs_ids))

            new_envs = [transaction_dict[id] for id in new_envs_ids]
            for env in new_envs:
                self.send_envelope(env.SerializeToString())

    def receive_envelope(self):
        sz = 0
        sz = struct.unpack('i', self.sock.recv(4))[0]

        data = []
        while sz:
            buf = self.sock.recv(sz)
            if not buf:
                raise ValueError("Buffer receive truncated")
            data.append(buf)
            sz -= len(buf)
        envelope_bytes = b''.join(buf)
        try:
            env = envelopewrapper.EnvelopeWrapper()
            env.ParseFromString(envelope_bytes)
        except Exception as e:
            print("Error parsing protobuf: " + str(e))
            print(envelope_bytes)

            return None

        print("Received envelope successfully")
        print(envelope_bytes)

        return env

    def receive_envelope_2(self):
        try:
            try:
                data_to_read = b''
                new_data = b''
                to_receive = 10
                while to_receive > 0:
                    new_data = new_data + self.sock.recv(to_receive)
                    data_to_read = data_to_read + new_data
                    to_receive -= len(new_data)
            except socket.timeout as err:
                print("Socket recv timed out")
                raise
        except Exception:
            return None

        (size, init_pos) = varint_decoder(data_to_read, 0)
        print("Receiving " + str(size) + " bytes. Init byte is in " + str(init_pos))

        # data_to_read = data_to_read[pos:] + self.sock.recv(size - 4 + pos)


        to_read = size - (len(data_to_read) - init_pos)
        print("Received " + str(len(data_to_read)) + " from " + str(size) + " and to_read is " + str(to_read))
        while to_read > 0:
            try:
                print("Reading " + str(to_read) + " bytes from socket...")
                new_data = self.sock.recv(to_read)
            except Exception as e:
                print("Error with socket (second), continuing....")
                print(e)
                return None
            data_to_read = data_to_read + new_data
            to_read -= len(new_data)

        print("Size of received envelope: " + str(len(data_to_read[init_pos:])))

        env = envelopewrapper.EnvelopeWrapper()
        try:
            env.ParseFromString(data_to_read[init_pos:])
        except Exception as e:
            print("Error parsing byte buffer ")
            # print(bytes.fromhex(data_to_read[init_pos:]).decode("utf-8"))
            print(data_to_read[init_pos:])
            print(e)

            # env = envelopewrapper.EnvelopeWrapper()
            # size = len(data_to_read)
            # try:
            #     env.ParseFromString(data_to_read[init_pos:size-5])
            # except Exception as e:
            #     print("Error on second parsing: " + str(e))
            #     print(data_to_read[init_pos:size-5])
            #
            # print("Second parsing actually worked")

            return None

        print("Received envelopeWrapper for channel " + env.channelId)
        # print(bytes.fromhex(data_to_read[init_pos:]).decode("utf-8"))
        print(data_to_read[init_pos:])

        # time.sleep(int(abs(random.gauss(6, 5))))
        return env

    def send_envelope(self, message):
        print("Sending envelope...")
        delimiter = encoder._VarintBytes(len(message))
        message = delimiter + message
        msg_len = len(message)
        total_sent = 0
        while total_sent < msg_len:
            sent = self.sock.send(message[total_sent:])
            if sent == 0:
                raise RuntimeError('Socket connection broken')
            total_sent = total_sent + sent

        print("Envelope sent")

    def connect_socket(self):
        print("Connecting to BftFabricProxy socket " + HOST + ":" + str(self.port) + "...")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        connected = False
        while not connected:
            try:
                self.sock.connect((HOST, self.port))
                connected = True
                self.sock.settimeout(2)
            except Exception as e:
                time.sleep(2)
                pass

        print("Connected.")

        return self.sock


if __name__ == '__main__':
    # GreenletProfiler.set_clock_type('cpu')

    from optparse import OptionParser

    parser = OptionParser()
    parser.add_option("-n", "--number", dest="n",
                      help="Number of parties", metavar="N", type="int")
    parser.add_option("-b", "--propose-size", dest="B",
                      help="Number of transactions to propose", metavar="B", type="int")
    parser.add_option("-f", "--adversaries", dest="f",
                      help="Number of adversaries", metavar="F", type="int")
    parser.add_option("-x", "--transactions", dest="tx",
                      help="Number of transactions proposed by each party", metavar="TX", type="int", default=-1)
    parser.add_option("-p", "--port", dest="p",
                      help="Hyperledger proxy connection port", metavar="P", type="int", default=5000)

    parser.add_option("-i", "--ip", dest="i",
                      help="Hyperledger proxy connection ip", metavar="I", type="string")
    (options, args) = parser.parse_args()
    if (options.n and options.f):
        if not options.B:
            options.B = int(math.ceil(options.n * math.log(options.n)))
        if options.tx < 0:
            options.tx = options.B
        if not options.p:
            options.p = 5000
        if options.i:
            HOST = options.i

        # connect_socket(options.p)
        _test_honeybadger(int(options.n), int(options.f), int(options.B), None, options.p)
    else:
        parser.error('Please specify the arguments')
