"""Raw-ctypes Windows Portable Devices (WPD) client -- the byte-level
transport underneath RealMtpBackend, used instead of the Shell's
IFileOperation copy engine.

Why this exists (2026-09-18 measurements against a real console, see
docs/PERF-MTP.md): IFileOperation gives no progress, no cancellation and no
visibility into where its time goes -- a 0.41 GB .nsz took 79.0/79.1/79.2 s
across three separate runs while the console's own progress bar finished in
about 19 s. WPD's CreateObjectWithPropertiesAndData() hands back an IStream
we write ourselves, so every byte is accounted for and the tail after the
last byte is separately measurable.

Every CLSID/IID below was read off this machine's own type libraries
(portabledeviceapi.dll / PortableDeviceTypes.dll) rather than transcribed
from documentation or memory. Every PROPERTYKEY was then verified against
the connected device by dumping an object's full property set and matching
the values back to what the Shell reports for the same object (e.g.
WPD_OBJECT_NAME == '1: SD Card').

No third-party dependency: this is plain ctypes over the COM vtables, so it
survives PyInstaller packaging unchanged (comtypes' runtime code generation
does not).

READ-ONLY apart from create_file_object()/create_folder(), which are the
only two functions here that write anything to a device.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import POINTER, byref, c_void_p, c_ulong, c_ulonglong, c_wchar_p, c_ushort, c_ubyte
from ctypes.wintypes import DWORD, ULONG, LPWSTR, LPCWSTR, BOOL

ole32 = ctypes.oledll.ole32
ole32_c = ctypes.windll.ole32
# Without an explicit argtype, a 64-bit pointer handed back as a Python int
# overflows ctypes' default c_int parameter.
ole32_c.CoTaskMemFree.argtypes = [c_void_p]
ole32_c.CoTaskMemFree.restype = None

S_OK = 0
CLSCTX_INPROC_SERVER = 1
COINIT_APARTMENTTHREADED = 2


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text=None):
        super().__init__()
        if text:
            ole32.CLSIDFromString(text, byref(self))

    def __str__(self):
        buf = ctypes.create_unicode_buffer(40)
        ole32.StringFromGUID2(byref(self), buf, 40)
        return buf.value

    def __eq__(self, other):
        return isinstance(other, GUID) and bytes(self) == bytes(other)

    def __hash__(self):
        return hash(bytes(self))


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", DWORD)]

    def __init__(self, guid_text=None, pid=0):
        super().__init__()
        if guid_text:
            self.fmtid = GUID(guid_text)
            self.pid = pid

    def __str__(self):
        return f"{self.fmtid},{self.pid}"

    def __eq__(self, other):
        return isinstance(other, PROPERTYKEY) and bytes(self) == bytes(other)

    def __hash__(self):
        return hash(bytes(self))


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", c_ushort), ("wReserved1", c_ushort), ("wReserved2", c_ushort),
                ("wReserved3", c_ushort), ("data", c_ubyte * 16)]

    def value(self):
        vt = self.vt
        if vt == 0 or vt == 1:
            return None
        if vt == 31:  # VT_LPWSTR
            return ctypes.cast(ctypes.c_void_p(int.from_bytes(bytes(self.data)[:8], "little")), c_wchar_p).value
        if vt == 19:  # VT_UI4
            return int.from_bytes(bytes(self.data)[:4], "little")
        if vt == 3:   # VT_I4
            return int.from_bytes(bytes(self.data)[:4], "little", signed=True)
        if vt == 21:  # VT_UI8
            return int.from_bytes(bytes(self.data)[:8], "little")
        if vt == 20:  # VT_I8
            return int.from_bytes(bytes(self.data)[:8], "little", signed=True)
        if vt == 11:  # VT_BOOL
            return int.from_bytes(bytes(self.data)[:2], "little") != 0
        if vt == 72:  # VT_CLSID
            ptr = int.from_bytes(bytes(self.data)[:8], "little")
            if not ptr:
                return None
            return str(ctypes.cast(ctypes.c_void_p(ptr), POINTER(GUID))[0])
        return f"<vt={vt}>"


# -- CLSIDs / IIDs, all read from the machine's own type libraries -----------
CLSID_PortableDeviceManager = "{0AF10CEC-2ECD-4B92-9581-34F6AE0637F3}"
IID_IPortableDeviceManager = "{A1567595-4C2F-4574-A6FA-ECEF917B9A40}"
CLSID_PortableDevice = "{728A21C5-3D9E-48D7-9810-864848F0F404}"
IID_IPortableDevice = "{625E2DF8-6392-4CF0-9AD1-3CFA5F17775C}"
CLSID_PortableDeviceValues = "{0C15D503-D017-47CE-9016-7B3F978721CC}"
IID_IPortableDeviceValues = "{6848F6F2-3155-4F86-B6F5-263EEEAB3143}"
CLSID_PortableDeviceKeyCollection = "{DE2D022D-2480-43BE-97F0-D1FA2CF98F4F}"
IID_IPortableDeviceKeyCollection = "{DADA2357-E0AD-492E-98DB-DD61C53BA353}"
IID_IPortableDeviceContent = "{6A96ED84-7C73-4480-9938-BF5AF477D426}"
IID_IEnumPortableDeviceObjectIDs = "{10ECE955-CF41-4728-BFA0-41EEDF1BBF19}"
IID_IPortableDeviceProperties = "{7F6D695C-03DF-4439-A809-59266BEEE3A6}"
IID_IPortableDeviceResources = "{FD8878AC-D841-4D17-891C-E6829CDB6934}"
IID_IStream = "{0000000C-0000-0000-C000-000000000046}"
IID_IPortableDeviceDataStream = "{88E04DB3-1012-4D64-9996-F703A950D3F4}"

# -- Well-known WPD property keys (verified against the device by dump) ------
_OBJ = "{EF6B490D-5CD8-437A-AFFC-DA8B60EE4A3C}"
WPD_OBJECT_ID = PROPERTYKEY(_OBJ, 2)
WPD_OBJECT_PARENT_ID = PROPERTYKEY(_OBJ, 3)
WPD_OBJECT_NAME = PROPERTYKEY(_OBJ, 4)
WPD_OBJECT_FORMAT = PROPERTYKEY(_OBJ, 6)
WPD_OBJECT_CONTENT_TYPE = PROPERTYKEY(_OBJ, 7)
WPD_OBJECT_SIZE = PROPERTYKEY(_OBJ, 11)
WPD_OBJECT_ORIGINAL_FILE_NAME = PROPERTYKEY(_OBJ, 12)
_CLIENT = "{204D9F0C-2292-4080-9F42-40664E70F859}"
WPD_CLIENT_NAME = PROPERTYKEY(_CLIENT, 2)
WPD_CLIENT_MAJOR_VERSION = PROPERTYKEY(_CLIENT, 3)
WPD_CLIENT_MINOR_VERSION = PROPERTYKEY(_CLIENT, 4)
WPD_CLIENT_REVISION = PROPERTYKEY(_CLIENT, 5)
_STORAGE = "{01A3057A-74D6-4E80-BEA7-DC4C212CE50A}"
WPD_STORAGE_CAPACITY = PROPERTYKEY(_STORAGE, 4)
WPD_STORAGE_FREE_SPACE_IN_BYTES = PROPERTYKEY(_STORAGE, 5)

# An object's own bytes, for IPortableDeviceResources::GetStream.
WPD_RESOURCE_DEFAULT = PROPERTYKEY("{E81E79BE-34F0-41BF-B53F-F1A06AE87842}", 0)
STGM_READ = 0
S_FALSE = 1

WPD_CONTENT_TYPE_FOLDER = GUID("{27E2E392-A111-48E0-AB0C-E17705A05F85}")
WPD_CONTENT_TYPE_GENERIC_FILE = GUID("{0085E0A6-8D34-45D7-BC5C-447E59C73D48}")
WPD_OBJECT_FORMAT_UNSPECIFIED = GUID("{30000000-AE6C-4804-98BA-C57B46965FE7}")

DEVICE_OBJECT_ID = "DEVICE"


class ComError(Exception):
    def __init__(self, hr, what):
        self.hr = hr & 0xFFFFFFFF
        super().__init__(f"{what} failed: 0x{self.hr:08X}")


def _check(hr, what):
    if hr != S_OK:
        raise ComError(hr, what)
    return hr


def vcall(ptr, index, argtypes, *args, what="call", restype=ctypes.c_long, check=True):
    """Invoke method #index of the COM object at `ptr` through its vtable."""
    vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    hr = proto(vtbl[index])(ptr, *args)
    if check:
        _check(hr, what)
    return hr


