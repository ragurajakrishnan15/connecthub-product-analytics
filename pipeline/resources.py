"""Peak memory of the current process (and finished children on POSIX), in MB."""
import sys


def peak_rss_mb():
    if sys.platform == 'win32':
        import ctypes
        import ctypes.wintypes as wt

        class Counters(ctypes.Structure):
            _fields_ = [('cb', wt.DWORD), ('PageFaultCount', wt.DWORD),
                        ('PeakWorkingSetSize', ctypes.c_size_t),
                        ('WorkingSetSize', ctypes.c_size_t),
                        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                        ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]

        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = wt.HANDLE
        k32.K32GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(Counters), wt.DWORD]
        c = Counters()
        c.cb = ctypes.sizeof(Counters)
        if not k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
            return None
        return round(c.PeakWorkingSetSize / 2**20, 1)
    import resource
    own = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    children = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    scale = 2**20 if sys.platform == 'darwin' else 2**10  # bytes on macOS, KB on Linux
    return round(max(own, children) / scale, 1)
