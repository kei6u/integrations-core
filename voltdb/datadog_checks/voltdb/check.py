# (C) Datadog, Inc. 2020-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from typing import Any, List, Optional, cast

import pkg_resources
import requests
from six import raise_from

from datadog_checks.base import AgentCheck
from datadog_checks.base.utils.db import Query, QueryManager

from . import queries
from .config import Config
from .types import Instance

BASE_PARSED_VERSION = pkg_resources.get_distribution('datadog-checks-base').parsed_version


class VoltDBCheck(AgentCheck):
    __NAMESPACE__ = 'voltdb'

    def __init__(self, name, init_config, instances):
        # type: (str, dict, list) -> None
        super(VoltDBCheck, self).__init__(name, init_config, instances)
        self._config = Config(cast(Instance, self.instance), debug=self.log.debug)

        if self._config.auth is not None:
            password = self._config.auth._password
            self.register_secret(password)

        manager_queries = [
            queries.CPUMetrics,
            queries.MemoryMetrics,
            queries.SnapshotStatusMetrics,
            queries.CommandLogMetrics,
            queries.ProcedureMetrics,
            queries.LatencyMetrics,
            queries.StatementMetrics,
            queries.GCMetrics,
            queries.IOStatsMetrics,
            queries.TableMetrics,
            queries.IndexMetrics,
        ]

        if BASE_PARSED_VERSION < pkg_resources.parse_version('15.0.0'):
            # On Agent < 7.24.0 we must to pass `Query` objects instead of dicts.
            manager_queries = [Query(query) for query in manager_queries]  # type: ignore

        self._query_manager = QueryManager(
            self,
            self._execute_query_raw,
            queries=manager_queries,
            tags=self._config.tags,
        )
        self.check_initializations.append(self._query_manager.compile_queries)

    def _get_system_information(self):  # Isolated for unit testing purposes.
        # type: () -> requests.Response
        # See: https://docs.voltdb.com/UsingVoltDB/sysprocsysteminfo.php#sysprocsysinforetvalovervw
        url = self._config.api_url
        auth = self._config.auth
        params = self._config.build_api_params(procedure='@SystemInformation', parameters=['OVERVIEW'])

        try:
            return self.http.get(url, auth=auth, params=params)
        except Exception:
            raise

    def _fetch_version(self):
        # type: () -> Optional[str]
        try:
            r = self._get_system_information()
        except Exception:
            raise

        try:
            r.raise_for_status()
        except Exception as exc:
            message = 'Error response from VoltDB: {}'.format(exc)
            try:
                # Try including detailed error message from response.
                details = r.json()['statusstring']
            except Exception:
                pass
            else:
                message += ' (details: {})'.format(details)
            raise_from(Exception(message), exc)

        data = r.json()
        rows = data['results'][0]['data']  # type: List[tuple]

        # NOTE: there will be one VERSION row per server in the cluster.
        # Arbitrarily use the first one we see.
        for _, column, value in rows:
            if column == 'VERSION':
                return self._transform_version(value)

        self.log.debug('VERSION column not found: %s', [column for _, column, _ in rows])
        return None

    def _transform_version(self, raw):
        # type: (str) -> str
        # VoltDB does not include .0 patch numbers (eg 10.0, not 10.0.0).
        # Need to ensure they're present so the version is always in 3 parts: major.minor.patch.
        major, rest = raw.split('.', 1)
        minor, found, patch = rest.partition('.')
        if not found:
            patch = '0'
        return '{}.{}.{}'.format(major, minor, patch)

    @AgentCheck.metadata_entrypoint
    def _submit_version(self, version):
        # type: (str) -> None
        self.set_metadata('version', version)

    def _check_can_connect_and_submit_version(self):
        # type () -> None
        host, port = self._config.netloc
        tags = ['host:{}'.format(host), 'port:{}'.format(port)] + self._config.tags

        try:
            version = self._fetch_version()
        except Exception as exc:
            message = 'Unable to connect to VoltDB: {}'.format(exc)
            self.service_check('can_connect', self.CRITICAL, message=message, tags=tags)
            raise

        self.service_check('can_connect', self.OK, tags=tags)

        if version is not None:
            self._submit_version(version)

    def _execute_query_raw(self, query):
        # type: (str) -> List[tuple]
        # Ad-hoc format, close to the HTTP API format.
        # Eg 'A:[B, C]' -> '?Procedure=A&Parameters=[B, C]'
        procedure, _, parameters = query.partition(":")

        url = self._config.api_url
        auth = self._config.auth
        params = self._config.build_api_params(procedure=procedure, parameters=parameters)

        response = self.http.get(url, auth=auth, params=params)
        response.raise_for_status()

        data = response.json()
        return data['results'][0]['data']

    def check(self, _):
        # type: (Any) -> None
        self._check_can_connect_and_submit_version()
        self._query_manager.execute()
