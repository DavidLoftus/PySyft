# stdlib
from enum import Enum


class ApacheArrowCompression(Enum):
    ZSTD = "zstd"
    LZ4 = "lz4"
    LZ4_RAW = "lz4_raw"
    BROTLI = "brotli"
    SNAPPY = "snappy"
    GZIP = "gzip"
    NONE = 0


class ExperimentalFlags:
    def __init__(self) -> None:
        self._APACHE_ARROW_TENSOR_SERDE = True
        self._APACHE_ARROW_COMPRESSION = ApacheArrowCompression.ZSTD

    @property
    def APACHE_ARROW_TENSOR_SERDE(self) -> bool:
        return self._APACHE_ARROW_TENSOR_SERDE

    @APACHE_ARROW_TENSOR_SERDE.getter
    def APACHE_ARROW_TENSOR_SERDE(self) -> bool:
        return self._APACHE_ARROW_TENSOR_SERDE

    @APACHE_ARROW_TENSOR_SERDE.setter
    def APACHE_ARROW_TENSOR_SERDE(self, value: bool) -> None:
        self._APACHE_ARROW_TENSOR_SERDE = value

    @property
    def APACHE_ARROW_COMPRESSION(self) -> ApacheArrowCompression:
        return self._APACHE_ARROW_COMPRESSION

    @APACHE_ARROW_COMPRESSION.setter
    def APACHE_ARROW_COMPRESSION(self, value: ApacheArrowCompression) -> None:
        self._APACHE_ARROW_COMPRESSION = value


flags = ExperimentalFlags()
