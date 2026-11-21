import csv
import io

from app.config import MAX_SOURCE_BYTES, MAX_SOURCE_ROWS, SOURCE_COLUMNS
from app.errors import Problem
from app.records import SourceRow


def decode(data, name):
    if len(data) > MAX_SOURCE_BYTES:
        raise Problem(413, 'FILE_LIMIT', {'source': name})
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise Problem(422, 'FILE_ENCODING', {'source': name})


def read_source(data, name, stats, isolates):
    text, encoding = decode(data, name)
    if not text:
        raise Problem(422, 'FILE_EMPTY', {'source': name})
    stats.update(encoding=encoding, status='HEADER_ONLY', rows=0, blank=0, headers=0)
    columns = SOURCE_COLUMNS[name]
    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    header = None
    try:
        while True:
            line = reader.line_num + 1
            fields = next(reader, None)
            if fields is None:
                break
            stats['rows'] += 1
            if stats['rows'] > MAX_SOURCE_ROWS:
                raise Problem(413, 'FILE_LIMIT', {'source': name, 'line': line})
            if not any(field.strip() for field in fields):
                stats['blank'] += 1
                continue
            if header is None:
                header = tuple(field.strip().lstrip('\ufeff') for field in fields)
                missing = set(columns) - set(header)
                # \u6e20\u9053\u6574\u5355\u9000\u6b3e\u53ea\u56de\u4f20\u7968\u7801\u548c\u9000\u6b3e\u989d\uff0crefunds.csv \u5141\u8bb8\u7701\u7565 show \u5217\u3002
                optional = {'show'} if name == 'refunds.csv' else set()
                if len(header) != len(set(header)) or set(header) - set(columns) or missing - optional:
                    raise Problem(422, 'FILE_COLUMNS', {'source': name, 'line': line, 'expected': columns})
                stats['headers'] += 1
                continue
            if tuple(field.strip().lstrip('\ufeff') for field in fields) == header:
                stats['headers'] += 1
                continue
            stats['status'] = 'DATA'
            row = SourceRow(name, line, reader.line_num, tuple(fields), dict(zip(header, fields)))
            if len(fields) != len(header):
                isolates.add(row, 'COLUMN_COUNT', expected=len(header), actual=len(fields))
                continue
            yield row
    except csv.Error as exc:
        raise Problem(422, 'FILE_CSV', {'source': name, 'line': line}) from exc
    if header is None:
        raise Problem(422, 'FILE_BLANK', {'source': name})
