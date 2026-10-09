"""Open a recording whether or not it is compressed.

Recordings are plain text and compress to about a third of their size, so a
student who wants to keep several of them can gzip them and the tools will
still read them.
"""
import gzip
import io


def opentext(path):
    if str(path).endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), errors="replace")
    return open(path, errors="replace")
