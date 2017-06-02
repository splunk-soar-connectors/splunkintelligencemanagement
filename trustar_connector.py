# --
# File: trustar/trustar_connector.py
#
# Copyright (c) Phantom Cyber Corporation, 2017
#
# This unpublished material is proprietary to Phantom Cyber.
# All rights reserved. The methods and
# techniques described herein are considered trade secrets
# and/or confidential. Reproduction or distribution, in whole
# or in part, is forbidden except by express written permission
# of Phantom Cyber Corporation.
#
# --

# Standard library imports
import datetime
import json
import os
import math
import hashlib
import socket
import requests
import pytz
import dateutil.parser

# Phantom imports
import phantom.app as phantom
from phantom.base_connector import BaseConnector
from phantom.action_result import ActionResult

# Local imports
import trustar_consts as consts

app_dir = os.path.dirname(os.path.abspath(__file__))  # noqa
if os.path.exists('{}/tzlocal'.format(app_dir)):  # noqa
    os.sys.path.insert(0, '{}/dependencies/tzlocal'.format(app_dir))  # noqa
    os.sys.path.insert(0, '{}/dependencies'.format(app_dir))  # noqa
from tzlocal import get_localzone  # pylint: disable=E0401

# Dictionary containing details of possible HTTP error codes in API Response
ERROR_RESPONSE_DICT = {
    consts.TRUSTAR_REST_RESP_BAD_REQUEST: consts.TRUSTAR_REST_RESP_BAD_REQUEST_MSG,
    consts.TRUSTAR_REST_RESP_UNAUTHORIZED: consts.TRUSTAR_REST_RESP_UNAUTHORIZED_MSG,
    consts.TRUSTAR_REST_RESP_RESOURCE_NOT_FOUND: consts.TRUSTAR_REST_RESP_RESOURCE_NOT_FOUND_MSG,
    consts.TRUSTAR_REST_RESP_TOO_LONG: consts.TRUSTAR_REST_RESP_TOO_LONG_MSG,
    consts.TRUSTAR_REST_RESP_INTERNAL_SERVER_ERROR: consts.TRUSTAR_REST_RESP_INTERNAL_SERVER_ERROR_MSG,
    consts.TRUSTAR_REST_RESP_GATEWAY_TIMEOUT: consts.TRUSTAR_REST_RESP_GATEWAY_TIMEOUT_MSG
}


def _break_ip_address(cidr_ip_address):
    """ Function divides the input parameter into IP address and network mask.

    :param cidr_ip_address: IP address in format of IP/prefix_size
    :return: IP, prefix_size
    """

    if "/" in cidr_ip_address:
        ip_address, prefix_size = cidr_ip_address.split("/")
    else:
        ip_address = cidr_ip_address
        prefix_size = 0

    return ip_address, int(prefix_size)


def _is_ipv6(ip_address):
    """ Function that checks given address and return True if address is IPv6 address.

    :param ip_address: input parameter IP address
    :return: status (success/failure)
    """

    try:
        # Validating IPv6 address
        socket.inet_pton(socket.AF_INET6, ip_address)
    except socket.error:
        return False

    return True


