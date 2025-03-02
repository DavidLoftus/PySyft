# future
from __future__ import annotations

# stdlib
import functools
from functools import lru_cache
import operator
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
from typing import Union

# third party
from google.protobuf.reflection import GeneratedProtocolMessageType
import numpy as np
import torch

# syft absolute
# absolute
import syft as sy

# relative
from . import utils
from .... import logger
from ....grid import GridURL
from ....proto.core.tensor.share_tensor_pb2 import ShareTensor as ShareTensor_PB
from ...common.serde.deserialize import _deserialize as deserialize
from ...common.serde.serializable import serializable
from ...common.serde.serialize import _serialize as serialize
from ...smpc.store.crypto_store import CryptoStore
from ..passthrough import PassthroughTensor  # type: ignore

METHODS_FORWARD_ALL_SHARES = {
    "repeat",
    "copy",
    "diagonal",
    "flatten",
    "transpose",
    "partition",
    "resize",
    "ravel",
    "compress",
    "reshape",
    "squeeze",
    "swapaxes",
    "__pos__",
    "__neg__",
    "take",
    "choose",
    "cumsum",
    "trace",
}
INPLACE_OPS = {"resize", "put"}
RING_SIZE_TO_OP = {
    2: {
        "add": operator.xor,
        "sub": operator.xor,
        "mul": operator.and_,
        "lt": operator.lt,
        "gt": operator.gt,
        "ge": operator.ge,
        "le": operator.le,
        "eq": operator.eq,
        "ne": operator.ne,
    },
    2
    ** 32: {
        "add": operator.add,
        "sub": operator.sub,
        "mul": operator.mul,
        "lt": operator.lt,
        "gt": operator.gt,
        "ge": operator.ge,
        "le": operator.le,
        "eq": operator.eq,
        "ne": operator.ne,
    },
}

CACHE_CLIENTS: Dict[str, Any] = {}


def populate_store(*args: List[Any], **kwargs: Dict[Any, Any]) -> None:
    ShareTensor.crypto_store.populate_store(*args, **kwargs)  # type: ignore


