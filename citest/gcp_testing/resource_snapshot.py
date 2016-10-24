# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Collects resources managed by Google APIs.

Usage:
  python citest/gcp_testing/resource_snapshot.py \
      --bindings=project=my-project,bucket=my-bucket \
      --output_file=baseline \
      compute storage

  python citest/gcp_testing/resource_snapshot.py \
      --bindings=project=my-project,bucket=my-bucket \
      --output_file=delta \
      compute storage

  python citest/gcp_testing/resource_snapshot.py \
      --compare baseline delta

  python citest/gcp_testing/resource_snapshot.py \
      --compare baseline delta \
      --delete_after \
      --delete_for_real
"""

import argparse
import collections
import json
import pickle
import re

from googleapiclient.errors import HttpError

from citest.gcp_testing.gcp_agent import GcpAgent
from citest.base import ExecutionContext


def to_json_string(obj):
  """Convert object as a JSON string."""
  return json.JSONEncoder(indent=2, encoding='utf-8').encode(obj)


def make_resource_filter(name_filters):
  """Returns a function that filters a resource name based on our filters.

  Args:
    name_filters: [list of compiled re's]

  Returns:
    A callable that takes a resource name and returns True to keep it.
  """
  def wanted(key):
    # pylint: disable=missing-docstring
    for exp in name_filters:
      if exp.match(key):
        return True
    return False
  return wanted


class Explorer(object):
  """Explores APIs to see what resources they manage and how to inspect them."""

  @property
  def options(self):
    """Command line options."""
    return self.__options

  @staticmethod
  def collect_apis():
    """Collect names and current versions of all the Google APIs."""
    context = ExecutionContext()
    agent = GcpAgent.make_agent(api='discovery', version='v1')
    apis = agent.list_resource(context, 'apis')

    catalog = {}
    for entry in apis:
      preferred = entry.get('preferred', True)
      if not preferred:
        continue
      catalog[entry['name']] = entry['version']
    return catalog

  def __init__(self, options):
    self.__options = options

  def pick_scope(self, choices, api, mutable=False):
    """Given a list of scopes, pick the "safest" one.

    By "safest" we mean one that is the closest match for read (not mutable)
    or write (mutable). We're just guessing blind here. We'll prefer a
    read/write-only scope on the resource itself and work our way back from
    there if we cannot find it one.

    Args:
      choices: [list of string]  List of auth scopes to pick from.
      api: [string]  The name of the resource API the scope is for.
      mutable: [boolean]  False for a read-only scope true for a write-only one.
    """
    if not choices:
      return None

    best_name = 'write' if mutable else 'read'
    best = [choice for choice in choices if choice.find(best_name) >= 0]
    if len(best) == 1:
      return best[0]


    only = [choice for choice in best if choice.find('only') > 0]
    if len(only) == 1:
      return only[0]

    # Either nothing was best_name or there are multiple ones.
    # Either way, lets rule out an unpreferred scopes. This will cause us
    # to favor "read" or "read_only" over "read_write" for mutable
    # and to favor "full" scopes if mutable and no "write" scope listed.
    alternative = [choice for choice in (only or best or choices)
                   if (mutable and choice.find('full') > 0
                       or not mutable and choice.find('write') < 0)]
    if len(alternative) == 1:
      return alternative[0]

    # Since we did not find a scope limited to read
    # attempt to locate one that is defined by our API itself
    # as opposed to giving more global permissions.
    special = [choice for choice in (only or best or alternative or choices)
               if choice.find(api) > 0]

    if special:
      return special[0]

    # Just pick one.
    return (only or best or alternative or choices)[0]

  def determine_scope_map(self, resources, api, mutable=False):
    """Determine the scope we'll need to list each API.

    Args:
      resources: [map of resource, method_spec] The resources to map
      api: [string] The name of the api containing the resource.

    Returns:
       This returns an inverse map keyed by the scope and containing
       all the APIs that use that scope. This is more useful because we'll
       be reusing the scoped client where we can.
    """
    resource_to_scope = {
        key : self.pick_scope(value.get('scopes'), api, mutable=mutable)
        for key, value in resources.items()}
    scope_to_resource = {}
    for resource, scope in resource_to_scope.items():
      if scope in scope_to_resource:
        scope_to_resource[scope].add(resource)
      else:
        scope_to_resource[scope] = set([resource])
    return scope_to_resource

  def find_listable_resources(self, api, version, name_filters):
    """Find all the resources within an API that we can list elements of.

    Args:
      api: [string] The API name containing the resources.
      version: [string] The API version.
      name_filters: [set(string)] list of regexes for resource names to match.

    Returns:
      Map of resource name to the method specification for listing it.
    """
    doc = GcpAgent.download_discovery_document(api, version)
    resources = doc['resources']
    return self.__find_listable_resources_helper(None, resources, name_filters)

  def __find_listable_resources_helper(
      self, container, resources, name_filters):
    """Helper method for find_listable_resources.

    This is potentially recursive to handle nested resources.

    Args:
      container: [string] The parent resource, if any.
      resources: [list of resource specification] The discovery document
          resource specification to look for a list method within.
      name_filters: [set(string)] list of regexes for resource names to match.
    """
    wanted = make_resource_filter(name_filters)

    listable = {}
    unlistable = set()
    container_prefix = '{0}.'.format(container) if container else ''
    for name, value in resources.items():
      key = '{prefix}{name}'.format(prefix=container_prefix, name=name)
      if not wanted(key):
        continue
      children = value.get('resources')
      if children:
        sub_listable, sub_unlistable = self.__find_listable_resources_helper(
            key, children, name_filters)
        listable.update(sub_listable)
        unlistable = unlistable.union(sub_unlistable)
      methods = value.get('methods') or {}
      list_method = methods.get('aggregatedList') or methods.get('list')
      if list_method:
        listable[key] = list_method
      else:
        unlistable.add(key)
    return listable, unlistable

  def determine_required_parameters(self, method_spec):
    """Determine which parameters are required within a method specification.

    Args:
      method_spec: [string] The specification from the discovery document.

    Returns:
      map of parameter name to the parameter specification.
    """
    result = {}
    parameters = method_spec.get('parameters', {})
    for key, key_spec in parameters.items():
      if key_spec.get('required', False):
        result[key] = key_spec
    return result

  def stringify_api(self, api, listable, unlistable, scope_map):
    """Print API to stdout."""
    lines = ['API:  "{api}"'.format(api=api)]
    if unlistable:
      lines.append('  UNLISTABLE')
      for name in unlistable:
        lines.append('    * {0}'.format(name))

    for scope, resource_list in scope_map.items():
      lines.append('  {0}'.format(scope))
      for name in resource_list:
        lines.append('    * {0}: params=({1})'.format(
            name,
            ', '.join([key
                       for key, value
                       in self.determine_required_parameters(
                           listable[name]).items()])))
    lines.append('-' * 40 + '\n')
    return '\n'.join(lines)


class ResourceList(
    collections.namedtuple('ResourceList',
                           ['aggregated', 'params', 'response'])):
  """A collection of resource instances and their container parameters.


  Attributes:
    aggregated: [bool] If true, the result is from an aggregatedList.
        Aggregated list results are in the form (key, value) where
        key is the key from the aggregatedListResponse and value is the
        value for that key. Otherwise the response is just the list of values.
    params: [dict] The parameters used to query the list
    response: [list] The list of values from the query. These are not the full
        response json dictionaries, rather just the entity name/identifier.
  """

  def stringify(self, resource):
    """Convert to string.

    Args:
      resource: [string] The name of the resource this result was from.
    """
    bindings = ', '.join(['{0}={1}'.format(name, value)
                          for name, value in self.params.items()])
    lines = ['RESOURCE {0}({1}) + {2}'.format(
        resource, bindings, len(self.response))]
    for name in self.response:
      lines.append('  * {0}'.format(name))
    return '\n'.join(lines)


class Processor(object):
  """Functions for performing heuristics on APIs and resources."""

  @property
  def explorer(self):
    """The bound explorer helper class."""
    return self.__explorer

  def __init__(self, explorer):
    self.__explorer = explorer
    self.__options = explorer.options
    self.__default_variables = {}
    if self.__options.bindings:
      bindings = self.__options.bindings.split(',')
      pairs = [binding.split('=') for binding in bindings]
      self.__default_variables = {name: value for name, value in pairs}

  def make_agent(self, api, version, default_scope, default_variables=None):
    """Construct an agent to talk to a Google API.

    Args:
      api: [string] The API name containing the resources.
      version: [string] The API version.
      default_scope: [string] The oauth scope to use if options.credentials_path
    """
    credentials = self.__options.credentials_path or None
    default_variables = default_variables or self.__default_variables
    scope_list = [default_scope] if credentials else None
    return GcpAgent.make_agent(
        api=api, version=version,
        scopes=scope_list,
        credentials_path=credentials,
        default_variables=default_variables)

  def __determine_list_method_and_transform(self, agent, resource):
    """Determine which list method to use, and result transform for it.

    We'll use either list or aggregatedList or None depending on what
    is available, the parameters it requires, and the arguments we have.

    Args:
       agent: [GcpAgent] The agent for talking to the service
    """
    error = None
    for method_name in ['list', 'aggregatedList']:
      try:
        agent.resource_method_to_variables(method_name, resource,
                                           **agent.default_variables)
        if method_name == 'list':
          return 'list', None, None

        path = (agent.discovery_document['resources'][resource]
                ['methods'][method_name]['path'])
        data_label = path.split('/')[-1]
        def transform(items):
          result = []
          for key, entry_values in items.items():
            data_values = entry_values.get(data_label, None)
            if data_values:
              result.extend([(key, value) for value in data_values])
            return result
        return 'aggregatedList', transform, None
      except KeyError:
        pass  # Unknown method
      except ValueError as err:
        error = err.message
        # Maybe try again if more remaining.

    return None, None, error

  def list_api(self, api, version, scope_map):
    """List the instances of an API."""
    result = {}
    errors = {}
    context = ExecutionContext()

    for scope, resource_list in scope_map.items():
      agent = self.make_agent(api, version, scope)
      for resource in resource_list:
        method_name, transform, error_msg = (
            self.__determine_list_method_and_transform(agent, resource))
        if method_name is None:
          print '*** ' + error_msg
          errors[resource] = error_msg
          continue

        try:
          params = agent.resource_method_to_variables(method_name, resource)
          instances = agent.list_resource(
              context, resource,
              method_variant=method_name,
              item_list_transform=transform)
        except ValueError as err:
          print '*** ' + str(err)
          errors[resource] = err.message
          continue

        if instances and method_name == 'aggregatedList':
          answers = []
          for values in instances:
            key = values[0]
            entry = values[1]
            answers.append((key, entry.get('name') or entry.get('id')
                            or entry.get('selfLink') or entry))

          resource_list = ResourceList(True, params, answers)
        elif instances and isinstance(instances[0], dict):
          resource_list = ResourceList(
              False, params, [entry.get('name') or entry.get('id')
                              or entry.get('selfLink') or 'UNKNOWN '+str(entry)
                              for entry in instances])
        else:
          resource_list = ResourceList(False, params, instances)

        result[resource] = resource_list

    return result, errors

  def __stringify_enumerated(self, objs, bullet='*', prefix='', title=None):
    """Produce a string with one object element per line."""
    if not objs:
      return ''

    lines = []
    if title:
      lines.append('{prefix}{title}'.format(prefix=prefix, title=title))
    for value in objs:
      lines.append('{prefix}  {bullet} {value}'.format(
          prefix=prefix, bullet=bullet, value=value))
    return '\n'.join(lines)

  def compare_resource_list(self, resource, before, after, show_same=False):
    """Compare resource lists and print the differences.

    Args:
      resource: [string] The name of the resource.
      before: [ResourceList] The baseline snapshot.
      after: [ResourceList] The snapshot to compare.
      show_same: [boolean] If true, also show unchanged values.
    """
    indent = '  '
    if before.params != after.params:
      print '{0}PARAMETERS for "{1}" DIFFER so values are disjoint'.format(
          indent, resource)
      indent += '  '
      print '{0}Before: {1}\n{0}After : {2}'.format(
          indent, before.params, after.params)
      return

    before_names = set(before.response)
    after_names = set(after.response)
    removed = before_names.difference(after_names)
    added = after_names.difference(before_names)
    same = before_names.intersection(after_names)
    if len(removed) + len(added) + len(same) == 0:
      if show_same and resource:
        print '{0}RESOURCE: {1}  empty'.format(indent, resource)
      return

    if added or removed or show_same:
      print '{0}RESOURCE: {1}  +/-/= {2}/{3}/{4}'.format(
          indent, resource, len(added), len(removed), len(same))
      indent += '  '

    if added:
      print self.__stringify_enumerated(added, bullet='+', prefix=indent)
    if removed:
      print self.__stringify_enumerated(removed, bullet='-', prefix=indent)
    if show_same:
      print self.__stringify_enumerated(same, bullet='=', prefix=indent)

  def compare_api_resources(self, api, before, after, name_filters,
                            show_same=False):
    """Compare all ResourceLists within an API.

    Args:
      api: [string] The API name.
      before: [dict] Map of resource name to ResourceList for baseline.
      after: [dict] Map of resource name to ResourceList for comparison.
      name_filters: [list] List of compiled regular expressions to filter
         resource names to consider.
      show_same: [boolean] Also show unchanged resources.
    """
    wanted = make_resource_filter(name_filters)
    before_keys = set([key for key in before.keys() if wanted(key)])
    after_keys = set([key for key in after.keys() if wanted(key)])
    resources_removed = before_keys.difference(after_keys)
    resources_added = after_keys.difference(before_keys)

    if resources_removed:
      print self.__stringify_enumerated(
          resources_removed, title='MISSING RESOURCES', prefix='  ')
    if resources_added:
      print self.__stringify_enumerated(
          resources_added, title='EXTRA RESOURCES', prefix='  ')

    for resource in before_keys.intersection(after_keys):
      self.compare_resource_list(resource, before[resource], after[resource],
                                 show_same=show_same)

  def compare(self, before, after, api_to_name_filters, show_same=False):
    """Compare a suite of API ResourceLists to another.

    Args:
      before: [dict] Baseline map of {api: {resource: ResourceList}}
      after: [dict] Comparision map of {api: {resource: ResourceList}}
      api_to_name_filters: [dict] {api: [resource name regexs]}
      show_name: [boolean]: Also show unchanged resources.
    """
    before_apis = set([api for api in before.keys()
                       if api_to_name_filters.get(api)])
    after_apis = set([api for api in after.keys()
                      if api_to_name_filters.get(api)])
    apis_removed = before_apis.difference(after_apis)
    apis_added = after_apis.difference(before_apis)

    if apis_removed:
      print self.__stringify_enumerated(
          apis_removed, title='MISSING APIS')
    if apis_added:
      print self.__stringify_enumerated(
          apis_added, title='EXTRA RESOURCES')

    for api in before_apis.intersection(after_apis):
      print 'API "{0}"'.format(api)
      self.compare_api_resources(api, before[api], after[api],
                                 api_to_name_filters[api],
                                 show_same=show_same)
      print '-' * 40 + '\n'

  def delete_added(self, api, version, before, after, name_filters):
    """Delete resources that were added since the baseline.

    Args:
      api: [string] The API name containing the resources.
      version: [string] The API version.
      before: [dict] {resource: [ResourceList]} baseline.
      after: [dict] {resource: [ResourceList]} changed.
      name_filters: [list] list of regexps for names to consider.
    """
    if name_filters is None:
      return

    wanted = make_resource_filter(name_filters)
    discovery_doc = GcpAgent.download_discovery_document(
        api=api, version=version)

    common_resources = set([
        resource
        for resource in set(before.keys()).intersection(set(after.keys()))
        if wanted(resource)])
    for resource in common_resources:
      if before[resource].params != after[resource].params:
        print 'WARNING: ignoring "{0}" because parameters do not match.'.format(
            resource)
        continue

      before_values = set(before[resource].response)
      after_values = set(after[resource].response)
      added = after_values.difference(before_values)
      if not added:
        continue

      delete = (discovery_doc
                .get('resources', {})
                .get(resource, {})
                .get('methods', {})
                .get('delete', None))
      if not delete:
        print '*** Cannot find delete method for "{0}"'.format(resource)
        continue

      scope = self.__explorer.pick_scope(
          delete.get('scopes', []), api, mutable=True)
      agent = self.make_agent(api, version, scope,
                              default_variables=after[resource].params)
      print '{action} from API={api} with scope={scope}'.format(
          action=('Deleting' if self.__options.delete_for_real
                  else 'Simulating Delete'),
          api=api,
          scope=scope if self.__options.credentials_path else '<default>')

      aggregated = after[resource].aggregated
      self.__delete_all(agent, resource, added, aggregated)
      print '-' * 40 + '\n'

  def __delete_all(self, agent, resource, results_to_delete, aggregated):
    """Implements the actual delete heuristics.

    Args:
      agent: [GcpAgent] The agent to delete the resources.
      resource: [string] The resource type to delete.
      results_to_delete: [string] The listing results to be deleted.
         These may be the ids or may be tuples (params, result) if
         the listing was an aggreatedList.
      aggregated: [bool] Indicates whether results_to_delete were aggregated.
    """
    context = ExecutionContext()
    for elem in results_to_delete:
      params = {}
      name = elem
      if aggregated:
        name = elem[1]
        param_name, param_value = elem[0].split('/', 1)
        if param_name[-1] == 's':
          param_name = param_name[:-1]

        # Just because the aggregation returned a parameter
        # does not mean the delete API takes it. Confirm before adding.
        if (agent.resource_type_to_discovery_info(resource)
            .get('methods', {}).get('delete', {}).get('parameters', {})
            .get(param_name)):
          params[param_name] = param_value

      name = elem[1] if aggregated else elem
      try:
        if self.__options.delete_for_real:
          agent.invoke_resource(context, 'delete', resource,
                                resource_id=name, **params)
          print 'Deleted "{resource}" {name}'.format(
              resource=resource, name=name)
        else:
          variables = agent.resource_method_to_variables(
              'delete', resource, resource_id=name, **params)
          args = ','.join([' {0}={1!r}'.format(key, value)
                           for key, value in variables.items()])
          if args:
            args = '\n  ' + args
          print 'Ideally, this would delete "{resource}" {name}{args}'.format(
              resource=resource, name=name, args=args)
      except HttpError as http_error:
        if http_error.resp.status == 404:
          print 'WARNING: Ignoring 404 deleting "{resource}" {name}'.format(
              resource=resource, name=name)
        else:
          print ('WARNING: Ignore error deleting "{resource}" {name}: {msg}'
                 .format(resource=resource, name=name, msg=http_error))


class Main(object):
  """Implements command line program for producing and manipulating snapshots.
  """
  @staticmethod
  def __get_options():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'apis', nargs='+',
        help='The list of APIs to process. These can be in the form'
        ' <api>.<resource> to limit particular resources.'
        ' The <resource> can have "*" wildcards.')

    parser.add_argument(
        '--catalog', default=False, action='store_true',
        help='Show a catalog of all the listable resources.')
    parser.add_argument(
        '--print_api_spec', default=False, action='store_true',
        help='Print the discovery document for the API itself.')

    parser.add_argument(
        '--bindings', default=None,
        help='A comma-separated list of variable bindings where a binding is'
        ' <name>=<value>. These will be used as parameters when calling API'
        ' methods as needed.')
    parser.add_argument(
        '--credentials_path', default='',
        help='Path to overide credentials from JSON file.')

    parser.add_argument(
        '--list', default=False, action='store_true',
        help='List the resource instances.')

    parser.add_argument(
        '--output_path', default=None,
        help='Store listing results to file.')
    parser.add_argument(
        '--compare', default=None, nargs=2,
        help='Compare two stored result files.')
    parser.add_argument(
        '--show_unchanged', default=False, action='store_true',
        help='Also show unchanged values in listing compare.')
    parser.add_argument(
        '--delete_added', default=False, action='store_true',
        help='Delete the added resources after --compare.'
        ' Requires --delete_for_real, otherwise it is a dry run.')
    parser.add_argument(
        '--delete_for_real', default=False, action='store_true',
        help='Actually attempt the deletes, do not just hypothesize.')

    return parser.parse_args()

  @property
  def exit_code(self):
    """Return the exit code."""
    return self.__exit_code

  def __init__(self, options):
    self.__options = options
    self.__explorer = Explorer(options)
    self.__processor = Processor(self.__explorer)
    self.__version_map = None
    self.__aggregated_listings = {}
    self.__listable_unlistable_scope_cache = {}
    self.__api_to_resource_regexs = {}
    self.__exit_code = 0

  @property
  def version_map(self):
    """Returns map of API name to current version."""
    if self.__version_map is None:
      self.__version_map = Explorer.collect_apis()
    return self.__version_map

  @staticmethod
  def main():
    """Runs the command-line program."""
    program = Main(Main.__get_options())
    program.run()
    return program.exit_code

  @staticmethod
  def apis_to_resource_filter(api_list):
    """Return a dictionary of apis and distinct resource filters for each."""
    roots = {}
    for elem in api_list:
      parts = elem.split('.', 1)
      root = parts[0]
      value = r'\*' if len(parts) == 1 else re.escape(parts[1])
      value = value.replace(r'\*', '.*')
      if root in roots:
        roots[root].add(re.compile(value))
      else:
        roots[root] = set([re.compile(value)])
    return roots

  def run(self):
    """Run the commandline program."""
    apis = self.__options.apis
    if apis == ['all']:
      apis = self.version_map.keys()

    self.__api_to_resource_regexs = self.apis_to_resource_filter(apis)
    bad_apis = [api for api in self.__api_to_resource_regexs.keys()
                if not api in self.version_map]
    if bad_apis:
      print 'Unknown apis: {0}'.format(', '.join(['"{0}"'.format(s)
                                                  for s in bad_apis]))
      self.__exit_code = -1
      return

    self.process_commands()

    options = self.__options
    if options.output_path and self.__aggregated_listings:
      with open(options.output_path, 'wb+') as f:
        pickler = pickle.Pickler(f)
        pickler.dump(self.__aggregated_listings)

    before = None
    after = None
    if options.compare:
      print 'Compare {0} vs {1}'.format(*options.compare)
      with open(options.compare[0], 'rb+') as f:
        unpickler = pickle.Unpickler(f)
        before = unpickler.load()
      with open(options.compare[1], 'rb+') as f:
        unpickler = pickle.Unpickler(f)
        after = unpickler.load()
      self.__processor.compare(before, after, self.__api_to_resource_regexs,
                               show_same=options.show_unchanged)

    if options.delete_added:
      for api in set(before.keys()).intersection(after.keys()):
        self.__processor.delete_added(api, self.version_map[api],
                                      before[api], after[api],
                                      self.__api_to_resource_regexs.get(api))

  def process_commands(self):
    """Run all the commands."""
    if self.__options.catalog:
      self.__foreach_api(self.do_command_print_catalog)

    if self.__options.print_api_spec:
      self.__foreach_api(self.do_command_print_api_spec)

    if self.__options.list:
      self.__foreach_api(self.do_command_collect_api)

  def __foreach_api(self, fn):
    """Execute a function for each api name of interest."""
    return {api: fn(api) for api in self.__api_to_resource_regexs.keys()}

  def __get_listable_unlistable_scope_map(self, api):
    """Determine the listable and unlistable methods and the scope to use."""
    if api not in self.__listable_unlistable_scope_cache:
      version = self.version_map[api]
      listable, unlistable = self.__explorer.find_listable_resources(
          api, version, self.__api_to_resource_regexs[api])
      scope_map = self.__explorer.determine_scope_map(listable, api)
      self.__listable_unlistable_scope_cache[api] = (listable, unlistable,
                                                     scope_map)
    return self.__listable_unlistable_scope_cache[api]

  def do_command_print_catalog(self, api):
    """Print all the listable (and non-listable) resources of the api."""
    listable, unlistable, scope_map = (
        self.__get_listable_unlistable_scope_map(api))
    text = self.__explorer.stringify_api(api, listable, unlistable, scope_map)
    print text
    return text

  def do_command_print_api_spec(self, api):
    """Print the list method specification for each of the API resources."""
    # pylint: disable=unused-variable
    listable, unlistable, scope_map = (
        self.__get_listable_unlistable_scope_map(api))
    text = '\n'.join(['LISTABLE Resources for {api}'.format(api=api),
                      to_json_string(listable)])
    print text
    return text

  def do_command_collect_api(self, api):
    """Collect all the instances of each of the api resources."""
    # pylint: disable=unused-variable
    listable, unlistable, scope_map = (
        self.__get_listable_unlistable_scope_map(api))
    version = self.version_map[api]

    print 'API:  "{0}"'.format(api)
    found, errors = self.__processor.list_api(api, version, scope_map)
    if errors:
      print 'ERRORS:{0}'.format(
          ''.join(['\n  E {0} {1}'.format(resource, msg)
                   for resource, msg in errors.items()]))
    self.__aggregated_listings[api] = found
    for resource, resource_list in found.items():
      print resource_list.stringify(resource)
    return found

if __name__ == '__main__':
  exit(Main.main())
