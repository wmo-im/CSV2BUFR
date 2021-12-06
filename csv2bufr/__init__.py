###############################################################################
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
###############################################################################

__version__ = "0.1.0"

import csv
from datetime import timezone, datetime
import hashlib
from io import StringIO, BytesIO
import logging
import tempfile
from typing import Union

from eccodes import (codes_bufr_new_from_file, codes_bufr_new_from_samples,
                     codes_set_array, codes_set, codes_get_native_type,
                     codes_write, codes_release, codes_get,
                     CODES_MISSING_LONG, CODES_MISSING_DOUBLE)
from jsonschema import validate

# some 'constants'
SUCCESS = True
NUMBERS = (float, int, complex)
MISSING = ("NA", "NaN", "NAN", "None")

NULLIFY_INVALID = True  # TODO: move to env. variable

LOGGER = logging.getLogger(__name__)

def parse_wigos_id( wigos_id: str) -> dict:
    tokens = wigos_id.split("-")
    assert len(tokens) == 4
    result = {
        "wigos-id-series": int(tokens[0]),
        "wigos-id-issuer": int(tokens[1]),
        "wigos-id-issue-number": int(tokens[2]),
        "wigos-id-local": tokens[3]
    }
    return result


def validate_mapping_dict(mapping_dict: dict) -> bool:
    """
    Validate mapping dictionary

    :param mapping_dict: TODO: describe

    :returns: `bool` of validation result
    """

    file_schema = {
        "type": "object",
        "properties": {
            "inputDelayedDescriptorReplicationFactor": {
                "type": ["array", "null"]
            },
            "sequence": {
                "type": ["array"]
            }
        }
    }
    # now validate
    try:
        validate(mapping_dict, file_schema)
    except Exception as e:
        message = "invalid mapping dictionary"
        LOGGER.error(message)
        raise e
    # now schema for each element in the sequence array
    # TODO: make optional elements optional
    element_schema = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string"
            },
            "value": {
                "type": [
                    "boolean", "object", "array", "number", "string", "null"
                ]
            },
            "column": {
                "type": ["string", "null"]
            },
            "valid-min": {
                 "type": ["number", "null"]
            },
            "valid-max": {
                 "type": ["number", "null"]
            },
            "scale": {
                "type": ["number", "null"]
            },
            "offset": {
                "type": ["number", "null"]
            }
        }
    }

    # now iterate over elements and validate each
    for element in mapping_dict["sequence"]:
        try:
            validate(element, schema=element_schema)
        except Exception as e:
            message = f"invalid element ({e.json_path}) for {element['key']} in mapping file: {e.message}"  # noqa
            LOGGER.error(message)
            raise e
        if (element["scale"] is None) is not (element["offset"] is None):
            message = f"scale and offset should either both be present or both set to missing for {element['key']} in mapping file"  # noqa
            LOGGER.error(message)
            e = ValueError(message)
            raise e

    return SUCCESS


def apply_scaling(value: Union[NUMBERS], element: dict) -> Union[NUMBERS]:
    """
    Apply simple scaling and offsets

    :param value: TODO describe
    :param element: TODO describe

    :returns: scaled value
    """

    if isinstance(value, NUMBERS):
        if None not in [element["scale"], element["offset"]]:
            try:
                value = value * pow(10, element["scale"]) + element["offset"]
            except Exception as e:
                LOGGER.error(e.message)
                raise e
    return value


def validate_value(key: str, value: Union[NUMBERS],
                   valid_min: Union[NUMBERS],
                   valid_max: Union[NUMBERS],
                   nullify_on_fail: bool = False) -> Union[NUMBERS]:
    """
    Check numeric values lie within specified range (if specified)

    :param key: TODO describe
    :param value: TODO describe
    :param valid_min: TODO describe
    :param valid_max: TODO describe
    :param nullify_on_fail: TODO describe

    :returns: validated value
    """

    if value is None:
        return value
    if not isinstance(value, NUMBERS):
        # TODO: add checking against code / flag table here?
        return value

    if None not in [valid_min, valid_max]:
        if value > valid_max or value < valid_min:
            e = ValueError(f"{key}: Value ({value}) out of valid range ({valid_min} - {valid_max}).")  # noqa
            if nullify_on_fail:
                message = f"{e} Element set to missing"
                LOGGER.warning(message)
                return None
            else:
                LOGGER.error(str(e))
                raise e

    return value


