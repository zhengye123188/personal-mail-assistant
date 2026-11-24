"""Read login-keychain items silently and preserve their ACL when updating them."""

import ctypes as C
from contextlib import ExitStack, contextmanager
from ctypes.util import find_library

from keyring.backends.macOS import Keyring
from keyring.errors import KeyringError, PasswordSetError


_cf = C.CDLL(find_library("CoreFoundation"))
_security = C.CDLL(find_library("Security"))
_ref = C.c_void_p


def _function(library, name, result, *arguments):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = arguments
    return function


_string = _function(_cf, "CFStringCreateWithCString", _ref, _ref, C.c_char_p, C.c_uint32)
_data = _function(_cf, "CFDataCreate", _ref, _ref, C.c_char_p, C.c_long)
_dictionary = _function(_cf, "CFDictionaryCreate", _ref, _ref, C.POINTER(_ref), C.POINTER(_ref),
                        C.c_long, _ref, _ref)
_release = _function(_cf, "CFRelease", None, _ref)
_update = _function(_security, "SecItemUpdate", C.c_int32, _ref, _ref)
_add = _function(_security, "SecItemAdd", C.c_int32, _ref, _ref)
_get_interaction = _function(_security, "SecKeychainGetUserInteractionAllowed", C.c_int32,
                             C.POINTER(C.c_ubyte))
_set_interaction = _function(_security, "SecKeychainSetUserInteractionAllowed", C.c_int32, C.c_ubyte)
_find_password = _function(_security, "SecKeychainFindGenericPassword", C.c_int32, _ref,
                           C.c_uint32, C.c_char_p, C.c_uint32, C.c_char_p,
                           C.POINTER(C.c_uint32), C.POINTER(_ref), _ref)
_free_content = _function(_security, "SecKeychainItemFreeContent", C.c_int32, _ref, _ref)
_key_callbacks = C.addressof(C.c_byte.in_dll(_cf, "kCFTypeDictionaryKeyCallBacks"))
_value_callbacks = C.addressof(C.c_byte.in_dll(_cf, "kCFTypeDictionaryValueCallBacks"))
_utf8 = 0x08000100  # kCFStringEncodingUTF8
_not_found = -25300  # errSecItemNotFound
_duplicate = -25299  # errSecDuplicateItem


def _constant(name):
    return _ref.in_dll(_security, name).value


def _attributes(fields):
    array = _ref * len(fields)
    keys = array(*(_constant(name) for name in fields))
    values = array(*fields.values())
    return _dictionary(None, keys, values, len(fields), _key_callbacks, _value_callbacks)


@contextmanager
def _without_interaction():
    # This legacy file-keychain flag belongs to the process. KeyringVault holds
    # its shared lock across this scope and across every interactive operation.
    previous = C.c_ubyte()
    if _get_interaction(C.byref(previous)) or (previous.value and _set_interaction(False)):
        raise KeyringError("无法静默访问登录钥匙串。")
    try:
        yield
    finally:
        if previous.value and _set_interaction(True):
            raise KeyringError("无法恢复钥匙串交互设置。")


class MacKeyring(Keyring):
    def get_password_without_ui(self, service, username):
        # Use the file-keychain API whose UI is controlled by the switch above.
        service_bytes = service.encode("utf-8")
        username_bytes = username.encode("utf-8")
        length, data = C.c_uint32(), _ref()
        with _without_interaction():
            status = _find_password(None, len(service_bytes), service_bytes, len(username_bytes),
                                    username_bytes, C.byref(length), C.byref(data), None)
            if status == _not_found:
                return None
            if status:
                raise KeyringError(f"钥匙串读取失败（OSStatus {status}）。") from OSError(status, "SecKeychain")
            try:
                return C.string_at(data, length.value).decode("utf-8")
            finally:
                if data.value:
                    _free_content(None, data)

    def delete_password_without_ui(self, service, username):
        # Only used for an uncommitted copy that this process just created.
        with _without_interaction():
            self.delete_password(service, username)

    def set_password(self, service, username, password):
        # keyring 25 deletes then re-adds an item. A denied write could lose both
        # its old password and the user's existing Always Allow decision.
        with ExitStack() as cleanup:
            def own(reference):
                if not reference:
                    raise MemoryError("无法创建钥匙串写入数据。")
                cleanup.callback(_release, reference)
                return reference

            identity = {
                "kSecClass": _constant("kSecClassGenericPassword"),
                "kSecAttrService": own(_string(None, service.encode("utf-8"), _utf8)),
                "kSecAttrAccount": own(_string(None, username.encode("utf-8"), _utf8)),
            }
            encoded = password.encode("utf-8")
            change = {"kSecValueData": own(_data(None, encoded, len(encoded)))}
            query = own(_attributes(identity))
            updated = own(_attributes(change))
            status = _update(query, updated)
            if status == _not_found:
                status = _add(own(_attributes(identity | change)), None)
                if status == _duplicate:  # Another writer inserted the item first.
                    status = _update(query, updated)
            if status:
                raise PasswordSetError(f"钥匙串写入失败（OSStatus {status}）。") from OSError(status, "SecItem")
