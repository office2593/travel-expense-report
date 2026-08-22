"""
Optional third-party PDF compression via the iLovePDF / iLoveAPI REST API
(https://developer.ilovepdf.com) -- this is the specific tool named in the
request (ilovepdf.com/compress_pdf), wired up as a real integration point.

PRIVACY TRADE-OFF, read before enabling this instead of compress_pdf.py:
Every report sent through here is uploaded to iLoveAPI's servers to be
processed. These are client financial documents, and the receipts appendix
carries personal documents (flight tickets, invoices). compress_pdf.py
achieves most of the practical size reduction self-hosted, with nothing
leaving the firm's infrastructure -- that's the recommended default. Use
this module only if that trade-off has been consciously accepted (e.g.
iLoveAPI's compression quality/speed is worth it for a given volume), and
check their data processing / retention terms first.

Setup:
    1. Register at https://developer.ilovepdf.com and create a project to
       get a Project ID (public key) and Secret Key.
    2. pip install pylovepdf
    3. Set ILOVEPDF_PUBLIC_KEY / ILOVEPDF_SECRET_KEY as environment
       variables (never hardcode them).
"""

from __future__ import annotations

import os
from pathlib import Path


def compress_via_ilovepdf(
    input_path: str | Path,
    output_dir: str | Path,
    compression_level: str = "recommended",  # "low" | "recommended" | "extreme"
) -> Path:
    """Uploads `input_path` to iLoveAPI, compresses it, and downloads the
    result into `output_dir`. Requires the pylovepdf package and API keys
    set as environment variables (see module docstring)."""
    try:
        from pylovepdf.ilovepdf import Task
    except ImportError as e:
        raise ImportError(
            "pylovepdf is not installed. Run: pip install pylovepdf"
        ) from e

    public_key = os.environ.get("ILOVEPDF_PUBLIC_KEY")
    secret_key = os.environ.get("ILOVEPDF_SECRET_KEY")
    if not public_key or not secret_key:
        raise RuntimeError(
            "Set ILOVEPDF_PUBLIC_KEY and ILOVEPDF_SECRET_KEY environment "
            "variables before calling compress_via_ilovepdf()."
        )

    task = Task(public_key, secret_key, verify_ssl=True, proxies=None)
    task.new_task("compress")
    task.add_file(str(input_path))
    task.set_output_folder(str(output_dir))
    task.execute()
    task.download()
    task.delete_current_task()  # remove the file from iLoveAPI's servers once downloaded

    produced = list(Path(output_dir).glob("*.pdf"))
    if not produced:
        raise RuntimeError("iLoveAPI compression finished but no output file was found.")
    return produced[0]


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python ilovepdf_compress.py input.pdf output_dir")
        sys.exit(1)
    result = compress_via_ilovepdf(sys.argv[1], sys.argv[2])
    print("Compressed file:", result)