def encode(mapping_dict: dict, data_dict: dict) -> BytesIO:
    """
    This is the primary function that does the conversion to BUFR

    :param mapping_dict: dictionary containing eccodes key and mapping to
                         data dict, includes option to specify
                         valid min and max, scale and offset.
    :param data_dict: dictionary containing data values

    :return: BytesIO object containing BUFR message
    """

    # initialise message to be encoded
    bufr_msg = codes_bufr_new_from_samples("BUFR4")

    # set delayed replication factors if present
    if mapping_dict["inputDelayedDescriptorReplicationFactor"] is not None:
        codes_set_array(bufr_msg, "inputDelayedDescriptorReplicationFactor",
                        mapping_dict["inputDelayedDescriptorReplicationFactor"])  # noqa

    # ===================
    # Now encode the data
    # ===================
    for element in mapping_dict["sequence"]:
        key = element["key"]
        value = None
        assert value is None
        if element["value"] is not None:
            value = element["value"]
        elif element["column"] is not None:
            value = data_dict[element["column"]]
        else:
            # change the following to debug or leave as warning?
            LOGGER.debug(f"No value for {key} but included in mapping file, value set to missing")  # noqa
        # now set
        if value is not None:
            LOGGER.debug(f"setting value {value} for element {key}.")
            if isinstance(value, list):
                try:
                    LOGGER.debug("calling codes_set_array")
                    codes_set_array(bufr_msg, key, value)
                except Exception as e:
                    LOGGER.error(f"error calling codes_set_array({bufr_msg}, {key}, {value}): {e}")  # noqa
                    raise e
            else:
                try:
                    LOGGER.debug("calling codes_set")
                    nt = codes_get_native_type(bufr_msg, key)
                    # convert to native type, required as in Malawi data 0
                    # encoded as "0" for some elements.
                    if nt is int and not isinstance(value, int):
                        LOGGER.warning(f"int expected for {key} but received {type(value)} ({value})")  # noqa
                        if isinstance(value, float):
                            value = int(round(value))
                        else:
                            value = int(value)
                        LOGGER.warning(f"value converted to int ({value})")
                    elif nt is float and not isinstance(value, float):
                        LOGGER.warning(f"float expected for {key} but received {type(value)} ({value})")  # noqa
                        value = float(value)
                        LOGGER.warning(f"value converted to float ({value})")
                    else:
                        value = value
                    codes_set(bufr_msg, key, value)
                except Exception as e:
                    LOGGER.error(f"error calling codes_set({bufr_msg}, {key}, {value}): {e}")  # noqa
                    raise e

    # ==============================
    # Message now ready to be packed
    # ==============================
    try:
        codes_set(bufr_msg, "pack", True)
    except Exception as e:
        LOGGER.error(f"error calling codes_set({bufr_msg}, 'pack', True): {e}")
        raise e

    # =======================================================
    # now write to in memory file and return object to caller
    # =======================================================
    try:
        fh = BytesIO()
        codes_write(bufr_msg, fh)
        codes_release(bufr_msg)
        fh.seek(0)
    except Exception as e:
        LOGGER.error(f"error writing to internal BytesIO object, {e}")
        raise e

    # =============================================
    # Return BytesIO object containing BUFR message
    # =============================================
    return fh


