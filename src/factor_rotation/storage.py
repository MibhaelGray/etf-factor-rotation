"""Parquet storage on a local directory or S3 (FR_DATA_ROOT=s3://bucket/prefix).

Layout:
  options_raw/date=YYYY-MM-DD/underlying=SYM/HHMMSS.parquet   one file per snapshot
  exposures/date=YYYY-MM-DD/HHMMSS.parquet                    per-underlying summary
  exposures_by_strike/date=YYYY-MM-DD/underlying=SYM/HHMMSS.parquet
  bars_daily/symbol=SYM.parquet                               full history, rewritten on update
  technicals/date=YYYY-MM-DD.parquet                          one row per symbol
  factor_pairs/date=YYYY-MM-DD.parquet                        one row per factor pair
"""

import os
import posixpath
import threading
from datetime import datetime

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pyarrow import fs

from .config import settings


def _resolve(path: str) -> tuple[fs.FileSystem, str]:
    full = posixpath.join(settings().data_root, path)
    if "://" in full:
        return fs.FileSystem.from_uri(full)
    return fs.LocalFileSystem(), os.path.abspath(full)


def write(df: pd.DataFrame, path: str) -> str:
    """Write the file so a reader never finds it half written. On a local disk the new file is written beside the
    old one and swapped in with one rename: a reader gets the old file or the new one, whole. (Until 2026-11-14 the
    file was opened in place, which empties it first; a job reading bars while the dashboard refreshed them failed
    with "Parquet file size is 0 bytes".) On S3 an object only appears once its upload has finished."""
    filesystem, target = _resolve(path)
    filesystem.create_dir(posixpath.dirname(target), recursive=True)
    table = pa.Table.from_pandas(df, preserve_index=False)
    if not isinstance(filesystem, fs.LocalFileSystem):
        with filesystem.open_output_stream(target) as out:
            pq.write_table(table, out, compression="zstd")
        return target
    # The name doesn't end in .parquet, so list_files never takes a file in progress for data.
    tmp = f"{target}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "wb") as out:
            pq.write_table(table, out, compression="zstd")
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return target


def read(path: str) -> pd.DataFrame:
    filesystem, target = _resolve(path)
    with filesystem.open_input_file(target) as f:
        return pq.read_table(f).to_pandas()


def exists(path: str) -> bool:
    filesystem, target = _resolve(path)
    return filesystem.get_file_info(target).type != fs.FileType.NotFound


def modified(path: str) -> datetime | None:
    filesystem, target = _resolve(path)
    info = filesystem.get_file_info(target)
    return info.mtime if info.type != fs.FileType.NotFound else None


def stamp(path: str) -> int | None:
    """The file's modified time in nanoseconds, None if it doesn't exist. Changes whenever the file is rewritten."""
    filesystem, target = _resolve(path)
    info = filesystem.get_file_info(target)
    return info.mtime_ns if info.type != fs.FileType.NotFound else None


def list_files(prefix: str) -> list[str]:
    filesystem, target = _resolve(prefix)
    _, root = _resolve("")
    selector = fs.FileSelector(target, recursive=True, allow_not_found=True)
    files = [
        i.path for i in filesystem.get_file_info(selector)
        if i.type == fs.FileType.File and i.path.endswith(".parquet")
    ]
    return sorted(posixpath.relpath(p, root) for p in files)


def read_many(prefix: str) -> pd.DataFrame:
    frames = [read(f) for f in list_files(prefix)]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
