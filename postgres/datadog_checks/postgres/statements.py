# (C) Datadog, Inc. 2020-present
# All rights reserved
# Licensed under Simplified BSD License (see LICENSE)
from __future__ import unicode_literals
import copy

import psycopg2
import psycopg2.extras

from datadog_checks.base.log import get_check_logger
from datadog_checks.base.utils.db.sql import compute_sql_signature, normalize_query_tag
from datadog_checks.base.utils.db.statement_metrics import StatementMetrics, apply_row_limits

from .util import milliseconds_to_nanoseconds

try:
    import datadog_agent
except ImportError:
    from ..stubs import datadog_agent


STATEMENTS_QUERY = """
SELECT {cols}
  FROM {pg_stat_statements_view} as pg_stat_statements
  LEFT JOIN pg_roles
         ON pg_stat_statements.userid = pg_roles.oid
  LEFT JOIN pg_database
         ON pg_stat_statements.dbid = pg_database.oid
  WHERE pg_database.datname = %s
  AND query != '<insufficient privilege>'
  LIMIT {limit}
"""

DEFAULT_STATEMENTS_LIMIT = 10000

# Required columns for the check to run
PG_STAT_STATEMENTS_REQUIRED_COLUMNS = frozenset({'calls', 'query', 'total_time', 'rows'})

PG_STAT_STATEMENTS_OPTIONAL_COLUMNS = frozenset({'queryid'})

# Columns to apply as tags
PG_STAT_STATEMENTS_TAG_COLUMNS = {
    'datname': 'db',
    'rolname': 'user',
    'query': 'query',
}

# Monotonically increasing count columns to be converted to metrics
PG_STAT_STATEMENTS_METRIC_COLUMNS = {
    'calls': 'postgresql.queries.count',
    'total_time': 'postgresql.queries.time',
    'rows': 'postgresql.queries.rows',
    'shared_blks_hit': 'postgresql.queries.shared_blks_hit',
    'shared_blks_read': 'postgresql.queries.shared_blks_read',
    'shared_blks_dirtied': 'postgresql.queries.shared_blks_dirtied',
    'shared_blks_written': 'postgresql.queries.shared_blks_written',
    'local_blks_hit': 'postgresql.queries.local_blks_hit',
    'local_blks_read': 'postgresql.queries.local_blks_read',
    'local_blks_dirtied': 'postgresql.queries.local_blks_dirtied',
    'local_blks_written': 'postgresql.queries.local_blks_written',
    'temp_blks_read': 'postgresql.queries.temp_blks_read',
    'temp_blks_written': 'postgresql.queries.temp_blks_written',
}

SYNTHETIC_METRIC_COLUMNS = {
    'avg_time': 'postgresql.queries.avg_time',
}

METRIC_COLUMNS = copy.copy(PG_STAT_STATEMENTS_METRIC_COLUMNS)
METRIC_COLUMNS.update(SYNTHETIC_METRIC_COLUMNS)

# Limits to restrict collection to top K and bottom K queries for each metric
DEFAULT_METRIC_LIMITS = {
    'calls': (800, 0),
    'total_time': (800, 0),
    'rows': (800, 0),
    'shared_blks_hit': (50, 0),
    'shared_blks_read': (50, 0),
    'shared_blks_dirtied': (50, 0),
    'shared_blks_written': (50, 0),
    'local_blks_hit': (50, 0),
    'local_blks_read': (50, 0),
    'local_blks_dirtied': (50, 0),
    'local_blks_written': (50, 0),
    'temp_blks_read': (50, 0),
    'temp_blks_written': (50, 0),
    # Synthetic column limits
    'avg_time': (800, 0),
    'shared_blks_ratio': (0, 100),
}


def compute_synthetic_rows(rows):
    """
    Given a list of rows, generate a new list of rows with "synthetic" column values derived from
    the existing row values.
    """
    synthetic_rows = []
    for row in rows:
        new = copy.copy(row)
        new['avg_time'] = new['total_time'] / new['calls'] if new['calls'] > 0 else 0
        new['shared_blks_ratio'] = new['shared_blks_hit'] / (new['shared_blks_hit'] + new['shared_blks_read']) if new['shared_blks_hit'] + new['shared_blks_read'] > 0 else 0

        synthetic_rows.append(new)
    
    return synthetic_rows


def reduce_rows(rows, metrics, key):
    """
    Aggregate the rows into a set of unique rows identified by their "key" function.

    Each new row will have a "total" column representing the total columns.
    """
    reduced_rows = {}
    for row in rows:
        k = key(row)

        if k in reduced_rows:
            reduced = reduced_rows[k]
            reduced['total'] += 1
            for metric in metrics:
                reduced[metric] += row[metric]
        else:
            reduced = copy.copy(row)
            reduced['total'] = 1
            reduced_rows[k] = reduced
    
    return list(reduced_rows.values())


def generate_aggregate_rows(rows, all_rows, metrics, key):
    """
    Generates the "catch-all" row for rows not limited.
    """
    aggregated = {}
    for row in reduce_rows(rows=all_rows, metrics=metrics, key=key):
        row['query'] = None  # Query is no longer relevant as this row is an aggregate row of many queries
        aggregated[key(row)] = row

    for row in rows:
        k = key(row)
        agg_row = aggregated.get(k)
        for metric in metrics:
            agg_row[metric] -= row[metric]
        agg_row['total'] -= 1
    
    return list(aggregated.values())


