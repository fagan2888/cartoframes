# -#- coding: utf-8 -#-
"""
Analysis in cartoframes takes two forms:

* **Pipelines**: :obj:`AnalysisTree` pipelines where multiple analyses can be
  listed sequentially off a base data source node (e.g., a
  :obj:`Table` object). This tree is lazily evaluated by applying a
  ``.compute()`` method after it is created. Besides the class
  constructor, analyses can be appended to the tree after it has been
  instantiated. See :obj:`AnalysisTree` for more information. This is modeled
  after Builder analysis workflows and scikit-learn's `PipeLine class
  <http://scikit-learn.org/stable/modules/generated/sklearn.pipeline.Pipeline.html>`__.

  Example:

      .. code::

          from cartoframes import AnalysisTree, analyses as ca
          tree = AnalysisTree(
              Table(cc, 'brooklyn_demographics'),
              [
                   ('buffer', ca.Buffer(100.0)),
                   ('join', ca.Join(
                       Table('gps_pings').filter('type=cell')
                       on='the_geom',
                       type='left',
                       null_replace=0)
                   ),
                   ('distinct', ca.Distinct(on='user_id')),
                   ('agg', ca.Agg(
                       by=['geoid', 'the_geom', ],
                       [('num_gps_pings', 'count'),
                        ('num_gps_pings', 'avg'),
                        ('median_income', 'min')]),
                   ('div', ca.Division([
                       ('num_gps_pings', 'total_pop'),
                       ('num_gps_pings', 'the_geom')
                   ]))
              ]
          )

* **Method Chaining**: By chaining analysis methods off of a base data source
  node. A base data source can be one of :obj:`Table` or :obj:`Query`, which
  represent queries against the user's CARTO account. For a full list of
  analyses, see the methods of :obj:`Query` and :obj:`Table`.


.. todo::

    * Add status updates (e.g., ``node 5 / 7 complete``) by using tqdm
    * Have a better representation of an analysis than a :obj:`tuple`? E.g.,
      scikit-learn passes class instances to the Pipeline

      .. code::

          from sklearn import svm
          from sklearn.datasets import samples_generator
          from sklearn.feature_selection import SelectKBest, f_regression
          from sklearn.pipeline import Pipeline
          # build pipeline
          anova_filter = SelectKBest(f_regression, k=5)
          clf = svm.SVC(kernel='linear')
          anova_svm = Pipeline([('anova', anova_filter), ('svc', clf)])

      Each 'analysis' in cartoframes could exist as a class and be constructed
      similarly. Most would be a clone of the camshaft node, and others would
      be more data-science-specific. Having a solid definition of each analysis
      would remove the clunkiness of having an ill-defined tuple with name and
      parameters.
    * Method chaining builds up an AnalysisTree by reapeatedly applying the
      ``.append(...)`` to ``self``
    * Chained methods are lazily evaluated as well
    * Add AnalysisTree validation steps for column names / existence of data,
      etc. for each step of the tree
    * Instantiating the Table or Query classes is clumsy if the ``cc`` needs to
      be passed to it everytime -- should it be instantiated differently? Maybe
      like ``cc.table('foo')``, which is equivalent to ``Table(cc, 'foo')``?
      One conflict here is that ``cc.query`` already exists and means something
      different.
    * ``Layer`` should have a ``Query`` attribute instead of storing the query
      as a string?
    * Idea: Partial evaluation to get states of the data along the tree? User
      could create a shorter tree to do this instead.
    * Operator overloading for operations like `Analyses + Analysis` does an
      ``AnalysisTree.append`` under the hood
    * Add method for trashing / invalidating analysis table and starting anew
    * What's the standard on column name inheritance from analysis n to n+1?
      Which columns come over, which don't, and which are added?
    * What can be gleaned from http://www.opengeospatial.org/standards/wps ?
    * Draw inspiration from Spark:
      http://spark.apache.org/docs/2.2.0/api/python/pyspark.sql.html
      And place functions into a `functions` module
      http://spark.apache.org/docs/2.2.0/api/python/pyspark.sql.html#module-pyspark.sql.functions
    * Keep in mind that the chain is actually a tree since data can come in
      at different nodes. AnalysisTree may be a better name.
"""
import pandas as pd
from cartoframes import utils


def _buffer(q_obj, dist):
    """Buffer a query or table"""
    if isinstance(q_obj, Query):
        query = q_obj.query
    else:
        query = q_obj
    return '''
        SELECT
            ST_Buffer(the_geom, {dist}) as the_geom
        FROM (
            {source_query}
        ) as _w
    '''.format(
        dist=dist,
        source_query=query
    )