def release(ptr):
    if ptr:
        vcall(ptr, 2, (), what="Release", restype=ctypes.c_ulong, check=False)


def query_interface(ptr, iid_text):
    out = c_void_p()
    vcall(ptr, 0, (POINTER(GUID), POINTER(c_void_p)), byref(GUID(iid_text)), byref(out),
          what=f"QueryInterface({iid_text})")
    return out


def co_create(clsid_text, iid_text):
    out = c_void_p()
    _check(ole32_c.CoCreateInstance(byref(GUID(clsid_text)), None, CLSCTX_INPROC_SERVER,
                                    byref(GUID(iid_text)), byref(out)),
           f"CoCreateInstance({clsid_text})")
    return out


def co_initialize():
    ole32_c.CoInitializeEx(None, COINIT_APARTMENTTHREADED)


def _take_string(ptr_obj):
    """Read a [out] LPWSTR the callee allocated, then free it."""
    value = ctypes.cast(ptr_obj, c_wchar_p).value
    ole32_c.CoTaskMemFree(ptr_obj)
    return value


# -- IPortableDeviceValues ---------------------------------------------------

def values_new():
    return co_create(CLSID_PortableDeviceValues, IID_IPortableDeviceValues)


def values_set_string(values, key, text):
    vcall(values, 7, (POINTER(PROPERTYKEY), LPCWSTR), byref(key), text, what="SetStringValue")


