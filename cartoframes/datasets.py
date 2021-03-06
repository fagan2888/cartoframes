import binascii as ba
from warnings import warn
import pandas as pd
import time
from tqdm import tqdm

from .columns import Column, normalize_names, normalize_name

from carto.exceptions import CartoException, CartoRateLimitException

# avoid _lock issue: https://github.com/tqdm/tqdm/issues/457
tqdm(disable=True, total=0)  # initialise internal lock


class Dataset(object):
    FAIL = 'fail'
    REPLACE = 'replace'
    APPEND = 'append'

    PRIVATE = 'private'
    PUBLIC = 'public'
    LINK = 'link'

    DEFAULT_RETRY_TIMES = 3

    def __init__(self, carto_context, table_name, schema='public', df=None):
        self.cc = carto_context
        self.table_name = normalize_name(table_name)
        self.schema = schema
        self.df = df
        self.normalized_column_names = None
        if self.df is not None:
            _save_index_as_column(self.df)
            self.normalized_column_names = _normalize_column_names(self.df)
        if self.table_name != table_name:
            warn('Table will be named `{}`'.format(table_name))

    @staticmethod
    def create_from_query(cart_context, query, table_name):
        dataset = Dataset(cart_context, table_name)
        dataset.cc.batch_sql_client \
               .create_and_wait_for_completion(
                   '''BEGIN; {drop}; {create}; {cartodbfy}; COMMIT;'''
                   .format(drop=dataset._drop_table_query(),
                           create=dataset._create_table_from_query(query),
                           cartodbfy=dataset._cartodbfy_query()))

        return dataset

    def upload(self, with_lonlat=None, if_exists='fail'):
        if self.df is None:
            raise ValueError('You have to create a `Dataset` with a pandas DataFrame in order to upload it to CARTO')

        if not self.exists():
            self._create_table(with_lonlat)
        else:
            if if_exists == Dataset.FAIL:
                raise NameError(('Table with name {table_name} already exists in CARTO.'
                                 ' Please choose a different `table_name` or use'
                                 ' if_exists="replace" to overwrite it').format(table_name=self.table_name))
            elif if_exists == Dataset.REPLACE:
                self._create_table(with_lonlat)

        self._copyfrom(with_lonlat)

        return self

    def download(self, limit=None, decode_geom=False, retry_times=DEFAULT_RETRY_TIMES):
        table_columns = self.get_table_columns()
        query = self._get_read_query(table_columns, limit)

        return self.cc.fetch(query, decode_geom=decode_geom)

    def delete(self):
        if self.exists():
            self.cc.sql_client.send(self._drop_table_query(False))
            return True

        return False

    def exists(self):
        """Checks to see if table exists"""
        try:
            self.cc.sql_client.send(
                'EXPLAIN SELECT * FROM "{table_name}"'.format(
                    table_name=self.table_name),
                do_post=False)
            return True
        except CartoException as err:
            # If table doesn't exist, we get an error from the SQL API
            self.cc._debug_print(err=err)
            return False

    def _create_table(self, with_lonlat=None):
        job = self.cc.batch_sql_client \
                  .create_and_wait_for_completion(
                      '''BEGIN; {drop}; {create}; {cartodbfy}; COMMIT;'''
                      .format(drop=self._drop_table_query(),
                              create=self._create_table_query(with_lonlat),
                              cartodbfy=self._cartodbfy_query()))

        if job['status'] != 'done':
            raise CartoException('Cannot create table: {}.'.format(job['failed_reason']))

    def _cartodbfy_query(self):
        return "SELECT CDB_CartodbfyTable('{org}', '{table_name}')" \
            .format(org=(self.cc.creds.username() if self.cc.is_org else 'public'),
                    table_name=self.table_name)

    def _copyfrom(self, with_lonlat=None):
        geom_col = _get_geom_col_name(self.df)

        columns = ','.join(norm for norm, orig in self.normalized_column_names)
        self.cc.copy_client.copyfrom(
            """COPY {table_name}({columns},the_geom)
               FROM stdin WITH (FORMAT csv, DELIMITER '|');""".format(table_name=self.table_name, columns=columns),
            self._rows(self.df, [c for c in self.df.columns if c != 'cartodb_id'], with_lonlat, geom_col)
        )

    def _rows(self, df, cols, with_lonlat, geom_col):
        for i, row in df.iterrows():
            csv_row = ''
            the_geom_val = None
            lng_val = None
            lat_val = None
            for col in cols:
                if with_lonlat and col in Column.SUPPORTED_GEOM_COL_NAMES:
                    continue
                val = row[col]
                if pd.isnull(val) or val is None:
                    val = ''
                if with_lonlat:
                    if col == with_lonlat[0]:
                        lng_val = row[col]
                    if col == with_lonlat[1]:
                        lat_val = row[col]
                if col == geom_col:
                    the_geom_val = row[col]
                else:
                    csv_row += '{val}|'.format(val=val)

            if the_geom_val is not None:
                geom = _decode_geom(the_geom_val)
                if geom:
                    csv_row += 'SRID=4326;{geom}'.format(geom=geom.wkt)
            if with_lonlat is not None and lng_val is not None and lat_val is not None:
                csv_row += 'SRID=4326;POINT({lng} {lat})'.format(lng=lng_val, lat=lat_val)

            csv_row += '\n'
            yield csv_row.encode()

    def _drop_table_query(self, if_exists=True):
        return '''DROP TABLE {if_exists} {table_name}'''.format(
            table_name=self.table_name,
            if_exists='IF EXISTS' if if_exists else '')

    def _create_table_from_query(self, query):
        create_query = '''CREATE TABLE {table_name} AS ({query})'''.format(table_name=self.table_name, query=query)
        return create_query

    def _create_table_query(self, with_lonlat=None):
        if with_lonlat is None:
            geom_type = _get_geom_col_type(self.df)
        else:
            geom_type = 'Point'

        col = ('{col} {ctype}')
        cols = ', '.join(col.format(col=norm,
                                    ctype=_dtypes2pg(self.df.dtypes[orig]))
                         for norm, orig in self.normalized_column_names)

        if geom_type:
            cols += ', {geom_colname} geometry({geom_type}, 4326)'.format(geom_colname='the_geom', geom_type=geom_type)

        create_query = '''CREATE TABLE {table_name} ({cols})'''.format(table_name=self.table_name, cols=cols)
        return create_query

    def _get_read_query(self, table_columns, limit=None):
        """Create the read (COPY TO) query"""
        query_columns = [column.name for column in table_columns if column.name != 'the_geom_webmercator']

        query = 'SELECT {columns} FROM "{schema}"."{table_name}"'.format(
            table_name=self.table_name,
            schema=self.schema,
            columns=', '.join(query_columns))

        if limit is not None:
            if isinstance(limit, int) and (limit >= 0):
                query += ' LIMIT {limit}'.format(limit=limit)
            else:
                raise ValueError("`limit` parameter must an integer >= 0")

        return query

    def get_table_columns(self):
        """Get column names and types from a table"""
        query = '''
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = '{table}' AND table_schema = '{schema}'
        '''.format(table=self.table_name, schema=self.schema)

        try:
            table_info = self.cc.sql_client.send(query)
            return [Column(c['column_name'], pgtype=c['data_type']) for c in table_info['rows']]
        except CartoException as e:
            # this may happen when using the default_public API key
            if str(e) == 'Access denied':
                query = '''
                    SELECT *
                    FROM "{schema}"."{table}" LIMIT 0
                '''.format(table=self.table_name, schema=self.schema)
                return get_columns(self.cc, query)

    def get_table_column_names(self, exclude=None):
        """Get column names and types from a table"""
        columns = [c.name for c in self.get_table_columns()]

        if exclude and isinstance(exclude, list):
            columns = list(set(columns) - set(exclude))

        return columns