class AnalysisTree(object):
    """Build up an analysis tree à la Builder Analysis or scikit learn
    Pipeline. Once evaluated with ``AnalysisTree.compute()``, the results will
    persist as a table in the user CARTO account and be returned into the
    ``data`` attribute.

    :obj:`AnalysisTree` allows you to build up a tree of analyses which are
    applied sequentially to a source (:obj:`Query` or :obj:`Table`).

    Example:

      Build and evaluate an analysis tree, return the results into a
      pandas DataFrame, and map the output

      .. code::

        from cartoframes import AnalysisTree, Table, CartoContext
        from cartoframes import analyses as ca
        cc = CartoContext()

        # base data node
        bklyn_demog = Table(cc, 'brooklyn_demographics')

        # build analysis tree
        tree = AnalysisTree(
            bklyn_demog,
            [
                # buffer by 100 meters
                ca.Buffer(100.0),
                # spatial join
                ca.Join(target=Table(cc, 'gps_pings').filter('type=cell'),
                    on='the_geom',
                    type='left'),
                ca.Distinct(on='user_id'),
                # aggregate points to polygons
                ca.Agg(by='geoid', ops=[('count', 'num_gps_pings'), ]),
                # add new column to normalize point count
                ca.Div([('num_gps_pings', 'total_pop')])
            ]
        )

        # evaluate analysis
        tree.compute()

        # visualize with carto map
        tree.map(color='num_gps_pings_per_total_pop')

    Parameters:

      source (:obj:`str`, :obj:`Table`, or :obj:`Query`): If str, the name of a table
        in user account. If :obj:`Table` or :obj:`Query`, the base data for the
        analysis tree.
      analyses (list): A list of analyses to apply to `source`. The following
        are available analyses and their parameters:

        - :obj:`Buffer`:
          - radius (float, required): radius of buffer in meters
        - :obj:`Join`:
          - target (:obj:`Table`, :obj:`Query`, or :obj:`str`): The data source
            that the `source` is joined against.
          - on (:obj:`str`): If a :obj:`str`, the column name to join on. If
            `the_geom` is provided, a spatial join will be performed. If a
            :obj:`tuple` is provided, the first element is the column from
            `source` which is matched to the second element, the column from
            `target`.
        - :obj:`Div`: Divide one column by another. If the second column is
          `the_geom`, the result will be numerator per sq km
        - :obj:`Agg`: Aggregate data according to the `agg`/`column` pairs,
          and grouping by `by`, which can be a :obj:`str` or list of
          :obj:`str`.
        - :obj:`Distinct`: Return only the distinct rows of the Table or Query.
        - Etc. many more to come


    Attributes:
      - data (pandas.DataFrame): ``None`` until the analysis tree is
        evaluated, and then a dataframe of the results
      - state (:obj:`str`): Current state of the :obj:`AnalysisTree`:

        - 'not evaluated': Chain has not yet been evaluated
        - 'running': Analysis is currently running
        - 'enqueued': Analysis is queued to be run
        - 'complete': Chain successfully run. Results stored in
          :obj:`AnalysisTree.data` and ``.results_url``.
        - 'failed': Failure message if the analysis failed to complete

      - results_url: URL where results stored on CARTO. Note: user has to
        be authenticated to view the table
      - Add method for running the analysis off of a subsample of the data.
        E.g., ``.compute(subsample=0.1)``, ``.compute_preview()``, etc. etc.
        With the goal that users feel compelled to run the analysis first on
        a smaller sample to get the results before running the whole enchilada.
    """ # noqa
    def __init__(self, source, analyses):
        self.context = source.context
        self.analyses = analyses
        self.source = source
        self.data = None
        self.final_query = None

    def _build_tree(self):
        """Builds up an analysis based on `analyses`"""
        temp = 'SELECT * FROM ({query}) as _w{n}'
        last = temp.format(query=self.source.query, n=0)
        for idx, analysis in enumerate(self.analyses):
            last = temp.format(
                query=analysis[0](last, *analysis[1]),
                n=idx+1
            )
        self.final_query = last

    def __repr__(self):
        """.. todo::

            this should pretty print the analyses and parameters
        """
        print(str(self.analyses))

    @property
    def results_url(self):
        """returns the URL where the analysis table exists on CARTO"""
        pass

    def append(self, analysis):
        """Append a new analysis to an existing tree.

        Example:

            .. code::

                tree = AnalysisTree(
                    Table('transactions'),
                    [('buffer', 10),
                     ('augment', 'median_income')]
                )
                tree.append(('knn', {'mean': 'median_income'}))

        Args:
          analysis (analysis): An analysis node
        """
        pass

    def compute(self):
        """Trigger the AnalysisTree to run.

        Example:

            ::

                tree = AnalysisTree(...)
                # compute analysis tree
                tree.compute()
                # show results
                df.data.head()

        Returns:
            promise object, which reports the status of the analysis if not
            complete. Once the analysis finishes, the results will be stored
            in the attributes ``data``, ``results_url``, and ``state``.
        """
        if self.final_query:
            return self.context.query(self.final_query)
        else:
            raise ValueError('No analysis nodes provided to analysis tree')


