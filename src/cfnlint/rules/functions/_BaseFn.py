"""
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""

from __future__ import annotations

from collections import deque
from typing import Any, Tuple

from cfnlint.helpers import ToPy, ensure_list, is_function, is_types_compatible
from cfnlint.jsonschema import ValidationError, ValidationResult, Validator
from cfnlint.rules.jsonschema.CfnLintJsonSchema import CfnLintJsonSchema, SchemaDetails
from cfnlint.schema import OTHER_SCHEMA_MANAGER
from cfnlint.schema.resolver import RefResolver

all_types = ("array", "boolean", "integer", "number", "object", "string")
singular_types = ("boolean", "integer", "number", "string")


class BaseFn(CfnLintJsonSchema):
    def __init__(
        self,
        name: str = "",
        types: Tuple[str, ...] | None = None,
        functions: Tuple[str, ...] | None = None,
        resolved_rule: str = "",
        schema_details: SchemaDetails | None = None,
    ) -> None:
        super().__init__(schema_details=schema_details)
        self.fn = ToPy(name)
        self.types = types or tuple([])
        self.functions = functions or tuple([])
        self.resolved_rule = resolved_rule
        self.child_rules[self.resolved_rule] = None
        if name and name != "Fn::GetAZs":
            self._schema = OTHER_SCHEMA_MANAGER.get_schema(
                f"other.functions.{name.replace('Fn::', '').lower()}"
            )

    def key_value(self, instance: dict[str, Any]) -> Tuple[str, Any]:
        return list(instance.keys())[0], instance.get(self.fn.name)

    def fix_errors(self, errs: ValidationResult) -> ValidationResult:
        for err in errs:
            if err.validator not in ["ref"]:  # fix the list to all validators
                err.validator = self.fn.py
            yield err

    def schema(self, validator: Validator, instance: Any) -> dict[str, Any]:
        return self._schema

    def validator(self, validator: Validator, schema: dict[str, Any]) -> Validator:
        return validator.evolve(
            context=validator.context.evolve(
                strict_types=False,
            ),
            function_filter=validator.function_filter.evolve(
                add_cfn_lint_keyword=False,
            ),
            resolver=RefResolver.from_schema(schema=schema),
        )

    def _clean_resolve_errors(
        self, err: ValidationError, value: Any, instance: Any, validator: Validator
    ) -> ValidationError:
        if len(err.path) > 1:
            err.path = deque([err.path[0]])
        err.message = err.message.replace(f"{value!r}", f"{instance!r}")
        err.message = f"{err.message} when {self.fn.name!r} is resolved"
        if validator.context.parameter_sets:
            k, v = is_function(instance)
            if k == "Ref":
                if v in validator.context.parameters:
                    deployment_files = []
                    for parameter_set in validator.context.parameter_sets:
                        if (
                            v in parameter_set.parameters
                            and value == parameter_set.parameters[v]
                        ):
                            if parameter_set.source:
                                deployment_files.append(parameter_set.source)
                            else:
                                err.message = f"{err.message} to {value!r}"
                                break
                    else:
                        err.message = (
                            f"{err.message} to {value!r} from {deployment_files!r}"
                        )
        if self.child_rules[self.resolved_rule]:
            err.rule = self.child_rules[self.resolved_rule]
            for i, err_ctx in enumerate(err.context):
                err.context[i] = self._clean_resolve_errors(
                    err_ctx, value, instance, validator
                )
        return err

    def resolve(
        self,
        validator: Validator,
        s: Any,
        instance: Any,
        schema: Any,
    ) -> ValidationResult:
        if not self.resolved_rule:
            return

        key, _ = self.key_value(instance)

        validator = validator.evolve(
            context=validator.context.evolve(
                strict_types=False,
            ),
        )
        all_errs = []
        for value, v, resolve_err in validator.resolve_value(instance):
            if resolve_err:
                yield from self.fix_errors(iter([resolve_err]))
                continue
            errs = list(
                self.fix_errors(
                    v.descend(
                        instance=value,
                        schema=s,
                        path=key,
                    )
                )
            )

            if not errs and not v.context.is_resolved_from_parameters:
                return

            for err in errs:
                all_errs.append(
                    self._clean_resolve_errors(err, value, instance, validator)
                )

        yield from iter(all_errs)

    def _resolve_ref(self, validator, schema) -> Any:
        resolve = getattr(validator.resolver, "resolve", None)
        ref = schema["$ref"]
        if resolve is None:
            resolved = validator.resolver.resolving(ref)
        else:
            _, resolved = validator.resolver.resolve(ref)

        return resolved

    def resolve_type(self, validator, schema) -> list[str]:
        if "type" in schema:
            return ensure_list(schema["type"])  # type: ignore

        if "$ref" in schema:
            resolved = self._resolve_ref(validator, schema)

            return self.resolve_type(validator, resolved)

        return []

    def validate_fn_output_types(
        self, validator: Validator, s: Any, instance: Any
    ) -> ValidationResult:
        tS = self.resolve_type(validator, s)
        if tS:
            if not is_types_compatible(self.types, tS):
                reprs = ", ".join(repr(type) for type in tS)
                yield ValidationError(f"{instance!r} is not of type {reprs}")

    def validate(
        self,
        validator: Validator,
        s: Any,
        instance: Any,
        schema: Any,
    ) -> ValidationResult:
        # validate this function will return the correct type
        errs = list(
            self.fix_errors(self.validate_fn_output_types(validator, s, instance))
        )

        key, value = self.key_value(instance)
        fn_schema = self.schema(validator, instance)
        errs.extend(
            list(
                self.fix_errors(
                    self.validator(validator, fn_schema).descend(
                        value,
                        fn_schema,
                        path=key,
                    )
                )
            )
        )

        if errs:
            yield from iter(errs)
            return

        yield from self.resolve(validator, s, instance, schema)