def recursive_read(context, query, retry_times=Dataset.DEFAULT_RETRY_TIMES):
    try:
        return context.copy_client.copyto_stream(query)
    except CartoRateLimitException as err:
        if retry_times > 0:
            retry_times -= 1
            warn('Read call rate limited. Waiting {s} seconds'.format(s=err.retry_after))
            time.sleep(err.retry_after)
            warn('Retrying...')
            return recursive_read(context, query, retry_times=retry_times)
        else:
            warn(('Read call was rate-limited. '
                  'This usually happens when there are multiple queries being read at the same time.'))
            raise err


def get_columns(context, query):
    col_query = '''SELECT * FROM ({query}) _q LIMIT 0'''.format(query=query)
    table_info = context.sql_client.send(col_query)
    return Column.from_sql_api_fields(table_info['fields'])


def _save_index_as_column(df):
    index_name = df.index.name
    if index_name is not None:
        if index_name not in df.columns:
            df.reset_index(inplace=True)
            df.set_index(index_name, drop=False, inplace=True)


def _normalize_column_names(df):
    column_names = [c for c in df.columns if c not in Column.RESERVED_COLUMN_NAMES]
    normalized_columns = normalize_names(column_names)

    column_tuples = [(norm, orig) for orig, norm in zip(column_names, normalized_columns)]

    changed_cols = '\n'.join([
        '\033[1m{orig}\033[0m -> \033[1m{new}\033[0m'.format(
            orig=orig,
            new=norm)
        for norm, orig in column_tuples if norm != orig])

    if changed_cols != '':
        tqdm.write('The following columns were changed in the CARTO '
                   'copy of this dataframe:\n{0}'.format(changed_cols))

    return column_tuples


