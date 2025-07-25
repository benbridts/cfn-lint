"""
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""

from __future__ import annotations

import json
from collections import deque
from copy import deepcopy
from typing import Any, Iterator

import regex as re

from cfnlint.context.conditions.exceptions import Unsatisfiable
from cfnlint.helpers import (
    AVAILABILITY_ZONES,
    PSEUDOPARAMS,
    REGEX_SUB_PARAMETERS,
    REGIONS,
    is_function,
)
from cfnlint.jsonschema import ValidationError, Validator
from cfnlint.jsonschema._typing import ResolutionResult
from cfnlint.jsonschema._utils import equal


def unresolvable(validator: Validator, instance: Any) -> ResolutionResult:
    return
    yield


def ref(validator: Validator, instance: Any) -> ResolutionResult:
    if not isinstance(instance, (str, dict)):
        return

    for instance, instance_validator, _ in validator.resolve_value(instance):
        if validator.is_type(instance, "string"):
            # if the ref is to pseudo-parameter or parameter we can validate the values
            for v, c in instance_validator.context.ref_value(instance):
                yield v, instance_validator.evolve(context=c), None
            return


def find_in_map(validator: Validator, instance: Any) -> ResolutionResult:
    if not validator.is_type(instance, "array"):
        return
    if len(instance) not in [3, 4]:
        return

    default_value_found = False
    if len(instance) == 4:
        options = instance[3]
        if validator.is_type(options, "object"):
            if "DefaultValue" in options:
                default_value_found = True
                for value, v, _ in validator.resolve_value(options["DefaultValue"]):
                    yield (
                        value,
                        v.evolve(
                            context=v.context.evolve(
                                path=v.context.path.evolve(
                                    value_path=deque([4, "DefaultValue"])
                                )
                            ),
                        ),
                        None,
                    )

    if not default_value_found and not validator.context.mappings.maps:
        if validator.context.mappings.is_transform:
            return
        yield (
            None,
            validator,
            ValidationError(
                f"{instance[0]!r} is not one of "
                f"{list(validator.context.mappings.maps.keys())!r}",
                path=deque([0]),
            ),
        )

    mappings = list(validator.context.mappings.maps.keys())
    results = []
    found_valid_combination = False
    k, v = is_function(instance[0])
    if k == "Ref" and v in PSEUDOPARAMS:
        return
    for map_name, map_v, _ in validator.resolve_value(instance[0]):
        if not validator.is_type(map_name, "string"):
            continue

        if all(not (equal(map_name, each)) for each in mappings):
            if not default_value_found:
                # Validated again as Fn::Transform can be used along side
                # other key/values
                if validator.context.mappings.is_transform:
                    continue
                results.append(
                    (
                        None,
                        map_v,
                        ValidationError(
                            f"{map_name!r} is not one of {mappings!r}",
                            path=deque([0]),
                        ),
                    )
                )
            continue

        if validator.context.mappings.maps[map_name].is_transform:
            continue

        k, v = is_function(instance[2])
        if k == "Ref" and v in PSEUDOPARAMS:
            continue

        k, v = is_function(instance[1])
        if k == "Ref" and v in PSEUDOPARAMS:
            if isinstance(instance[2], str):
                found_top_level_key = False
                found_second_key = False
                for top_level_key, top_values in validator.context.mappings.maps[
                    map_name
                ].keys.items():
                    if v == "AWS::AccountId":
                        if not re.match("^[0-9]{12}$", top_level_key):
                            continue
                    elif v == "AWS::Region":
                        if top_level_key not in REGIONS:
                            continue
                    found_top_level_key = True
                    for second_level_key, second_v, _ in validator.resolve_value(
                        instance[2]
                    ):
                        if second_level_key in top_values.keys:
                            for value in validator.context.mappings.maps[
                                map_name
                            ].find_in_map(
                                top_level_key,
                                second_level_key,
                            ):
                                found_second_key = True
                                yield (
                                    value,
                                    validator.evolve(
                                        context=validator.context.evolve(
                                            path=validator.context.path.evolve(
                                                value_path=deque(
                                                    [
                                                        "Mappings",
                                                        map_name,
                                                        top_level_key,
                                                        second_level_key,
                                                    ]
                                                )
                                            )
                                        )
                                    ),
                                    None,
                                )

                if not found_top_level_key:
                    yield (
                        None,
                        validator,
                        ValidationError(
                            f"{instance[1]!r} is not a "
                            f"first level key for mapping {map_name!r}",
                            path=deque([1]),
                        ),
                    )
                elif not found_second_key:
                    yield (
                        None,
                        validator,
                        ValidationError(
                            f"{instance[2]!r} is not a "
                            "second level key when "
                            f"{instance[1]!r} is resolved "
                            f"for mapping {map_name!r}",
                            path=deque([2]),
                        ),
                    )
            continue

        for top_level_key, top_v, _ in validator.resolve_value(instance[1]):
            if validator.is_type(top_level_key, "integer"):
                top_level_key = str(top_level_key)
            if not validator.is_type(top_level_key, "string"):
                continue

            top_level_keys = list(validator.context.mappings.maps[map_name].keys.keys())
            if all(not (equal(top_level_key, each)) for each in top_level_keys):
                if not default_value_found:
                    results.append(
                        (
                            None,
                            top_v,
                            ValidationError(
                                f"{top_level_key!r} is not one of "
                                f"{top_level_keys!r} for mapping "
                                f"{map_name!r}",
                                path=deque([1]),
                            ),
                        )
                    )
                continue

            if (
                not top_level_key
                or validator.context.mappings.maps[map_name]
                .keys[top_level_key]
                .is_transform
            ):
                continue

            top_v = validator.evolve(
                context=top_v.context.evolve(resolve_pseudo_parameters=False)
            )
            for second_level_key, second_v, err in top_v.resolve_value(instance[2]):
                if validator.is_type(second_level_key, "integer"):
                    second_level_key = str(second_level_key)
                if not validator.is_type(second_level_key, "string"):
                    continue
                second_level_keys = list(
                    validator.context.mappings.maps[map_name]
                    .keys[top_level_key]
                    .keys.keys()
                )
                if all(
                    not (equal(second_level_key, each)) for each in second_level_keys
                ):
                    if not default_value_found:
                        results.append(
                            (
                                None,
                                second_v,
                                ValidationError(
                                    f"{second_level_key!r} is not "
                                    f"one of {second_level_keys!r} "
                                    f"for mapping {map_name!r} and "
                                    f"key {top_level_key!r}",
                                    path=deque([2]),
                                ),
                            )
                        )
                    continue

                found_valid_combination = True

                for value in validator.context.mappings.maps[map_name].find_in_map(
                    top_level_key,
                    second_level_key,
                ):
                    yield (
                        value,
                        second_v.evolve(
                            context=second_v.context.evolve(
                                path=second_v.context.path.evolve(
                                    value_path=deque(
                                        [
                                            "Mappings",
                                            map_name,
                                            top_level_key,
                                            second_level_key,
                                        ]
                                    )
                                )
                            )
                        ),
                        None,
                    )

    if not found_valid_combination:
        yield from iter(results)


def get_azs(validator: Validator, instance: Any) -> ResolutionResult:
    if not isinstance(instance, (str, dict)):
        return

    for instance, v, _ in validator.resolve_value(instance):
        if v.is_type(instance, "string"):
            if instance == "":
                for region in v.context.regions:
                    yield (
                        AVAILABILITY_ZONES.get(region),
                        v,
                        None,
                    )
            # if the ref is to pseudo-parameter or parameter we can validate the values
            elif instance in AVAILABILITY_ZONES:
                yield AVAILABILITY_ZONES.get(instance), v, None


def _join_expansion(
    validator: Validator, instances: Any
) -> Iterator[tuple[Any, Validator]]:
    if len(instances) == 0:
        return

    if len(instances) == 1:
        for value, instance_validator, _ in validator.resolve_value(instances[0]):
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError(f"Incorrect value type for {value!r}")
            yield [value], instance_validator
        return

    for value, instance_validator, errs in validator.resolve_value(instances[0]):
        if errs:
            continue
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"Incorrect value type for {value!r}")
        for values, expansion_validator in _join_expansion(
            instance_validator, instances[1:]
        ):
            yield [value] + values, expansion_validator


def join(validator: Validator, instance: Any) -> ResolutionResult:
    # quick validations
    if not validator.is_type(instance, "array"):
        return
    if not len(instance) == 2:
        return

    for delimiter, delimiter_v, _ in validator.resolve_value(instance[0]):
        if not delimiter_v.is_type(delimiter, "string"):
            continue
        for values, values_v, _ in delimiter_v.resolve_value(instance[1]):
            if not values_v.is_type(values, "array"):
                continue
            try:
                for value, expansion_validator in _join_expansion(values_v, values):
                    # reset the value path because we could get a Ref value_path
                    yield (
                        delimiter.join([str(v) for v in value]),
                        expansion_validator.evolve(
                            context=expansion_validator.context.evolve(
                                path=expansion_validator.context.path.evolve(
                                    value_path=deque([])
                                )
                            )
                        ),
                        None,
                    )
            except (ValueError, TypeError):
                return


def select(validator: Validator, instance: Any) -> ResolutionResult:
    # quick validations
    if not validator.is_type(instance, "array"):
        return
    if not len(instance) == 2:
        return

    # get the values from the list
    indexes = validator.resolve_value(instance[0])
    objs = validator.resolve_value(instance[1])

    for i, _, _ in indexes:
        for obj, obj_v, _ in objs:
            try:
                i = int(i)
            except ValueError:
                continue
            if not validator.is_type(obj, "array"):
                continue
            if len(obj) <= i:
                continue
            yield from obj_v.resolve_value(obj[i])


def split(validator: Validator, instance: Any) -> ResolutionResult:
    if not validator.is_type(instance, "array"):
        return
    if not len(instance) == 2:
        return

    for delimiter, _, _ in validator.resolve_value(instance[0]):
        for source_string, source_v, _ in validator.resolve_value(instance[1]):
            if not source_v.is_type(delimiter, "string"):
                continue
            if not source_v.is_type(source_string, "string"):
                continue

            yield source_string.split(delimiter), source_v, None


def _sub_parameter_expansion(
    validator: Validator, parameters: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    parameters = parameters.copy()
    if len(parameters) == 0:
        yield {}
        return

    if len(parameters) == 1:
        for key, value in parameters.items():
            for resolved_value, _, _ in validator.resolve_value(value):
                yield {key: resolved_value}
        return

    key = list(parameters.keys())[0]
    value = parameters.pop(key)
    for resolved_value, _, _ in validator.resolve_value(value):
        for values in _sub_parameter_expansion(validator, parameters):
            yield dict({key: resolved_value}, **values)


def _sub_string(validator: Validator, string: str) -> ResolutionResult:
    sub_regex = re.compile(r"(\${([^!].*?)})")

    def _replace(matchobj):
        nonlocal validator
        for value, c in validator.context.ref_value(matchobj.group(2).strip()):
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError(f"Parameter {matchobj.group(2)!r} has wrong type")

            validator = validator.evolve(
                context=validator.context.evolve(
                    ref_values=c.ref_values,
                )
            )
            return str(value)
        raise ValueError(f"No matches for {matchobj.group(2)!r}")

    try:
        yield re.sub(sub_regex, _replace, string), validator, None
    except ValueError:
        return


def sub(validator: Validator, instance: Any) -> ResolutionResult:
    if not (
        validator.is_type(instance, "array") or validator.is_type(instance, "string")
    ):
        return

    if validator.is_type(instance, "array"):
        if len(instance) != 2:
            return

        string = instance[0]
        parameters = deepcopy(instance[1])
        if not validator.is_type(string, "string"):
            return
        if not validator.is_type(parameters, "object"):
            return

        sub_parameters = REGEX_SUB_PARAMETERS.findall(string)
        for parameter in sub_parameters:
            if parameter in parameters:
                continue
            if "." in parameter:
                parameters[parameter] = {"Fn::GetAtt": parameter}
            else:
                parameters[parameter] = {"Ref": parameter}

        for resolved_parameters in _sub_parameter_expansion(validator, parameters):
            resolved_validator = validator.evolve(
                context=validator.context.evolve(
                    ref_values=resolved_parameters,
                )
            )
            for sub_string, sub_validator, _ in _sub_string(resolved_validator, string):
                for key in instance[1].keys():
                    sub_validator.context.ref_values.pop(key, None)

                yield sub_string, sub_validator, None

        return

    # its a string
    sub_parameters = REGEX_SUB_PARAMETERS.findall(instance)
    parameters = {}
    for parameter in sub_parameters:
        if "." in parameter:
            parameters[parameter] = {"Fn::GetAtt": parameter}
        else:
            parameters[parameter] = {"Ref": parameter}
    for resolved_parameters in _sub_parameter_expansion(validator, parameters):
        resolved_validator = validator.evolve(
            context=validator.context.evolve(
                ref_values=resolved_parameters,
            )
        )
        yield from _sub_string(resolved_validator, instance)


def if_(validator: Validator, instance: Any) -> ResolutionResult:
    if not validator.is_type(instance, "array"):
        return

    if len(instance) != 3:
        return

    for i in [1, 2]:
        for value, v, err in validator.resolve_value(instance[i]):
            try:
                yield (
                    value,
                    v.evolve(
                        context=v.context.evolve(
                            path=v.context.path.evolve(value_path=deque([i])),
                            conditions=v.context.conditions.evolve(
                                {instance[0]: True if i == 1 else False}
                            ),
                        ),
                    ),
                    err,
                )
            except Unsatisfiable:
                continue


def to_json_string(validator: Validator, instance: Any) -> ResolutionResult:
    for value, v, err in validator.resolve_value(instance):
        yield json.dumps(value), v, err


# not all functions need to be resolved.  These functions
# allow us to pull up values from nested functions
# allowing us to test the possible values against the schema
fn_resolvers: dict[str, Any] = {
    "Fn::Base64": unresolvable,
    "Fn::Cidr": unresolvable,
    "Fn::FindInMap": find_in_map,
    "Fn::ForEach": unresolvable,
    "Fn::GetAtt": unresolvable,
    "Fn::GetAZs": get_azs,
    "Fn::ImportValue": unresolvable,
    "Fn::If": if_,
    "Fn::Join": join,
    "Fn::Select": select,
    "Fn::Split": split,
    "Fn::Sub": sub,
    "Fn::Transform": unresolvable,
    "Fn::ToJsonString": to_json_string,
    "Fn::Equals": unresolvable,
    "Fn::Or": unresolvable,
    "Fn::And": unresolvable,
    "Fn::Not": unresolvable,
    "Condition": unresolvable,
    "Fn::Length": unresolvable,
    "Ref": ref,
}
