"""Dependency-neutral primitives shared by immutable contract modules."""

from __future__ import annotations

import functools
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from importlib import resources
from typing import Annotated, Any, ClassVar, Self

import pydantic as _p
from pydantic import BaseModel, ConfigDict, Field

STABLE_ID_BODY = r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]*"
STABLE_ID_PATTERN, SHA256_PATTERN = rf"^{STABLE_ID_BODY}$", r"^[0-9a-f]{64}$"
DEFAULT_MUTABLE_TOKENS = frozenset({"latest", "current", "default", "stable", "main", "head"})


@functools.lru_cache(maxsize=16)
def load_data_json(filename: str) -> Any:
    return json.loads(resources.files("truealpha_contracts.data").joinpath(filename).read_text(encoding="utf-8"))


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


def reject_binary_float(value: Any, label: str = "value") -> Any:
    if isinstance(value, float):
        raise ValueError(f"{label} cannot be a binary float")
    return value


def finite_decimal_input(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("binary float is forbidden")
    if value is not None:
        try:
            p = value if isinstance(value, Decimal) else Decimal(value)
        except (InvalidOperation, TypeError, ValueError):
            return value
        if not p.is_finite():
            raise ValueError("non-finite Decimal values are forbidden")
    return value


def require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def require_stable_and_immutable(
    value: str, field_name: str, *, mutable_tokens: frozenset[str] = DEFAULT_MUTABLE_TOKENS
) -> str:
    if re.fullmatch(STABLE_ID_PATTERN, value) is None:
        raise ValueError(f"{field_name} must be a stable coordinate")
    if {t for t in re.split(r"[._:/@+\-]", value.lower()) if t} & mutable_tokens:
        raise ValueError(f"{field_name} must name an immutable version")
    return value


def sorted_unique_coordinates(
    values: Sequence[str],
    field_name: str,
    *,
    allow_empty: bool = True,
    immutable: bool = False,
    mutable_tokens: frozenset[str] = DEFAULT_MUTABLE_TOKENS,
) -> tuple[str, ...]:
    if not allow_empty and not values:
        raise ValueError(f"{field_name} must not be empty")
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} has duplicates")
    for v in values:
        if immutable:
            require_stable_and_immutable(v, field_name, mutable_tokens=mutable_tokens)
        elif re.fullmatch(STABLE_ID_PATTERN, v) is None:
            raise ValueError(f"{field_name} must contain stable coordinates")
    return tuple(sorted(values))


def drop_none_fields(data: dict[str, Any], *fields: str) -> dict[str, Any]:
    for n in fields:
        if data.get(n) is None:
            data.pop(n, None)
    return data


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def identify(model: BaseModel, *, id_field: str, prefix: str, hash_field: str = "content_sha256") -> None:
    eh = canonical_sha256(model.model_dump(mode="json", exclude={id_field, hash_field}))
    eid = f"{prefix}:{eh}"
    if getattr(model, hash_field) not in ("", eh):
        raise ValueError(f"{hash_field} does not match canonical content")
    if getattr(model, id_field) not in ("", eid):
        raise ValueError(f"{id_field} does not match canonical content")
    object.__setattr__(model, hash_field, eh)
    object.__setattr__(model, id_field, eid)


class CaptureEnvironment(StrEnum):
    LOCAL = "local"
    LOCAL_DEV = "local_dev"
    LOCAL_TEST = "local_test"
    GITHUB_CI = "github_ci"
    PREVIEW = "preview"
    STAGING = "staging"
    PRODUCTION = "production"


def identify_by_grain(model: BaseModel, *, id_field: str, prefix: str, identity_fields: tuple[str, ...]) -> None:
    eid = f"{prefix}:{canonical_sha256({'kind': prefix, 'identity': model.model_dump(mode='json', include=set(identity_fields))})}"
    eh = canonical_sha256(model.model_dump(mode="json", exclude={id_field, "content_sha256"}))
    if getattr(model, id_field) not in ("", eid) or getattr(model, "content_sha256") not in ("", eh):
        raise ValueError(f"{id_field}/content_sha256 grain mismatch")
    object.__setattr__(model, id_field, eid)
    object.__setattr__(model, "content_sha256", eh)


SafeDecimal = Annotated[Decimal, _p.BeforeValidator(finite_decimal_input)]
AwareDatetime = Annotated[datetime, _p.AfterValidator(lambda v, i: require_aware(v, i.field_name or "datetime"))]
ImmutableId = Annotated[
    str,
    Field(pattern=STABLE_ID_PATTERN),
    _p.AfterValidator(lambda v, i: require_stable_and_immutable(v, i.field_name or "coordinate")),
]
SortedUniqueIds = Annotated[
    tuple[str, ...], _p.AfterValidator(lambda v, i: sorted_unique_coordinates(v, i.field_name or "coordinates"))
]
StableId = Annotated[str, Field(pattern=STABLE_ID_PATTERN)]
Sha256 = Annotated[str, Field(pattern=SHA256_PATTERN)]
ContentHash = Annotated[str, Field(pattern=r"^(?:|[0-9a-f]{64})$")]


def content_id(prefix: str) -> Any:
    return Field(default="", pattern=rf"^(?:|{prefix}:[0-9a-f]{{64}})$")


def content_ref(prefix: str, **kwargs: Any) -> Any:
    return Field(pattern=rf"^{prefix}:[0-9a-f]{{64}}$", **kwargs)


class IdentifiedFrozenModel(FrozenModel):
    id_field: ClassVar[str]
    id_prefix: ClassVar[str]
    identity_fields: ClassVar[tuple[str, ...]] = ()

    def _canonicalize(self) -> None:
        pass

    @_p.model_validator(mode="after")
    def freeze_and_identify(self) -> Self:
        self._canonicalize()
        if self.identity_fields:
            identify_by_grain(self, id_field=self.id_field, prefix=self.id_prefix, identity_fields=self.identity_fields)
        else:
            identify(self, id_field=self.id_field, prefix=self.id_prefix)
        return self


class StrictIdentifiedFrozenModel(IdentifiedFrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class CanonicalHash(BaseModel):
    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class OmitNoneFields(BaseModel):
    omit_none_fields: ClassVar[tuple[str, ...]]

    @_p.model_serializer(mode="wrap")
    def omit_absent_fields(self, handler: _p.SerializerFunctionWrapHandler) -> dict[str, Any]:
        return drop_none_fields(handler(self), *self.omit_none_fields)


class AttrBox(dict[str, Any]):
    optional_attrs: ClassVar[tuple[str, ...]] = ()

    def __getattr__(self, name: str) -> Any:
        if name in self.optional_attrs:
            return self.get(name)
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def model_dump(
        self, *, mode: str = "json", exclude: set[str] | None = None, include: set[str] | None = None
    ) -> dict[str, Any]:
        return {
            k: (v.model_dump(mode=mode) if hasattr(v, "model_dump") else dict(v) if isinstance(v, Mapping) else v)
            for k, v in self.items()
            if (not exclude or k not in exclude) and (include is None or k in include)
        }

    def model_dump_json(self) -> str:
        return json.dumps(self, default=lambda x: x.model_dump(mode="json") if hasattr(x, "model_dump") else str(x))

    def model_copy(self, *, update: Mapping[str, Any] | None = None) -> AttrBox:
        return type(self)({**self, **(update or {})})


def box_attrs(o: Any, cls: type[AttrBox] = AttrBox) -> Any:
    if type(o) is dict:
        return cls({k: box_attrs(v, cls) for k, v in o.items()})
    return tuple(box_attrs(x, cls) for x in o) if isinstance(o, (list, tuple)) else o
