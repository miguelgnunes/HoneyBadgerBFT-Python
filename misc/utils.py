from gevent import monkey
monkey.patch_all()

import random
import struct
import os

from miguel.proto import envelopewrapper_pb2 as envelopewrapper
from google.protobuf.internal.encoder import _VarintBytes
from google.protobuf.internal.decoder import _DecodeVarint32


class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

# TR_SIZE = 250
TR_SIZE = 1024


class Transaction:  # assume amout is in term of short
    def __init__(self, myid = -1, envelope = None):
        global idCounter
        self.envelope = envelope
        self.trId = myid
    # def __repr__(self):
    #     return bcolors.OKBLUE + "{{Transaction from %s to %s with %d}}" % (self.source, self.target, self.amount) + bcolors.ENDC
    #

    def __repr__(self):
        return encodeMyTransaction(self)
        # return bcolors.OKBLUE + "{{Transaction with id %d and envelope channel Id %s }}" % (self.trId, self.envelope.channelId) + bcolors.ENDC
        # return bcolors.OKBLUE + "{{Transaction with envelope %s }}" % (self.envelope) + bcolors.ENDC

    def __str__(self):
        return bcolors.OKBLUE + "{{Transaction with id %d and envelope channel Id %s }}" % (self.trId, self.envelope.channelId) + bcolors.ENDC
        # return bcolors.OKBLUE + "{{Transaction with envelope %s }}" % (self.envelope) + bcolors.ENDC

    def __eq__(self, other):
        if isinstance(other, self.__class__):
            return self.trId == other.trId
            # return self.envelope == other.envelope
        return False

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.trId)
        # return hash(self.envelope)

    def __len__(self):
        return TR_SIZE


def encodeMyTransaction(tr):
    envelope = tr.envelope.SerializeToString()
    delimiter = _VarintBytes(len(envelope))
    message = delimiter + envelope

    return struct.pack('<H', tr.trId) + message + os.urandom(TR_SIZE - len(message) - 2)

def decodeMyTransaction(byteStr):

    id = struct.unpack("<H", byteStr[:2])[0]

    msg_len, new_pos = _DecodeVarint32(byteStr, 2)

    data = byteStr[new_pos:new_pos + msg_len]

    env = envelopewrapper.EnvelopeWrapper()
    env.ParseFromString(data)
    newTr = Transaction(id)

    newTr.envelope = env

    return newTr
