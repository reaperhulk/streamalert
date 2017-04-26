'''
Copyright 2017-present, Airbnb Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
'''

import csv
import json
import logging
import re
import StringIO
import zlib

from abc import ABCMeta, abstractmethod
from collections import OrderedDict
from fnmatch import fnmatch

import jsonpath_rw

logging.basicConfig()
LOGGER = logging.getLogger('StreamAlert')

def get_parser(parserid):
    """Helper method to fetch parser classes

    Args:
        parserid: the name of the parser class to get

    Returns:
        - A Parser class
    """
    return PARSERS[parserid]

PARSERS = {}
def parser(cls):
    """Class decorator to register parsers"""
    PARSERS[cls.__parserid__] = cls
    return cls

class ParserBase:
    """Abstract Parser class to be inherited by all StreamAlert Parsers"""
    __metaclass__ = ABCMeta
    __parserid__ = ''

    def __init__(self, schema, options):
        """Setup required parser properties

        Args:
            schema: Dict of log data schema.
            options: Parser options dict - delimiter, separator, or log_patterns
        """
        self.schema = schema
        self.options = options or {}

    @abstractmethod
    def parse(self, data):
        """Main parser method to be overridden by all Parser classes

        Args:
            data [str or dict]: Data to be parsed.

        Returns:
            [list] A list of dictionaries representing parsed records.
        """
        pass

    def type(self):
        """Returns the type of parser. Overriden in GzipJSONParser to just return json"""
        return self.__parserid__

    def matched_log_pattern(self, record, log_patterns):
        """Return True if all log patterns of this record match"""
        # Return False immediately if there are no log patterns
        # or if the data being tested is not a dict
        if not (log_patterns and isinstance(record, dict)):
            return False

        pattern_result = []
        for field, pattern_list in log_patterns.iteritems():
            # handle nested log_patterns
            if isinstance(pattern_list, dict):
                return self.matched_log_pattern(record[field], pattern_list)

            if not isinstance(pattern_list, list):
                LOGGER.debug('designated log_patterns should be a \'list\'')
                continue

            # the pattern field value in the record
            try:
                value = record[field]
            except (KeyError, TypeError):
                LOGGER.debug('declared log pattern field [%s] is not a valid field '
                             'for this record: %s', field, record)
                continue
            # append the result of any of the log_patterns being True
            pattern_result.append(any(fnmatch(value, pattern)
                                      for pattern in pattern_list))

        LOGGER.debug('%s pattern result: %s', self.type(), pattern_result)

        # if all pattern group results are True
        return all(pattern_result)


@parser
class JSONParser(ParserBase):
    __parserid__ = 'json'

    def _key_check(self, json_records):
        """Verify the declared schema matches the json payload

        Args:
            json_records [list]: List of dictionaries representing JSON payloads

        If keys do not match per the schema, records are removed from the
        passed in json_records list
        """
        schema_keys = set(self.schema.keys())
        valid_records = []
        schema_match = False

        for json_record in json_records:
            json_keys = set(json_record.keys())
            if json_keys == schema_keys:
                schema_match = True
                for key, key_type in self.schema.iteritems():
                    # If the value is a map of defined key/value pairs
                    if key_type and isinstance(key_type, dict):
                        # subkey check
                        schema_match = (set(json_record[key].keys()) == set(self.schema[key].keys()))

            else:
                LOGGER.debug('JSON Key mismatch: %s vs. %s', json_keys, schema_keys)

            if schema_match:
                valid_records.append(json_record)

        return valid_records

    def _parse_records(self, json_payload):
        """Iterate over a json_payload. Identify and extract nested payloads.
        Nested payloads can be detected with log_patterns (`records` should be a
        JSONpath selector that yields the desired nested records).

        If desired, fields present on the root record can be merged into child
        events using the `envelope` option.

        Args:
            json_payload [dict]: The parsed json data

        Returns:
            [list] A list of dictionaries representing parsed records.
        """
        # Check options and return the payload if there is nothing special to do
        if not self.options:
            return [json_payload]

        optional_keys = self.options.get('optional_top_level_keys')
        # Handle optional keys
        if self.options and optional_keys:
            # Note: This function exists because dict/OrderedDict cannot
            #       be keys in a dictionary.
            def default_optional_values(key):
                """Return a default value for a given schema type"""
                if key == 'string':
                    return str()
                elif key == 'integer':
                    return int()
                elif key == 'float':
                    return float()
                elif key == 'boolean':
                    return bool()
                elif key == []:
                    return list()
                elif key == OrderedDict():
                    return dict()

            for key_name, value_type in optional_keys.iteritems():
                # Update the schema to ensure the record is valid
                self.schema.update({key_name: value_type})
                # If the optional key isn't in our parsed json payload
                if key_name not in json_payload:
                    # Set default value
                    json_payload[key_name] = default_optional_values(value_type)

        json_records = []
        records_schema = self.options.get('json_path')
        # Handle jsonpath extraction of records
        if self.options and records_schema:
            envelope = {}
            envelope_schema = self.options.get('envelope_keys', {})
            if len(envelope_schema):
                self.schema.update({'stream_log_envelope': envelope_schema})
                envelope_keys = envelope_schema.keys()
                envelope_jsonpath = jsonpath_rw.parse("$." + ",".join(envelope_keys))
                envelope_matches = [match.value for match in envelope_jsonpath.find(json_payload)]
                envelope = dict(zip(envelope_keys, envelope_matches))

            records_jsonpath = jsonpath_rw.parse(records_schema)
            for match in records_jsonpath.find(json_payload):
                record = match.value
                if len(envelope):
                    record.update({'stream_log_envelope': envelope})

                json_records.append(record)

        if not json_records:
            json_records.append(json_payload)

        return json_records

    def parse(self, data):
        """Parse a string into a list of JSON payloads.

        Args:
            data [str or dict]: Data to be parsed.

        Returns:
            [list] A list of dictionaries representing parsed records.
            [boolean] False if the data is not JSON or the data does not follow the schema.
        """
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError as err:
                LOGGER.debug('JSON parse failed: %s', str(err))
                return False

        json_records = self._parse_records(data)
        valid_records = self._key_check(json_records)

        if len(valid_records):
            return valid_records

        return False

