import array
import ctypes
import ctypes.util
import os

CONFIG_HYBRID_FLAG = 0x8
CONFIG_JOINT_STEREO = 0x10
CONFIG_FAST_FLAG = 0x200
CONFIG_SHAPE_OVERRIDE = 0x8000
CONFIG_JOINT_OVERRIDE = 0x10000
CONFIG_COMPATIBLE_WRITE = 0x400000


class WavpackConfig(ctypes.Structure):
    _fields_ = [
        ("bitrate", ctypes.c_float),
        ("shaping_weight", ctypes.c_float),
        ("bits_per_sample", ctypes.c_int),
        ("bytes_per_sample", ctypes.c_int),
        ("qmode", ctypes.c_int),
        ("flags", ctypes.c_int),
        ("xmode", ctypes.c_int),
        ("num_channels", ctypes.c_int),
        ("float_norm_exp", ctypes.c_int),
        ("block_samples", ctypes.c_int32),
        ("extra_flags", ctypes.c_int32),
        ("sample_rate", ctypes.c_int32),
        ("channel_mask", ctypes.c_int32),
        ("md5_checksum", ctypes.c_ubyte * 16),
        ("md5_read", ctypes.c_ubyte),
        ("num_tag_strings", ctypes.c_int),
        ("tag_strings", ctypes.c_void_p),
    ]


BLOCK_OUT = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32)

DROP_IDS = (0x21, 0x2A, 0x2F)
HAS_CHECKSUM = 0x10000000


def simplify_block(block):
    out = bytearray(block[:32])
    i = 32
    while i < len(block):
        sub_id = block[i]
        if sub_id & 0x80:
            size = (block[i + 1] | (block[i + 2] << 8) | (block[i + 3] << 16)) * 2 + 4
        else:
            size = block[i + 1] * 2 + 2
        if (sub_id & 0x3F) not in DROP_IDS:
            out += block[i : i + size]
        i += size
    flags = int.from_bytes(out[24:28], "little") & ~HAS_CHECKSUM
    out[24:28] = flags.to_bytes(4, "little")
    out[8:10] = (0x405).to_bytes(2, "little")
    out[4:8] = (len(out) - 8).to_bytes(4, "little")
    return bytes(out)


def load_library():
    name = os.environ.get("WAVPACK_LIB") or ctypes.util.find_library("wavpack") or "libwavpack.so.1"
    lib = ctypes.CDLL(name)
    lib.WavpackOpenFileOutput.restype = ctypes.c_void_p
    lib.WavpackOpenFileOutput.argtypes = [BLOCK_OUT, ctypes.c_void_p, ctypes.c_void_p]
    lib.WavpackSetConfiguration.argtypes = [ctypes.c_void_p, ctypes.POINTER(WavpackConfig), ctypes.c_uint32]
    lib.WavpackPackInit.argtypes = [ctypes.c_void_p]
    lib.WavpackPackSamples.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32), ctypes.c_uint32]
    lib.WavpackFlushSamples.argtypes = [ctypes.c_void_p]
    lib.WavpackGetErrorMessage.restype = ctypes.c_char_p
    lib.WavpackGetErrorMessage.argtypes = [ctypes.c_void_p]
    return lib


class Encoder:
    def __init__(self, bits_per_sample=2.5, block_samples=2000, sample_rate=48000, lossless=False):
        self.lib = load_library()
        self.blocks = []
        self.callback = BLOCK_OUT(self._collect)
        self.ctx = self.lib.WavpackOpenFileOutput(self.callback, None, None)
        cfg = WavpackConfig()
        cfg.bitrate = bits_per_sample
        cfg.bits_per_sample = 16
        cfg.bytes_per_sample = 2
        cfg.num_channels = 2
        cfg.channel_mask = 3
        cfg.sample_rate = sample_rate
        cfg.block_samples = block_samples
        cfg.shaping_weight = 0.0
        cfg.flags = (
            (0 if lossless else CONFIG_HYBRID_FLAG)
            | CONFIG_JOINT_STEREO
            | CONFIG_JOINT_OVERRIDE
            | CONFIG_SHAPE_OVERRIDE
            | CONFIG_FAST_FLAG
            | CONFIG_COMPATIBLE_WRITE
        )
        if not self.lib.WavpackSetConfiguration(self.ctx, ctypes.byref(cfg), 0xFFFFFFFF):
            raise RuntimeError(self.lib.WavpackGetErrorMessage(self.ctx))
        if not self.lib.WavpackPackInit(self.ctx):
            raise RuntimeError(self.lib.WavpackGetErrorMessage(self.ctx))

    def _collect(self, _id, data, count):
        self.blocks.append(simplify_block(ctypes.string_at(data, count)))
        return 1

    def encode(self, pcm):
        frames = len(pcm) // 4
        values = array.array("i", memoryview(pcm[: frames * 4]).cast("h"))
        samples = (ctypes.c_int32 * (frames * 2)).from_buffer(values)
        self.blocks = []
        self.lib.WavpackPackSamples(self.ctx, samples, frames)
        self.lib.WavpackFlushSamples(self.ctx)
        return self.blocks
