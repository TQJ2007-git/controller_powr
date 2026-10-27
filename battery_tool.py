# -*- coding: utf-8 -*-
"""
设备电量小工具
================

在 Windows 10/11 上同时显示三个外设的电量：

* 鼠标  MCHOSE A7 Pro   —— 2.4G 接收器（RealTek/MCHOSE 私有协议）
* 键盘  MCHOSE G98     —— 2.4G 接收器（Compx V1 协议，report 0x13）
* 手柄  Xbox 无线控制器 —— 蓝牙，读取 Windows PnP 电量属性

纯 Python 标准库实现（ctypes + tkinter），无需安装任何第三方包。

用法：
    python battery_tool.py            # 打开小面板
    python battery_tool.py --once     # 命令行读取一次
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import queue
import sys
import threading
import time

APP_NAME = "设备电量"
REFRESH_SECONDS = 60

MOUSE_VID = 0x5253
MOUSE_USAGE_PAGE = 0xFF01
KEYBOARD_VID = 0x41E4
KEYBOARD_USAGE_PAGE = 0xFF02
XBOX_NAME_HINT = "xbox"


# --------------------------------------------------------------------------
# Win32 / HID 基础
# --------------------------------------------------------------------------

hid = ctypes.WinDLL("hid")
setupapi = ctypes.WinDLL("setupapi")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
cm = ctypes.WinDLL("cfgmgr32")


class GUID(ctypes.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class SP_DEVINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", w.DWORD), ("Reserved", ctypes.c_size_t)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", ctypes.c_ushort), ("UsagePage", ctypes.c_ushort),
                ("InputReportByteLength", ctypes.c_ushort),
                ("OutputReportByteLength", ctypes.c_ushort),
                ("FeatureReportByteLength", ctypes.c_ushort),
                ("Reserved", ctypes.c_ushort * 17),
                ("NumberLinkCollectionNodes", ctypes.c_ushort),
                ("NumberInputButtonCaps", ctypes.c_ushort),
                ("NumberInputValueCaps", ctypes.c_ushort),
                ("NumberInputDataIndices", ctypes.c_ushort),
                ("NumberOutputButtonCaps", ctypes.c_ushort),
                ("NumberOutputValueCaps", ctypes.c_ushort),
                ("NumberOutputDataIndices", ctypes.c_ushort),
                ("NumberFeatureButtonCaps", ctypes.c_ushort),
                ("NumberFeatureValueCaps", ctypes.c_ushort),
                ("NumberFeatureDataIndices", ctypes.c_ushort)]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", w.ULONG), ("VendorID", ctypes.c_ushort),
                ("ProductID", ctypes.c_ushort), ("VersionNumber", ctypes.c_ushort)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]


hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
hid.HidD_GetAttributes.argtypes = [w.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]
hid.HidD_GetPreparsedData.argtypes = [w.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
hid.HidD_GetProductString.argtypes = [w.HANDLE, ctypes.c_void_p, w.ULONG]
setupapi.SetupDiGetClassDevsW.restype = w.HANDLE
setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), w.LPCWSTR, w.HWND, w.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID),
                                                w.DWORD, ctypes.POINTER(SP_DEVINFO)]
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [w.HANDLE, ctypes.POINTER(SP_DEVINFO),
                                                      ctypes.c_void_p, w.DWORD,
                                                      ctypes.POINTER(w.DWORD), ctypes.c_void_p]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [w.HANDLE]
k32.CreateFileW.restype = w.HANDLE
k32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
k32.CreateEventW.restype = w.HANDLE
k32.CreateEventW.argtypes = [ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]
k32.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
k32.GetOverlappedResult.argtypes = [w.HANDLE, ctypes.POINTER(OVERLAPPED),
                                    ctypes.POINTER(w.DWORD), w.BOOL]
k32.WriteFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD,
                          ctypes.POINTER(w.DWORD), ctypes.POINTER(OVERLAPPED)]
k32.ReadFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD,
                         ctypes.POINTER(w.DWORD), ctypes.POINTER(OVERLAPPED)]
k32.CancelIoEx.argtypes = [w.HANDLE, ctypes.POINTER(OVERLAPPED)]
k32.CloseHandle.argtypes = [w.HANDLE]

FILE_FLAG_OVERLAPPED = 0x40000000
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
INVALID_HANDLE = w.HANDLE(-1).value

_GUID_HID = GUID()
hid.HidD_GetHidGuid(ctypes.byref(_GUID_HID))


def _hid_paths():
    """枚举所有 HID 接口路径。"""
    h = setupapi.SetupDiGetClassDevsW(ctypes.byref(_GUID_HID), None, None, 0x12)
    if not h or h == INVALID_HANDLE:
        return []
    found = []
    index = 0
    try:
        while True:
            did = SP_DEVINFO()
            did.cbSize = ctypes.sizeof(SP_DEVINFO)
            if not setupapi.SetupDiEnumDeviceInterfaces(h, None, ctypes.byref(_GUID_HID),
                                                        index, ctypes.byref(did)):
                break
            need = w.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(h, ctypes.byref(did), None, 0,
                                                      ctypes.byref(need), None)
            if need.value:
                buf = ctypes.create_string_buffer(need.value)
                ctypes.cast(buf, ctypes.POINTER(w.DWORD))[0] = 8  # cbSize (x64)
                if setupapi.SetupDiGetDeviceInterfaceDetailW(h, ctypes.byref(did), buf,
                                                             need.value, ctypes.byref(need), None):
                    found.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
            index += 1
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(h)
    return found


def _hid_info(path):
    """读取接口的 VID / 用途页 / 报告长度。"""
    info = {"path": path}
    h = k32.CreateFileW(path, 0, 3, None, 3, 0, None)
    if not h or h == INVALID_HANDLE:
        return info
    try:
        attrs = HIDD_ATTRIBUTES()
        attrs.Size = ctypes.sizeof(attrs)
        if hid.HidD_GetAttributes(h, ctypes.byref(attrs)):
            info["vid"] = attrs.VendorID
            info["pid"] = attrs.ProductID
        pp = ctypes.c_void_p()
        if hid.HidD_GetPreparsedData(h, ctypes.byref(pp)) and pp.value:
            caps = HIDP_CAPS()
            if hid.HidP_GetCaps(pp, ctypes.byref(caps)) == 0x110000:
                info["usage_page"] = caps.UsagePage
                info["in_len"] = caps.InputReportByteLength
                info["out_len"] = caps.OutputReportByteLength
            hid.HidD_FreePreparsedData(pp)
        buf = ctypes.create_unicode_buffer(256)
        if hid.HidD_GetProductString(h, buf, 512):
            info["product"] = buf.value
    finally:
        k32.CloseHandle(h)
    return info


def _find_hid(vid, usage_page):
    for path in _hid_paths():
        info = _hid_info(path)
        if info.get("vid") == vid and info.get("usage_page") == usage_page:
            return info
    return None


class HidDevice:
    """带超时的重叠 I/O HID 句柄。"""

    def __init__(self, info):
        self.info = info
        self.path = info["path"]
        access = GENERIC_READ | GENERIC_WRITE
        self.handle = k32.CreateFileW(self.path, access, 3, None, 3,
                                      FILE_FLAG_OVERLAPPED, None)
        if not self.handle or self.handle == INVALID_HANDLE:
            raise OSError(ctypes.get_last_error(), "打开 HID 设备失败")
        self.event = k32.CreateEventW(None, True, False, None)
        self.in_len = info.get("in_len") or 65
        self.out_len = info.get("out_len") or 65

    def close(self):
        if self.handle and self.handle != INVALID_HANDLE:
            k32.CloseHandle(self.handle)
            self.handle = None
        if self.event:
            k32.CloseHandle(self.event)
            self.event = None

    def write(self, data):
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        ov = OVERLAPPED()
        ov.hEvent = self.event
        k32.ResetEvent(self.event)
        ok = k32.WriteFile(self.handle, buf, len(data), None, ctypes.byref(ov))
        if not ok and ctypes.get_last_error() != 997:  # ERROR_IO_PENDING
            raise OSError(ctypes.get_last_error(), "写入失败")
        if k32.WaitForSingleObject(self.event, 1000) != 0:
            k32.CancelIoEx(self.handle, ctypes.byref(ov))
            raise OSError(1460, "写入超时")
        done = w.DWORD()
        if not k32.GetOverlappedResult(self.handle, ctypes.byref(ov), ctypes.byref(done), False):
            raise OSError(ctypes.get_last_error(), "写入失败")
        return done.value

    def read(self, timeout_ms=300):
        buf = ctypes.create_string_buffer(self.in_len)
        ov = OVERLAPPED()
        ov.hEvent = self.event
        k32.ResetEvent(self.event)
        ok = k32.ReadFile(self.handle, buf, self.in_len, None, ctypes.byref(ov))
        if not ok and ctypes.get_last_error() != 997:
            return None
        if k32.WaitForSingleObject(self.event, timeout_ms) != 0:
            k32.CancelIoEx(self.handle, ctypes.byref(ov))
            return None
        done = w.DWORD()
        if not k32.GetOverlappedResult(self.handle, ctypes.byref(ov), ctypes.byref(done), False):
            return None
        return bytes(buf.raw[:done.value])


# --------------------------------------------------------------------------
# 鼠标：MCHOSE A7 Pro（RealTek 方案，report 0x11 发送 / 0x13 回包）
# --------------------------------------------------------------------------

def read_mouse():
    info = _find_hid(MOUSE_VID, MOUSE_USAGE_PAGE)
    if not info:
        return {"percent": None, "charging": False, "state": "off", "note": "未连接"}
    dev = None
    try:
        dev = HidDevice(info)
        payload = bytes([0x0B, 0xAA]) + bytes(18)
        frame = bytes([0x11]) + bytes(b ^ 0xFF for b in payload)
        dev.write(frame)
        deadline = time.time() + 1.5
        while time.time() < deadline:
            r = dev.read(250)
            if not r or len(r) < 8:
                continue
            dec = bytes(b ^ 0xFF for b in r[1:])
            if dec[0] != 0xE2:
                continue
            pct = dec[4]
            if not (0 < pct <= 100):
                continue
            name = bytes(c for c in dec[9:17] if 32 <= c < 127).decode("ascii", "ignore")
            return {"percent": pct, "charging": bool(dec[3]), "state": "ok",
                    "note": ("充电中 · " if dec[3] else "") + (name or "已连接")}
    except OSError:
        pass
    finally:
        if dev:
            dev.close()
    return {"percent": None, "charging": False, "state": "sleep", "note": "无响应（动一下鼠标）"}


# --------------------------------------------------------------------------
# 键盘：MCHOSE G98 / Compx V1（report 0x13，20 字节，末字节为前 19 字节之和）
# --------------------------------------------------------------------------

KB_SESSION_CMD = 0x05
KB_BATTERY_CMD = 0x4A
KB_PUSH_CMD = 0x0A
KB_PUSH_TYPE_BATTERY = 0x05


def _kb_frame(cmd, param):
    pkt = bytearray(20)
    pkt[0] = 0x13
    pkt[1] = cmd
    pkt[2] = param
    pkt[19] = sum(pkt[:19]) & 0xFF
    return bytes(pkt)


class KeyboardReader:
    """保持会话；查询失败时下次重新开会话。"""

    def __init__(self):
        self.dev = None
        self.path = None
        self.failures = 0

    def _open(self):
        info = _find_hid(KEYBOARD_VID, KEYBOARD_USAGE_PAGE)
        if not info:
            self.close()
            return False
        try:
            self.dev = HidDevice(info)
            self.path = info["path"]
            self.dev.write(_kb_frame(KB_SESSION_CMD, 0x01))
            # 会话 ACK，允许键盘从休眠中醒来
            deadline = time.time() + 1.0
            while time.time() < deadline:
                self.dev.read(150)
            return True
        except OSError:
            self.close()
            return False

    def close(self):
        if self.dev:
            self.dev.close()
        self.dev = None
        self.path = None

    def read(self):
        if self.dev is None and not self._open():
            return {"percent": None, "charging": False, "state": "off", "note": "未连接"}

        result = {"percent": None, "charging": False, "state": "sleep",
                  "note": "无响应（按任意键唤醒）"}
        for attempt in range(2):
            try:
                self.dev.write(_kb_frame(KB_BATTERY_CMD, 0x01))
            except OSError:
                self.close()
                return {"percent": None, "charging": False, "state": "off", "note": "未连接"}
            deadline = time.time() + 1.2
            while time.time() < deadline:
                r = self.dev.read(150)
                if not r or len(r) < 8:
                    continue
                if r[1] == KB_BATTERY_CMD:
                    pct, flag = r[5], r[6]
                elif r[1] == KB_PUSH_CMD and r[5] == KB_PUSH_TYPE_BATTERY:
                    pct, flag = r[6], r[7]
                else:
                    continue
                if pct <= 1 or pct > 100:      # 0/1 是休眠占位值
                    result = {"percent": None, "charging": False, "state": "sleep",
                              "note": "已休眠"}
                    continue
                self.failures = 0
                return {"percent": pct, "charging": bool(flag & 0x10), "state": "ok",
                        "note": "充电中" if flag & 0x10 else "已连接"}

        self.failures += 1
        if self.failures >= 2:
            # 连续失败：会话可能被别的程序占住，关掉句柄下轮重开
            self.close()
            result["note"] = "无响应（按任意键唤醒，或拔插接收器）"
        return result


# --------------------------------------------------------------------------
# 手柄：Xbox 无线控制器（蓝牙 PnP 电量属性）
# --------------------------------------------------------------------------

DEVPROP_TYPE_BYTE = 0x00000003
DEVPROP_TYPE_STRING = 0x00000012
BATTERY_PROPKEY = (GUID(0x104EA319, 0x6EE2, 0x4701,
                        (ctypes.c_ubyte * 8)(0xBD, 0x47, 0x8D, 0xDB, 0xF4, 0x25, 0xBB, 0xE5)), 2)
FRIENDLY_NAME_PROPKEY = (GUID(0xA45C254E, 0xDF1C, 0x4EFD,
                              (ctypes.c_ubyte * 8)(0x80, 0x20, 0x67, 0xD1, 0x46, 0xA8, 0x50, 0xE0)), 14)


class DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", w.ULONG)]


cm.CM_Get_Device_ID_List_SizeW.argtypes = [ctypes.POINTER(w.ULONG), w.LPCWSTR, w.ULONG]
cm.CM_Get_Device_ID_ListW.argtypes = [w.LPCWSTR, w.LPWSTR, w.ULONG, w.ULONG]
cm.CM_Locate_DevNodeW.argtypes = [ctypes.POINTER(w.DWORD), w.LPCWSTR, w.ULONG]
cm.CM_Get_DevNode_Status.argtypes = [ctypes.POINTER(w.ULONG), ctypes.POINTER(w.ULONG),
                                     w.DWORD, w.ULONG]
cm.CM_Get_DevNode_PropertyW.argtypes = [w.DWORD, ctypes.POINTER(DEVPROPKEY),
                                        ctypes.POINTER(w.ULONG), ctypes.c_void_p,
                                        ctypes.POINTER(w.ULONG), w.ULONG]

CM_GETIDLIST_FILTER_ENUMERATOR = 0x00000001
DN_STARTED = 0x00000008


class SYSTEMTIME(ctypes.Structure):
    _fields_ = [("wYear", w.WORD), ("wMonth", w.WORD), ("wDayOfWeek", w.WORD),
                ("wDay", w.WORD), ("wHour", w.WORD), ("wMinute", w.WORD),
                ("wSecond", w.WORD), ("wMilliseconds", w.WORD)]


class BT_ADDR(ctypes.Union):
    _fields_ = [("ullLong", ctypes.c_ulonglong), ("rgBytes", ctypes.c_ubyte * 6)]


class BT_DEVICE_INFO(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD), ("Address", BT_ADDR), ("ulClassofDevice", w.DWORD),
                ("fConnected", w.BOOL), ("fRemembered", w.BOOL), ("fAuthenticated", w.BOOL),
                ("stLastSeen", SYSTEMTIME), ("stLastUsed", SYSTEMTIME), ("szName", w.WCHAR * 248)]


class BT_SEARCH(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD), ("fReturnAuthenticated", w.BOOL),
                ("fReturnRemembered", w.BOOL), ("fReturnUnknown", w.BOOL),
                ("fReturnConnected", w.BOOL), ("fIssueInquiry", w.BOOL),
                ("cTimeoutMultiplier", ctypes.c_ubyte), ("hRadio", w.HANDLE)]


_bth = ctypes.WinDLL("bthprops.cpl", use_last_error=True)
_bth.BluetoothFindFirstDevice.restype = w.HANDLE
_bth.BluetoothFindFirstDevice.argtypes = [ctypes.POINTER(BT_SEARCH),
                                          ctypes.POINTER(BT_DEVICE_INFO)]
_bth.BluetoothFindNextDevice.argtypes = [w.HANDLE, ctypes.POINTER(BT_DEVICE_INFO)]
_bth.BluetoothFindDeviceClose.argtypes = [w.HANDLE]


def _bt_connected_devices():
    """当前已连接的蓝牙设备 {mac: 名称}；取不到（无蓝牙适配器）时返回 None。

    休眠/关闭的设备的 PnP 节点依然存在并保留旧电量，所以必须用真实连接状态过滤。
    """
    sp = BT_SEARCH()
    sp.dwSize = ctypes.sizeof(sp)
    sp.fReturnAuthenticated = True
    sp.fReturnRemembered = True
    sp.fReturnConnected = True
    info = BT_DEVICE_INFO()
    info.dwSize = ctypes.sizeof(info)
    handle = _bth.BluetoothFindFirstDevice(ctypes.byref(sp), ctypes.byref(info))
    if not handle:
        return None
    found = {}
    try:
        while True:
            if info.fConnected:
                mac = "".join(f"{b:02x}" for b in info.Address.rgBytes[::-1])
                found[mac] = info.szName
            if not _bth.BluetoothFindNextDevice(handle, ctypes.byref(info)):
                break
    finally:
        _bth.BluetoothFindDeviceClose(handle)
    return found


def _devnode_list(filter_text):
    size = w.ULONG(0)
    if cm.CM_Get_Device_ID_List_SizeW(ctypes.byref(size), filter_text,
                                      CM_GETIDLIST_FILTER_ENUMERATOR) != 0:
        return []
    buf = ctypes.create_unicode_buffer(size.value)
    if cm.CM_Get_Device_ID_ListW(filter_text, buf, size.value,
                                 CM_GETIDLIST_FILTER_ENUMERATOR) != 0:
        return []
    raw = bytes(buf)
    text = raw.decode("utf-16-le", "ignore")
    return [s for s in text.split("\x00") if s]


def _devnode_property(devnode, key, expected_type):
    key_struct = DEVPROPKEY(key[0], key[1])
    ptype = w.ULONG(0)
    size = w.ULONG(0)
    cm.CM_Get_DevNode_PropertyW(devnode, ctypes.byref(key_struct), ctypes.byref(ptype),
                                None, ctypes.byref(size), 0)
    if not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if cm.CM_Get_DevNode_PropertyW(devnode, ctypes.byref(key_struct), ctypes.byref(ptype),
                                   buf, ctypes.byref(size), 0) != 0:
        return None
    if ptype.value != expected_type:
        return None
    if expected_type == DEVPROP_TYPE_BYTE:
        return buf.raw[0]
    return buf.raw[:size.value].decode("utf-16-le", "ignore").split("\x00")[0]


def _norm_name(text):
    return " ".join((text or "").split()).lower()


def read_controller():
    connected = _bt_connected_devices()
    connected_names = {_norm_name(n) for n in connected.values()} if connected else set()
    for dev_id in _devnode_list("BTHLE"):
        devnode = w.DWORD(0)
        if cm.CM_Locate_DevNodeW(ctypes.byref(devnode), dev_id, 0) != 0:
            continue
        name = _devnode_property(devnode, FRIENDLY_NAME_PROPKEY, DEVPROP_TYPE_STRING) or ""
        if XBOX_NAME_HINT not in name.lower() and "xbox" not in dev_id.lower():
            continue

        # BTHLE\Dev_<addr>\... -> <addr>
        #
        # 注意：蓝牙经典地址和 BLE 地址不是同一个（两套 MAC），容器 ID 也各自独立，
        # 所以只能拿名称跟"当前已连接设备"对，或者恰好 MAC 相同。
        mac = dev_id.split("\\")[1].replace("Dev_", "").lower()
        if connected is not None and mac not in connected \
                and _norm_name(name) not in connected_names:
            return {"percent": None, "charging": False, "state": "off", "note": "未连接"}

        status = w.ULONG(0)
        problem = w.ULONG(0)
        started = (cm.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem),
                                            devnode, 0) == 0
                   and bool(status.value & DN_STARTED))
        if connected is None and not started:
            return {"percent": None, "charging": False, "state": "off", "note": "未连接"}

        pct = _devnode_property(devnode, BATTERY_PROPKEY, DEVPROP_TYPE_BYTE)
        if pct is None or not (0 < pct <= 100):
            return {"percent": None, "charging": False, "state": "ok", "note": "系统未提供电量"}
        return {"percent": pct, "charging": False, "state": "ok", "note": "蓝牙已连接"}
    return {"percent": None, "charging": False, "state": "off", "note": "未连接"}


# --------------------------------------------------------------------------
# 后台读取
# --------------------------------------------------------------------------

_keyboard = KeyboardReader()
_read_lock = threading.Lock()   # 定时刷新与「立即刷新」可能同时发生


def read_all():
    with _read_lock:
        return {
            "mouse": read_mouse(),
            "keyboard": _keyboard.read(),
            "controller": read_controller(),
        }


def _worker(out_queue, stop_event):
    while not stop_event.is_set():
        try:
            out_queue.put(read_all())
        except Exception as exc:  # 单个设备出错不应让整个面板停摆
            out_queue.put({"_error": str(exc)})
        stop_event.wait(REFRESH_SECONDS)


# --------------------------------------------------------------------------
# 界面
# --------------------------------------------------------------------------

CARD_BG = "#ffffff"
CARD_BORDER = "#e7eaf0"
WINDOW_BG = "#f3f5f8"
TEXT = "#111827"
MUTED = "#6b7280"
ACCENT = "#2563eb"
FONT = "Microsoft YaHei UI"


def _level_color(pct):
    if pct is None:
        return "#c3cbd6"
    if pct <= 20:
        return "#ef4444"
    if pct <= 50:
        return "#f59e0b"
    return "#22c55e"


def _round_rect(canvas, x1, y1, x2, y2, r, **kwargs):
    """在 Canvas 上画圆角矩形。"""
    points = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
              x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
              x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(points, smooth=True, **kwargs)


def _draw_icon(canvas, kind, cx, cy, color):
    """左侧设备图标，统一线宽和配色。"""
    if kind == "mouse":
        _round_rect(canvas, cx - 7, cy - 11, cx + 7, cy + 11, 7,
                    fill="", outline=color, width=2)
        canvas.create_line(cx, cy - 10, cx, cy - 3, fill=color, width=2)
    elif kind == "keyboard":
        _round_rect(canvas, cx - 13, cy - 8, cx + 13, cy + 8, 3,
                    fill="", outline=color, width=2)
        for dx in (-8, -3, 2, 7):
            canvas.create_oval(cx + dx - 1, cy - 3, cx + dx + 1, cy - 1,
                               fill=color, outline="")
        canvas.create_line(cx - 7, cy + 4, cx + 7, cy + 4, fill=color, width=2)
    else:  # pad
        _round_rect(canvas, cx - 14, cy - 7, cx + 14, cy + 7, 7,
                    fill="", outline=color, width=2)
        canvas.create_line(cx - 9, cy, cx - 3, cy, fill=color, width=2)
        canvas.create_line(cx - 6, cy - 3, cx - 6, cy + 3, fill=color, width=2)
        canvas.create_oval(cx + 3, cy - 2, cx + 7, cy + 2, fill=color, outline="")
        canvas.create_oval(cx + 9, cy - 2, cx + 13, cy + 2, fill=color, outline="")


class BatteryCard:
    H = 98

    def __init__(self, parent, title, subtitle, kind, tint, ink):
        import tkinter as tk
        import tkinter.font

        self.canvas = tk.Canvas(parent, height=self.H, bg=WINDOW_BG,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="x", padx=18, pady=5)
        self.canvas.bind("<Configure>", lambda _e: self._redraw())

        self.title = title
        self.subtitle = subtitle
        self.kind = kind
        self.tint = tint
        self.ink = ink
        self.result = {}
        self._note_font = (FONT, 9)
        self._measure = tk.font.Font(font=self._note_font)

    def update(self, result):
        self.result = result or {}
        self._redraw()

    def _ellipsize(self, text, width):
        if self._measure.measure(text) <= width:
            return text
        while text and self._measure.measure(text + "…") > width:
            text = text[:-1]
        return text + "…"

    def _redraw(self):
        c = self.canvas
        w = c.winfo_width()
        if w <= 1:
            return
        c.delete("all")

        H = self.H
        pct = self.result.get("percent")
        note = self.result.get("note") or self.subtitle
        color = _level_color(pct)

        # 卡片：底下垫一层淡淡的投影
        _round_rect(c, 2, 4, w - 2, H + 1, 15, fill="#e3e7ee", outline="")
        _round_rect(c, 1, 1, w - 3, H - 2, 15, fill=CARD_BG, outline=CARD_BORDER)

        # 左侧图标
        cx, cy = 44, 46
        c.create_oval(cx - 21, cy - 21, cx + 21, cy + 21, fill=self.tint, outline="")
        _draw_icon(c, self.kind, cx, cy, self.ink)

        # 名称 / 状态
        c.create_text(76, 34, text=self.title, anchor="w", fill=TEXT,
                      font=(FONT, 12, "bold"))
        c.create_text(76, 58, text=self._ellipsize(note, w - 76 - 96),
                      anchor="w", fill=MUTED, font=self._note_font)

        # 右侧环形电量
        rx, ry, r = w - 52, 46, 24
        c.create_oval(rx - r, ry - r, rx + r, ry + r, outline="#edf0f4", width=5)
        if pct:
            if pct >= 100:  # Tk 的 360° 圆弧画不出来，满格单独处理
                c.create_oval(rx - r, ry - r, rx + r, ry + r, outline=color, width=5)
            else:
                c.create_arc(rx - r, ry - r, rx + r, ry + r, start=90,
                             extent=-3.6 * pct, style="arc", outline=color, width=5)
        text = f"{pct}%" if pct is not None else "—"
        c.create_text(rx, ry - 1, text=text, fill=color,
                      font=("Segoe UI", 9, "bold"))


def _window_icon():
    """画一个 32x32 的电池图标当窗口图标。"""
    import tkinter as tk

    img = tk.PhotoImage(width=32, height=32)
    img.put(ACCENT, to=(0, 0, 32, 32))
    img.put("#ffffff", to=(7, 9, 24, 23))       # 外壳
    img.put(ACCENT, to=(9, 11, 22, 21))
    img.put("#ffffff", to=(24, 14, 27, 18))     # 正极触点
    img.put("#ffffff", to=(9, 14, 19, 21))      # 60% 电量
    return img


def run_gui():
    import tkinter as tk
    from tkinter import ttk

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    root.title(APP_NAME)
    root.configure(bg=WINDOW_BG)
    root._icon = _window_icon()
    root.iconphoto(True, root._icon)
    root.geometry("460x470")
    root.minsize(420, 440)

    style = ttk.Style()
    style.theme_use("clam")
    style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                    borderwidth=0, focuscolor=ACCENT, padding=(14, 6),
                    font=(FONT, 9))
    style.map("Accent.TButton",
              background=[("pressed", "#1e40af"), ("active", "#1d4ed8")])

    header = tk.Frame(root, bg=WINDOW_BG)
    header.pack(fill="x", padx=20, pady=(16, 6))
    tk.Label(header, text=APP_NAME, bg=WINDOW_BG, fg=TEXT,
             font=(FONT, 19, "bold")).pack(side="left")
    tk.Label(header, text=f"{REFRESH_SECONDS}s 自动刷新", bg="#e8eefb", fg="#3b5bdb",
             font=(FONT, 8), padx=9, pady=3).pack(side="right", pady=(10, 0))

    cards = {
        "mouse": BatteryCard(root, "MCHOSE A7 Pro", "鼠标 · 2.4G",
                             "mouse", "#eef2ff", "#4f6bed"),
        "keyboard": BatteryCard(root, "MCHOSE G98", "键盘 · 2.4G",
                                "keyboard", "#eafaf1", "#10b981"),
        "controller": BatteryCard(root, "Xbox 无线控制器", "手柄 · 蓝牙",
                                  "pad", "#fff4e8", "#f59e0b"),
    }

    footer = tk.Frame(root, bg=WINDOW_BG)
    footer.pack(fill="x", side="bottom", padx=20, pady=(0, 12))

    tk.Frame(root, bg="#e5e9f0", height=1).pack(fill="x", side="bottom",
                                                padx=20, pady=(4, 0))

    status_label = tk.Label(footer, text="正在读取…", bg=WINDOW_BG, fg=MUTED,
                            font=(FONT, 9))
    status_label.pack(side="left")

    topmost = tk.BooleanVar(value=True)

    def toggle_top():
        root.attributes("-topmost", topmost.get())

    tk.Checkbutton(footer, text="置顶", variable=topmost, command=toggle_top,
                   bg=WINDOW_BG, fg=MUTED, activebackground=WINDOW_BG,
                   activeforeground=TEXT, selectcolor=WINDOW_BG,
                   font=(FONT, 9), bd=0, highlightthickness=0).pack(side="right",
                                                                    padx=(10, 0))

    def refresh_now():
        status_label.configure(text="正在读取…")
        thread = threading.Thread(target=lambda: q.put(read_all()), daemon=True)
        thread.start()

    ttk.Button(footer, text="立即刷新", command=refresh_now,
               style="Accent.TButton").pack(side="right")

    q = queue.Queue()
    stop_event = threading.Event()

    def apply(payload):
        if "_error" in payload:
            status_label.configure(text=f"读取出错：{payload['_error']}")
            return
        for key, card in cards.items():
            card.update(payload.get(key, {}))
        status_label.configure(text="上次更新 " + time.strftime("%H:%M:%S"))

    def poll():
        try:
            while True:
                apply(q.get_nowait())
        except queue.Empty:
            pass
        root.after(200, poll)

    def on_close():
        stop_event.set()
        _keyboard.close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.attributes("-topmost", True)
    root.after(100, poll)

    threading.Thread(target=_worker, args=(q, stop_event), daemon=True).start()
    root.mainloop()


def run_cli():
    result = read_all()
    labels = {"mouse": "鼠标  MCHOSE A7 Pro", "keyboard": "键盘  MCHOSE G98",
              "controller": "手柄  Xbox 无线控制器"}
    for key, label in labels.items():
        item = result[key]
        pct = f"{item['percent']}%" if item.get("percent") is not None else "--"
        print(f"{label:26s} {pct:>4s}   {item.get('note', '')}")
    _keyboard.close()


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_cli()
    else:
        run_gui()
