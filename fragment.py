import sys
import struct
import binascii

SCHC_TTL = 60   # 60 seconds

# fragmentation parameters
fp8 = {
    # 123|1234|1
    "hdr_size": 8,
    "rid_size": 3,
    "rid_shift": 5,
    "rid_mask": 0xe0,
    "dtag_size": 4,
    "dtag_shift": 2,
    "dtag_mask": 0x1e,
    "fcn_size": 1,
    "fcn_shift": 0,
    "fcn_mask": 0x01,
    }

fp16 = {
    # 12345678|1234|1234
    "hdr_size": 16,
    "rid_size": 8,
    "rid_shift": 8,
    "rid_mask": 0xff00,
    "dtag_size": 4,
    "dtag_shift": 4,
    "dtag_mask": 0x00f0,
    "fcn_size": 4,
    "fcn_shift": 0,
    "fcn_mask": 0x000f,
    }

fp_ietf100 = {
    # 1234|1234|1|1234567
    "hdr_size": 16,
    "rid_size": 4,
    "rid_shift": 12,
    "rid_mask": 0xf000,
    "dtag_size": 4,
    "dtag_shift": 8,
    "dtag_mask": 0x0f00,
    "win_size": 1,
    "win_shift": 7,
    "win_mask": 0x0080,
    "fcn_size": 7,
    "fcn_shift": 0,
    "fcn_mask": 0x007f,
    }

fp = fp_ietf100

def int_to_bytestr(n, length, endianess='big'):
    '''
    pycom doesn't support str.to_bytes() and __mul__ of str.
    '''
    h = '%x' % n
    s = binascii.unhexlify(
        "".join(["0" for i in range(length*2-len(h))]) + h)
    return s if endianess == 'big' else s[::-1]

def bytestr_to_int(b):
    n = 0
    for i in b:
        n = (n<<8)+ord(i)
    return n

class fragment:

    pos = 0

    def __init__(self, srcbuf, rule_id, dtag, noack=True, window_size=None):
        self.srcbuf = srcbuf
        # check rule_id size
        if rule_id > 2**fp["rid_size"] - 1:
            raise ValueError("rule_id is too big for the rule id field.")
        #
        self.max_fcn = (1<<fp["fcn_size"])-2
        self.fcn = self.max_fcn
        self.end_of_fragment = (1<<fp["fcn_size"])-1
        #
        print("rule_id =", rule_id, "dtag =", dtag)
        h_rule_id = rule_id<<fp["rid_shift"]&fp["rid_mask"]
        h_dtag = dtag<<fp["dtag_shift"]&fp["dtag_mask"]
        h_win = 0
        if window_size:
            h_win = 1<<fp["win_shift"]&fp["win_mask"]
        self.base_hdr = h_rule_id + h_dtag + h_win

    def next_fragment(self, l2_size):
        rest_size = l2_size
        ret = 1
        if self.pos + l2_size > len(self.srcbuf):
            self.fcn = self.end_of_fragment
            rest_size = len(self.srcbuf) - self.pos
            ret = 0
        elif self.fcn == 1:
            self.fcn = self.max_fcn
        else:
            self.fcn -= 1
        hdr = self.base_hdr + self.fcn<<fp["fcn_shift"]&fp["fcn_mask"]
        #
        h = int_to_bytestr(hdr, int(fp["hdr_size"]/8))
        print("fcn =", self.fcn, "pos = ", self.pos, "rest =", rest_size)
        piece = h + self.srcbuf[self.pos:self.pos+rest_size]
        self.pos += rest_size
        return ret, piece

_SCHC_DEFRAG_NOTYET = 1
_SCHC_DEFRAG_DONE = 0
_SCHC_DEFRAG_ERROR = -1

class defragment_message():
    '''
    defragment fragments into a message
    '''
    fragment_list = {}
    ttl = SCHC_TTL

    def __init__(self, fcn, piece):
        self.defrag(fcn, piece)

    def defrag(self, fcn, piece):
        s = self.fragment_list.get(fcn)
        if s:
            # it's received already.
            return _SCHC_DEFRAG_ERROR
        # set new piece
        self.fragment_list[fcn] = piece
        return _SCHC_DEFRAG_NOTYET

    def assemble(self, fcn):
        return "".join([self.fragment_list[str(i)] for i in
                         range(len(self.fragment_list))])

    def is_alive(self):
        self.ttl -= 1
        if self.ttl > 0:
            return True
        return False

class defragment_factory():
    msg_list = {}

    def __init__(self):
        self.end_of_fragment = (1<<fp["fcn_size"])-1

    def defrag(self, recvbuf):
        # XXX no thread safe
        hdr_size_byte = int(fp["hdr_size"]/8)
        fmt = ">%dB"%hdr_size_byte
        hdr = bytestr_to_int(recvbuf[:hdr_size_byte])
        dtag = hdr&fp["dtag_mask"]>>fp["dtag_shift"]
        fcn = hdr&fp["fcn_mask"]>>fp["fcn_shift"]
        piece = recvbuf[hdr_size_byte:]
        print("dtag=", dtag, "fcn=", fcn, "piece=", repr(piece))
        #
        m = self.msg_list.get(dtag)
        if m:
            ret = m.defrag(fcn, piece)
            if ret == _SCHC_DEFRAG_ERROR:
                print("%s dtag=%s fcn=%s" % (buf, repr(dtag), repr(fcn)))
                return ret, None
            if fcn == self.end_of_fragment:
                return _SCHC_DEFRAG_DONE, m.assemble()
            return _SCHC_DEFRAG_NOTYET, None
        else:
            # if the piece is the end of fragment, don't put to the list.
            if fcn == self.end_of_fragment:
                return _SCHC_DEFRAG_DONE, piece
            # otherwise, put it into the list.
            self.msg_list[dtag] = defragment_message(fcn, piece)
            return _SCHC_DEFRAG_NOTYET, None

    def purge(self):
        # XXX no thread safe
        for dtag in self.msg_list.iterkeys():
            if self.msg_list[dtag].is_alive():
                continue
            # delete it
            self.msg_list.pop(dtag)

#
# test code
#
def test_defrag(sent_buf):
    import time
    dfg = defragment_factory()
    for i in sent_buf:
        print("piece=", repr(i))
        ret, buf = dfg.defrag(i)
        if ret == _SCHC_DEFRAG_NOTYET:
            print("not yet")
        elif ret == _SCHC_DEFRAG_DONE:
            print("done")
            print(repr(buf))
            break
        else:
            print("error")
        #
        # purge the members if possible.
        dfg.purge()
        time.sleep(1)

if __name__ == "__main__" :
    sent_buf = []
    #
    #buf = struct.pack(">HHHHBBBBHH",1,2,3,4,5,6,7,8,9,10)
    message = "Hello LoRa"
    fmt = ">%ds" % len(message)
    buf = struct.pack(fmt, message)
    fg = fragment(buf, 1, 5, window_size=1)
    l2_size = len(message)  # it must be set in each sending message.
    #l2_size = 4
    while True:
        ret, piece, = fg.next_fragment(l2_size)
        print("fragment", binascii.hexlify(piece), "%s"%piece)
        sent_buf.append(piece)
        if ret == 0:
            break

    if True:
        print("=== defrag test")
        test_defrag(sent_buf)

