"""Engine frame (RGB565, as the screen bitmap holds it) -> JPEG, for the window passthrough (PLAN 1.9).

Uses GDI+ from Windows through ctypes (no third-party modules; the bridge runs on Windows anyway): a 800x600 frame
encodes in a few milliseconds. Pillow is used instead when GDI+ is not available; the last resort is PNG
(launch.png_rgb565, pure Python, slow).

    enc = FrameEncoder(quality=80)
    fmt, data = enc.encode(rgb565_bytes, w, h, pitch)     # ('jpg', b'...') or ('png', b'...')
"""
import ctypes
import uuid
from ctypes import POINTER, Structure, byref, c_int, c_int64, c_size_t, c_uint16, c_uint32, c_ubyte, c_void_p

PIXEL_FORMAT_16BPP_RGB565 = 0x00021006
CLSID_JPEG = '557CF401-1A04-11D3-9A73-0000F81EF32E'
ENCODER_QUALITY = '1D5BE4B5-FA4A-452D-9CDD-5DB35105E7EB'


class GUID(Structure):
    _fields_ = [('Data1', c_uint32), ('Data2', c_uint16), ('Data3', c_uint16), ('Data4', c_ubyte * 8)]

    @classmethod
    def of(cls, s):
        u = uuid.UUID(s)
        g = cls(u.time_low, u.time_mid, u.time_hi_version)
        g.Data4[:] = list(u.bytes[8:])
        return g


class EncoderParameter(Structure):
    _fields_ = [('Guid', GUID), ('NumberOfValues', c_uint32), ('Type', c_uint32), ('Value', c_void_p)]


class EncoderParameters(Structure):
    _fields_ = [('Count', c_uint32), ('Parameter', EncoderParameter * 1)]


class StartupInput(Structure):
    _fields_ = [('GdiplusVersion', c_uint32), ('DebugEventCallback', c_void_p),
                ('SuppressBackgroundThread', c_int), ('SuppressExternalCodecs', c_int)]


class GdiPlusJpeg:
    def __init__(self, quality=80):
        self.gp = ctypes.WinDLL('gdiplus')
        self.ole = ctypes.WinDLL('ole32')
        gp, ole = self.gp, self.ole
        gp.GdiplusStartup.argtypes = [POINTER(c_size_t), POINTER(StartupInput), c_void_p]
        gp.GdipCreateBitmapFromScan0.argtypes = [c_int, c_int, c_int, c_int, c_void_p, POINTER(c_void_p)]
        gp.GdipSaveImageToStream.argtypes = [c_void_p, c_void_p, POINTER(GUID), POINTER(EncoderParameters)]
        gp.GdipDisposeImage.argtypes = [c_void_p]
        ole.CreateStreamOnHGlobal.argtypes = [c_void_p, c_int, POINTER(c_void_p)]
        self.token = c_size_t()
        si = StartupInput(1, None, 0, 0)
        st = gp.GdiplusStartup(byref(self.token), byref(si), None)
        if st != 0:
            raise OSError(f'GdiplusStartup: {st}')
        self.clsid = GUID.of(CLSID_JPEG)
        self.q = c_uint32(int(quality))
        self.params = EncoderParameters(1)
        p = self.params.Parameter[0]
        p.Guid = GUID.of(ENCODER_QUALITY)
        p.NumberOfValues = 1
        p.Type = 4          # EncoderParameterValueTypeLong
        p.Value = ctypes.cast(byref(self.q), c_void_p)
        # IStream methods by vtable slot
        self._Release = ctypes.WINFUNCTYPE(c_uint32, c_void_p)
        self._Read = ctypes.WINFUNCTYPE(c_int, c_void_p, c_void_p, c_uint32, POINTER(c_uint32))
        self._Seek = ctypes.WINFUNCTYPE(c_int, c_void_p, c_int64, c_uint32, POINTER(c_int64))

    def _vt(self, obj, slot, proto):
        vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
        return proto(vtbl[slot])

    def encode(self, data, w, h, pitch):
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        bmp = c_void_p()
        st = self.gp.GdipCreateBitmapFromScan0(w, h, pitch, PIXEL_FORMAT_16BPP_RGB565, buf, byref(bmp))
        if st != 0:
            raise OSError(f'GdipCreateBitmapFromScan0: {st}')
        stream = c_void_p()
        try:
            if self.ole.CreateStreamOnHGlobal(None, 1, byref(stream)) != 0:
                raise OSError('CreateStreamOnHGlobal')
            st = self.gp.GdipSaveImageToStream(bmp, stream, byref(self.clsid), byref(self.params))
            if st != 0:
                raise OSError(f'GdipSaveImageToStream: {st}')
            size = c_int64()
            self._vt(stream, 5, self._Seek)(stream, 0, 1, byref(size))    # STREAM_SEEK_CUR: bytes written
            self._vt(stream, 5, self._Seek)(stream, 0, 0, None)           # rewind
            out = ctypes.create_string_buffer(size.value)
            got = c_uint32()
            self._vt(stream, 3, self._Read)(stream, out, size.value, byref(got))
            return out.raw[:got.value]
        finally:
            self.gp.GdipDisposeImage(bmp)
            if stream:
                self._vt(stream, 2, self._Release)(stream)


class FrameEncoder:
    def __init__(self, quality=80):
        self.quality = quality
        self.impl = None
        try:
            self.impl = GdiPlusJpeg(quality)
            self.kind = 'gdiplus'
        except (OSError, AttributeError):
            try:
                import PIL.Image  # noqa: F401
                self.kind = 'pillow'
            except ImportError:
                self.kind = 'png'

    def encode(self, data, w, h, pitch):
        if self.kind == 'gdiplus':
            return 'jpg', self.impl.encode(data, w, h, pitch)
        if self.kind == 'pillow':
            import io
            import PIL.Image
            img = PIL.Image.frombuffer('RGB', (w, h), bytes(data), 'raw', 'BGR;16', pitch, 1)
            f = io.BytesIO()
            img.save(f, 'JPEG', quality=self.quality)
            return 'jpg', f.getvalue()
        from launch import png_rgb565
        return 'png', png_rgb565(data, w, h, pitch)
