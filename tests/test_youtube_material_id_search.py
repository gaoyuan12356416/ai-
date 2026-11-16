"""A numeric ID lookup must reach its primary key instead of scanning names."""
import sqlite3
import unittest

from features.youtube_auto_publish.source import COLUMNS, MaterialSource


class MaterialIdSearchTests(unittest.TestCase):
    def test_numeric_search_selects_exact_material_using_primary_key(self):
        with sqlite3.connect(':memory:') as connection:
            columns = ','.join('`%s` %s' % (key, 'INTEGER PRIMARY KEY' if key == 'id' else 'TEXT')
                               for key in COLUMNS)
            connection.execute('CREATE TABLE videos (' + columns + ')')
            for mid, name in ((8365481, 'Target'), (83654810, 'Longer ID'), (9, 'Contains 8365481')):
                row = dict.fromkeys(COLUMNS, '')
                row.update(id=mid, name=name, url='https://ai.yingliangads.com/video.mp4')
                connection.execute('INSERT INTO videos VALUES (' + ','.join('?' for _ in COLUMNS) + ')',
                                   [row[key] for key in COLUMNS])
            plans = []

            def query(sql):
                plans.extend(connection.execute('EXPLAIN QUERY PLAN ' + sql).fetchall())
                return connection.execute(sql).fetchall()

            rows = MaterialSource('', query)._read('SELECT * FROM videos', search='8365481')
            self.assertEqual([row['id'] for row in rows], ['8365481'])
            self.assertTrue(any('INTEGER PRIMARY KEY' in str(row) for row in plans), plans)


if __name__ == '__main__':
    unittest.main()