@parser
class GzipJSONParser(JSONParser):
    __parserid__ = 'gzip-json'

    def parse(self, data):
        """Parse a gzipped string into JSON.

        Args:
            data [str]: Data to be parsed.

        Returns:
            [list] A list of dictionaries representing parsed records.
            [boolean] False if the data is not Gzipped JSON or the columns do not match.
        """
        try:
            data = zlib.decompress(data, 47)
            return super(GzipJSONParser, self).parse(data)
        except zlib.error:
            return False

    def type(self):
        """Return the parserid for the super of this (json, not gzip-json)"""
        return super(GzipJSONParser, self).__parserid__

@parser
class CSVParser(ParserBase):
    __parserid__ = 'csv'
    __default_delimiter = ','

    def _get_reader(self, data):
        """Return the CSV reader for the given payload source

        Returns:
            [StringIO] CSV reader object if the parse was successful
            [boolean] False if parse was unsuccessful
        """
        delimiter = self.options.get('delimiter', self.__default_delimiter)

        # TODO(ryandeivert): either subclass a current parser or add a new
        # parser to support parsing CSV data that contains a header line
        try:
            csv_data = StringIO.StringIO(data)
            reader = csv.reader(csv_data, delimiter=delimiter)
        except (ValueError, csv.Error):
            return False

        return reader

    def parse(self, data):
        """Parse a string into a comma separated value reader object.

        Args:
            data [str]: Data to be parsed.

        Returns:
            [list] A list of dictionaries representing parsed records.
            [boolean] False if the data is not CSV or the columns do not match.
        """
        reader = self._get_reader(data)
        if not reader:
            return False

        csv_payloads = []
        try:
            for row in reader:
                # check number of columns match
                if len(row) != len(self.schema):
                    LOGGER.debug('csv key mismatch: %s vs. %s', len(row), len(self.schema))
                    return False

                # extract the keys from the row via the index
                csv_payloads.append({key: row[index] for index, key in enumerate(self.schema)})

            return csv_payloads
        except csv.Error:
            return False


@parser
class KVParser(ParserBase):
    __parserid__ = 'kv'
    __default_separator = '='
    __default_delimiter = ' '

    def parse(self, data):
        """Parse a key value string into a dictionary.

        Args:
            data [str]: Data to be parsed.

        Returns:
            [list] A list of dictionaries representing parsed records.
            [boolean] False if the columns do not match.
        """
        # get the delimiter (character between key/value pairs) and the
        # separator (the character between keys and values)
        delimiter = self.options.get('delimiter', self.__default_delimiter)
        separator = self.options.get('separator', self.__default_separator)

        kv_payload = {}
        try:
            # remove any blank strings that may exist in our list
            fields = filter(None, data.split(delimiter))
            # first check the field length matches our # of keys
            if len(fields) != len(self.schema):
                LOGGER.debug('KV field length mismatch: %s vs %s', fields, self.schema)
                return False

            regex = re.compile('.+{}.+'.format(separator))
            for index, field in enumerate(fields):
                # verify our fields match the kv regex
                if regex.match(field):
                    key, value = field.split(separator)
                    # handle duplicate keys
                    if key in kv_payload:
                        # load key from our configuration
                        kv_payload[self.schema.keys()[index]] = value
                    else:
                        # load key from data
                        kv_payload[key] = value
                else:
                    LOGGER.error('key/value regex failure for %s', field)

            return [kv_payload]
        except UnicodeDecodeError:
            return False


@parser
class SyslogParser(ParserBase):
    __parserid__ = 'syslog'

    def parse(self, data):
        """Parse a syslog string into a dictionary

        Matches syslog events with the following format:
            timestamp(Month DD HH:MM:SS) host application: message
        Example(s):
            Jan 10 19:35:33 vagrant-ubuntu-trusty-64 sudo: session opened for root
            Jan 10 19:35:13 vagrant-ubuntu-precise-32 ssh[13941]: login for mike

        Args:
            data: Data to be parsed

        Returns:
            - A list of syslog records.
            - False if the data does not match the syslog regex.
        """
        syslog_regex = re.compile(r"(?P<timestamp>^\w{3}\s\d{2}\s(\d{2}:?)+)\s"
                                  r"(?P<host>(\w[-]*)+)\s"
                                  r"(?P<application>\w+)(\[\w+\])*:\s"
                                  r"(?P<message>.*$)")

        match = syslog_regex.search(data)
        if not match:
            return False

        return [{key: match.group(key) for key in self.schema.keys()}]