class Query(object):
    """:obj:`Query` gives a representation of a query in a users's CARTO
    account.

    Example:

      ::

        from cartoframes import CartoContext, Query
        cc = CartoContext()
        snapshot = Query('''
            SELECT
                count(*) as num_sightings,
                b.acadia_district_name,
                b.the_geom
            FROM
                bird_sightings as a, acadia_districts as b
            WHERE
                ST_Intersects(b.the_geom, a.the_geom)
            GROUP BY 2, 3
        ''')
        snapshot.local_moran('num_sightings', 5).filter('significance<=0.05')


    Parameters:

        context (:obj:`CartoContext`): :obj:`CartoContext` instance
          authenticated against the user's CARTO account.
        query (str): Valid query against user's CARTO account.
    """
    def __init__(self, context, query):
        self.query = query
        self.context = context

    @property
    def columns(self):
        """return the column names of the table or query

        Returns:
          pandas.Index: Column names
        """
        subquery = 'SELECT * FROM ({query}) AS _W LIMIT 0'.format(
            query=self.query)
        cols = self.context.sql_client.send(subquery)
        return pd.Index(cols['fields'].keys())

    @property
    def pgtypes(self):
        """return the dtypes of the columns of the table or query

        Returns:
            pandas.Series: Data types (in a PostgreSQL database) of columns
        """
        subquery = 'SELECT * FROM ({query}) as _w LIMIT 0'.format(
            query=self.query)
        dtypes = self.context.sql_client.send(subquery)
        temp = {k: v['type'] for k, v in utils.dict_items(dtypes['fields'])}
        return pd.Series(temp)

    def _validate_query(self, cols=None):
        """
        Validate that the query has the needed column names for the analysis
        to run
        """
        util_cols = ('cartodb_id', 'the_geom', 'the_geom_webmercator', )
        if cols is None:
            cols = util_cols
        self.context.query(
            'select {cols} FROM ({query}) as _w'.format(
                cols=','.join(cols),
                query=self.query
            )
        )

    def read(self):
        """Read the query to a pandas DataFrame

        Returns:
            pandas.DataFrame: Query represented as a pandas DataFrame
        """
        return self.context.query(self.query)

    def moran_local(self, colname, denom=None, n_neighbors=5):
        """Local Moran's I

        Args:
          colname (:obj:`str`): Column name for performing Local Moran's I
            analysis on
          denom (:obj:`str`, optional): Optional denominator for `colname`.
          n_neighbors (:obj:`int`, optional): Number of neighbors for each
            geometry. Defaults to 5.
        """
        pass

    def div(self, numerator, denominator):
        """Divided one column by another column or expression to produce a new
        column

        Example:

            Divide by a constant to convert from square kilometers to square
            miles.

            ::

                from cartoframes import CartoContext, Table
                cc = CartoContext()
                t = Table(cc, 'acadia_biodiversity')
                t.div('ospreys_per_sqkm', 1.6**2)

            Normalize a column by another column.

            ::

                t = Table(cc, 'acadia_biodiversity')
                t.div('osprey_count', 'num_observations')

            Get the density of a value.

            ::

                t = Table(cc, 'acadia_biodiversity')
                t.div('osprey_count', 'the_geom')
        """
        pass

    def plot(self):
        """Plot all the columns in the query.

        Example:

        ::

            Query('''
                SELECT simpson_index, species
                FROM acadia_biodiversity
            ''').plot()
            <matplotlib plot>

        """
        return self.context.query(self.query).plot()

    def buffer(self, dist):
        """Buffer query

        Example:

            .. code::

                q = Query('...')
                buffered_q = q.buffer(150).compute()
                cc.map(layers=[buffered_q, q])

        Args:
            dist (float): Distance in meters to buffer a geometry
        """
        return Query(self.context, _buffer(self, dist))

    def custom(self, query):
        """Define custom query to add to the tree"""
        pass

    def describe(self, cols=None):
        """Gives back basic statistics for a table

        Args:
          cols (list of str): List of column names to get summary statistics

        Returns:
            pandas.DataFrame: A statistical description of the data

        .. todo: add geometry information
        """
        if cols is None:
            cols = self.context.read(limit=0).columns
        # qualities = ('count', 'avg', 'min', 'max', )
        summary_query = utils.minify_sql((
            'SELECT {aggcols}',
            'FROM ({query}) as _w')).format(
                aggcols=('{agg}({col}) as {col}'.format(agg=None, col=None)),
                query=self.query)
        return self.context.query(summary_query)


class Table(Query):
    """Table object"""
    def __init__(self, context, table_name):
        """Table object"""
        super(Table, self).__init__(
            context,
            'SELECT * FROM {}'.format(table_name)
        )