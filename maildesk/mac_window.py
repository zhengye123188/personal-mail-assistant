"""Keep the native window title for the Window menu while drawing our inbox header."""

import ctypes


def hide_title_text(window):
    # Qt's Cocoa winId is an NSView*. AppKit controls the title's visibility
    # separately from its value and the standard close/minimize/zoom buttons.
    runtime = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
    runtime.sel_registerName.argtypes = [ctypes.c_char_p]
    runtime.sel_registerName.restype = ctypes.c_void_p
    get_window = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
        ("objc_msgSend", runtime))
    set_visibility = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)(
        ("objc_msgSend", runtime))
    native_window = get_window(int(window.winId()), runtime.sel_registerName(b"window"))
    if native_window:
        # NSWindowTitleHidden
        set_visibility(native_window, runtime.sel_registerName(b"setTitleVisibility:"), 1)
