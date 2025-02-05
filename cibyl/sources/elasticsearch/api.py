"""
#    Copyright 2022 Red Hat
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""

import logging
import re
from functools import partial
from typing import Callable, Dict, List, Union
from urllib.parse import urlsplit

from elasticsearch.helpers import scan
from xsdata.formats.dataclass.parsers import XmlParser

from cibyl.cli.argument import Argument
from cibyl.cli.ranged_argument import RANGE_OPERATORS
from cibyl.exceptions.elasticsearch import ElasticSearchError
from cibyl.models.attribute import AttributeDictValue
from cibyl.models.ci.base.build import Build
from cibyl.models.ci.base.job import Job
from cibyl.models.ci.base.test import Test
from cibyl.sources.elasticsearch.client import ElasticSearchClient
from cibyl.sources.server import ServerSource
from cibyl.sources.source import speed_index
from cibyl.sources.zuul.utils.tests.tempest.parser import XMLTempestTestSuite
from cibyl.utils.filtering import (apply_filters, matches_regex,
                                   satisfy_case_insensitive_match,
                                   satisfy_exact_match, satisfy_regex_match)
from cibyl.utils.models import has_builds_job, has_tests_job

LOG = logging.getLogger(__name__)


# shorthand type for the representation returned by elasticsearch
ElkJob = Dict[str, Union[str, dict]]


def get_build_filters(**kwargs: Argument) -> List[Callable]:
    """Get a list of functions that should be used to filter the builds,
    according to user input."""
    checks_to_apply = []
    builds_arg = kwargs.get('builds')
    if builds_arg and builds_arg.value:
        checks_to_apply.append(partial(satisfy_exact_match,
                                       user_input=builds_arg,
                                       field_to_check="build_num"))

    build_status = kwargs.get('build_status')
    if build_status and build_status.value:
        checks_to_apply.append(partial(satisfy_case_insensitive_match,
                                       user_input=build_status,
                                       field_to_check="build_result"))
    return checks_to_apply


def filter_jobs(jobs_found: List[ElkJob], **kwargs) -> List[ElkJob]:
    """Filter the result from the Jenkins API according to user input"""
    checks_to_apply = []

    jobs_arg = kwargs.get('jobs')
    if jobs_arg and jobs_arg.value:
        pattern = re.compile("|".join(jobs_arg.value))
        checks_to_apply.append(partial(satisfy_regex_match, pattern=pattern,
                                       field_to_check="job_name"))

    jobs_scope_arg = kwargs.get('jobs_scope')
    if jobs_scope_arg:
        pattern = re.compile(jobs_scope_arg)
        checks_to_apply.append(partial(satisfy_regex_match, pattern=pattern,
                                       field_to_check="job_name"))

    spec_jobs_name_arg = kwargs.get('spec')
    if spec_jobs_name_arg and spec_jobs_name_arg.value:
        checks_to_apply.append(partial(satisfy_exact_match,
                                       user_input=spec_jobs_name_arg,
                                       field_to_check="job_name"))

    return apply_filters(jobs_found, *checks_to_apply)


def filter_builds(builds_found: List[Dict],
                  checks_to_apply: List[Callable]) -> List[Dict]:

    """Filter the result from ElasticSearch according to user input
    :param builds_found: Collection of builds to filter
    :param: checks_to_apply: List of function that the builds should satisfy
    :returns: The builds that satisfy all the conditions included in the checks
    functions
    """

    for build in builds_found:
        # ensure that the build number is passed as a string, Jenkins usually
        # sends it as an int
        build["build_num"] = str(build["build_num"])

    return apply_filters(builds_found, *checks_to_apply)


class ElasticSearch(ServerSource):
    """Elasticsearch Source"""

    def __init__(self, driver: str = 'elasticsearch',
                 name: str = "elasticsearch", priority: int = 0,
                 elastic_client: object = None,
                 enabled: bool = True, url: str = None) -> None:
        super().__init__(name=name, driver=driver, priority=priority,
                         enabled=enabled)
        self.url = url
        self.es_client = elastic_client
        try:
            url_parsed = urlsplit(self.url)
            self.host = f"{url_parsed.scheme}://{url_parsed.hostname}"
            self.port = url_parsed.port
        except ValueError as exception:
            raise ElasticSearchError(
                'The URL given is not valid'
            ) from exception

    def setup(self) -> None:
        """ Ensure that a connection to the elasticsearch server can be
        established.
        """
        if self.es_client is None:
            self.es_client = ElasticSearchClient(
                self.host,
                self.port
            ).connect()

    def teardown(self) -> None:
        if self.es_client:
            self.es_client.disconnect()

    @speed_index({'base': 1})
    def get_jobs(self, **kwargs: Argument) -> AttributeDictValue:
        """Get jobs from elasticsearch

            :returns: Job objects queried from elasticsearch
            :rtype: :class:`AttributeDictValue`
        """

        # Empty query for all hits or elements
        query_body = {
            "query": {
                "match_all": {}
            },
            "_source": ["job_name", "job_url"]
        }

        hits = self.__query_get_hits(
            query=query_body,
            index='logstash_jenkins_jobs_cibyl'
        )

        # make the hits list a flat list of dicts with the job information for
        # easier filtering
        hits = [hit['_source'] for hit in hits]
        hits = filter_jobs(hits, **kwargs)
        job_objects = {}
        for hit in hits:
            job_name = hit['job_name']
            url = hit['job_url']
            job_objects[job_name] = Job(name=job_name, url=url)
        return AttributeDictValue("jobs", attr_type=Job, value=job_objects)

    def __query_get_hits(self,
                         query: dict,
                         index: str = '*') -> list:
        """Perform the search query to ElasticSearch
        and return all the hits

        :param query: Query to perform
        :type query: dict
        :param index: Index
        :type index: str
        :return: List of hits.
        """
        try:
            LOG.debug("Using the following query: %s",
                      str(query).replace("'", '"'))
            # https://github.com/elastic/elasticsearch-py/issues/91
            # For aggregations we should use the search method of the client
            if 'aggs' in query:
                results = self.es_client.connection.search(
                    index=index,
                    body=query,
                    size=10000,
                )
                aggregation_key = list(results['aggregations'].keys())[0]
                buckets = results['aggregations'][aggregation_key]['buckets']
                return buckets
            # For normal queries we can use the scan helper
            hits = [item for item in scan(
                self.es_client.connection,
                index=index,
                query=query,
                size=10000
            )]
            return hits
        except Exception as exception:
            raise ElasticSearchError(
                "Error getting the results."
            ) from exception

    @speed_index({'base': 2})
    def get_builds(self, **kwargs: Argument) -> AttributeDictValue:
        """
            Get builds from elasticsearch server.

            :returns: container of jobs with build information from
            elasticsearch server
        """
        # Empty query for all hits or elements
        query_body = {
            "query": {
                "match_all": {}
            },
            "_source": ["job_name", "job_url", "build_num", "build_result",
                        "build_duration"]
        }

        hits = self.__query_get_hits(
            query=query_body,
            index='logstash_jenkins_jobs_cibyl'
        )

        # keep track if there is any flag that would
        # cause builds to be filtered, to remove later jobs that are empty due
        # to this filtering
        filtering_builds = False
        build_filters = get_build_filters(**kwargs)
        filtering_builds = bool(build_filters)

        # make the hits list a flat list of dicts with the job information for
        # easier filtering
        hits = [hit['_source'] for hit in hits]
        hits = filter_jobs(hits, **kwargs)
        hits = filter_builds(hits, build_filters)
        jobs_found = {}
        for build in hits:
            job_name = build['job_name']
            url = build['job_url']
            if job_name not in jobs_found:
                # ensure that job is created
                jobs_found[job_name] = Job(name=job_name, url=url)

            build_result = build.get('build_result')
            build_id = str(build['build_num'])
            build_duration = build.get('build_duration')
            if build_duration is not None:
                build_duration = int(build_duration)
            jobs_found[job_name].add_build(Build(build_id,
                                                 build_result,
                                                 build_duration))

        final_jobs = jobs_found
        if filtering_builds:
            # if there was some argument that leads to filtering out tests,
            # make sure that the output jobs have at least one test
            final_jobs = {job_name: job for job_name, job in
                          jobs_found.items() if has_builds_job(job)}
        jobs_found = AttributeDictValue("jobs", attr_type=Job,
                                        value=final_jobs)

        if 'last_build' in kwargs:
            return self.get_last_build(jobs_found)

        return jobs_found

    def get_last_build(self,
                       builds_jobs: AttributeDictValue) -> AttributeDictValue:
        """
            Get last build from builds. It's determined
            by the build_id

            :returns: container of jobs with last build information
        """
        job_object = {}
        for job_name, build_info in builds_jobs.items():
            job_url = builds_jobs[job_name].url.value
            builds = build_info.builds

            if not builds:
                continue

            last_build_number = sorted(builds.keys(), key=int)[-1]
            last_build_info = builds[last_build_number]
            job_object[job_name] = Job(name=job_name, url=job_url)
            job_object[job_name].add_build(last_build_info)

        return AttributeDictValue("jobs", attr_type=Job, value=job_object)

    @speed_index({'base': 3})
    def get_tests(self, **kwargs: Argument) -> AttributeDictValue:
        """
            Get tests for a elasticsearch job.

            :returns: container of jobs with the last completed build
            (if any) and the tests
        """
        self.check_builds_for_test(**kwargs)

        # Empty query for all hits or elements
        query_body = {
            "query": {
                "match_all": {}
            },
            "_source": ["job_name", "job_url", "build_num", "build_result",
                        "build_duration", "test_results_*"]
        }
        hits = self.__query_get_hits(
            query=query_body,
            index='logstash_jenkins_jobs_cibyl'
        )
        # make the hits list a flat list of dicts with the job information for
        # easier filtering
        hits = [hit['_source'] for hit in hits]
        build_filters = get_build_filters(**kwargs)
        hits = filter_jobs(hits, **kwargs)
        hits = filter_builds(hits, build_filters)

        # keep track if there is any flag that would
        # cause tests to be filtered, to remove later jobs that are empty due
        # to this filtering
        tests_filtering = False
        tests_pattern = None
        if 'tests' in kwargs and kwargs['tests'].value:
            tests_filtering = True
            tests_pattern = re.compile("|".join(kwargs['tests'].value))

        test_result_argument = []
        if 'test_result' in kwargs:
            test_result_argument = [status.upper()
                                    for status in
                                    kwargs.get('test_result').value]
            tests_filtering |= bool(test_result_argument)

        test_duration_arguments = []
        if 'test_duration' in kwargs:
            test_duration_arguments = kwargs.get('test_duration').value
            tests_filtering |= bool(test_duration_arguments)
        jobs_found = {}
        for build in hits:
            job_name = build['job_name']
            url = build['job_url']
            if job_name not in jobs_found:
                # ensure that job is created
                jobs_found[job_name] = Job(name=job_name, url=url)
            build_result = build.get('build_result')
            build_id = str(build['build_num'])
            build_duration = build.get('build_duration')
            if build_duration is not None:
                build_duration = int(build_duration)
            # try adding the build, if it's already there, the information will
            # simply be merged in add_build
            jobs_found[job_name].add_build(Build(build_id,
                                                 build_result,
                                                 build_duration))
            test_suites = [key for key in build if
                           key.startswith("test_results_")]
            xml_parser = XmlParser()
            for test_suite in test_suites:
                tests = xml_parser.from_string(build[test_suite],
                                               XMLTempestTestSuite)

                for test in tests.testcase:
                    test_name = test.name
                    test_status = "SUCCESS"
                    if test.skipped:
                        test_status = "SKIPPED"
                    if test.failure:
                        test_status = "FAILURE"
                    class_name = test.classname
                    test_duration = test.time
                    # check if necessary to filter by test name or by
                    # test class name:
                    if tests_pattern:
                        matches_test_name = matches_regex(tests_pattern,
                                                          test_name)
                        matches_test_class = (class_name is not None and
                                              matches_regex(tests_pattern,
                                                            class_name))
                        if not (matches_test_class or matches_test_name):
                            continue
                    # Check if necessary filter by Test Status:
                    if test_result_argument and \
                            test_status not in test_result_argument:
                        continue

                    if test_duration_arguments and \
                       not self.match_filter_test_by_duration(
                           test_duration,
                           test_duration_arguments):
                        continue

                    # Duration comes in seconds. Convert to ms:
                    if test_duration:
                        test_duration *= 1000

                    jobs_found[job_name].builds[build_id].add_test(
                        Test(
                            name=test_name,
                            result=test_status,
                            duration=test_duration,
                            class_name=class_name
                        )
                    )

        final_jobs = jobs_found
        if tests_filtering:
            # if there was some argument that leads to filtering out tests,
            # make sure that the output jobs have at least one test
            final_jobs = {job_name: job for job_name, job in
                          jobs_found.items() if has_tests_job(job)}

        if 'last_build' in kwargs:
            # if user requested last_build, make sure we only send that
            return self.get_last_build(final_jobs)

        return AttributeDictValue("jobs", attr_type=Job, value=final_jobs)

    def match_filter_test_by_duration(self,
                                      test_duration: float,
                                      test_duration_arguments: list) -> bool:
        """Match if the duration of a test pass all the
        conditions provided by the user that are located
        in the arguments

        :params test_duration: Duration of a job
        :type node_name: float
        :params test_duration_arguments: Conditions provided
        by the user
        :type list: str

        :returns: Return if match all the conditions or no
        :rtype: bool
        """
        for test_duration_argument in test_duration_arguments:
            operator = RANGE_OPERATORS[test_duration_argument.operator]
            operand = test_duration_argument.operand
            if not operator(test_duration, float(operand)):
                return False
        return True