def values_set_uint(values, key, number):
    vcall(values, 9, (POINTER(PROPERTYKEY), ULONG), byref(key), number, what="SetUnsignedIntegerValue")


def values_set_u64(values, key, number):
    vcall(values, 13, (POINTER(PROPERTYKEY), c_ulonglong), byref(key), number,
          what="SetUnsignedLargeIntegerValue")


def values_set_guid(values, key, guid):
    vcall(values, 27, (POINTER(PROPERTYKEY), POINTER(GUID)), byref(key), byref(guid), what="SetGuidValue")


def values_get_string(values, key):
    out = c_void_p()
    vcall(values, 8, (POINTER(PROPERTYKEY), POINTER(c_void_p)), byref(key), byref(out),
          what="GetStringValue")
    return _take_string(out)


def values_count(values):
    out = DWORD()
    vcall(values, 3, (POINTER(DWORD),), byref(out), what="GetCount")
    return out.value


def values_at(values, index):
    key = PROPERTYKEY()
    var = PROPVARIANT()
    vcall(values, 4, (DWORD, POINTER(PROPERTYKEY), POINTER(PROPVARIANT)), index, byref(key), byref(var),
          what="GetAt")
    return key, var


# -- device / content --------------------------------------------------------

def list_device_ids():
    manager = co_create(CLSID_PortableDeviceManager, IID_IPortableDeviceManager)
    try:
        count = DWORD(0)
        vcall(manager, 3, (POINTER(LPWSTR), POINTER(DWORD)), None, byref(count), what="GetDevices(count)")
        if not count.value:
            return []
        # An array of c_void_p, NOT of LPWSTR: indexing a c_wchar_p array
        # hands back a Python str and loses the pointer we must free.
        buf = (c_void_p * count.value)()
        vcall(manager, 3, (POINTER(c_void_p), POINTER(DWORD)), buf, byref(count), what="GetDevices")
        ids = []
        for i in range(count.value):
            ids.append(ctypes.cast(buf[i], c_wchar_p).value)
            ole32_c.CoTaskMemFree(buf[i])
        return ids
    finally:
        release(manager)


def open_device(pnp_id, client_name="SwitchAgent"):
    info = values_new()
    try:
        values_set_string(info, WPD_CLIENT_NAME, client_name)
        values_set_uint(info, WPD_CLIENT_MAJOR_VERSION, 1)
        values_set_uint(info, WPD_CLIENT_MINOR_VERSION, 0)
        values_set_uint(info, WPD_CLIENT_REVISION, 0)
        device = co_create(CLSID_PortableDevice, IID_IPortableDevice)
        vcall(device, 3, (LPCWSTR, c_void_p), pnp_id, info, what="IPortableDevice::Open")
        return device
    finally:
        release(info)


