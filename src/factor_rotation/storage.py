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
    filesystem, target = _resolve(path)
    filesystem.create_dir(posixpath.dirname(target), recursive=True)
    with filesystem.open_output_stream(target) as out:
        pq.write_table(pa.Table.from_pandas(df, preserve_index=False), out, compression="zstd")
    return target


def read(path: str) -> pd.DataFrame:
    filesystem, target = _resolve(path)
    with filesystem.open_input_file(target) as f:
        return pq.read_table(f).to_pandas()


def exists(path: str) -> bool:
    filesystem, target = _resolve(path)
    return filesystem.get_file_info(target).type != fs.FileType.NotFound


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
