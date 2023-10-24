from __future__ import annotations

import contextlib
import functools
import io
import operator
import sys
import typing as t
from dataclasses import dataclass

from pydantic_core import core_schema

if t.TYPE_CHECKING:
    import numpy as np
    import pandas as pd
    import tensorflow as tf
    import torch
    from pydantic import GetCoreSchemaHandler
    from pydantic import GetJsonSchemaHandler
    from typing_extensions import Literal

    TensorType = t.Union[np.ndarray[t.Any, t.Any], tf.Tensor, torch.Tensor]
else:
    from bentoml._internal.utils.lazy_loader import LazyLoader

    np = LazyLoader("np", globals(), "numpy")
    tf = LazyLoader("tf", globals(), "tensorflow")
    torch = LazyLoader("torch", globals(), "torch")
    pa = LazyLoader("pa", globals(), "pyarrow")
    pd = LazyLoader("pd", globals(), "pandas")

T = t.TypeVar("T")
# This is an internal global state that is True when the model is being serialized for arrow
__in_arrow_serialization__ = False


@contextlib.contextmanager
def arrow_serialization():
    global __in_arrow_serialization__
    __in_arrow_serialization__ = True
    try:
        yield
    finally:
        __in_arrow_serialization__ = False


class FileEncoder(t.Generic[T]):
    __slots__ = ("decoder", "encoder")

    def __init__(
        self, decoder: t.Callable[[t.Any], T], encoder: t.Callable[[T], bytes]
    ) -> None:
        self.decoder = decoder
        self.encoder = encoder

    def __get_pydantic_core_schema__(
        self, source: type[t.Any], handler: t.Callable[[t.Any], core_schema.CoreSchema]
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            function=self.decoder,
            schema=core_schema.bytes_schema(),
            serialization=core_schema.plain_serializer_function_ser_schema(
                self.encoder
            ),
        )


def _get_file(obj: bytes | t.BinaryIO) -> t.BinaryIO:
    if hasattr(obj, "read"):
        return obj
    return io.BytesIO(obj)


def _get_file_bytes(obj: t.BinaryIO) -> bytes:
    obj.seek(0)
    return obj.read()


File = t.Annotated[t.BinaryIO, FileEncoder(_get_file, _get_file_bytes)]

# `slots` is available on Python >= 3.10
if sys.version_info >= (3, 10):
    slots_true = {"slots": True}
else:
    slots_true = {}


def _dict_filter_none(d: dict[str, t.Any]) -> dict[str, t.Any]:
    return {k: v for k, v in d.items() if v is not None}


@dataclass(unsafe_hash=True, **slots_true)
class TensorSchema:
    format: str
    dtype: t.Optional[str] = None
    shape: t.Optional[t.Tuple[int, ...]] = None

    @property
    def dim(self) -> int | None:
        if self.shape is None:
            return None
        return functools.reduce(operator.mul, self.shape, 1)

    def __get_pydantic_json_schema__(
        self, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler
    ) -> dict[str, t.Any]:
        return _dict_filter_none(
            {
                "type": "tensor",
                "format": self.format,
                "dtype": self.dtype,
                "shape": self.shape,
                "dim": self.dim,
                "media_type": "application/json",
            }
        )

    def __get_pydantic_core_schema__(
        self, source_type: t.Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            self._validate,
            core_schema.list_schema(),
            serialization=core_schema.plain_serializer_function_ser_schema(self.encode),
        )

    def encode(self, arr: TensorType) -> list[t.Any]:
        if self.format == "numpy-array":
            numpy_array = arr
        elif self.format == "tf-tensor":
            numpy_array = arr.numpy()
        else:
            numpy_array = arr.cpu().numpy()
        if __in_arrow_serialization__:
            numpy_array = numpy_array.flatten()
        return numpy_array.tolist()

    @property
    def framework_dtype(self) -> t.Any:
        dtype = self.dtype
        if dtype is None:
            return None
        if self.format == "numpy-array":
            return getattr(np, dtype)
        elif self.format == "tf-tensor":
            return getattr(tf, dtype)
        else:
            return getattr(torch, dtype)

    def _validate(self, obj: t.Any) -> t.Any:
        arr: t.Any
        if self.format == "numpy-array":
            arr = np.array(obj, dtype=self.framework_dtype)
            if self.shape is not None:
                arr = arr.reshape(self.shape)
        elif self.format == "tf-tensor":
            arr = tf.constant(obj, dtype=self.framework_dtype, shape=self.shape)  # type: ignore
        else:
            arr = torch.tensor(obj, dtype=self.framework_dtype)
            if self.shape is not None:
                arr = arr.reshape(self.shape)

        return arr


@dataclass(unsafe_hash=True, **slots_true)
class DataframeSchema:
    orient: str = "records"
    columns: list[str] | None = None

    def __get_pydantic_json_schema__(
        self, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler
    ) -> dict[str, t.Any]:
        return _dict_filter_none(
            {
                "type": "dataframe",
                "orient": self.orient,
                "columns": self.columns,
                "media_type": "application/json",
            }
        )

    def __get_pydantic_core_schema__(
        self, source_type: t.Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            self._validate,
            core_schema.list_schema(core_schema.dict_schema())
            if self.orient == "records"
            else core_schema.dict_schema(
                keys_schema=core_schema.str_schema(),
                values_schema=core_schema.list_schema(),
            ),
            serialization=core_schema.plain_serializer_function_ser_schema(self.encode),
        )

    def encode(self, df: pd.DataFrame) -> list | dict:
        if self.orient == "records":
            return df.to_dict(orient="records")
        elif self.orient == "columns":
            return df.to_dict(orient="list")
        else:
            raise ValueError("Only 'records' and 'columns' are supported for orient")

    def _validate(self, obj: t.Any) -> pd.DataFrame:
        return pd.DataFrame(obj, columns=self.columns)


@t.overload
def Tensor(
    format: Literal["numpy-array"], dtype: str, shape: tuple[int, ...]
) -> t.Type[np.ndarray[t.Any, t.Any]]:
    ...


@t.overload
def Tensor(
    format: Literal["tf-tensor"], dtype: str, shape: tuple[int, ...]
) -> t.Type[tf.Tensor]:
    ...


@t.overload
def Tensor(
    format: Literal["torch-tensor"], dtype: str, shape: tuple[int, ...]
) -> t.Type[torch.Tensor]:
    ...


def Tensor(
    format: Literal["numpy-array", "torch-tensor", "tf-tensor"],
    dtype: str | None = None,
    shape: tuple[int, ...] | None = None,
) -> type:
    if format == "numpy-array":
        annotation = np.ndarray[t.Any, t.Any]
    elif format == "torch-tensor":
        annotation = torch.Tensor
    else:
        annotation = tf.Tensor
    return t.Annotated[annotation, TensorSchema(format, dtype, shape)]


def Dataframe(
    orient: t.Literal["records", "columns"] = "records",
    columns: list[str] | None = None,
) -> t.Type[pd.DataFrame]:
    return t.Annotated[pd.DataFrame, DataframeSchema(orient, columns)]