def close_device(device):
    if device:
        vcall(device, 8, (), what="Close", check=False)
        release(device)


def get_content(device):
    out = c_void_p()
    vcall(device, 5, (POINTER(c_void_p),), byref(out), what="Content")
    return out


def get_properties(content):
    out = c_void_p()
    vcall(content, 4, (POINTER(c_void_p),), byref(out), what="Properties")
    return out


def enum_children(content, parent_object_id):
    enum_ptr = c_void_p()
    vcall(content, 3, (DWORD, LPCWSTR, c_void_p, POINTER(c_void_p)),
          0, parent_object_id, None, byref(enum_ptr), what="EnumObjects")
    ids = []
    try:
        while True:
            batch = (c_void_p * 32)()
            fetched = ULONG(0)
            hr = vcall(enum_ptr, 3, (ULONG, POINTER(c_void_p), POINTER(ULONG)), 32, batch, byref(fetched),
                       what="Next", check=False)
            if fetched.value == 0:
                break
            for i in range(fetched.value):
                ids.append(ctypes.cast(batch[i], c_wchar_p).value)
                ole32_c.CoTaskMemFree(batch[i])
            if hr != S_OK:
                break
    finally:
        release(enum_ptr)
    return ids


def get_all_properties(properties, object_id):
    out = c_void_p()
    vcall(properties, 5, (LPCWSTR, c_void_p, POINTER(c_void_p)), object_id, None, byref(out),
          what="GetValues")
    return out


def read_props(properties, object_id):
    """{PROPERTYKEY-as-str: python value} for one object."""
    values = get_all_properties(properties, object_id)
    try:
        result = {}
        for i in range(values_count(values)):
            key, var = values_at(values, i)
            result[str(key)] = var.value()
        return result
    finally:
        release(values)


def object_name(props):
    return props.get(str(WPD_OBJECT_ORIGINAL_FILE_NAME)) or props.get(str(WPD_OBJECT_NAME))


def create_file_object(content, parent_object_id, filename, size):
    """CreateObjectWithPropertiesAndData -> (stream, optimal_chunk_size).

    WRITES to the device when the returned stream is written+committed.
    """
    values = values_new()
    try:
        values_set_string(values, WPD_OBJECT_PARENT_ID, parent_object_id)
        values_set_u64(values, WPD_OBJECT_SIZE, size)
        values_set_string(values, WPD_OBJECT_ORIGINAL_FILE_NAME, filename)
        values_set_string(values, WPD_OBJECT_NAME, filename)
        stream = c_void_p()
        chunk = DWORD(0)
        cookie = c_void_p()
        vcall(content, 7, (c_void_p, POINTER(c_void_p), POINTER(DWORD), POINTER(c_void_p)),
              values, byref(stream), byref(chunk), byref(cookie),
              what="CreateObjectWithPropertiesAndData")
        if cookie:
            ole32_c.CoTaskMemFree(cookie)
        return stream, chunk.value
    finally:
        release(values)


def stream_write(stream, buffer, length):
    """Writes exactly `length` bytes, looping until the stream has taken them
    all. IStream::Write may legitimately report a SHORT count; treating the
    requested length as written would silently under-deliver the object and
    leave the device waiting for bytes that never arrive."""
    total = 0
    while total < length:
        written = ULONG(0)
        vcall(stream, 4, (c_void_p, ULONG, POINTER(ULONG)),
              ctypes.byref(buffer, total), length - total, byref(written),
              what="IStream::Write")
        if written.value == 0:
            raise ComError(0, f"IStream::Write accepted 0 of {length - total} remaining bytes")
        total += written.value
    return total


# HRESULT_FROM_WIN32(ERROR_SEM_TIMEOUT): "the semaphore timeout period has
# expired" -- what the WPD stack returns when the device does not answer the
# end of a transfer within its own fixed timeout (~82s observed). Seen on
# real hardware 2026-09-19 committing a 388 MiB .nsz to DBI's install node:
# every byte had been accepted and the console's own screen reported the
# install finished ("Общее время установки: 0:00:35"), but DBI deletes the
# virtual object on completion rather than answering, so the commit never
# got its response. That is a device that is BUSY, not a transfer that
# failed -- see WpdSession.send_file.
ERROR_SEM_TIMEOUT_HRESULT = 0x80070079


