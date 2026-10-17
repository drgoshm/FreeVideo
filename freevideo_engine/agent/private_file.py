"""Create a file that only the current user can open."""
import os

WINDOWS = os.name == 'nt'


def create(path):
    """Exclusively create `path` for writing and return its descriptor.

    Raises FileExistsError when the file is already there. On Windows the file is created with its
    permissions already in place, so there is no moment at which another account could open it.
    """
    if WINDOWS:
        try:
            return _create_windows(path)
        except FileExistsError:
            raise
        except Exception:
            # Windows mode bits cannot replace a protected DACL. Fail closed.
            raise PermissionError('Could not create a private installation token') from None
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)


def _create_windows(path):
    import ctypes
    import msvcrt
    from ctypes import wintypes as w

    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)

    class Attributes(ctypes.Structure):
        _fields_ = [('length', w.DWORD), ('descriptor', ctypes.c_void_p), ('inherit', w.BOOL)]

    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi.OpenProcessToken.restype = w.BOOL
    advapi.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
    advapi.GetTokenInformation.restype = w.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertSidToStringSidW.restype = w.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(Attributes), w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p

    def failed():
        return ctypes.WinError(ctypes.get_last_error())

    # The account this process runs as, as a SID string.
    token = w.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
        raise failed()
    try:
        size = w.DWORD(0)
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser: ask for the size
        if not size.value:
            raise failed()
        user = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, user, size.value, ctypes.byref(size)):
            raise failed()
    finally:
        kernel.CloseHandle(token)
    text = ctypes.c_void_p()
    # TOKEN_USER starts with the pointer to the SID.
    if not advapi.ConvertSidToStringSidW(ctypes.cast(user, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.byref(text)):
        raise failed()
    try:
        sid = ctypes.wstring_at(text.value)
    finally:
        kernel.LocalFree(text)

    # Protected (nothing inherited from the folder): this user, the system and administrators only.
    descriptor = ctypes.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            'D:P(A;;FA;;;%s)(A;;FA;;;SY)(A;;FA;;;BA)' % sid, 1, ctypes.byref(descriptor), None):
        raise failed()
    try:
        attributes = Attributes(ctypes.sizeof(Attributes), descriptor.value, False)
        # GENERIC_WRITE, shared like os.open so a concurrent reader waits for the full line, CREATE_NEW, FILE_ATTRIBUTE_NORMAL
        handle = kernel.CreateFileW(os.fspath(path), 0x40000000, 0x3, ctypes.byref(attributes), 1, 0x80, None)
        error = ctypes.get_last_error()
    finally:
        kernel.LocalFree(descriptor)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        if error in (80, 183):  # ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS
            raise FileExistsError(os.fspath(path))
        raise ctypes.WinError(error)
    try:
        return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
    except Exception:
        kernel.CloseHandle(handle)
        os.unlink(path)
        raise