def bufr2geojson(identifier: str, bufr_msg: BytesIO, template: dict) -> dict:
    """
    Function to convert BUFR message to GeoJSON

    :param identifier: identifier of BUFR message
    :param bufr_msg: Bytes of BUFR message
    :param data_dict: dictionary contain template for GeoJSON data
                      including mapping from BUFR elements to GeoJSON

    :return: dict of GeoJSON representation from the BUFR message
    """

    # code to validate template here

    # FIXME: need eccodes function to init BUFR from bytes
    with tempfile.TemporaryFile() as fh:
        fh.write(bufr_msg.read())
        fh.seek(0)
        bufr_msg2 = codes_bufr_new_from_file(fh)

    # unpack the data for reading
    codes_set(bufr_msg2, "unpack", True)

    result = extract(bufr_msg2, template)

    # add unique ID to GeoJSON
    result["id"] = identifier
    # now set resultTime
    result["properties"]["resultTime"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds")

    return result


def extract(bufr_msg: int, object_: Union[dict, list]) -> Union[dict, list]:
    """
    Function to recursively traverse object and extract values from BUFR
    message

    :param bufr_msg: Integer used by eccodes to access message
    :param object_: dictionary or list specifying what to extract
                   from the BUFR message.

    :return: extracted dict or list
    """

    if isinstance(object_, dict):
        # check if format or eccodes in object
        if "format" in object_:
            assert "args" in object_
            args = extract(bufr_msg, object_["args"])
            if None not in args:
                result = object_["format"].format(*args)
            else:
                result = None
        elif "eccodes_key" in object_:
            result = codes_get(bufr_msg, object_["eccodes_key"])
            if result in (CODES_MISSING_LONG, CODES_MISSING_DOUBLE):
                result = None
        else:
            for k in object_:
                object_[k] = extract(bufr_msg, object_[k])
            result = object_
    elif isinstance(object_, list):
        for idx in range(len(object_)):
            object_[idx] = extract(bufr_msg, object_[idx])
        result = object_
    else:
        result = object_

    return result


def transform(data: str, mappings: dict, station_metadata: dict) -> dict:
    """
    TODO: describe function

    :param data: TODO: describe
    :param mappings: TODO: describe
    :param station_metadata: TODO: describe

    :return: `dict` of BUFR messages
    """

    # validate mappings
    e = validate_mapping_dict(mappings)
    if e is not SUCCESS:
        raise ValueError("Invalid mappings")

    LOGGER.debug("mapping dictionary validated")

    # TODO: add in code to validate station_metadata

    # we may have multiple rows in the file, create list object to return
    # one item per message
    messages = {}
    # now convert data to StringIO object
    fh = StringIO(data)
    # now read csv data and iterate over rows
    reader = csv.reader(fh, delimiter=',', quoting=csv.QUOTE_NONNUMERIC)
    rows_read = 0
    for row in reader:
        if rows_read == 0:
            col_names = row
        else:
            data = row
            data_dict = dict(zip(col_names, data))
            try:
                data_dict = {**data_dict, **station_metadata['data']}
            except Exception as e:
                message = "issue merging station and data dictionaries."
                LOGGER.error(f"{message}{e}")
                raise e
            # Iterate over items to map, perform unit conversions and validate
            for element in mappings["sequence"]:
                value = element["value"]
                column = element["column"]
                # select between "value" and "column" fields.
                if value is not None:
                    value = element["value"]
                elif column is not None:
                    # get column name
                    # make sure column is in data_dict
                    if (column not in data_dict):
                        message = f"column '{column}' not found in data dictionary"  # noqa
                        raise ValueError(message)
                    value = data_dict[column]
                    if value in MISSING:
                        value = None
                    else:
                        value = apply_scaling(value, element)
                else:
                    LOGGER.debug(f"value and column both None for element {element['key']}")  # noqa
                # now validate value
                LOGGER.debug(f"validating value {value} for element {element['key']}")  # noqa
                value = validate_value(element["key"], value,
                                       element["valid-min"],
                                       element["valid-max"],
                                       NULLIFY_INVALID)

                LOGGER.debug(f"value {value} validated for element {element['key']}")  # noqa
                # update data dictionary
                if column is not None:
                    data_dict[column] = value
                LOGGER.debug(f"value {value} updated for element {element['key']}")  # noqa

            # now encode the data (this one line is where the magic happens
            # once the dictionaries have been read in)
            msg = encode(mappings, data_dict)
            key = hashlib.md5(msg.read()).hexdigest()
            LOGGER.debug(key)
            msg.seek(0)
            messages[key] = msg

        rows_read += 1

    num_messages = rows_read - 1
    LOGGER.info(f"{num_messages} row{'s'[:num_messages^1]} read and converted to BUFR")  # noqa

    return messages
