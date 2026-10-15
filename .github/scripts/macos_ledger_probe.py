"""Throwaway: why does the ledger refuse the compiled package on macOS?"""
import os
import sys
import traceback

from neptune_ledger.catalog import registry as R
from neptune_ledger.catalog.check import check_package

root = sys.argv[1]
fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
checked = check_package(fd, "register")
print("findings:", [(f.code, f.subject, f.detail) for f in checked.findings][:10])
if checked.package is not None:
    for name in ("series_intervals",):
        try:
            getattr(R, name)(fd, checked.package)
            print(name, "ok")
        except Exception:
            print(name, "FAILED"); traceback.print_exc()
try:
    R.package_rows(str(checked.package_id), checked.manifest, checked.lines); print("package_rows ok")
except Exception:
    print("package_rows FAILED"); traceback.print_exc()
try:
    R.thread_rows(checked.lines); print("thread_rows ok")
except Exception as exc:
    print("thread_rows FAILED", getattr(exc, "record_id", ""), getattr(exc, "detail", "")); traceback.print_exc()