def stream_commit(stream):
    vcall(stream, 8, (DWORD,), 0, what="IStream::Commit")


def stream_object_id(stream):
    """The object id the device gave the file just committed through
    `stream` (IPortableDeviceDataStream::GetObjectID, vtable slot 14: after
    IUnknown's 3, ISequentialStream's 2 and IStream's 9), or None when the
    stream will not say. Read-only."""
    try:
        data_stream = query_interface(stream, IID_IPortableDeviceDataStream)
    except ComError:
        return None
    try:
        out = c_void_p()
        vcall(data_stream, 14, (POINTER(c_void_p),), byref(out), what="IPortableDeviceDataStream::GetObjectID")
        return _take_string(out) if out.value else None
    except ComError:
        return None
    finally:
        release(data_stream)


def read_object(content, object_id, max_bytes):
    """An object's bytes (its default resource), or None as soon as it turns
    out to hold more than `max_bytes`. READ-ONLY: IPortableDeviceContent::
    Transfer (slot 5) -> IPortableDeviceResources::GetStream (slot 5, after
    IUnknown's 3, GetSupportedResources, GetResourceAttributes) -> IStream::
    Read (slot 3) until the stream is done."""
    resources = c_void_p()
    vcall(content, 5, (POINTER(c_void_p),), byref(resources), what="Transfer")
    try:
        stream = c_void_p()
        optimal = DWORD(0)
        vcall(resources, 5, (LPCWSTR, POINTER(PROPERTYKEY), DWORD, POINTER(DWORD), POINTER(c_void_p)),
              object_id, byref(WPD_RESOURCE_DEFAULT), STGM_READ, byref(optimal), byref(stream),
              what="GetStream")
        try:
            buffer = ctypes.create_string_buffer(max(optimal.value, 65536))
            chunks = []
            total = 0
            while True:
                read = ULONG(0)
                hr = vcall(stream, 3, (c_void_p, ULONG, POINTER(ULONG)), byref(buffer), len(buffer), byref(read),
                           what="IStream::Read", check=False) & 0xFFFFFFFF
                if hr not in (S_OK, S_FALSE):
                    raise ComError(hr, "IStream::Read")
                if read.value:
                    total += read.value
                    if total > max_bytes:
                        return None
                    chunks.append(buffer.raw[:read.value])
                if hr == S_FALSE or not read.value:
                    return b"".join(chunks)
        finally:
            release(stream)
    finally:
        release(resources)


def create_folder(content, parent_object_id, name):
    """CreateObjectWithPropertiesOnly for a folder. WRITES to the device."""
    values = values_new()
    try:
        values_set_string(values, WPD_OBJECT_PARENT_ID, parent_object_id)
        values_set_string(values, WPD_OBJECT_NAME, name)
        values_set_string(values, WPD_OBJECT_ORIGINAL_FILE_NAME, name)
        values_set_guid(values, WPD_OBJECT_CONTENT_TYPE, WPD_CONTENT_TYPE_FOLDER)
        values_set_guid(values, WPD_OBJECT_FORMAT, WPD_OBJECT_FORMAT_UNSPECIFIED)
        out = c_void_p()
        vcall(content, 6, (c_void_p, POINTER(c_void_p)), values, byref(out),
              what="CreateObjectWithPropertiesOnly")
        return _take_string(out)
    finally:
        release(values)


# ---------------------------------------------------------------------------
# Session-level API -- what RealMtpBackend actually talks to
# ---------------------------------------------------------------------------

# A device_id in this project is a Shell parsing path (see mtp/base.py); WPD
# names the same physical device by the PnP id embedded in that path, so the
# two are mechanically convertible and no second notion of device identity is
# introduced. Verified on real hardware 2026-09-18: the Shell path and
# IPortableDeviceManager::GetDevices()'s id differ only by this prefix, and
# the fingerprint switchagent stores round-trips through the conversion
# unchanged (tools/wpd_transfer_test.py --list prints it for comparison).
SHELL_DEVICE_PREFIX = "::{20D04FE0-3AEA-1069-A2D8-08002B30309D}\\"