def _dtypes2pg(dtype):
    """Returns equivalent PostgreSQL type for input `dtype`"""
    mapping = {
        'float64': 'numeric',
        'int64': 'integer',
        'float32': 'numeric',
        'int32': 'integer',
        'object': 'text',
        'bool': 'boolean',
        'datetime64[ns]': 'timestamp',
        'datetime64[ns, UTC]': 'timestamp',
    }
    return mapping.get(str(dtype), 'text')


def _get_geom_col_name(df):
    geom_col = getattr(df, '_geometry_column_name', None)
    if geom_col is None:
        try:
            geom_col = next(x for x in df.columns if x.lower() in Column.SUPPORTED_GEOM_COL_NAMES)
        except StopIteration:
            pass

    return geom_col


def _get_geom_col_type(df):
    geom_col = _get_geom_col_name(df)
    if geom_col is None:
        return None

    try:
        geom = _decode_geom(_first_not_null_value(df, geom_col))
    except IndexError:
        warn('Dataset with null geometries')
        geom = None

    if geom is None:
        return None

    return geom.geom_type


def _first_not_null_value(df, col):
    return df[col].loc[~df[col].isnull()].iloc[0]


def _encode_decode_decorator(func):
    """decorator for encoding and decoding geoms"""
    def wrapper(*args):
        """error catching"""
        try:
            processed_geom = func(*args)
            return processed_geom
        except ImportError as err:
            raise ImportError('The Python package `shapely` needs to be '
                              'installed to encode or decode geometries. '
                              '({})'.format(err))
    return wrapper


@_encode_decode_decorator
def _decode_geom(ewkb):
    """Decode encoded wkb into a shapely geometry
    """
    # it's already a shapely object
    if hasattr(ewkb, 'geom_type'):
        return ewkb

    from shapely import wkb
    from shapely import wkt
    if ewkb:
        try:
            return wkb.loads(ba.unhexlify(ewkb))
        except Exception:
            try:
                return wkb.loads(ba.unhexlify(ewkb), hex=True)
            except Exception:
                try:
                    return wkb.loads(ewkb, hex=True)
                except Exception:
                    try:
                        return wkb.loads(ewkb)
                    except Exception:
                        try:
                            return wkt.loads(ewkb)
                        except Exception:
                            pass
    return None
