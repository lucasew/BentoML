import io
import logging
import typing as t
from typing import TYPE_CHECKING

from multipart.multipart import parse_options_header
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import Response

from ...exceptions import BentoMLException
from ..types import FileLike
from ..utils.http import set_content_length
from .base import IODescriptor

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    FileKind = t.Literal["binaryio", "textio"]

    FileType = t.Union[io.IOBase, t.BinaryIO, FileLike[bytes]]


class File(IODescriptor["FileType"]):
    """
    :code:`File` defines API specification for the inputs/outputs of a Service, where either
    inputs will be converted to or outputs will be converted from file-like objects as
    specified in your API function signature.

    Sample implementation of a ViT service:

    .. code-block:: python

        # vit_svc.py
        import bentoml
        from bentoml.io import File

        svc = bentoml.Service("vit-object-detection")

        @svc.api(input=File(), output=File())
        def predict(input_pdf):
            return input_pdf

    Users then can then serve this service with :code:`bentoml serve`:

    .. code-block:: bash

        % bentoml serve ./vit_svc.py:svc --auto-reload

        (Press CTRL+C to quit)
        [INFO] Starting BentoML API server in development mode with auto-reload enabled
        [INFO] Serving BentoML Service "vit-object-detection" defined in "vit_svc.py"
        [INFO] API Server running on http://0.0.0.0:3000

    Users can then send requests to the newly started services with any client:

    .. tabs::

        .. code-tab:: python

            import requests
            requests.post(
                "http://0.0.0.0:3000/predict",
                files = {"upload_file": open('test.pdf', 'rb')},
                headers = {"content-type": "multipart/form-data"}
            ).text


        .. code-tab:: bash

            % curl -H "Content-Type: multipart/form-data" -F 'fileobj=@test.pdf;type=application/pdf' http://0.0.0.0:3000/predict

    Args:
        mime_type (:code:`str`, `optional`, default to :code:`None`):
            Return MIME type of the :code:`starlette.response.Response`, only available
            when used as output descriptor

    Returns:
        :obj:`~bentoml._internal.io_descriptors.IODescriptor`: IO Descriptor that file-like objects.

    """

    _mime_type: str

    def __new__(
        cls, kind: "FileKind" = "binaryio", mime_type: t.Optional[str] = None
    ) -> "File":
        mime_type = mime_type if mime_type is not None else "application/octet-stream"

        if kind == "binaryio":
            res = object.__new__(BytesIOFile)
        else:
            raise ValueError(f"invalid File kind '{kind}'")

        res._mime_type = mime_type
        return res

    def input_type(self) -> t.Type[t.Any]:
        return FileLike

    def openapi_schema_type(self) -> t.Dict[str, str]:
        return {"type": "string", "format": "binary"}

    def openapi_request_schema(self) -> t.Dict[str, t.Any]:
        """Returns OpenAPI schema for incoming requests"""
        return {self._mime_type: {"schema": self.openapi_schema_type()}}

    def openapi_responses_schema(self) -> t.Dict[str, t.Any]:
        """Returns OpenAPI schema for outcoming responses"""
        return {self._mime_type: {"schema": self.openapi_schema_type()}}

    async def init_http_response(self) -> Response:
        return Response(None, media_type=self._mime_type)

    async def finalize_http_response(
        self, response: Response, obj: t.Union[FileLike, bytes]
    ):
        if isinstance(obj, bytes):
            body = obj
        else:
            body = obj.read()

        response.body = body
        set_content_length(response)


class BytesIOFile(File):
    async def from_http_request(self, request: Request) -> t.IO[bytes]:
        content_type, _ = parse_options_header(request.headers["content-type"])
        if content_type.decode("utf-8") == "multipart/form-data":
            form = await request.form()
            found_mimes: t.List[str] = []
            val: t.Union[str, UploadFile]
            for val in form.values():  # type: ignore
                if isinstance(val, UploadFile):
                    found_mimes.append(val.content_type)  # type: ignore
                    if val.content_type == self._mime_type:
                        res = FileLike[bytes](val.file, val.filename)
                        break
            else:
                if len(found_mimes) == 0:
                    raise BentoMLException("no File found in multipart form")
                else:
                    raise BentoMLException(
                        f"multipart File should have Content-Type '{self._mime_type}', got files with content types {', '.join(found_mimes)}"
                    )
            return res  # type: ignore
        if content_type.decode("utf-8") == self._mime_type:
            body = await request.body()
            return FileLike(io.BytesIO(body), "<request body>")
        raise BentoMLException(
            f"File should have Content-Type '{self._mime_type}' or 'multipart/form-data', got {content_type} instead"
        )
