#!/usr/bin/env python
# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import json
import posixpath
import requests
import sys
import textwrap
import time

from django.utils.translation import gettext as _
from urllib.parse import urlparse

from desktop.lib import export_csvxls
from desktop.lib.i18n import force_unicode
from desktop.lib.rest.http_client import HttpClient, RestException
from desktop.lib.rest.resource import Resource
from notebook.connectors.base import Api, QueryError, ExecutionWrapper, ResultWrapper

from trino import exceptions
from trino.auth import BasicAuthentication
from trino.client import ClientSession, TrinoRequest, TrinoQuery

def query_error_handler(func):
  def decorator(*args, **kwargs):
    try:
      return func(*args, **kwargs)
    except RestException as e:
      try:
        message = force_unicode(json.loads(e.message)['errors'])
      except:
        message = e.message
      message = force_unicode(message)
      raise QueryError(message)
    except Exception as e:
      message = force_unicode(str(e))
      raise QueryError(message)
  return decorator



class TrinoApi(Api):

  def __init__(self, user, interpreter=None):
    Api.__init__(self, user, interpreter=interpreter)

    self.options = interpreter['options']

    self.server_host, self.server_port, self.http_scheme = self.parse_api_url(self.options['url'])
    self.auth = None

    if self.options.get('auth_username') and self.options.get('auth_password'):
      self.auth_username = self.options['auth_username']
      self.auth_password = self.options['auth_password']
      self.auth = BasicAuthentication(self.auth_username, self.auth_password)

    trino_session = ClientSession(user.username)
    
    self.db = TrinoRequest(
      host=self.server_host,
      port=self.server_port,
      client_session=trino_session,
      http_scheme=self.http_scheme,
      auth=self.auth
    )

  @query_error_handler
  def parse_api_url(self, api_url):
    parsed_url = urlparse(api_url)
    return parsed_url.hostname, parsed_url.port, parsed_url.scheme


  @query_error_handler
  def create_session(self, lang=None, properties=None):
    pass


  @query_error_handler
  def execute(self, notebook, snippet):
    database = snippet['database']
    query_client = TrinoQuery(self.db, 'USE ' + database)
    query_client.execute()
    
    statement = snippet['statement'].rstrip(';')
    query_client = TrinoQuery(self.db, statement)
    response = self.db.post(query_client.query)
    status = self.db.process(response)

    return {
      'row_n': 0,
      'next_uri': status.next_uri,
      'sync': None,
      'has_result_set': status.next_uri is not None,
      'guid': status.id,
      'result': {
        'has_more': status.id is not None,
        'data': status.rows,
        'meta': [{
            'name': col['name'],
            'type': col['type'],
            'comment': ''
          }
          for col in status.columns
        ]
        if status.columns else [],
        'type': 'table'
      }
    }


  @query_error_handler
  def check_status(self, notebook, snippet):
    response = {}
    status = 'expired'

    if snippet['result']['handle']['next_uri'] is None:
      status = 'available'
    else:
      _response = self.db.get(snippet['result']['handle']['next_uri'])
      _status = self.db.process(_response)
      if _status.stats['state'] == 'QUEUED':
        status = 'waiting'
      elif _status.stats['state'] == 'RUNNING':
        status = 'available' # need to varify
      else:
        status = 'available'

    response['status'] = status

    if status != 'available':
      response['next_uri'] = _status.next_uri
    else:
      response['next_uri'] = snippet['result']['handle']['next_uri']

    return response


  @query_error_handler
  def fetch_result(self, notebook, snippet, rows, start_over):
    data = []
    _columns = []
    _next_uri = snippet['result']['handle']['next_uri']
    processed_rows = snippet['result']['handle'].get('row_n', 0)
    status = False

    if processed_rows == 0:
      data = snippet['result']['handle']['result']['data']

    while _next_uri:
      try:
        response = self.db.get(_next_uri)
      except requests.exceptions.RequestException as e:
        raise trino.exceptions.TrinoConnectionError("failed to fetch: {}".format(e))

      status = self.db.process(response)
      data += status.rows
      _columns = status.columns

      if len(data) >= processed_rows + 100:
        if processed_rows < 0:
          data = data[0:100]
        else:
          data = data[processed_rows:processed_rows + 100]
        break

      _next_uri = status.next_uri
      current_length = len(data)
      data = data[processed_rows:processed_rows + 100]
      processed_rows = processed_rows - current_length

    return {
        'row_n': 100 + processed_rows,
        'next_uri': _next_uri,
        'has_more': bool(status.next_uri) if status else False,
        'data': data or [],
        'meta': [{
            'name': column['name'],
            'type': column['type'],
            'comment': ''
          }
          for column in _columns if status
        ],
        'type': 'table'
    }


  @query_error_handler
  def autocomplete(self, snippet, database=None, table=None, column=None, nested=None, operation=None):
    response = {}

    # if catalog is None:
    #   response['catalogs'] = self._show_catalogs()
    if database is None:
      response['databases'] = self._show_databases()
    elif table is None:
      response['tables_meta'] = self._show_tables(database)
    elif column is None:
      columns = self._get_columns(database, table)
      response['columns'] = [col['name'] for col in columns]
      response['extended_columns'] = [{
          'comment': col.get('comment'),
          'name': col.get('name'),
          'type': col['type']
        }
        for col in columns
      ]

    return response


  @query_error_handler
  def get_sample_data(self, snippet, database=None, table=None, column=None, is_async=False, operation=None):
    
    statement = self._get_select_query(database, table, column, operation)
    query_client = TrinoQuery(self.db, statement)
    query_client.execute()

    response = {
      'status': 0,
      'rows': [],
      'full_headers': []
    }
    response['rows'] = query_client.result.rows
    response['full_headers'] = query_client.columns

    return response
  

  def _get_select_query(self, database, table, column=None, operation=None, limit=100):
    if operation == 'hello':
      statement = "SELECT 'Hello World!'"
    else:
      column = '%(column)s' % {'column': column} if column else '*'
      statement = textwrap.dedent('''\
          SELECT %(column)s
          FROM %(database)s.%(table)s
          LIMIT %(limit)s
          ''' % {
            'database': database,
            'table': table,
            'column': column,
            'limit': limit,
        })

    return statement


  def close_statement(self, notebook, snippet):
    try:
      if snippet['result']['handle']['next_uri']:
        self.db.delete(snippet['result']['handle']['next_uri'])
      else:
        return {'status': -1} # missing operation ids
    except Exception as e:
      if 'does not exist in current session:' in str(e):
        return {'status': -1}  # skipped
      else:
        raise e

    return {'status': 0}


  def close_session(self, session):
    # Avoid closing session on page refresh or editor close for now
    pass


  def _show_databases(self):
    catalogs = self._show_catalogs()
    databases = []

    for catalog in catalogs:

      query_client = TrinoQuery(self.db, 'SHOW SCHEMAS FROM ' + catalog)
      response = query_client.execute()
      databases += [f'{catalog}.{item}' for sublist in response.rows for item in sublist]

    return databases


  def _show_catalogs(self):

    query_client = TrinoQuery(self.db, 'SHOW CATALOGS')
    response = query_client.execute()
    res = response.rows
    catalogs = [item for sublist in res for item in sublist]

    return catalogs


  def _show_tables(self, database):
    
    query_client = TrinoQuery(self.db, 'USE ' + database)
    query_client.execute()
    query_client = TrinoQuery(self.db, 'SHOW TABLES')
    response = query_client.execute()
    tables = response.rows
    return [{
        'name': table[0],
        'type': 'table',
        'comment': '',
      }
      for table in tables
    ]


  def _get_columns(self, database, table):

    query_client = TrinoQuery(self.db, 'USE ' + database)
    query_client.execute()
    query_client = TrinoQuery(self.db, 'DESCRIBE ' + table)
    response = query_client.execute()
    columns = response.rows

    return [{
        'name': col[0],
        'type': col[1],
        'comment': '',
      }
      for col in columns
    ]
  
  def download(self, notebook, snippet, file_format='csv'):
    from beeswax import data_export #TODO: Move to notebook?
    from beeswax import conf

    result_wrapper = TrinoExecutionWrapper(self, notebook, snippet)

    max_rows = conf.DOWNLOAD_ROW_LIMIT.get()
    max_bytes = conf.DOWNLOAD_BYTES_LIMIT.get()

    content_generator = data_export.DataAdapter(result_wrapper, max_rows=max_rows, max_bytes=max_bytes)
    return export_csvxls.create_generator(content_generator, file_format)


