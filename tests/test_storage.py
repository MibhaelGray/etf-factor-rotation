"""Stored files are swapped in whole: a reader never finds one half written. A temporary data folder.

    uv run python -m unittest discover -s tests
"""

import os
import tempfile
import unittest
from unittest import mock

import pandas as pd

from factor_rotation import storage

PATH = "bars_daily/symbol=AAA.parquet"
OLD, NEW = pd.DataFrame({"close": [1.0, 2.0]}), pd.DataFrame({"close": [1.0, 2.0, 3.0]})


class Write(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        p = mock.patch.dict(os.environ, {"FR_DATA_ROOT": tmp.name})
        p.start()
        self.addCleanup(p.stop)

    def files(self):
        return sorted(os.listdir(os.path.join(self.root, "bars_daily")))

    def test_a_file_is_written_and_read_back_and_nothing_else_is_left_behind(self):
        storage.write(OLD, PATH)
        storage.write(NEW, PATH)
        self.assertEqual(storage.read(PATH)["close"].tolist(), [1.0, 2.0, 3.0])
        self.assertEqual((self.files(), storage.list_files("bars_daily")), (["symbol=AAA.parquet"], [PATH]))

    def test_a_reader_in_the_middle_of_a_write_gets_the_old_file_whole(self):
        storage.write(OLD, PATH)
        real, seen = storage.pq.write_table, []

        def slow(table, out, **kw):
            seen.append(storage.read(PATH)["close"].tolist())       # another job reads while the new file is unwritten
            seen.append(storage.list_files("bars_daily"))           # and lists the folder
            real(table, out, **kw)
            seen.append(storage.read(PATH)["close"].tolist())       # written but not yet swapped in

        with mock.patch.object(storage.pq, "write_table", slow):
            storage.write(NEW, PATH)
        self.assertEqual(seen, [[1.0, 2.0], [PATH], [1.0, 2.0]])
        self.assertEqual(storage.read(PATH)["close"].tolist(), [1.0, 2.0, 3.0])

    def test_a_write_that_fails_leaves_the_old_file_and_no_scraps(self):
        storage.write(OLD, PATH)
        with mock.patch.object(storage.pq, "write_table", side_effect=OSError("disk full")), self.assertRaises(OSError):
            storage.write(NEW, PATH)
        self.assertEqual((storage.read(PATH)["close"].tolist(), self.files()), ([1.0, 2.0], ["symbol=AAA.parquet"]))


if __name__ == "__main__":
    unittest.main()