class TrustarConnector(BaseConnector):
    """ This is an AppConnector class that inherits the BaseConnector class. It implements various actions supported by
    TruSTAR and helper methods required to run the actions.
    """

    def __init__(self):

        # Calling the BaseConnector's init function
        super(TrustarConnector, self).__init__()
        self._url = None
        self._client_id = None
        self._client_secret = None
        self._access_token = None
        self._app_state = dict()

        return

    def initialize(self):
        """ This is an optional function that can be implemented by the AppConnector derived class. Since the
        configuration dictionary is already validated by the time this function is called, it's a good place to do any
        extra initialization of any internal modules. This function MUST return a value of either phantom.APP_SUCCESS or
        phantom.APP_ERROR. If this function returns phantom.APP_ERROR, then AppConnector::handle_action will not get
        called.
        """

        # Get configuration dictionary
        config = self.get_config()
        self._url = config[consts.TRUSTAR_CONFIG_URL].strip('/')
        self._client_id = config[consts.TRUSTAR_CONFIG_CLIENT_ID]
        self._client_secret = config[consts.TRUSTAR_CONFIG_CLIENT_SECRET]
        # Load the state of app stored in JSON file
        self._app_state = self.load_state()
        # Custom validation for IP address
        self.set_validator(consts.TRUSTAR_HUNT_IP_PARAM, self._is_ip)

        return phantom.APP_SUCCESS

    def _is_ip(self, cidr_ip_address):
        """ Function that checks given address and return True if address is valid IPv4/IPv6 address.

        :param cidr_ip_address: IP/CIDR
        :return: status (success/failure)
        """

        try:
            ip_address, net_mask = _break_ip_address(cidr_ip_address)
        except Exception as e:
            self.debug_print(consts.TRUSTAR_IP_VALIDATION_FAILED, e)
            return False

        # Validate IP address
        if not (phantom.is_ip(ip_address) or _is_ipv6(ip_address)):
            self.debug_print(consts.TRUSTAR_IP_VALIDATION_FAILED)
            return False

        # Check if net mask is out of range
        if (":" in ip_address and net_mask not in range(0, 129)) or ("." in ip_address and
                                                                     net_mask not in range(0, 33)):
            self.debug_print(consts.TRUSTAR_IP_VALIDATION_FAILED)
            return False

        return True

    def _make_rest_call(self, endpoint, action_result, headers=None, params=None, data=None, method="get",
                        timeout=None, auth=None):
        """ Function that makes the REST call to the device. It is a generic function that can be called from various
        action handlers.

        :param endpoint: REST endpoint that needs to appended to the service address
        :param action_result: object of ActionResult class
        :param headers: request headers
        :param params: request parameters if method is get
        :param data: request body
        :param method: get/post/put/delete ( Default method will be 'get' )
        :param timeout: request timeout
        :param auth: client credentials
        :return: status success/failure(along with appropriate message), response obtained by making an API call
        """

        response_data = None

        try:
            request_func = getattr(requests, method)
        except AttributeError:
            self.debug_print(consts.TRUSTAR_ERR_API_UNSUPPORTED_METHOD.format(method=method))
            # Set the action_result status to error, the handler function will most probably return as is
            return action_result.set_status(
                phantom.APP_ERROR, consts.TRUSTAR_ERR_API_UNSUPPORTED_METHOD, method=method
            ), response_data
        except Exception as e:
            self.debug_print(consts.TRUSTAR_EXCEPTION_OCCURRED, e)
            # Set the action_result status to error, the handler function will most probably return as is
            return action_result.set_status(phantom.APP_ERROR, consts.TRUSTAR_EXCEPTION_OCCURRED, e), response_data

        try:
            # For all actions
            if auth is None:
                auth_headers = {"Authorization": "Bearer {token}".format(token=self._access_token)}
                # Update headers
                if headers:
                    auth_headers.update(headers)
                response = request_func("{base_url}{endpoint}".format(base_url=self._url, endpoint=endpoint),
                                        params=params, headers=auth_headers, data=data, verify=False,
                                        timeout=timeout)
            # For generating API token
            else:
                response = request_func("{base_url}{endpoint}".format(base_url=self._url, endpoint=endpoint),
                                        auth=auth, data=data, verify=False, timeout=timeout)
        except Exception as e:
            self.debug_print(consts.TRUSTAR_ERR_SERVER_CONNECTION, e)
            # Set the action_result status to error, the handler function will most probably return as is
            return action_result.set_status(phantom.APP_ERROR, consts.TRUSTAR_ERR_SERVER_CONNECTION, e), response_data

        # Store response status_code, text and headers in debug data, it will get dumped in the logs
        if hasattr(action_result, 'add_debug_data'):
            if response is not None:
                action_result.add_debug_data({'r_status_code': response.status_code})
                action_result.add_debug_data({'r_text': response.text})
                action_result.add_debug_data({'r_headers': response.headers})
            else:
                action_result.add_debug_data({'r_text': 'r is None'})

        # Try parsing the json
        try:
            content_type = response.headers.get("content-type")
            if content_type and content_type.find("json") != -1:
                response_data = response.json()
            else:
                response_data = response.text

        except Exception as e:
            # r.text is guaranteed to be NON None, it will be empty, but not None
            msg_string = consts.TRUSTAR_ERR_JSON_PARSE.format(raw_text=response.text)
            self.debug_print(msg_string, e)
            # Set the action_result status to error, the handler function will most probably return as is
            return action_result.set_status(phantom.APP_ERROR, msg_string, e), response_data

        if response.status_code in ERROR_RESPONSE_DICT:
            message = ERROR_RESPONSE_DICT[response.status_code]

            # Overriding message if available in response
            if isinstance(response_data, dict):
                message = response_data.get("message", message)

            self.debug_print(consts.TRUSTAR_ERR_FROM_SERVER.format(status=response.status_code, detail=message))
            # Set the action_result status to error, the handler function will most probably return as is
            return action_result.set_status(phantom.APP_ERROR, consts.TRUSTAR_ERR_FROM_SERVER,
                                            status=response.status_code, detail=message), response_data

        # In case of success scenario
        if response.status_code == consts.TRUSTAR_REST_RESP_SUCCESS:
            if isinstance(response_data, dict) or isinstance(response_data, list):
                return phantom.APP_SUCCESS, response_data

            # If response obtained is not in the desired format
            self.debug_print(consts.TRUSTAR_UNEXPECTED_RESPONSE.format(response=response_data))
            return action_result.set_status(phantom.APP_ERROR, consts.TRUSTAR_UNEXPECTED_RESPONSE.format(
                response=response_data
            )), response_data

        # If response code is unknown
        message = consts.TRUSTAR_REST_RESP_OTHER_ERROR_MSG

        if isinstance(response_data, dict):
            message = response_data.get("message", message)

        self.debug_print(consts.TRUSTAR_ERR_FROM_SERVER.format(status=response.status_code, detail=message))

        # All other response codes from REST call
        # Set the action_result status to error, the handler function will most probably return as is
        return action_result.set_status(phantom.APP_ERROR, consts.TRUSTAR_ERR_FROM_SERVER,
                                        status=response.status_code,
                                        detail=message), response_data

    def _generate_api_token(self, action_result):
        """ This function is used to generate token.

        :param action_result: object of ActionResult class
        :return: status success/failure
        """

        data = {'grant_type': 'client_credentials'}

        timeout = 30 if self.get_action_identifier() == "test_asset_connectivity" else None

        # Querying endpoint to generate token
        status, response = self._make_rest_call(consts.TRUSTAR_GENERATE_TOKEN_ENDPOINT, action_result, method="post",
                                                data=data, timeout=timeout, auth=(self._client_id, self._client_secret))

        # Something went wrong
        if phantom.is_fail(status):
            return action_result.get_status()

        # Get access token
        self._access_token = response.get("access_token")

        # Validate access token
        if not self._access_token:
            self.debug_print(consts.TRUSTAR_TOKEN_GENERATION_ERR)
            return action_result.set_status(phantom.APP_ERROR, consts.TRUSTAR_TOKEN_GENERATION_ERR)

        return phantom.APP_SUCCESS

    def _test_asset_connectivity(self, param):
        """ This function tests the connectivity of an asset with given credentials.

        :param param: (not used in this method)
        :return: status success/failure
        """

        action_result = ActionResult()
        self.save_progress(consts.TRUSTAR_CONNECTION_TEST_MSG)
        self.save_progress("Configured URL: {url}".format(url=self._url))

        # Generate token
        token_generation_status = self._generate_api_token(action_result)

        # Something went wrong while generating token
        if phantom.is_fail(token_generation_status):
            self.save_progress(action_result.get_message())
            self.set_status(phantom.APP_ERROR, consts.TRUSTAR_TEST_CONNECTIVITY_FAIL)
            return action_result.get_status()

        self.set_status_save_progress(phantom.APP_SUCCESS, consts.TRUSTAR_TEST_CONNECTIVITY_PASS)
        return action_result.get_status()

    def _hunt_correlated_reports(self, action_result, ioc_to_hunt):
        """ This action gets the list of correlated reports for the IOC provided.

        :param action_result: object of ActionResult class
        :param ioc_to_hunt: IOC to query
        :return: request status and response of the request
        """

        # Generate token
        token_generation_status = self._generate_api_token(action_result)

        # Something went wrong while generating token
        if phantom.is_fail(token_generation_status):
            return action_result.get_status(), None

        # Prepare request params
        params = {'q': ioc_to_hunt}

        # Make REST call
        return self._make_rest_call(consts.TRUSTAR_HUNT_ACTIONS_ENDPOINT, action_result, params=params)

    def _hunt_ip(self, param):
        """ Get list of all TruSTAR incident report IDs that correlate with the provided IP.

        :param param: dictionary on input parameters
        :return: status success/failure
        """

        action_result = self.add_action_result(ActionResult(dict(param)))
        summary_data = action_result.update_summary({})

        # Get mandatory parameters
        ip = param[consts.TRUSTAR_HUNT_IP_PARAM]

        # Get correlated reports
        resp_status, response = self._hunt_correlated_reports(action_result, ip)

        # Something went wrong
        if phantom.is_fail(resp_status):
            return action_result.get_status()

        # Update summary data
        summary_data["total_correlated_reports"] = len(response)
        if not summary_data["total_correlated_reports"]:
            summary_data["possible_reason"] = consts.TRUSTAR_REASON_FOR_REPORT_UNAVAILABILITY

        for report_id in response:
            action_result.add_data({"report_id": report_id})

        return action_result.set_status(phantom.APP_SUCCESS)

    def _hunt_url(self, param):
        """ Get list of all TruSTAR incident report IDs that correlate with the provided URL.

        :param param: dictionary on input parameters
        :return: status success/failure
        """

        action_result = self.add_action_result(ActionResult(dict(param)))
        summary_data = action_result.update_summary({})

        # Get mandatory parameters
        url = param[consts.TRUSTAR_HUNT_URL_PARAM]

        # Get correlated reports
        resp_status, response = self._hunt_correlated_reports(action_result, url)

        # Something went wrong
        if phantom.is_fail(resp_status):
            return action_result.get_status()

        # Update summary data
        summary_data["total_correlated_reports"] = len(response)
        if not summary_data["total_correlated_reports"]:
            summary_data["possible_reason"] = consts.TRUSTAR_REASON_FOR_REPORT_UNAVAILABILITY

        for report_id in response:
            action_result.add_data({"report_id": report_id})

        return action_result.set_status(phantom.APP_SUCCESS)

    def _hunt_domain(self, param):
        """ Get list of all TruSTAR incident report IDs that correlate with the provided domain.

        :param param: dictionary on input parameters
        :return: status success/failure
        """

        action_result = self.add_action_result(ActionResult(dict(param)))
        summary_data = action_result.update_summary({})

        # Get mandatory parameters
        domain = param[consts.TRUSTAR_HUNT_DOMAIN_PARAM]
        # Fetch domain from URL, if URL is provided
        if phantom.is_url(domain):
            domain = phantom.get_host_from_url(domain)

        # Get correlated reports
        resp_status, response = self._hunt_correlated_reports(action_result, domain)

        # Something went wrong
        if phantom.is_fail(resp_status):
            return action_result.get_status()

        # Update summary data
        summary_data["total_correlated_reports"] = len(response)
        if not summary_data["total_correlated_reports"]:
            summary_data["possible_reason"] = consts.TRUSTAR_REASON_FOR_REPORT_UNAVAILABILITY

        for report_id in response:
            action_result.add_data({"report_id": report_id})

        return action_result.set_status(phantom.APP_SUCCESS)

    def _hunt_file(self, param):
        """ Get list of all TruSTAR incident report IDs that correlate with the provided hash.

        :param param: dictionary on input parameters
        :return: status success/failure
        """

        action_result = self.add_action_result(ActionResult(dict(param)))
        summary_data = action_result.update_summary({})

        # Get mandatory parameters
        file_param = param[consts.TRUSTAR_HUNT_FILE_PARAM]

        # Get correlated reports
        resp_status, response = self._hunt_correlated_reports(action_result, file_param)

        # Something went wrong
        if phantom.is_fail(resp_status):
            return action_result.get_status()

        # Update summary data
        summary_data["total_correlated_reports"] = len(response)
        if not summary_data["total_correlated_reports"]:
            summary_data["possible_reason"] = consts.TRUSTAR_REASON_FOR_REPORT_UNAVAILABILITY

        for report_id in response:
            action_result.add_data({"report_id": report_id})

        return action_result.set_status(phantom.APP_SUCCESS)

    def _hunt_email(self, param):
        """ Get list of all TruSTAR incident report IDs that correlate with the provided email.

        :param param: dictionary on input parameters
        :return: status success/failure
        """

        action_result = self.add_action_result(ActionResult(dict(param)))
        summary_data = action_result.update_summary({})

        # Get mandatory parameters
        email = param[consts.TRUSTAR_HUNT_EMAIL_PARAM]

        # Get correlated reports
        resp_status, response = self._hunt_correlated_reports(action_result, email)

        # Something went wrong
        if phantom.is_fail(resp_status):
            return action_result.get_status()

        # Update summary data
        summary_data["total_correlated_reports"] = len(response)
        if not summary_data["total_correlated_reports"]:
            summary_data["possible_reason"] = consts.TRUSTAR_REASON_FOR_REPORT_UNAVAILABILITY

        for report_id in response:
            action_result.add_data({"report_id": report_id})

        return action_result.set_status(phantom.APP_SUCCESS)

    def _hunt_cve_number(self, param):
        """ Get list of all TruSTAR incident report IDs that correlate with the provided
         CVE(Common Vulnerability and Exposure) number.

        :param param: dictionary on input parameters
        :return: status success/failure
        """

        action_result = self.add_action_result(ActionResult(dict(param)))
        summary_data = action_result.update_summary({})

        # Get mandatory parameters
        cve_number = param[consts.TRUSTAR_HUNT_CVE_PARAM]

        # Get correlated reports
        resp_status, response = self._hunt_correlated_reports(action_result, cve_number)

        # Something went wrong
        if phantom.is_fail(resp_status):
            return action_result.get_status()

        # Update summary data
        summary_data["total_correlated_reports"] = len(response)
        if not summary_data["total_correlated_reports"]:
            summary_data["possible_reason"] = consts.TRUSTAR_REASON_FOR_REPORT_UNAVAILABILITY

        for report_id in response:
            action_result.add_data({"report_id": report_id})

        return action_result.set_status(phantom.APP_SUCCESS)

    def _hunt_malware(self, param):
        """ Get list of all TruSTAR incident report IDs that correlate with the provided
         Malware.

        :param param: dictionary on input parameters
        :return: status success/failure
        """

        action_result = self.add_action_result(ActionResult(dict(param)))
        summary_data = action_result.update_summary({})

        # Get mandatory parameters
        malware = param[consts.TRUSTAR_HUNT_MALWARE_PARAM]

        # Get correlated reports
        resp_status, response = self._hunt_correlated_reports(action_result, malware)

        # Something went wrong
        if phantom.is_fail(resp_status):
            return action_result.get_status()

        # Update summary data
        summary_data["total_correlated_reports"] = len(response)
        if not summary_data["total_correlated_reports"]:
            summary_data["possible_reason"] = consts.TRUSTAR_REASON_FOR_REPORT_UNAVAILABILITY

        for report_id in response:
            action_result.add_data({"report_id": report_id})

        return action_result.set_status(phantom.APP_SUCCESS)

    def _hunt_registry_key(self, param):
        """ Get list of all TruSTAR incident report IDs that correlate with the provided
         Registry Key.

        :param param: dictionary on input parameters
        :return: status success/failure
        """

        action_result = self.add_action_result(ActionResult(dict(param)))
        summary_data = action_result.update_summary({})

        # Get mandatory parameters
        registry_key = param[consts.TRUSTAR_HUNT_REGISTRY_KEY_PARAM]

        # Get correlated reports
        resp_status, response = self._hunt_correlated_reports(action_result, registry_key)

        # Something went wrong
        if phantom.is_fail(resp_status):
            return action_result.get_status()

        # Update summary data
        summary_data["total_correlated_reports"] = len(response)
        if not summary_data["total_correlated_reports"]:
            summary_data["possible_reason"] = consts.TRUSTAR_REASON_FOR_REPORT_UNAVAILABILITY

        for report_id in response:
            action_result.add_data({"report_id": report_id})

        return action_result.set_status(phantom.APP_SUCCESS)

    def _get_report(self, param):
        """ Return the raw report data, extracted indicators and other metadata for a TruSTAR report
         given its report id.

        :param param: dictionary of input parameters
        :return: status success/failure
        """

        action_result = self.add_action_result(ActionResult(dict(param)))
        summary_data = action_result.update_summary({})

        # Mandatory parameters
        report_id = param[consts.TRUSTAR_JSON_REPORT_ID]

        # Request parameters
        query_param = {'id': report_id}

        # Generate token
        token_generation_status = self._generate_api_token(action_result)

        # Something went wrong while generating token
        if phantom.is_fail(token_generation_status):
            return action_result.get_status()

        # Make REST call
        resp_status, response = self._make_rest_call(consts.TRUSTAR_GET_REPORT_ENDPOINT, action_result,
                                                     params=query_param, method="get")

        # Something went wrong
        if phantom.is_fail(resp_status):
            return action_result.get_status()

        # Overriding response
        for indicator in response.get('indicators', []):
            indicator[indicator['indicatorType']] = indicator['value']
            del indicator['indicatorType']
            del indicator['value']

        # Adding REST response to action_result.data
        action_result.add_data(response)

        summary_data['extracted_indicators_count'] = len(response.get('indicators', []))

        return action_result.set_status(phantom.APP_SUCCESS)

    def _normalize_timestamp(self, date_time):
        """ Attempt to convert a string timestamp in to a TruSTAR compatible format for submission.

        :param date_time: string/datetime object containing date, time, and ideally timezone
        examples of supported timestamp formats: "2017-02-23T23:01:54", "2017-02-23T23:01:54+0000"
        :return: datetime in ISO 8601 format
        """

        datetime_dt = datetime.datetime.now()

        try:
            if isinstance(date_time, str):
                datetime_dt = dateutil.parser.parse(date_time)
            elif isinstance(date_time, datetime.datetime):
                datetime_dt = date_time

        except Exception as e:
            self.debug_print(consts.TRUSTAR_EXCEPTION_OCCURRED, e)
            return None

        # If timestamp is timezone naive, add timezone
        if not datetime_dt.tzinfo:
            timezone = get_localzone()
            # Add system timezone
            datetime_dt = timezone.localize(datetime_dt)
            # Convert to UTC
            datetime_dt = datetime_dt.astimezone(pytz.utc)

        # Converts datetime to ISO8601
        return datetime_dt.isoformat()

    def _submit_report(self, param):
        """ Submit a report to community or enclaves and returns its TruSTAR report ID and
         extracted indicators from the report.

        :param param: dictionary of input parameters
        :return: status success/failure
        """

        action_result = self.add_action_result(ActionResult(dict(param)))
        summary_data = action_result.update_summary({})

        # Mandatory parameters
        report_title = param[consts.TRUSTAR_JSON_REPORT_TITLE]
        report_body = param[consts.TRUSTAR_JSON_REPORT_BODY]
        distribution_type = param[consts.TRUSTAR_JSON_DISTRIBUTION_TYPE]

        # Optional parameters
        enclave_ids = param.get(consts.TRUSTAR_JSON_ENCLAVE_IDS)
        time_discovered = param.get(consts.TRUSTAR_JSON_TIME_DISCOVERED)
        external_tracking_id = param.get(consts.TRUSTAR_JSON_TRACKING_ID)
        # If api_version is not specified default will be "1.1"
        api_version = param.get(consts.TRUSTAR_JSON_API_VERSION, '1.1')

        # Normalize timestamp
        report_time_began = self._normalize_timestamp(time_discovered)
        if not report_time_began:
            return action_result.set_status(phantom.APP_ERROR, consts.TRUSTAR_ERR_TIME_FORMAT)

        # Enclave id(s) is/are mandatory if distribution type is 'ENCLAVE'
        if distribution_type == 'ENCLAVE' and not enclave_ids:
            return action_result.set_status(phantom.APP_ERROR, consts.TRUSTAR_ERR_MISSING_ENCLAVE_ID)

        # Prepare request data
        submit_report_payload = {
            "incidentReport": {
                "title": report_title,
                "reportBody": report_body,
                "distributionType": distribution_type,
                "timeDiscovered": report_time_began
            }
        }

        # Update request data only if enclave_ids are provided
        if enclave_ids:
            enclave_id_list = enclave_ids.split(',')
            submit_report_payload["enclaveIds"] = enclave_id_list

        # Decide endpoint based on api version mentioned in asset configuration
        # Update request data if api_version is 1.2 and external_tracking_id is provided
        if api_version == "1.2":
            endpoint = consts.TRUSTAR_SUBMIT_REPORT_ENDPOINT_1_2
            if external_tracking_id:
                submit_report_payload["incidentReport"]["externalTrackingId"] = external_tracking_id
        else:
            endpoint = consts.TRUSTAR_SUBMIT_REPORT_ENDPOINT

        # Generate token
        token_generation_status = self._generate_api_token(action_result)

        # Something went wrong while generating token
        if phantom.is_fail(token_generation_status):
            return action_result.get_status()

        # Make REST call
        resp_status, response = self._make_rest_call(endpoint, action_result, data=json.dumps(submit_report_payload),
                                                     method="post", headers={'Content-Type': 'application/json'})

        # Something went wrong
        if phantom.is_fail(resp_status):
            return action_result.get_status()

        action_result.add_data(response)

        # Calculate indicators from response
        indicators_count = 0
        for indicator in response.get('reportIndicators', {}):
            indicators_count += len(response['reportIndicators'][indicator])

        # Update summary data
        summary_data['report_id'] = response['reportId']
        summary_data['total_extracted_indicators'] = indicators_count

        return action_result.set_status(phantom.APP_SUCCESS)

    def _create_dict_hash(self, input_dict):
        """ Function used to create hash of the given input dictionary.

        :param input_dict: dictionary for which we need to generate hash
        :return: MD5 hash
        """

        input_dict_str = None

        if not input_dict:
            return None

        try:
            input_dict_str = json.dumps(input_dict, sort_keys=True)
        except Exception as e:
            print str(e)
            self.debug_print('Handled exception in _create_dict_hash', e)
            return None

        return hashlib.md5(input_dict_str).hexdigest()

    def _create_artifact(self, artifact_name, cef, cef_types, container_id, artifact_created, artifact_count):
        """ Create artifact using the data provided.

        :param artifact_name: name of the artifact
        :param cef: dictionary containing artifact's cef and its value
        :param cef_types: dictionary containing artifact's cef and its contains
        :param container_id: container id in which we need to ingest artifacts
        :param artifact_created: created artifacts count
        :param artifact_count: expected artifacts count
        :return: status success
        """

        # Prepare artifact dictionary
        artifact = {'name': artifact_name, "description": "TruSTAR artifacts", "cef_types": cef_types, 'cef': cef,
                    'container_id': container_id}

        # If its the last artifact during ingestion update it to set 'run_automation' key
        if artifact_created == artifact_count:
            artifact.update({'run_automation': True})

        # The source_data_identifier should be created after all the keys have been set.
        artifact['source_data_identifier'] = self._create_dict_hash(artifact)

        # Save artifact
        artifact_return_value, status_string, artifact_id = self.save_artifact(artifact)

        # Something went wrong while creating artifact
        if phantom.is_fail(artifact_return_value):
            self.debug_print("Error while creating {}".format(artifact_name), artifact)

        return phantom.APP_SUCCESS

    def _ingest_data(self, iocs, artifact_count):
        """ This method creates container from the provided data and prepare data to create artifacts.

        :param iocs: data dictionary from which we need to create artifacts
        :param artifact_count: expected artifacts count that should be ingested
        :return: status success
        """

        # Prepare container dictionary
        container = dict(
            name=iocs['source'] + '-' + str(iocs['intervalSize']) + '-' + str(iocs['queryDate']),
            description=iocs['source'], data=iocs,
            source_data_identifier=iocs['source'] + '-' + str(iocs['intervalSize']) + '-' + str(iocs['queryDate'])
        )

        # Save container
        container_return_value, response, container_id = self.save_container(container)

        # Something went wrong while creating container
        if phantom.is_fail(container_return_value):
            self.save_progress("Error while creating container")
            self.debug_print("Error while creating container", object=container)
            return

        # Counter to maintain the count of artifacts created
        artifacts_created = 0
        # Dictionary object containing information about artifact name, its corresponding cef name and contains
        cef_mapping = {
            # File artifacts
            "SHA256": {"artifact_name": "File Artifact", "cef_name": "fileHashSha256", "cef_contains": ["sha256"]},
            "SHA1": {"artifact_name": "File Artifact", "cef_name": "fileHashSha1", "cef_contains": ["sha1"]},
            "MD5": {"artifact_name": "File Artifact", "cef_name": "fileHashMd5", "cef_contains": ["md5"]},
            "SOFTWARE": {"artifact_name": "File Artifact", "cef_name": "filePath",
                         "cef_contains": ["file name", "file path"]},
            # Email artifact
            "EMAIL_ADDRESS": {"artifact_name": "Email Artifact", "cef_name": "email", "cef_contains": ["email"]},

            # IP artifacts
            "IP": {"artifact_name": "IP Artifact", "cef_name": "destinationAddress", "cef_contains": ["ip"]},
            "CIDR_BLOCK": {"artifact_name": "IP Artifact", "cef_name": "destinationAddress", "cef_contains": ["ip"]},

            # Domain artifact
            "DOMAIN": {"artifact_name": "Domain Artifact", "cef_name": "destinationDnsDomain",
                       "cef_contains": ["domain"]},

            # URL artifact
            "URL": {"artifact_name": "URL Artifact", "cef_name": "requestURL", "cef_contains": ["url"]},

            # Malware artifact
            "MALWARE": {"artifact_name": "Malware Artifact", "cef_name": "malware",
                        "cef_contains": ["trustar malware"]},

            # CVE artifact
            "CVE": {"artifact_name": "CVE Artifact", "cef_name": "cs3", "cef_contains": ["trustar cve number"]},

            # Registry Key artifact
            "REGISTRY_KEY": {"artifact_name": "Registry Key Artifact", "cef_name": "registryKey",
                             "cef_contains": ["trustar registry key"]}
        }

        # Iterate over cef_mapping to get the required information to ingest artifacts
        for ioc_key in cef_mapping:
            cef = dict()
            cef_types = dict()
            # Fetch details of ioc_key from cef_mapping
            cef_details = cef_mapping[ioc_key]
            # Fetch artifact_name, cef_name and cef_contains of ioc_key from cef_mapping
            cef_name = cef_details["cef_name"]
            cef_contains = cef_details["cef_contains"]
            artifact_name = cef_details["artifact_name"]
            # Get list of all values of the ioc from the iocs parameter passed in the function
            ioc_values = iocs['indicators'].get(ioc_key)
            if ioc_values:
                # Ingest each value of ioc as a separate artifact
                for ioc_value in ioc_values:
                    cef[cef_name] = ioc_value
                    cef_types[cef_name] = cef_contains
                    # Increment the count of created artifact
                    artifacts_created += 1
                    # Create artifact
                    self._create_artifact(artifact_name, cef, cef_types, container_id, artifacts_created,
                                          artifact_count)

        return phantom.APP_SUCCESS

    def _on_poll(self, param):
        """ This method ingests the latest indicators that were recently shared on the TruSTAR Station
         (caller's enclave(s) and community reports) or collected from the open source.

        :param param: dictionary of input parameters
        :return: status success/failure
        """

        action_result = self.add_action_result(ActionResult(dict(param)))

        # Get action parameters
        start_time = param.get(phantom.APP_JSON_START_TIME)
        end_time = param.get(phantom.APP_JSON_END_TIME)

        # Generate token
        token_generation_status = self._generate_api_token(action_result)

        # Something went wrong while generating token
        if phantom.is_fail(token_generation_status):
            return action_result.get_status()

        self.save_progress("Fetching latest IOCs")

        # POLL NOW
        if self.is_poll_now():
            self.save_progress("Ignoring Source ID")
            self.save_progress("Ignoring Maximum containers and Maximum artifacts count")
            # Make REST call
            resp_status, response = self._make_rest_call(consts.TRUSTAR_LATEST_IOC_ENDPOINT, action_result)

        # First scheduled ingestion
        elif self._app_state.get('first_run', True):
            self._app_state['first_run'] = False
            # Make REST call
            resp_status, response = self._make_rest_call(consts.TRUSTAR_LATEST_IOC_ENDPOINT, action_result)

        # Scheduled ingestion
        else:
            # Convert epoch milliseconds to seconds
            start_time /= 1000
            end_time /= 1000
            # Find diff between start & end time, convert it into hours and consider ceil value as interval
            diff_in_seconds = end_time - start_time
            interval = int(math.ceil(diff_in_seconds / 3600.0))
            # Prepare request params
            params = {'intervalSize': interval}
            # Make REST call
            resp_status, response = self._make_rest_call(consts.TRUSTAR_LATEST_IOC_ENDPOINT, action_result,
                                                         params=params)

        # Something went wrong while fetching latest indicators
        if phantom.is_fail(resp_status):
            return action_result.get_status()

        # Get dictionary of indicators from response obtained
        indicators = response.get('indicators')
        if indicators:
            # Count total artifacts
            total_artifacts = 0
            for indicator in indicators.keys():
                total_artifacts += len(indicators[indicator])
            self._ingest_data(response, total_artifacts)

        return action_result.set_status(phantom.APP_SUCCESS)

    def finalize(self):
        """ This function gets called once all the param dictionary elements are looped over and no more handle_action
        calls are left to be made. It gives the AppConnector a chance to loop through all the results that were
        accumulated by multiple handle_action function calls and create any summary if required. Another usage is
        cleanup, disconnect from remote devices etc.
        """

        # Save current state of the app
        self.save_state(self._app_state)

        return phantom.APP_SUCCESS

    def handle_action(self, param):
        """ This function gets current action identifier and calls member function of its own to handle the action.

        :param param: dictionary which contains information about the actions to be executed
        :return: status success/failure
        """

        # Dictionary mapping each action with its corresponding actions
        action_mapping = {'test_asset_connectivity': self._test_asset_connectivity,
                          'hunt_ip': self._hunt_ip,
                          'hunt_url': self._hunt_url,
                          'hunt_domain': self._hunt_domain,
                          'hunt_email': self._hunt_email,
                          'hunt_file': self._hunt_file,
                          'hunt_cve_number': self._hunt_cve_number,
                          'hunt_malware': self._hunt_malware,
                          'hunt_registry_key': self._hunt_registry_key,
                          'get_report': self._get_report,
                          'submit_report': self._submit_report,
                          'on_poll': self._on_poll}

        action = self.get_action_identifier()

        try:
            run_action = action_mapping[action]
        except:
            raise ValueError("action {action} is not supported".format(action=action))

        return run_action(param)


if __name__ == '__main__':

    import sys
    import pudb

    pudb.set_trace()
    if len(sys.argv) < 2:
        print 'No test json specified as input'
        exit(0)
    with open(sys.argv[1]) as f:
        in_json = f.read()
        in_json = json.loads(in_json)
        print json.dumps(in_json, indent=4)
        connector = TrustarConnector()
        connector.print_progress_message = True
        return_value = connector._handle_action(json.dumps(in_json), None)
        print json.dumps(json.loads(return_value), indent=4)
    exit(0)