def pnp_id_from_device_id(device_id):
    """None when `device_id` is not a Shell WPD device path this can convert
    -- callers then fall back to the Shell transport rather than guessing."""
    if not device_id or not device_id.startswith(SHELL_DEVICE_PREFIX):
        return None
    tail = device_id[len(SHELL_DEVICE_PREFIX):]
    return tail or None


def device_id_from_pnp_id(pnp_id):
    return SHELL_DEVICE_PREFIX + pnp_id


class TransferTiming:
    """Where a single file's seconds actually went -- the whole reason this
    module exists (IFileOperation could only ever report one opaque total)."""

    __slots__ = ("create_seconds", "stream_seconds", "commit_seconds", "bytes_written",
                 "finalise_timed_out")

    def __init__(self, create_seconds, stream_seconds, commit_seconds, bytes_written,
                 finalise_timed_out=False):
        self.create_seconds = create_seconds
        self.stream_seconds = stream_seconds
        self.commit_seconds = commit_seconds
        self.bytes_written = bytes_written
        # True when every byte was accepted but the device never answered the
        # commit within the WPD stack's own timeout -- see stream_commit's
        # ERROR_SEM_TIMEOUT_HRESULT comment. The bytes are delivered; what
        # the device did with them afterwards is simply unknown from here.
        self.finalise_timed_out = finalise_timed_out

    @property
    def total_seconds(self):
        return self.create_seconds + self.stream_seconds + self.commit_seconds

    @property
    def stream_mb_per_second(self):
        if self.stream_seconds <= 0:
            return 0.0
        return self.bytes_written / (1024 * 1024) / self.stream_seconds

    def __str__(self):
        tail = " (timed out waiting for the device to finalise)" if self.finalise_timed_out else ""
        return (f"create={self.create_seconds:.2f}s stream={self.stream_seconds:.2f}s "
                f"({self.stream_mb_per_second:.1f} MB/s) commit={self.commit_seconds:.2f}s{tail}")


DEFAULT_PROGRESS_INTERVAL_SECONDS = 1.0