@serializable()
class ShareTensor(PassthroughTensor):
    crypto_store = CryptoStore()

    __slots__ = (
        "rank",
        "ring_size",
        "clients",  # clients connections
        "min_value",
        "max_value",
        "generator_przs",
        # Only ShareTensors with seed_przs could be sent over the wire
        "seed_przs",
        "parties_info",
        "nr_parties",
    )

    def __init__(
        self,
        rank: int,
        parties_info: List[GridURL],
        ring_size: int,
        seed_przs: int = 42,
        clients: Optional[List[Any]] = None,
        value: Optional[Any] = None,
        init_clients: bool = False,
    ) -> None:
        # TODO: Ring size needs to be changed to 2^64 (or other specific sizes)
        self.rank = rank
        self.ring_size = ring_size
        self.nr_parties = len(parties_info)
        self.parties_info = parties_info
        self.clients = []
        if clients is not None:
            self.clients = clients
        elif init_clients:  # type: ignore
            self.clients = ShareTensor.login_clients(parties_info)

        self.min_value, self.max_value = ShareTensor.compute_min_max_from_ring(
            self.ring_size
        )

        # This should be set only in the deserializer
        self.generator_przs = None
        self.seed_przs = seed_przs
        super().__init__(value)

    @staticmethod
    def login_clients(parties_info: List[GridURL]) -> Any:
        clients = []
        for party_info in parties_info:
            # if its localhost make it docker-host otherwise no change
            external_host_info = party_info.as_docker_host()
            client = CACHE_CLIENTS.get(str(external_host_info), None)

            if client is None:
                # default cache to true, here to prevent multiple logins
                # due to gevent monkey patching, context switch is done during
                # during socket connection initialization.
                CACHE_CLIENTS[str(external_host_info)] = True
                # TODO: refactor to use a guest account
                client = sy.login(  # nosec
                    url=external_host_info,
                    email="info@openmined.org",
                    password="changethis",
                    port=external_host_info.port,
                    verbose=False,
                )
                CACHE_CLIENTS[str(external_host_info)] = client
            clients.append(client)
        return clients

    def __getitem__(self, item: Union[str, int, slice]) -> ShareTensor:
        return ShareTensor(
            rank=self.rank,
            parties_info=self.parties_info,
            ring_size=self.ring_size,
            value=self.child[item],
            clients=self.clients,
        )

    def copy_tensor(self) -> ShareTensor:
        return ShareTensor(
            value=self.child,
            rank=self.rank,
            parties_info=self.parties_info,
            ring_size=self.ring_size,
            seed_przs=self.seed_przs,
            clients=self.clients,
        )

    @staticmethod
    @lru_cache(32)
    def compute_min_max_from_ring(ring_size: int = 2**32) -> Tuple[int, int]:
        if ring_size == 2:
            min_value, max_value = 0, 1
        else:
            min_value = (-ring_size) // 2
            max_value = (ring_size) // 2 - 1
        return min_value, max_value

    @staticmethod
    @lru_cache(maxsize=None)
    def get_op(ring_size: int, op_str: str) -> Callable[..., Any]:
        """Returns method attribute based on ring_size and op_str.
        Args:
            ring_size (int): Ring size
            op_str (str): Operation string.
        Returns:
            op (Callable[...,Any]): The operation method for the op_str.
        Raises:
            ValueError : If invalid ring size or op_str is given as input.
        """
        ops = RING_SIZE_TO_OP.get(ring_size, None)

        if ops is None:
            raise ValueError(f"Do not have operations for ring size {ring_size}")

        op = ops.get(op_str, None)
        if op is None:
            raise ValueError(
                f"Operator {op_str} does not exist for ring size {ring_size}"
            )

        return op

    """ TODO: Remove this -- we would use generate_przs since the scenario we are testing is that
    the secret is remotly
    @staticmethod
    def generate_shares(secret, nr_shares, ring_size=2 ** 64):
        from .fixed_precision_tensor import FixedPrecisionTensor

        if not isinstance(secret, (int, FixedPrecisionTensor)):
            secret = FixedPrecisionTensor(value=secret)

        shape = secret.shape
        min_value, max_value = ShareTensor.compute_min_max_from_ring(ring_size)

        generator_shares = np.random.default_rng()

        random_shares = []
        for i in range(nr_shares):
            random_value = generator_shares.integers(
                low=min_value, high=max_value, size=shape
            )
            fpt_value = FixedPrecisionTensor(value=random_value)
            random_shares.append(fpt_value)

        shares_fpt = []
        for i in range(nr_shares):
            if i == 0:
                share = value = random_shares[i]
            elif i < nr_shares - 1:
                share = random_shares[i] - random_shares[i - 1]
            else:
                share = secret - random_shares[i - 1]

            shares_fpt.append(share)

        # Add the ShareTensor class between them
        shares = []
        for rank, share_fpt in enumerate(shares_fpt):
            share_fpt.child = ShareTensor(rank=rank, value=share_fpt.child)
            shares.append(share_fpt)

        return shares
    """

    @staticmethod
    def generate_przs(
        value: Any,
        shape: Tuple[int, ...],
        rank: int,
        parties_info: List[GridURL],
        ring_size: int = 2**32,
        seed_przs: Optional[int] = None,
        generator_przs: Optional[Any] = None,
        init_clients: bool = True,
    ) -> "ShareTensor":

        nr_parties = len(parties_info)

        # Try:
        # 1. First get numpy type if secret is numpy and obtain ring size from there
        # 2. If not get the type from the ring size

        numpy_type = None
        ring_size_final = None

        ring_size_from_type = utils.TYPE_TO_RING_SIZE.get(
            getattr(value, "dtype", None), None
        )
        if ring_size_from_type is None:
            logger.warning("Could not get ring size from {value}")
        else:
            ring_size_final = ring_size_from_type
            numpy_type = value.dtype

        if numpy_type is None:
            numpy_type = utils.RING_SIZE_TO_TYPE.get(ring_size, None)
            ring_size_final = ring_size

        if numpy_type is None:
            raise ValueError(f"Ring size {ring_size} not known how to be treated")

        # relative
        from ..tensor import Tensor

        if (seed_przs is None) == (generator_przs is None):
            raise ValueError("Only seed_przs or generator should be populated")

        if value is None:
            value = Tensor(np.zeros(shape, dtype=numpy_type))

        # TODO: Sending the seed and having each party generate the shares is not safe
        # Since the parties would know some of the other parties shares (this might not impose a risk
        # when shares are not sent between parties -- like private addition/subtraction, but it might
        # impose for multiplication
        # The secret holder should generate the shares and send them to the other parties
        if generator_przs:
            generator_shares = generator_przs
        else:
            generator_shares = np.random.default_rng(seed_przs)

        if isinstance(value.child, ShareTensor):
            value = value.child

        share = ShareTensor(
            value=value.child,
            rank=rank,
            parties_info=parties_info,
            seed_przs=seed_przs,  # type: ignore #TODO:Inspect as we could pass none.
            init_clients=init_clients,
            ring_size=ring_size_final,  # type: ignore
        )

        share.generator_przs = generator_shares
        shares = [
            generator_shares.integers(
                low=share.min_value,
                high=share.max_value,
                size=shape,
                endpoint=True,
                dtype=numpy_type,
            )
            for _ in range(nr_parties)
        ]

        op = ShareTensor.get_op(ring_size_final, "sub")
        przs_share = op(shares[rank], shares[(rank + 1) % nr_parties])
        share.child = op(share.child, przs_share)

        return share

    @staticmethod
    def generate_przs_on_dp_tensor(
        value: Optional[Any],
        shape: Tuple[int],
        rank: int,
        parties_info: List[GridURL],
        seed_przs: int,
        share_wrapper: Any,
        ring_size: int = 2**32,
    ) -> PassthroughTensor:

        if value is not None:
            share = ShareTensor.generate_przs(
                value=value.child,
                shape=shape,
                rank=rank,
                parties_info=parties_info,
                seed_przs=seed_przs,
                ring_size=ring_size,
            )
        else:
            share = ShareTensor.generate_przs(
                value=value,
                shape=shape,
                rank=rank,
                parties_info=parties_info,
                seed_przs=seed_przs,
                ring_size=ring_size,
            )

        share_wrapper.child.child = share

        return share_wrapper

    @staticmethod
    def sanity_check(
        share: Union[int, float, torch.Tensor, np.ndarray, "ShareTensor"]
    ) -> None:
        """Check type for share

        Args:
            share (Union[int, float, ShareTensor, np.ndarray, torch.Tensor]): value to check

        Raises:
            ValueError: if type is not supported
        """
        if isinstance(share, float):
            raise ValueError("Type float not supported yet!")

        if isinstance(share, np.ndarray) and (
            not np.issubdtype(share.dtype, np.integer)
            and share.dtype != np.dtype("bool")
        ):
            raise ValueError(
                f"NPArray should have type int or bool, but found {share.dtype}"
            )

        if isinstance(share, torch.Tensor) and torch.is_floating_point(share):
            raise ValueError("Torch tensor should have type int, but found float")

    def apply_function(
        self, y: Union[int, float, torch.Tensor, np.ndarray, "ShareTensor"], op_str: str
    ) -> "ShareTensor":
        """Apply a given operation.

        Args:
            y (Union[int, float, torch.Tensor, np.ndarray, "ShareTensor"]): tensor to apply the operator.
            op_str (str): Operator.

        Returns:
            ShareTensor: Result of the operation.
        """

        op = ShareTensor.get_op(self.ring_size, op_str)
        numpy_type = utils.RING_SIZE_TO_TYPE.get(self.ring_size, None)
        if numpy_type is None:
            raise ValueError(f"Do not know numpy type for ring size {self.ring_size}")

        print("=====================================================")
        print("OP", op, numpy_type, self.ring_size)
        print("====================================================")

        if isinstance(y, ShareTensor):
            utils.get_ring_size(self.ring_size, y.ring_size)
            value = op(self.child, y.child)
        else:
            # TODO: Converting y to numpy because doing "numpy op torch tensor" raises exception
            value = op(self.child, np.array(y, numpy_type))  # TODO: change to np.int64

        res = self.copy_tensor()
        res.child = value
        return res

    def add(
        self, y: Union[int, float, torch.Tensor, np.ndarray, "ShareTensor"]
    ) -> "ShareTensor":
        """Apply the "add" operation between "self" and "y".

        Args:
            y (Union[int, float, torch.Tensor, np.ndarray, "ShareTensor"]): self + y

        Returns:
            ShareTensor. Result of the operation.
        """

        ShareTensor.sanity_check(y)

        new_share = self.apply_function(y, "add")

        return new_share

    def sub(
        self, y: Union[int, float, torch.Tensor, np.ndarray, "ShareTensor"]
    ) -> "ShareTensor":
        """Apply the "sub" operation between "self" and "y".

        Args:
            y (Union[int, float, torch.Tensor, np.ndarray, "ShareTensor"]): self - y

        Returns:
            ShareTensor. Result of the operation.
        """

        ShareTensor.sanity_check(y)
        new_share = self.apply_function(y, "sub")
        return new_share

    def rsub(
        self, y: Union[int, float, torch.Tensor, np.ndarray, "ShareTensor"]
    ) -> "ShareTensor":
        """Apply the "rsub" operation between "self" and "y"

        Args:
            y (Union[int, float, torch.Tensor, np.ndarray, "ShareTensor"]): y - self

        Returns:
            ShareTensor. Result of the operation.
        """

        ShareTensor.sanity_check(y)
        new_self = self.mul(-1)
        new_share = new_self.apply_function(y, "add")
        return new_share

    def mul(
        self, y: Union[int, float, torch.Tensor, np.ndarray, "ShareTensor"]
    ) -> "ShareTensor":
        """Apply the "mul" operation between "self" and "y".

        Args:
            y (Union[int, float, torch.Tensor, np.ndarray, "ShareTensor"]): self * y

        Returns:
            ShareTensor. Result of the operation.
        """
        # if isinstance(y, ShareTensor):
        #     raise ValueError(
        #         "We should not reach this point for private multiplication. Only public one"
        #     )

        ShareTensor.sanity_check(y)
        new_share = self.apply_function(y, "mul")
        return new_share

    def matmul(
        self, y: Union[int, float, torch.Tensor, np.ndarray, "ShareTensor"]
    ) -> "ShareTensor":
        """Apply the "matmul" operation between "self" and "y".

        Args:
            y (Union[int, float, torch.Tensor, np.ndarray, "ShareTensor"]): self @ y.

        Returns:
            ShareTensor: Result of the operation.
        """
        if isinstance(y, ShareTensor):
            raise ValueError("Private matmul not supported yet")

        ShareTensor.sanity_check(y)
        new_share = self.apply_function(y, "matmul")
        return new_share

    def rmatmul(self, y: torch.Tensor) -> "ShareTensor":
        """Apply the "rmatmul" operation between "y" and "self".

        Args:
            y (torch.Tensor): y @ self

        Returns:
            ShareTensor. Result of the operation.
        """
        if isinstance(y, ShareTensor):
            raise ValueError("Private matmul not supported yet")

        ShareTensor.sanity_check(y)
        new_share = y.apply_function(self, "matmul")
        return new_share

    def lt(self, y: Union[ShareTensor, np.ndarray]) -> "ShareTensor":
        """Apply the "lt" operation between "y" and "self".

        Args:
            y (Union[ShareTensor,np.ndarray]): self < y

        Returns:
            ShareTensor. Result of the operation.
        """
        # raise ValueError(
        #     "It should not reach this point since we generate SMPCAction for this"
        # )
        ShareTensor.sanity_check(y)
        new_share = self.apply_function(y, "lt")
        return new_share

    def gt(self, y: Union[ShareTensor, np.ndarray]) -> "ShareTensor":
        """Apply the "gt" operation between "y" and "self".

        Args:
            y (Union[ShareTensor,np.ndarray]): self > y

        Returns:
            ShareTensor. Result of the operation.
        """
        # raise ValueError(
        #     "It should not reach this point since we generate SMPCAction for this"
        # )
        ShareTensor.sanity_check(y)
        new_share = self.apply_function(y, "gt")
        return new_share

    def ge(self, y: Union[ShareTensor, np.ndarray]) -> "ShareTensor":
        """Apply the "ge" operation between "y" and "self".

        Args:
            y (Union[ShareTensor,np.ndarray]): self >= y

        Returns:
            ShareTensor. Result of the operation.
        """
        # raise ValueError(
        #     "It should not reach this point since we generate SMPCAction for this"
        # )
        ShareTensor.sanity_check(y)
        new_share = self.apply_function(y, "ge")
        return new_share

    def le(self, y: Union[ShareTensor, np.ndarray]) -> "ShareTensor":
        """Apply the "le" operation between "y" and "self".

        Args:
            y (Union[ShareTensor,np.ndarray]): self <= y

        Returns:
            ShareTensor. Result of the operation.
        """
        # raise ValueError(
        #     "It should not reach this point since we generate SMPCAction for this"
        # )
        ShareTensor.sanity_check(y)
        new_share = self.apply_function(y, "le")
        return new_share

    def ne(self, y: Union[ShareTensor, np.ndarray]) -> "ShareTensor":
        """Apply the "ne" operation between "y" and "self".

        Args:
            y (Union[ShareTensor,np.ndarray]): self != y

        Returns:
            ShareTensor. Result of the operation.
        """
        # raise ValueError(
        #     "It should not reach this point since we generate SMPCAction for this"
        # )
        ShareTensor.sanity_check(y)
        new_share = self.apply_function(y, "ne")
        return new_share

    def bit_decomposition(self) -> "ShareTensor":
        """Apply the "decomposition" operation on self

        Args:
            None

        Returns:
            ShareTensor. Result of the operation.
        """
        raise ValueError(
            "It should not reach this point since we generate SMPCAction for this"
        )

    def eq(self, other: Any) -> bool:
        """Equal operator.
        Check if "self" is equal with another object given a set of
            attributes to compare.
        Args:
            other (Any): Value to compare.
        Returns:
            bool: True if equal False if not.
        """
        # TODO: Rasswanth: Fix later after the comparison operation
        # relative
        # from .... import Tensor

        # if (
        #     isinstance(self.child, Tensor)
        #     and isinstance(other.child, Tensor)
        #     and (self.child != other.child).child.any()  # type: ignore
        # ):
        #     return False

        # if (
        #     isinstance(self.child, np.ndarray)
        #     and isinstance(other.child, np.ndarray)
        #     and (self.child != other.child).any()
        # ):
        #     return False

        # if self.rank != other.rank:
        #     return False

        # if self.ring_size != other.ring_size:
        #     return False

        # if self.nr_parties != other.nr_parties:
        #     return False

        # return True

        return self.child == other.child

    # TRASK: commenting out because ShareTEnsor doesn't appear to have .session_uuid or .config
    # def div(
    #     self, y: Union[int, float, torch.Tensor, np.ndarray, "ShareTensor"]
    # ) -> "ShareTensor":
    #     """Apply the "div" operation between "self" and "y".
    #
    #     Args:
    #         y (Union[int, float, torch.Tensor, np.ndarray, "ShareTensor"]): Denominator.
    #
    #     Returns:
    #         ShareTensor: Result of the operation.
    #
    #     Raises:
    #         ValueError: If y is not an integer or LongTensor.
    #     """
    #     if not isinstance(y, (int, torch.LongTensor)):
    #         raise ValueError("Div works (for the moment) only with integers!")
    #
    #     res = ShareTensor(session_uuid=self.session_uuid, config=self.config)
    #     # res = self.apply_function(y, "floordiv")
    #     res.tensor = self.tensor // y
    #     return res

    def bit_extraction(self, pos: int = 0) -> ShareTensor:
        """Extracts the bit at the specified position.

        Args:
            pos (int): position to extract bit.

        Returns:
            ShareTensor : extracted bits at specific position.

        Raises:
            ValueError: If invalid position is provided.
        """
        ring_bits = utils.get_nr_bits(self.ring_size)
        if pos < 0 or pos > ring_bits - 1:
            raise ValueError(
                f"Invalid position for bit_extraction: {pos}, must be in range:[0,{ring_bits-1}]"
            )
        shape = self.shape
        numpy_type = utils.RING_SIZE_TO_TYPE[self.ring_size]
        # logical shift
        bit_mask = np.ones(shape, dtype=numpy_type) << pos
        value = self.child & bit_mask
        value = value.astype(np.bool_)
        share = self.copy_tensor()
        share.child = value
        return share

    @staticmethod
    def hook_method(__self: ShareTensor, method_name: str) -> Callable[..., Any]:
        """Hook a framework method.

        Args:
            method_name (str): method to hook

        Returns:
            A hooked method
        """

        def method_all_shares(
            _self: ShareTensor, *args: List[Any], **kwargs: Dict[Any, Any]
        ) -> Any:

            share = _self.child
            if method_name != "resize":
                method = getattr(share, method_name)
            else:
                # Should be modified to remove copy
                # https://stackoverflow.com/questions/23253144/numpy-the-array-doesnt-have-its-own-data
                share = share.copy()
                method = getattr(share, method_name)

            if method_name not in INPLACE_OPS:
                new_share = method(*args, **kwargs)
            else:
                method(*args, **kwargs)
                new_share = share

            res = _self.copy_tensor()

            # TODO : Some operations return np.int64 by default, should modify
            # when we have support for np.int64 or do explicit casting.
            if method_name == "trace":
                new_share = np.array(new_share, dtype=np.int32)
            res.child = new_share

            return res

        return functools.partial(method_all_shares, __self)

    def __getattribute__(self, attr_name: str) -> Any:
        if attr_name in METHODS_FORWARD_ALL_SHARES or attr_name in INPLACE_OPS:
            return ShareTensor.hook_method(self, attr_name)

        return object.__getattribute__(self, attr_name)

    def _object2proto(self) -> ShareTensor_PB:
        proto_init_kwargs = {
            "rank": self.rank,
            "parties_info": [serialize(party) for party in self.parties_info],
            "seed_przs": self.seed_przs,
            "ring_size": sy.serialize(self.ring_size, to_bytes=True),
        }
        if isinstance(self.child, np.ndarray):
            proto_init_kwargs["array"] = serialize(self.child)
        elif isinstance(self.child, torch.Tensor):
            proto_init_kwargs["array"] = serialize(np.array(self.child))
        else:
            proto_init_kwargs["tensor"] = serialize(self.child)

        return ShareTensor_PB(**proto_init_kwargs)

    @staticmethod
    def _proto2object(proto: ShareTensor_PB) -> "ShareTensor":
        init_kwargs = {
            "rank": proto.rank,
            "parties_info": [deserialize(party) for party in proto.parties_info],
            "seed_przs": proto.seed_przs,
            "ring_size": int(sy.deserialize(proto.ring_size, from_bytes=True)),
        }
        if proto.HasField("tensor"):
            init_kwargs["value"] = deserialize(proto.tensor)
        else:
            init_kwargs["value"] = deserialize(proto.array)

        # init_kwargs["init_clients"] = True
        res = ShareTensor(**init_kwargs)
        generator_przs = np.random.default_rng(proto.seed_przs)
        res.generator_przs = generator_przs
        return res

    @staticmethod
    def get_protobuf_schema() -> GeneratedProtocolMessageType:
        return ShareTensor_PB

    __add__ = add
    __radd__ = add
    __sub__ = sub
    __rsub__ = rsub
    __mul__ = mul
    __rmul__ = mul
    __matmul__ = matmul
    __rmatmul__ = rmatmul
    __lt__ = lt
    __gt__ = gt
    __ge__ = ge
    __le__ = le
    __eq__ = eq
    __ne__ = ne
