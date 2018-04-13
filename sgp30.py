# 2018 / MIT / Tim Clem / github.io/misterfifths
# See LICENSE for details
# Heavily inspired by the Adafruit CircuitPython implementation by ladyada:
# https://github.com/adafruit/Adafruit_CircuitPython_SGP30
# CRC implementation taken entirely from there, in fact.

from __future__ import print_function

from smbus import SMBus
from collections import namedtuple
from time import sleep
import struct
from threading import Lock
from sys import stderr


SGP30Command = namedtuple(
    'SGP30Command',
    'opcode_bytes required_feature_set parameter_length response_length response_time_ms')


_SGP30_CMDS = {
    'get_serial_number':       SGP30Command([0x36, 0x82], None, 0, 9, 1),
    'get_feature_set_version': SGP30Command((0x20, 0x2f), None, 0, 3, 2),

    'init_air_quality':     SGP30Command((0x20, 0x03), 0x20, 0, 0, 10),
    'measure_air_quality':  SGP30Command((0x20, 0x08), 0x20, 0, 6, 12),
    'get_baseline':         SGP30Command((0x20, 0x15), 0x20, 0, 6, 10),
    'set_baseline':         SGP30Command((0x20, 0x1e), 0x20, 6, 0, 10),
    'set_humidity':         SGP30Command((0x20, 0x61), 0x20, 3, 0, 10),
    'measure_raw_signals':  SGP30Command((0x20, 0x50), 0x20, 0, 6, 25)
}

_SGP30_CRC_INIT = 0xff
_SGP30_CRC_POLYNOMIAL = 0x31

_SGP30_FEATURE_SET_BITMASK = 0b0000000011100000


def _log(*args):
    print('[SGP30]', *args, file=stderr)



_AirQuality = namedtuple('AirQuality', 'co2_ppm voc_ppb')
class AirQuality(_AirQuality):
    def is_probably_valid(self):
        return self.co2_ppm != 400 or self.voc_ppb != 0


class SGP30(object):
    def __init__(self, i2c_bus_number=1, i2c_address=0x58):
        self.i2c_bus_number = i2c_bus_number
        self.i2c_address = i2c_address

        self._raw_feature_set = None
        self.chip_version = None
        self.serial_number = None

        self._bus = SMBus()
        self.__bus_lock = Lock()


    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self.close()

    def open(self):
        self._bus.open(self.i2c_bus_number)

        self.serial_number = self._get_serial_number()
        sn_string = ' '.join(hex(x) for x in self.serial_number)
        _log('got serial number: ' + sn_string)

        self._raw_feature_set = self._get_feature_set_version()
        self.chip_version = self._raw_feature_set & _SGP30_FEATURE_SET_BITMASK
        _log('chip version: ' + hex(self.chip_version) + ' (raw: ' + hex(self._raw_feature_set) + ')')

        _log('initing...')
        self._init_air_quality()
        _log('inited')

    def close(self):
        self._bus.close()


    def _get_serial_number(self):
        return self._run_word_getter('get_serial_number')

    def _get_feature_set_version(self):
        # The spec sheet says we should ignore the MSB of this response,
        # and that the last five bits of the LSB are 'subject to change.'
        # Use _has_feature_set() to check its value against another.
        return self._run_word_getter('get_feature_set_version')[0]

    def _has_feature_set(self, required_feature_set):
        if required_feature_set is None: return True
        return self.chip_version == (required_feature_set & _SGP30_FEATURE_SET_BITMASK)


    def _init_air_quality(self):
        return self._run_command(_SGP30_CMDS['init_air_quality'])


    def measure_air_quality(self):
        return AirQuality(*self._run_word_getter('measure_air_quality'))

    def get_baseline(self):
        return AirQuality(*self._run_word_getter('get_baseline'))

    def set_baseline(self, co2_baseline, voc_baseline):
        self._run_word_setter('set_baseline', [co2_baseline, voc_baseline])

    def set_humidity(self, humidity):
        self._run_word_setter('set_humidity', [humidity])

    def measure_raw_signals(self):
        return self._run_word_getter('measure_raw_signals')


    def _run_word_getter(self, cmd_name):
        cmd = _SGP30_CMDS[cmd_name]
        assert cmd.parameter_length == 0, 'This method only understands commands that take no parameters'
        assert cmd.response_length % 3 == 0, 'This method only understands commands whose response is a set of (2-byte word + 1-byte checksum) pairs (i.e., the number of response bytes must be divisible by three)'

        word_count = cmd.response_length / 3
        raw_bytes = self._run_command(cmd)
        return self._read_checksummed_words(raw_bytes, word_count)

    def _run_word_setter(self, cmd_name, words):
        cmd = _SGP30_CMDS[cmd_name]
        assert cmd.response_length == 0, 'This method only understands commands that have no response'
        assert cmd.parameter_length > 0, 'This method only understands commands that take parameters'
        assert cmd.parameter_length % 3 == 0, 'This method only understands commands whose parameter is a set of (2-byte word + 1-byte checksum) pairs (i.e., the number of parameter bytes must be divisible by three)'

        param_bytes = self._bytes_for_checksummed_words(words)
        self._run_command(cmd, param_bytes)


    def _run_command(self, cmd, param_bytes=None):
        assert self._has_feature_set(cmd.required_feature_set), 'Unsupported chip version for this command'

        bytes_to_write = list(cmd.opcode_bytes)

        if cmd.parameter_length > 0:
            assert len(param_bytes) == cmd.parameter_length, 'Invalid number of parameter bytes for command'
            bytes_to_write.extend(param_bytes)

        with self.__bus_lock:
            self.__write_bytes(bytes_to_write)
            sleep(cmd.response_time_ms / 1000.0)

            if cmd.response_length > 0:
                return self.__read_bytes(cmd.response_length)

        return None

    # NOT LOCKED. Use _run_command!
    def __read_bytes(self, length):
        return bytearray(self._bus.read_i2c_block_data(self.i2c_address, 0, length))

    # NOT LOCKED. Use _run_command!
    def __write_bytes(self, raw_bytes):
        assert len(raw_bytes) >= 1

        cmd_byte = raw_bytes[0]
        arg_bytes = list(raw_bytes[1:])  # bytes may or may not be a tuple

        self._bus.write_i2c_block_data(self.i2c_address, cmd_byte, arg_bytes)

    @classmethod
    def _read_checksummed_word(cls, data, offset=0):
        word_bytes = data[offset:offset + 2]
        word, checksum = struct.unpack_from('>HB', data, offset)
        assert checksum == cls._crc(word_bytes), 'Bad checksum!'
        return word

    @classmethod
    def _read_checksummed_words(cls, data, count):
        res = []
        for i in range(count):
            offset = (2 + 1) * i  # 2 bytes + checksum byte
            word = cls._read_checksummed_word(data, offset=offset)
            res.append(word)

        return res

    @classmethod
    def _bytes_for_checksummed_words(cls, words):
        res = bytearray()
        byte_offset = 0
        for word in words:
            # just going to let the error from struct.pack handle the case of the word being > 65536
            word_bytes = struct.pack('>H', word)
            res.extend(word_bytes)
            byte_offset = byte_offset + 2

            res.append(cls._crc(word_bytes))
            byte_offset = byte_offset + 1

        return res

    @classmethod
    def _crc(cls, data):
        crc = _SGP30_CRC_INIT
        # calculates 8-Bit checksum with given polynomial
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x80:
                    crc = (crc << 1) ^ _SGP30_CRC_POLYNOMIAL
                else:
                    crc <<= 1
        return crc & 0xFF