class PostgresStatementMetrics(object):
    """Collects telemetry for SQL statements"""

    def __init__(self, config):
        self.config = config
        self.log = get_check_logger()
        self._state = StatementMetrics()

    def _execute_query(self, cursor, query, params=()):
        try:
            cursor.execute(query, params)
            return cursor.fetchall()
        except (psycopg2.ProgrammingError, psycopg2.errors.QueryCanceled) as e:
            self.log.warning('Statement-level metrics are unavailable: %s', e)
            return []

    def _get_pg_stat_statements_columns(self, db):
        """
        Load the list of the columns available under the `pg_stat_statements` table. This must be queried because
        version is not a reliable way to determine the available columns on `pg_stat_statements`. The database can
        be upgraded without upgrading extensions, even when the extension is included by default.
        """
        # Querying over '*' with limit 0 allows fetching only the column names from the cursor without data
        query = STATEMENTS_QUERY.format(
            cols='*',
            pg_stat_statements_view=self.config.pg_stat_statements_view,
            limit=0,
        )
        cursor = db.cursor()
        self._execute_query(cursor, query, params=(self.config.dbname,))
        colnames = [desc[0] for desc in cursor.description]
        return colnames
    
    def _get_pg_stat_statements_rows(self, db):
        """
        Execute the query to fetch per-statement row aggregates from the database.
        """
        available_columns = self._get_pg_stat_statements_columns(db)
        missing_columns = PG_STAT_STATEMENTS_REQUIRED_COLUMNS - set(available_columns)
        if len(missing_columns) > 0:
            self.log.warning(
                'Unable to collect statement metrics because required fields are unavailable: %s',
                ', '.join(list(missing_columns)),
            )
            return []

        desired_columns = (
            list(PG_STAT_STATEMENTS_METRIC_COLUMNS.keys())
            + list(PG_STAT_STATEMENTS_OPTIONAL_COLUMNS)
            + list(PG_STAT_STATEMENTS_TAG_COLUMNS.keys())
        )
        query_columns = list(set(desired_columns) & set(available_columns) | set(PG_STAT_STATEMENTS_TAG_COLUMNS.keys()))
        rows = self._execute_query(
            db.cursor(cursor_factory=psycopg2.extras.DictCursor),
            STATEMENTS_QUERY.format(
                cols=', '.join(query_columns),
                pg_stat_statements_view=self.config.pg_stat_statements_view,
                limit=DEFAULT_STATEMENTS_LIMIT,
            ),
            params=(self.config.dbname,),
        )
        return rows

    def collect_per_statement_metrics(self, db):
        try:
            return self._collect_per_statement_metrics(db)
        except Exception:
            db.rollback()
            self.log.exception('Unable to collect statement metrics due to an error')
            return []

    def _collect_per_statement_metrics(self, db):
        metrics = []

        all_rows = self._get_pg_stat_statements_rows(db)
        if not all_rows:
            return metrics

        def row_keyfunc(row):
            # old versions of pg_stat_statements don't have a query ID so fall back to the query string itself
            queryid = row['queryid'] if 'queryid' in row else row['query']
            return (queryid, row['datname'], row['rolname'])
        
        def aggregate_row_keyfunc(row):
            return (row['datname'], row['rolname'])

        all_rows = self._state.compute_derivative_rows(all_rows, PG_STAT_STATEMENTS_METRIC_COLUMNS.keys(), key=row_keyfunc)
        all_rows = compute_synthetic_rows(all_rows)

        rows = apply_row_limits(
            all_rows, DEFAULT_METRIC_LIMITS, tiebreaker_metric='calls', tiebreaker_reverse=True, key=row_keyfunc
        )

        # Produce the row aggregates to capture statement metrics that were dropped after applying limits
        aggregate_rows = generate_aggregate_rows(rows, all_rows, METRIC_COLUMNS.keys(), key=aggregate_row_keyfunc)

        # for row in rows + aggregate_rows:
        for row in rows:
            row = self.prepare_row(row)
            tags = self.get_row_tags(row)

            for column, metric_name in METRIC_COLUMNS.items():
                if column not in row:
                    continue
                value = row[column]
                metrics.append((metric_name, value, tags))

        return metrics

    def prepare_row(self, row):
        if row['query'] is not None:
            try:
                normalized_query = datadog_agent.obfuscate_sql(row['query'])
                normalized_query = normalize_query_tag(normalized_query)
            except Exception as e:
                # If query obfuscation fails, it is acceptable to log the raw query here because the
                # pg_stat_statements table contains no parameters in the raw queries.
                self.log.warning("Failed to obfuscate query '%s': %s", row['query'], e)
                normalized_query = 'Obfuscation Error'

            row['query'] = normalized_query
        else:
            row['query'] = 'Other Queries'

        # All "Deep Database Monitoring" timing metrics are in nanoseconds
        # Postgres tracks pg_stat* timing stats in milliseconds
        row['total_time'] = milliseconds_to_nanoseconds(row['total_time'])
        row['avg_time'] = milliseconds_to_nanoseconds(row['avg_time'])
        return row
    
    def get_row_tags(self, row):
        query_signature = compute_sql_signature(row['query'])
        # All "Deep Database Monitoring" statement-level metrics are tagged with a `query_signature`
        # which uniquely identifies the normalized query family. Where possible, this hash should
        # match the hash of APM "resources" (https://docs.datadoghq.com/tracing/visualization/resource/)
        # when the resource is a SQL query. Postgres' query normalization in the `pg_stat_statements` table
        # preserves most of the original query, so we tag the `resource_hash` with the same value as the
        # `query_signature`. The `resource_hash` tag should match the *actual* APM resource hash most of
        # the time, but not always. So this is a best-effort approach to link these metrics to APM metrics.
        tags = ['query_signature:' + query_signature, 'resource_hash:' + query_signature]

        for column, tag_name in PG_STAT_STATEMENTS_TAG_COLUMNS.items():
            if column not in row:
                continue
            value = row[column]
            tags.append('{tag_name}:{value}'.format(tag_name=tag_name, value=value))

        return tags