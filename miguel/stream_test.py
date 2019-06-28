import random
import struct
from collections import defaultdict
import math

import gevent
from gevent.event import Event
from gevent.queue import Queue
from pytest import fixture, mark, raises

import honeybadgerbft.core.honeybadger
#reload(honeybadgerbft.core.honeybadger)
from honeybadgerbft.core.honeybadger import HoneyBadgerBFT
from honeybadgerbft.crypto.threshsig.boldyreva import dealer
from honeybadgerbft.crypto.threshenc import tpke
from honeybadgerbft.core.honeybadger import BroadcastTag

from google.protobuf.internal.decoder import _DecodeVarint as varint_decoder
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
    #if seed is not None: print 'ROUTER SEED: %f' % (seed,)

    queues = [Queue() for _ in range(N)]
    _threads = []

    def makeSend(i):
        def _send(j, o):
            delay = rnd.random() * maxdelay
            if not i%3:
                delay *= 1000
            #delay = 0.1
            #print 'SEND   %8s [%2d -> %2d] %2.1f' % (o[0], i, j, delay*1000), o[1:]
            gevent.spawn_later(delay, queues[j].put_nowait, (i,o))
        return _send

    def makeRecv(j):
        def _recv():
            (i,o) = queues[j].get()
            #print 'RECV %8s [%2d -> %2d]' % (o[0], i, j)
            return (i,o)
        return _recv

    return ([makeSend(i) for i in range(N)],
            [makeRecv(j) for j in range(N)])

### Test asynchronous common subset
def _test_honeybadger(N=4, f=1, B=1, seed=None, port=5000):
    sid = 'sidA'
    # Generate threshold sig keys
    sPK, sSKs = dealer(N, f+1, seed=seed)
    # Generate threshold enc keys
    ePK, eSKs = tpke.dealer(N, f+1)

    rnd = random.Random(seed)
    #print 'SEED:', seed
    router_seed = rnd.random()
    sends, recvs = simple_router(N, seed=router_seed)

    new_tx_queue = Queue()

    badgers = [None] * N
    threads = [None] * N
    for i in range(N):
        badgers[i] = HoneyBadgerBFT(sid, i, B, N, f,
                                    sPK, sSKs[i], ePK, eSKs[i],
                                    sends[i], recvs[i])
        threads[i] = gevent.spawn(badgers[i].run)

    def submit_tx(tx):
        for b in badgers:
            b.submit_tx(tx)

    hyperledger_receiver = HyperledgerReceiver(port, B, submit_tx)
    hyperledger_receiver.connect_socket()
    receiver_thread = gevent.spawn(hyperledger_receiver.run)

    # new_transactions = [Transaction(j, receive_envelope()) for j in range(3 * N)]

    # for j in range(len(new_transactions)):
    #     for i in range(N):
    #         #if i == 1: continue
    #         # badgers[i].submit_tx('<[HBBFT Input %d]>' % i)
    #         badgers[i].submit_tx(str(j))

    # for i in range(N):
    #     badgers[i].submit_tx('<[HBBFT Input %d]>' % (i+10))
    #
    # for i in range(N):
    #     badgers[i].submit_tx('<[HBBFT Input %d]>' % (i+20))

    #gevent.killall(threads[N-f:])
    #gevent.sleep(3)tx_to_send
    #for i in range(N-f, N):
    #    inputs[i].put(0)
    try:
        outs = [threads[i].get() for i in range(N)]

        # Consistency check
        assert len(set(outs)) == 1

    except KeyboardInterrupt:
        gevent.killall(threads)
        gevent.kill(receiver_thread)
        raise


#@mark.skip('python 3 problem with gevent')
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
    assert recv_queue,get() == (sender, message)


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


def receive_envelope():
    # dataToRead = struct.unpack("H", socket.read(2))[0]
    # data = socket.read(dataToRead)
    #
    # env = envelopewrapper.EnvelopeWrapper()
    # env.ParseFromString(data)
    env = envelopewrapper.EnvelopeWrapper()
    env.channelId = "two"
    return env

# HOST = "localhost"
HOST = "host.docker.internal"
TIME_OUT = 10
class HyperledgerReceiver():

    def __init__(self, port, B, submit_tx_func):
        self.transaction_dict = {}
        self.B = B
        self.port = port
        self.submit_tx_func = submit_tx_func
        self.sock = None

    def run(self):
        i = 0
        while(True):
            new_tr = []
            start_time = time.time()
            for j in range(self.B):
                if time.time() - start_time > TIME_OUT:
                    break
                env = self.receive_envelope()
                new_tr.append(str(i))
                self.transaction_dict[i] = env
                i += 1

            if len(new_tr) > 0:
                self.submit_tx_func(",".join(new_tr))
            else:
                time.sleep(2)

    def receive_envelope(self):
        # data_to_read = struct.unpack("i", self.sock.recv(4))[0]
        data_to_read = self.sock.recv(4)

        (size, pos) = varint_decoder(data_to_read, 0)

        print("Receiving " + str(size) + " bytes")

        data_to_read = data_to_read[pos:] + self.sock.recv(size - 4 + pos)

        # data = self.sock.recv(data_to_read)

        # next_pos, pos = decoder(data, 0)


        env = envelopewrapper.EnvelopeWrapper()
        env.ParseFromString(data_to_read)

        time.sleep(int(abs(random.gauss(6, 5))))
        # env = envelopewrapper.EnvelopeWrapper()
        # env.channelId = "two"
        return env

    def connect_socket(self):

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((HOST, self.port))

# class HyperledgerSender():
#     sock = None
#
#     def __init__(self):




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
    (options, args) = parser.parse_args()
    if (options.n and options.f):
        if not options.B:
            options.B = int(math.ceil(options.n * math.log(options.n)))
        if options.tx < 0:
            options.tx = options.B
        if not options.p:
            options.p = 5000

        # connect_socket(options.p)
        _test_honeybadger(int(options.n), int(options.f), int(options.B), int(options.p))
    else:
        parser.error('Please specify the arguments')