class WpdSession:
    """One open IPortableDevice, for one physical device.

    Deliberately holds no state that outlives an open/close pair: the
    child-name cache exists only to keep a per-file existence check from
    re-enumerating the same destination folder for every file of a 45-file
    mod, and every write this class performs updates it in place.
    """

    def __init__(self, pnp_id, client_name="SwitchAgent"):
        self.pnp_id = pnp_id
        self._client_name = client_name
        self._device = None
        self._content = None
        self._properties = None
        self._children_cache = {}

    # -- lifecycle --------------------------------------------------------

    def open(self):
        co_initialize()
        self._device = open_device(self.pnp_id, self._client_name)
        self._content = get_content(self._device)
        self._properties = get_properties(self._content)
        self._children_cache = {}
        return self

    def close(self):
        release(self._properties)
        release(self._content)
        close_device(self._device)
        self._properties = self._content = self._device = None
        self._children_cache = {}

    def __enter__(self):
        return self.open()

    def __exit__(self, *_exc):
        self.close()
        return False

    # -- reads ------------------------------------------------------------

    def storages(self):
        """[(object_id, raw_name)] for every storage the device exposes, in
        the device's own order -- the raw names are exactly the ones the
        Shell reports ('1: SD Card', '5: SD Card install', ...), so
        resolve_storage_name() applies to them unchanged."""
        result = []
        for object_id in enum_children(self._content, DEVICE_OBJECT_ID):
            props = read_props(self._properties, object_id)
            result.append((object_id, object_name(props) or ""))
        return result

    def storage_properties(self, object_id):
        props = read_props(self._properties, object_id)
        return {
            "free_bytes": props.get(str(WPD_STORAGE_FREE_SPACE_IN_BYTES)),
            "total_bytes": props.get(str(WPD_STORAGE_CAPACITY)),
            "name": object_name(props) or "",
        }

    def children_by_name(self, parent_object_id):
        cached = self._children_cache.get(parent_object_id)
        if cached is not None:
            return cached
        mapping = {}
        for object_id in enum_children(self._content, parent_object_id):
            name = object_name(read_props(self._properties, object_id))
            if name:
                mapping[name] = object_id
        self._children_cache[parent_object_id] = mapping
        return mapping

    def child_id(self, parent_object_id, name):
        return self.children_by_name(parent_object_id).get(name)

    def object_size(self, object_id):
        return read_props(self._properties, object_id).get(str(WPD_OBJECT_SIZE))

    def read_file(self, object_id, max_bytes):
        return read_object(self._content, object_id, max_bytes)

    def forget_children(self, parent_object_id):
        self._children_cache.pop(parent_object_id, None)

    # -- writes -----------------------------------------------------------

    def ensure_folder(self, parent_object_id, name):
        existing = self.child_id(parent_object_id, name)
        if existing is not None:
            return existing
        object_id = create_folder(self._content, parent_object_id, name)
        self._children_cache.setdefault(parent_object_id, {})[name] = object_id
        return object_id

    def navigate(self, root_object_id, path, *, create_missing):
        """Walks a posix-style path one segment at a time ('' meaning the
        root itself). Returns None when a segment is missing and
        create_missing is False -- never guesses."""
        current = root_object_id
        for segment in (path or "").split("/"):
            if not segment:
                continue
            if create_missing:
                current = self.ensure_folder(current, segment)
            else:
                current = self.child_id(current, segment)
                if current is None:
                    return None
        return current

    def send_file(self, parent_object_id, filename, source_path, *, progress=None,
                  progress_interval_seconds=DEFAULT_PROGRESS_INTERVAL_SECONDS, remember=False):
        """Streams `source_path` into `parent_object_id` as `filename`.

        Returns a TransferTiming. `progress(bytes_done, bytes_total)` is
        called at most every progress_interval_seconds -- never per chunk,
        which at the measured 37 MB/s would be ~150 calls a second.

        Commit() returning success is the device's own acknowledgement that
        it accepted and finalised the object. That is a far stronger signal
        than anything the Shell transport could give us, but it still says
        nothing about what DBI then did with those bytes -- see
        verify_install_transport() in windows.py for why that distinction is
        preserved rather than upgraded to COMPLETED.

        remember=True records the new object in the parent's cached listing
        instead of dropping that listing. Only for a real filesystem: DBI's
        install node deletes its objects on completion, and there the next
        lookup has to ask the device again. Forgetting cost a full
        re-enumeration of the destination folder per file -- quadratic in
        the folder's size, and a homebrew port's data folder can hold 1,420
        files (Mega Man X Regenesis, measured 2026-09-23 at ~0.3-2 ms per
        listed child: minutes of listing for one folder).
        """
        size = source_path.stat().st_size
        started = time.perf_counter()
        stream, chunk_size = create_file_object(self._content, parent_object_id, filename, size)
        created = time.perf_counter()
        try:
            buffer = ctypes.create_string_buffer(chunk_size or 262144)
            written = 0
            last_report = created
            with source_path.open("rb") as handle:
                while True:
                    read = handle.readinto(buffer)
                    if not read:
                        break
                    stream_write(stream, buffer, read)
                    written += read
                    if progress is not None:
                        now = time.perf_counter()
                        if now - last_report >= progress_interval_seconds:
                            last_report = now
                            progress(written, size)
            streamed = time.perf_counter()
            # Report the full count BEFORE committing. Everything this side
            # of the cable can do is done; the commit below is purely waiting
            # on the device, and on real hardware that wait reached 82s for a
            # .nsz. Leaving the last partial second of bytes unreported until
            # after it made a finishing transfer look frozen at 98%.
            if progress is not None:
                progress(written, size)
            timed_out = False
            try:
                stream_commit(stream)
            except ComError as exc:
                if exc.hr != ERROR_SEM_TIMEOUT_HRESULT:
                    raise
                timed_out = True
            committed = time.perf_counter()
            new_object_id = stream_object_id(stream) if remember and not timed_out else None
        finally:
            release(stream)
        known = self._children_cache.get(parent_object_id)
        if new_object_id and known is not None and filename not in known:
            # Only ever added to a listing that was already read in full --
            # a listing that held just this file would hide its siblings
            # from every existence check after it.
            known[filename] = new_object_id
        else:
            self.forget_children(parent_object_id)
        return TransferTiming(created - started, streamed - created, committed - streamed, written,
                              finalise_timed_out=timed_out)


def available():
    """True when this machine can talk WPD at all -- never raises, so a
    caller can use it as a plain feature check before choosing a transport."""
    try:
        co_initialize()
        list_device_ids()
        return True
    except Exception:  # noqa: BLE001 -- a feature probe must never propagate
        return False