class TrinoExecutionWrapper(ExecutionWrapper):

  def fetch(self, handle, start_over=None, rows=None):
    if start_over:
      if not self.snippet['result'].get('handle') \
          or not self.snippet['result']['handle'].get('guid') \
          or not self.api.can_start_over(self.notebook, self.snippet):
        start_over = False
        handle = self.api.execute(self.notebook, self.snippet)
        self.snippet['result']['handle'] = handle

        if self.callback and hasattr(self.callback, 'on_execute'):
          self.callback.on_execute(handle)

        self.should_close = True
        self._until_available()

    if self.snippet['result']['handle'].get('sync', False):
      result = self.snippet['result']['handle']['result']
    else:
      result = self.api.fetch_result(self.notebook, self.snippet, rows, start_over)
      self.snippet['result']['handle']['row_n'] = result['row_n']
      self.snippet['result']['handle']['next_uri'] = result['next_uri']

    return ResultWrapper(result.get('meta'), result.get('data'), result.get('has_more'))

  def _until_available(self):
    if self.snippet['result']['handle'].get('sync', False):
      return # Request is already completed

    count = 0
    sleep_seconds = 1
    check_status_count = 0
    get_log_is_full_log = self.api.get_log_is_full_log(self.notebook, self.snippet)

    while True:
      response = self.api.check_status(self.notebook, self.snippet)
      old_uri = self.snippet['result']['handle']['next_uri']
      self.snippet['result']['handle']['next_uri'] = response['next_uri']
      if self.callback and hasattr(self.callback, 'on_status'):
        self.callback.on_status(response['status'])
      if self.callback and hasattr(self.callback, 'on_log'):
        log = self.api.get_log(self.notebook, self.snippet, startFrom=count)
        if get_log_is_full_log:
          log = log[count:]

        self.callback.on_log(log)
        count += len(log)

      if response['status'] not in ['waiting', 'running', 'submitted']:
        self.snippet['result']['handle']['next_uri'] = old_uri
        break
      check_status_count += 1
      if check_status_count > 5:
        sleep_seconds = 5
      elif check_status_count > 10:
        sleep_seconds = 10
      time.sleep(sleep_seconds)
